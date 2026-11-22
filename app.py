"""🎁 Idées cadeaux de Noël — petite app Streamlit familiale.

Stockage : une Google Sheet privée (6 onglets) via un compte de service.
Pour tester sans Google : PERENOEL_BACKEND=local streamlit run app.py
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import html
import json
import os
import queue
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

st.set_page_config(
    page_title="🎁",
    page_icon="🎁",
    layout="centered",
    initial_sidebar_state="collapsed",
)

TTL = 25  # secondes de cache pour les lectures
IDEE, ACHETE = "Idée", "Acheté"
HEADERS = {
    "personnes": ["nom"],
    "idees": ["id", "destinataire", "nom", "prix", "statut", "suggere_par", "cree_le", "lien", "prive"],
    "commentaires": ["id", "idee_id", "auteur", "texte", "cree_le"],
    "notes": ["destinataire", "texte", "modifie_par", "modifie_le"],
    "souhaits": ["id", "personne", "nom", "prix", "lien", "cree_le"],
    "votes": ["idee_id", "personne", "vote", "modifie_le"],  # vote : "+1" (♥) ou "-1"
}
TITRE = "Cadeaux Noël 2026"
UNDO_SECONDS = 12


SEP = " + "  # cadeaux communs : « Bruno + Victoire » dans la colonne destinataire


def dests(it) -> list[str]:
    return [d.strip() for d in str(it.get("destinataire", "")).split("+") if d.strip()]


def visible(it, user) -> bool:
    """Jamais pour un destinataire ; une idée secrète seulement pour son auteur."""
    if user in dests(it):
        return False
    return not it.get("prive") or it.get("suggere_par") == user


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BackendError(Exception):
    pass


class ConfigError(BackendError):
    pass


def _rows_to_records(tab: str, values: list[list]) -> list:
    expected = HEADERS[tab]
    if not values:
        raise BackendError(f"Onglet {tab} vide")
    header = [str(h).strip() for h in values[0]]
    if header[: len(expected)] != expected:
        raise BackendError(f"En-têtes inattendus dans {tab}: {header}")
    out = []
    for raw in values[1:]:
        raw = list(raw) + [""] * (len(expected) - len(raw))
        rec = {k: str(raw[i]).strip() for i, k in enumerate(expected)}
        if tab == "personnes":
            if rec["nom"]:
                out.append(rec["nom"])
        elif rec[expected[0]]:
            out.append(rec)
    return out


def _col_letter(idx: int) -> str:
    return chr(ord("A") + idx)


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class SheetsBackend:
    """Google Sheets via gspread. Never rewrites a whole tab: only appends,
    updates or deletes the affected rows."""

    def __init__(self, info: dict, sheet_id: str):
        import gspread

        self._gspread = gspread
        try:
            gc = gspread.service_account_from_dict(info)
            self.sh = gc.open_by_key(sheet_id)
            self._ws = {ws.title: ws for ws in self.sh.worksheets()}
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"Connexion impossible : {e}") from e
        for t in [t for t in HEADERS if t not in self._ws]:
            # Nouvel onglet ajouté dans une version plus récente de l'app (ex. « votes »).
            ws = self._call(self.sh.add_worksheet, title=t, rows=200, cols=len(HEADERS[t]))
            self._call(ws.update, range_name="A1", values=[HEADERS[t]], value_input_option="RAW")
            self._ws[t] = ws
        self._upgrade_headers()

    def _upgrade_headers(self):
        """Ajoute les nouvelles colonnes en fin de ligne d'en-tête (feuilles plus anciennes)."""
        resp = self._call(self.sh.values_batch_get, [f"'{t}'!1:1" for t in HEADERS])
        for t, vr in zip(HEADERS, resp.get("valueRanges", [])):
            rows = vr.get("values") or [[]]
            first = [str(x).strip() for x in rows[0]]
            exp = HEADERS[t]
            if first and len(first) < len(exp) and first == exp[: len(first)]:
                ws = self._ws[t]
                if ws.col_count < len(exp):
                    self._call(ws.add_cols, len(exp) - ws.col_count)
                self._call(ws.update, range_name="A1", values=[exp], value_input_option="RAW")

    def _call(self, fn, *args, **kwargs):
        for attempt in range(3):
            try:
                return fn(*args, **kwargs)
            except self._gspread.exceptions.APIError as e:
                code = getattr(e, "code", None)
                if code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise BackendError(str(e)) from e
            except Exception as e:  # noqa: BLE001  (réseau, auth…)
                if attempt < 1:
                    time.sleep(1)
                    continue
                raise BackendError(str(e)) from e

    # Lecture : un seul appel API pour tous les onglets.
    def read_all(self) -> dict:
        ranges = [f"'{t}'!A:{_col_letter(len(h) - 1)}" for t, h in HEADERS.items()]
        resp = self._call(self.sh.values_batch_get, ranges)
        vrs = resp.get("valueRanges", [])
        return {t: _rows_to_records(t, vr.get("values", [])) for t, vr in zip(HEADERS, vrs)}

    def _rows_by_id(self, tab: str) -> dict[str, int]:
        col = self._call(self._ws[tab].col_values, 1)
        return {v: i + 1 for i, v in enumerate(col) if i > 0 and v}

    def _delete_requests(self, tab: str, rows: list[int]) -> list[dict]:
        sid = self._ws[tab].id
        return [
            {"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS",
                                           "startIndex": r - 1, "endIndex": r}}}
            for r in sorted(set(rows), reverse=True)
            if r > 1
        ]

    def append(self, tab: str, rec: dict):
        values = [rec.get(k, "") for k in HEADERS[tab]]
        self._call(self._ws[tab].append_row, values, value_input_option="RAW",
                   insert_data_option="INSERT_ROWS", table_range="A1")

    def update(self, tab: str, rec_id: str, fields: dict):
        row = self._rows_by_id(tab).get(rec_id)
        if row is None:  # supprimé entre-temps par quelqu'un d'autre
            return
        data = [
            {"range": f"'{tab}'!{_col_letter(HEADERS[tab].index(k))}{row}", "values": [[v]]}
            for k, v in fields.items()
        ]
        self._call(self.sh.values_batch_update, {"valueInputOption": "RAW", "data": data})

    def append_many(self, tab: str, recs: list[dict]):
        values = [[r.get(k, "") for k in HEADERS[tab]] for r in recs]
        if values:
            self._call(self._ws[tab].append_rows, values, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")

    def delete_row(self, tab: str, rec_id: str):
        row = self._rows_by_id(tab).get(rec_id)
        if row:
            self._call(self.sh.batch_update, {"requests": self._delete_requests(tab, [row])})

    def delete_comment(self, cid: str):
        self.delete_row("commentaires", cid)

    def upsert_note(self, dest: str, texte: str, par: str, le: str):
        # Une ligne par personne dans « notes » : mise à jour de cette ligne, ou ajout.
        row = self._rows_by_id("notes").get(dest)
        if row is None:
            self.append("notes", {"destinataire": dest, "texte": texte, "modifie_par": par, "modifie_le": le})
        else:
            self._call(self.sh.values_batch_update, {"valueInputOption": "RAW", "data": [
                {"range": f"'notes'!B{row}:D{row}", "values": [[texte, par, le]]}]})

    def set_vote(self, iid: str, person: str, val: str, le: str):
        """Un vote par personne et par idée ; val vide = retirer le vote."""
        resp = self._call(self.sh.values_batch_get, ["'votes'!A:B"])
        rows = (resp.get("valueRanges") or [{}])[0].get("values", [])
        row = next((i + 1 for i, r in enumerate(rows)
                    if i > 0 and len(r) > 1 and r[0] == iid and r[1] == person), None)
        if not val:
            if row:
                self._call(self.sh.batch_update, {"requests": self._delete_requests("votes", [row])})
        elif row:
            self._call(self.sh.values_batch_update, {"valueInputOption": "RAW", "data": [
                {"range": f"'votes'!C{row}:D{row}", "values": [[val, le]]}]})
        else:
            self.append("votes", {"idee_id": iid, "personne": person, "vote": val, "modifie_le": le})

    def delete_idee(self, iid: str):
        # Un seul appel pour localiser l'idée, ses commentaires et ses votes ; un seul pour supprimer.
        resp = self._call(self.sh.values_batch_get, ["'idees'!A:A", "'commentaires'!A:B", "'votes'!A:A"])
        vrs = resp.get("valueRanges", [])
        idees = vrs[0].get("values", []) if vrs else []
        coms = vrs[1].get("values", []) if len(vrs) > 1 else []
        votes = vrs[2].get("values", []) if len(vrs) > 2 else []
        rows_i = [i + 1 for i, r in enumerate(idees) if i > 0 and r and r[0] == iid]
        rows_c = [i + 1 for i, r in enumerate(coms) if i > 0 and len(r) > 1 and r[1] == iid]
        rows_v = [i + 1 for i, r in enumerate(votes) if i > 0 and r and r[0] == iid]
        reqs = (self._delete_requests("idees", rows_i) + self._delete_requests("commentaires", rows_c)
                + self._delete_requests("votes", rows_v))
        if reqs:
            self._call(self.sh.batch_update, {"requests": reqs})


class LocalBackend:
    """Fichier JSON local, pour tester sans Google."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        delay = os.environ.get("PERENOEL_LOCAL_DELAY")  # simuler la latence réseau
        self.delay = float(delay) if delay else 0.0
        if not path.exists():
            raise ConfigError(f"{path} introuvable : lancez init_sheet.py --local")

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, db: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _write(self, fn):
        time.sleep(self.delay)
        with self.lock:
            db = self._load()
            fn(db)
            self._save(db)

    def read_all(self) -> dict:
        time.sleep(self.delay)
        with self.lock:
            db = self._load()
        return {"personnes": [p for p in db["personnes"] if p], "idees": db["idees"],
                "commentaires": db["commentaires"], "notes": db.get("notes", []),
                "souhaits": db.get("souhaits", []), "votes": db.get("votes", [])}

    def append(self, tab, rec):
        self._write(lambda db: db.setdefault(tab, []).append({k: rec.get(k, "") for k in HEADERS[tab]}))

    def update(self, tab, rec_id, fields):
        def f(db):
            for r in db[tab]:
                if r["id"] == rec_id:
                    r.update(fields)
        self._write(f)

    def upsert_note(self, dest, texte, par, le):
        def f(db):
            notes = db.setdefault("notes", [])
            rec = next((n for n in notes if n["destinataire"] == dest), None)
            if rec is None:
                notes.append(rec := {"destinataire": dest})
            rec.update(texte=texte, modifie_par=par, modifie_le=le)
        self._write(f)

    def append_many(self, tab, recs):
        self._write(lambda db: db.setdefault(tab, []).extend(
            {k: r.get(k, "") for k in HEADERS[tab]} for r in recs))

    def delete_row(self, tab, rec_id):
        self._write(lambda db: db.update({tab: [r for r in db.get(tab, []) if r["id"] != rec_id]}))

    def delete_comment(self, cid):
        self.delete_row("commentaires", cid)

    def set_vote(self, iid, person, val, le):
        def f(db):
            votes = [v for v in db.get("votes", []) if not (v["idee_id"] == iid and v["personne"] == person)]
            if val:
                votes.append({"idee_id": iid, "personne": person, "vote": val, "modifie_le": le})
            db["votes"] = votes
        self._write(f)

    def delete_idee(self, iid):
        def f(db):
            db["idees"] = [i for i in db["idees"] if i["id"] != iid]
            db["commentaires"] = [c for c in db["commentaires"] if c["idee_id"] != iid]
            db["votes"] = [v for v in db.get("votes", []) if v["idee_id"] != iid]
        self._write(f)


def _secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:  # noqa: BLE001  (pas de secrets.toml)
        return default


@st.cache_resource(show_spinner=False)
def get_backend():
    mode = os.environ.get("PERENOEL_BACKEND") or _secret("backend", "sheets")
    if mode == "local":
        return LocalBackend(Path(os.environ.get("PERENOEL_LOCAL_DB", "local_db.json")))
    info, sheet_id = _secret("gcp_service_account"), _secret("sheet_id")
    if not info or not sheet_id:
        raise ConfigError("secrets manquants")
    return SheetsBackend(dict(info), str(sheet_id))


@st.cache_data(ttl=TTL, show_spinner=False)
def load_data() -> dict:
    return get_backend().read_all()


# --------------------------------------------------------------------------- #
# Écritures en arrière-plan (une file unique => l'ordre est préservé)
# --------------------------------------------------------------------------- #
class Writer:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.pending: Counter = Counter()
        self.errors: set[str] = set()
        threading.Thread(target=self._run, daemon=True, name="perenoel-writer").start()

    def submit(self, sid: str, method: str, *args):
        with self.lock:
            self.pending[sid] += 1
        self.q.put((sid, method, args))

    def busy(self, sid: str) -> bool:
        with self.lock:
            return self.pending[sid] > 0

    def pop_error(self, sid: str) -> bool:
        with self.lock:
            if sid in self.errors:
                self.errors.discard(sid)
                return True
            return False

    def _run(self):
        while True:
            sid, method, args = self.q.get()
            try:
                getattr(get_backend(), method)(*args)
            except Exception:  # noqa: BLE001
                with self.lock:
                    self.errors.add(sid)
            finally:
                load_data.clear()
                with self.lock:
                    self.pending[sid] -= 1


@st.cache_resource(show_spinner=False)
def get_writer() -> Writer:
    return Writer()


def submit(method: str, *args):
    get_writer().submit(st.session_state.sid, method, *args)


# --------------------------------------------------------------------------- #
# État
# --------------------------------------------------------------------------- #
ss = st.session_state
ss.setdefault("sid", new_id())
ss.setdefault("open", None)


def get_db() -> dict:
    writer = get_writer()
    if writer.pop_error(ss.sid):
        ss.db_at = 0  # forcer une relecture dès que la file d'écriture est vide
        st.toast("La dernière modification n'a pas pu être enregistrée. Réessayez.", icon="⚠️")
    missing = "db" not in ss
    stale = missing or time.time() - ss.db_at > TTL
    if missing or (stale and not writer.busy(ss.sid)):
        try:
            ss.db = copy.deepcopy(load_data())
            ss.db_at = time.time()
        except ConfigError:
            st.error("L'application n'est pas encore configurée (secrets manquants).")
            st.stop()
        except Exception:  # noqa: BLE001
            if "db" in ss:
                st.toast("Connexion difficile, données peut-être pas à jour.", icon="⚠️")
            else:
                st.error("Impossible de charger les données pour le moment. Réessayez dans un instant.")
                st.stop()
    return ss.db


def find_idee(iid):
    return next((i for i in ss.get("db", {}).get("idees", []) if i["id"] == iid), None)


def go(**params):
    ss.open = None
    if ss.get("token"):
        params["k"] = ss.token  # garder l'accès dans l'URL (favori / écran d'accueil)
    st.query_params.from_dict({k: v for k, v in params.items() if v})


# --------------------------------------------------------------------------- #
# Mot de passe commun (optionnel : app_password dans les secrets)
# --------------------------------------------------------------------------- #
def _norm_pw(p) -> str:
    return str(p or "").strip().casefold()


def check_access():
    pw = _secret("app_password") or os.environ.get("PERENOEL_PASSWORD")
    if not pw:
        return
    token = hashlib.sha256(f"perenoel:{_norm_pw(pw)}".encode()).hexdigest()[:16]
    if ss.get("auth") or hmac.compare_digest(str(st.query_params.get("k", "")), token):
        ss.auth, ss.token = True, token
        if st.query_params.get("k") != token:
            st.query_params["k"] = token
        return
    st.markdown(f'<div class="hero">{motif("bonhomme")}</div>', unsafe_allow_html=True)
    title(TITRE)
    st.markdown('<div class="sub">Entrez le mot de passe de la famille.</div>', unsafe_allow_html=True)
    with st.form("login", border=False):
        typed = st.text_input("Mot de passe", type="password")
        ok = st.form_submit_button("Entrer", type="primary", width="stretch")
    if ok:
        if hmac.compare_digest(_norm_pw(typed), _norm_pw(pw)):
            ss.auth, ss.token = True, token
            st.query_params["k"] = token
            st.rerun()
        st.error("Mot de passe incorrect.")
    st.stop()


# --------------------------------------------------------------------------- #
# Actions (mise à jour optimiste de session_state, puis écriture en fond)
# --------------------------------------------------------------------------- #
def act_toggle(iid):
    it = find_idee(iid)
    if it:
        it["statut"] = IDEE if it["statut"] == ACHETE else ACHETE
        submit("update", "idees", iid, {"statut": it["statut"]})


def act_open(okey):
    ss.open = None if ss.open == okey else okey


def act_add_comment(iid, user, key):
    texte = (ss.get(key) or "").strip()
    if texte and "db" in ss:
        rec = {"id": new_id(), "idee_id": iid, "auteur": user, "texte": texte, "cree_le": now_iso()}
        ss.db["commentaires"].append(rec)
        submit("append", "commentaires", rec)


def act_note(dest, key, seen, user):
    if "db" not in ss:
        return
    texte = ss.get(key, "") or ""
    notes = ss.db.setdefault("notes", [])
    rec = next((n for n in notes if n["destinataire"] == dest), None)
    if rec is None:
        notes.append(rec := {"destinataire": dest})
    rec.update(texte=texte, modifie_par=user, modifie_le=now_iso())
    ss[seen] = texte
    submit("upsert_note", dest, texte, user, rec["modifie_le"])


def _set_undo(label, recs):
    """recs: liste de (onglet, enregistrement) à remettre si l'on annule."""
    ss.undo = {"label": label, "recs": recs, "at": time.time()}


def act_delete_idee(iid):
    it = find_idee(iid)
    if not it:
        return
    coms = [c for c in ss.db["commentaires"] if c["idee_id"] == iid]
    votes = [v for v in ss.db.get("votes", []) if v["idee_id"] == iid]
    ss.db["idees"] = [i for i in ss.db["idees"] if i["id"] != iid]
    ss.db["commentaires"] = [c for c in ss.db["commentaires"] if c["idee_id"] != iid]
    ss.db["votes"] = [v for v in ss.db.get("votes", []) if v["idee_id"] != iid]
    ss.open = None
    submit("delete_idee", iid)
    _set_undo(f"« {it['nom']} » supprimée", [("idees", it)] + [("commentaires", c) for c in coms]
              + [("votes", v) for v in votes])


def act_vote(iid, user, val):
    if "db" not in ss:
        return
    votes = ss.db.setdefault("votes", [])
    mine = next((v for v in votes if v["idee_id"] == iid and v["personne"] == user), None)
    new = "" if mine and mine["vote"] == val else val  # re-toucher le même bouton = retirer
    ss.db["votes"] = [v for v in votes if v is not mine]
    now = now_iso()
    if new:
        ss.db["votes"].append({"idee_id": iid, "personne": user, "vote": new, "modifie_le": now})
    submit("set_vote", iid, user, new, now)


def act_delete_wish(wid):
    w = next((w for w in ss.db.get("souhaits", []) if w["id"] == wid), None)
    if not w:
        return
    ss.db["souhaits"] = [x for x in ss.db["souhaits"] if x["id"] != wid]
    submit("delete_row", "souhaits", wid)
    _set_undo(f"« {w['nom']} » supprimé", [("souhaits", w)])


def act_undo():
    u = ss.pop("undo", None)
    if not u or "db" not in ss:
        return
    by_tab: dict[str, list] = {}
    for tab, rec in u["recs"]:
        by_tab.setdefault(tab, []).append(rec)
    for tab, recs in by_tab.items():
        ss.db.setdefault(tab, []).extend(recs)
        submit("append_many", tab, recs)


def act_wish_to_idea(wid, user):
    w = next((w for w in ss.db.get("souhaits", []) if w["id"] == wid), None)
    if not w:
        return
    rec = {"id": new_id(), "destinataire": w["personne"], "nom": w["nom"], "prix": w.get("prix", ""),
           "statut": IDEE, "suggere_par": user, "cree_le": now_iso(), "lien": w.get("lien", "")}
    ss.db["idees"].append(rec)
    submit("append", "idees", rec)


def act_del_comment(cid):
    if "db" not in ss:
        return
    ss.db["commentaires"] = [c for c in ss.db["commentaires"] if c["id"] != cid]
    submit("delete_comment", cid)


# --------------------------------------------------------------------------- #
# Présentation
# --------------------------------------------------------------------------- #
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT@9..144,500..700,100&display=swap');
:root { --accent:#a8372a; --paper:#fffdf9; color-scheme: light; }
:root { --bg:#fbf7f0; }
:root {
  --muted: color-mix(in srgb, currentColor 48%, transparent);
  --hair:  color-mix(in srgb, currentColor 13%, transparent);
}

/* Chrome Streamlit */
header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"], [data-testid="stAppDeployButton"], .stDeployButton,
#MainMenu, footer, [class*="viewerBadge"] { display:none !important; }

[data-testid="stMainBlockContainer"] {
  max-width: 1040px; padding: 2.25rem 1rem 6rem;
}
.st-key-picker { max-width: 560px; }
[data-testid="stMarkdownContainer"] p { margin-bottom: 0; }
[data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
[data-testid="stHorizontalBlock"] { overflow: visible !important; }

.ttl, .th .tn, [role="dialog"] h2 {
  font-family: 'Fraunces', Georgia, 'Times New Roman', serif !important;
  font-variation-settings: "SOFT" 100; letter-spacing: 0 !important;
}
.ttl { font-size: 2.2rem; font-weight: 650; line-height: 1.15; margin: .25rem 0 .5rem; }
.sub { color: var(--muted); font-size: .95rem; }

/* Boutons-lignes (choix du nom, liste des personnes) */
.st-key-picker, .st-key-people, .st-key-liste { gap: 0 !important; }
.st-key-picker button, .st-key-people button {
  width: 100%; justify-content: flex-start; min-height: 54px; padding: 0 2px;
  border-radius: 0; border-bottom: .5px solid var(--hair); color: inherit !important;
}
.st-key-picker button p { font-size: 1.3rem; }
.st-key-people button p { font-size: 1.15rem; }
.st-key-people button::after { content: "›"; margin-left: auto; padding-right: 4px;
  color: var(--muted); font-size: 1.5rem; font-weight: 300; }
.st-key-picker button > div, .st-key-people button > div { flex: 1 1 auto; justify-content: flex-start; text-align: left; }
@media (hover: hover) { .st-key-picker button:hover, .st-key-people button:hover { background: color-mix(in srgb, currentColor 5%, transparent); } }
.st-key-picker button:active, .st-key-people button:active { background: var(--hair); }
.st-key-who { gap: 0 !important; }
.st-key-who .sub { font-size: 1rem; color: inherit; }
.st-key-who .me { color: #7f1d16; font-weight: 700; }
.credits { margin-top: 3rem; text-align: center; font-size: .8rem; color: var(--muted); }

/* Liens discrets */
.st-key-switch button, [class*="st-key-add_"] button {
  min-height: 44px; padding: 0; color: var(--accent) !important;
}
.st-key-switch button { color: var(--muted) !important; }
.st-key-switch button p { font-size: .95rem; }
[class*="st-key-add_"] button p { font-size: 1rem; }

/* Tuiles par personne */
[class*="st-key-tile_"] {
  background: var(--paper); border: 1px solid #efe6d8;
  border-radius: 18px; padding: 14px 16px 16px; gap: 0 !important;
}
.th { display:flex; align-items:center; justify-content:space-between; gap: 8px; padding-bottom: 4px; }
.th .tl { display:flex; align-items:center; gap: 10px; min-width: 0; }
.th img.tm { width: 44px; height: 44px; object-fit: contain; flex: 0 0 auto; }
.deco { display:flex; align-items:flex-end; gap: 10px; margin: 0 0 4px; }
.deco img { height: 46px; width: auto; }
.deco img:nth-child(2) { height: 54px; } .deco img:nth-child(4) { height: 40px; }
.hero { display:flex; justify-content:center; margin: 8px 0 4px; }
.hero img { width: 120px; height: auto; }
.th .tn { font-size: 1.4rem; font-weight: 600; white-space: nowrap; }
.th .tc { font-size: .78rem; color: var(--muted); text-align: right; line-height: 1.25; }
[class*="st-key-liste_"] { gap: 0 !important; }
[class*="st-key-note_"] textarea { font-size: 16px; line-height: 1.4; min-height: 72px; }
[class*="st-key-note_"] [data-testid="stTextAreaRootElement"] {
  background: #f7f1e8 !important; border: none !important; border-radius: 12px !important;
}
[data-testid="InputInstructions"] { display: none !important; }

/* Barre de saut vers une personne (reste en haut en défilant) */
[data-testid="stLayoutWrapper"]:has(> .st-key-jump) { position: sticky; top: 0; z-index: 20; }
.st-key-jump { background: var(--bg); margin: 0 -1rem; padding: 6px 1rem 8px;
  border-bottom: .5px solid var(--hair); }
.jump { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
.jump::-webkit-scrollbar { display: none; }
.jump a { flex: 0 0 auto; padding: 8px 12px; min-height: 36px; border-radius: 999px; font-size: .9rem;
  color: inherit !important; text-decoration: none; background: var(--paper); border: 1px solid #efe6d8; }
.jump a:first-child { color: var(--accent) !important; }
.anchor { scroll-margin-top: 80px; height: 0; }
html { scroll-behavior: smooth; }

/* Mes souhaits */
.st-key-mine { background: var(--paper); border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent); border-radius: 18px;
  padding: 14px 16px 10px; gap: 0 !important; }
.st-key-mine { position: relative; box-shadow: 0 3px 0 #efe6d8; }
.st-key-mine::before { content: ""; position: absolute; inset: 6px; border: 1.5px dashed #e4b3aa;
  border-radius: 13px; pointer-events: none; }  /* « couture » autour de mes souhaits */
[class*="st-key-wrow_"] { flex-wrap: nowrap !important; border-bottom: .5px solid var(--hair); }
[class*="st-key-wrow_"] > div:first-child { flex: 1 1 auto; min-width: 0; }
.wish { display:flex; align-items:center; justify-content:space-between; gap: 12px; min-height: 48px; padding: 6px 0; }
.wish .nm { font-size: 1rem; line-height: 1.3; overflow-wrap: anywhere; }
.wish .px { font-size: .9rem; color: var(--muted); white-space: nowrap; }
.wish a.lnk { min-height: 24px; line-height: 24px; font-size: .85rem; }
.wdone { font-size: .75rem; color: var(--muted); white-space: nowrap; }
[class*="st-key-w2i_"] button { min-height: 44px; padding: 0 4px; color: var(--accent) !important; }
[class*="st-key-w2i_"] button p { font-size: .85rem; }
[class*="st-key-icoedit_"] button, [class*="st-key-icodel_"] button { min-height: 44px; min-width: 36px; padding: 0; color: var(--muted) !important; }
[class*="st-key-icoedit_"] button [data-testid="stMarkdownContainer"],
[class*="st-key-icodel_"] button [data-testid="stMarkdownContainer"] {
  position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
.st-key-add_wish button { min-height: 44px; padding: 0; color: var(--accent) !important; }

/* Barre « Annuler » après suppression */
.st-key-undo { position: fixed; left: 50%; bottom: max(16px, env(safe-area-inset-bottom)); transform: translateX(-50%);
  z-index: 50; width: min(92vw, 440px) !important; background: #1c1c1e; color: #f2f2f7;
  border-radius: 14px; padding: 4px 8px 4px 16px; box-shadow: 0 6px 24px rgba(0,0,0,.18); flex-wrap: nowrap !important; }
.st-key-undo > div:first-child { flex: 1 1 auto; min-width: 0; }
.st-key-undo .ul { font-size: .92rem; color: #f2f2f7; }
.st-key-undo button { min-height: 44px; color: #ffb4a9 !important; font-weight: 600; }
.nby { font-size: .75rem; color: var(--muted); margin-top: 4px; }

/* Lignes d'idées */
[class*="st-key-it_"] { border-bottom: .5px solid var(--hair); gap: 0 !important; }
[class*="st-key-row_"] { flex-wrap: nowrap !important; align-items: center; }
[class*="st-key-tog_"] button { min-height: 44px; min-width: 40px; padding: 0; justify-content: flex-start; color: var(--muted) !important; }
[class*="st-key-tog_"] button span { font-size: 1.45rem !important; }
[class*="st-key-tog_"] button [data-testid="stMarkdownContainer"],
[class*="st-key-delc_"] button [data-testid="stMarkdownContainer"] {
  position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap;
}
[class*="st-key-body_"] { position: relative; min-width: 0; flex: 1 1 auto; }
[class*="st-key-open_"] { position: absolute !important; inset: 0; width: auto !important; height: auto !important; z-index: 2; margin: 0; }
[class*="st-key-open_"] button, [class*="st-key-open_"] .stButton { width: 100%; height: 100%; opacity: 0; }

.row { display:flex; align-items:center; justify-content:space-between; gap: 12px; min-height: 56px; padding: 8px 0; }
.row .nm { font-size: 1.05rem; line-height: 1.3; overflow-wrap: anywhere; }
.row .by { font-size: .8rem; color: var(--muted); margin-top: 1px; }
.row .px { font-size: .95rem; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
.row.done .nm { color: var(--muted); }
[class*="st-key-it_"][class*="_prv"] { background: #eef2e4; border-left: 3px solid #5d7a4a;
  border-radius: 10px; padding-left: 8px; margin: 3px 0; border-bottom: none; }
.row .sec { color: #4f6a3d; font-weight: 600; }
[class*="st-key-votes_"] { flex-wrap: nowrap !important; gap: 4px !important; }
[class*="st-key-vup_"] button, [class*="st-key-vdn_"] button {
  min-height: 40px; min-width: 52px; padding: 0 10px; border-radius: 999px !important;
  border: 1px solid #e8dfd2 !important; background: var(--paper) !important; color: var(--muted) !important; }
[class*="st-key-vup_"][class*="_on"] button { color: #c0392b !important; border-color: #e8b4ac !important; background: #fbecea !important; }
[class*="st-key-vdn_"][class*="_on"] button { color: #5b4a3f !important; border-color: #cfc3b5 !important; background: #f1ebe3 !important; }
[class*="st-key-vup_"] button p, [class*="st-key-vdn_"] button p { font-size: .9rem; }
.vwho { font-size: .78rem; color: var(--muted); padding-left: 4px; overflow-wrap: anywhere; }
.row .lk { color: var(--muted); font-size: .85em; }
.lnks { display: flex; flex-wrap: wrap; column-gap: 14px; align-items: baseline; }
.lnote { font-size: .85rem; color: var(--muted); font-style: italic; overflow-wrap: anywhere; }
a.lnk { display: inline-block; min-height: 32px; line-height: 32px; color: var(--accent) !important;
  text-decoration: none; font-size: .95rem; overflow-wrap: anywhere; }

/* Détails dépliés */
[class*="st-key-det_"] { padding: 0 0 14px 44px; gap: .35rem !important; }
.lbl { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-top: 2px; }
.com { font-size: .95rem; line-height: 1.35; overflow-wrap: anywhere; }
.com .au { color: var(--muted); }
.vide { font-size: .9rem; color: var(--muted); }
[class*="st-key-crow_"] { flex-wrap: nowrap !important; align-items: center; }
[class*="st-key-crow_"] > div:first-child { flex: 1 1 auto; min-width: 0; }
[class*="st-key-delc_"] button { min-height: 36px; min-width: 36px; padding: 0; color: var(--muted) !important; }
[class*="st-key-det_"] [data-testid="stForm"] { padding: 0; }
[class*="st-key-det_"] [data-testid="stTextInputRootElement"] { background: transparent !important; border: none !important; border-bottom: .5px solid var(--hair) !important; border-radius: 0 !important; }
[class*="st-key-det_"] input { padding-left: 0; font-size: 16px; min-height: 44px; } /* 16px : pas de zoom iOS */
[class*="st-key-cbar_"] { flex-wrap: nowrap !important; align-items: center; }
[class*="st-key-cbar_"] [data-testid="stFormSubmitButton"] button { min-height: 44px; color: var(--accent) !important; padding: 0 4px; }
[class*="st-key-acts_"] button { min-height: 44px; padding: 0 2px; color: var(--muted) !important; }
[class*="st-key-acts_"] { gap: 1.5rem !important; }

/* Dialogues : champs en 16px pour éviter le zoom de Safari iOS */
[role="dialog"] input, [data-testid="stForm"] input { font-size: 16px; }
</style>
"""


def esc(s) -> str:
    return html.escape(str(s or ""))


_NUMERIC = re.compile(r"[\d\s.,\-–~à]+")


def fmt_prix(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    if "€" in p or not any(ch.isdigit() for ch in p):
        return p
    if _NUMERIC.fullmatch(p):
        return f"{p} €"
    return p


def norm_url(u: str) -> str:
    u = (u or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


# Un « mot » n'est un lien que s'il ressemble à une adresse web (domaine avec .fr, .com…).
_URL_RE = re.compile(r"^(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.I)


def parse_links(v) -> tuple[list[str], str]:
    """Contenu de la case « lien » -> (liens, texte libre restant)."""
    urls, words = [], []
    for tok in str(v or "").split():
        if _URL_RE.match(tok):
            u = norm_url(tok)
            if u not in urls:
                urls.append(u)
        else:
            # Répare aussi les mots enregistrés par erreur comme « https://mot ».
            words.append(re.sub(r"^https?://", "", tok, flags=re.I))
    return urls, " ".join(w for w in words if w)


def links_of(v) -> list[str]:
    return parse_links(v)[0]


def links_field_value(v) -> str:
    """Texte proposé dans le formulaire de modification."""
    urls, text = parse_links(v)
    return "\n".join(urls + ([text] if text else []))


def join_links(v) -> str:
    """À l'enregistrement : on garde ce qui a été tapé (lignes nettoyées), sans rien transformer."""
    return "\n".join(line.strip() for line in str(v or "").splitlines() if line.strip())


def links_html(v, cls="lnk") -> str:
    urls, text = parse_links(v)
    out = "".join(f'<a class="{cls}" href="{esc(u)}" target="_blank" rel="noopener noreferrer">'
                  f'{esc(url_label(u))} ↗</a>' for u in urls)
    if text:
        out += f'<span class="lnote">{esc(text)}</span>'
    return out


def url_label(u: str) -> str:
    host = urlparse(u).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or u)


def sort_key(it):
    return (0 if it.get("statut") == ACHETE else 1, it.get("cree_le", ""))


MOTIFS_TUILES = ["maison", "chaussettes", "tasse_pomme", "clementine", "couronne_bougies",
                 "boule_neige", "bonnet", "branche_orange", "confiture", "tarte", "bonhomme_neige",
                 "maison_pain_epice", "tasse_the", "pomme"]


def motif(name, cls=""):
    return f'<img class="{cls}" src="app/static/motifs/{name}.png" alt="" loading="lazy">'


def title(text):
    st.markdown(f'<div class="ttl">{esc(text)}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Dialogues
# --------------------------------------------------------------------------- #
def _idea_fields(user, dest_default, it=None):
    """Champs communs aux dialogues d'ajout et de modification."""
    it = it or {}
    others = [p for p in ss.db["personnes"] if p != user]
    nom = st.text_input("Nom", value=it.get("nom", ""), placeholder="ex. Un livre de cuisine")
    prix = st.text_input("Prix (optionnel)", value=it.get("prix", ""), placeholder="ex. 25")
    lien = st.text_area("Liens (optionnel, un par ligne)", value=links_field_value(it.get("lien", "")),
                        placeholder="https://…", height=80)
    pour = st.multiselect("Pour", others, default=[d for d in dest_default if d in others],
                          help="Plusieurs personnes = cadeau commun (ex. un couple)",
                          placeholder="Choisir…")
    secret = st.checkbox("Secret : visible seulement par moi", value=bool(it.get("prive")))
    return nom, prix, lien, pour, secret


def _check_idea(nom, pour):
    if not nom.strip():
        st.error("Le nom est obligatoire.")
        return False
    if not pour:
        st.error("Choisissez au moins une personne.")
        return False
    return True


@st.dialog("Ajouter une idée")
def dlg_add(dest, user):
    with st.form("f_add", border=False):
        nom, prix, lien, pour, secret = _idea_fields(user, [dest])
        st.caption(f"Suggéré par {user}")
        ok = st.form_submit_button("Ajouter", type="primary", width="stretch")
    if ok and _check_idea(nom, pour):
        rec = {"id": new_id(), "destinataire": SEP.join(pour), "nom": nom.strip(), "prix": prix.strip(),
               "statut": IDEE, "suggere_par": user, "cree_le": now_iso(),
               "lien": join_links(lien), "prive": "oui" if secret else ""}
        ss.db["idees"].append(rec)
        submit("append", "idees", rec)
        st.rerun()


@st.dialog("Modifier l'idée")
def dlg_edit(iid, user):
    it = find_idee(iid)
    if not it:
        st.rerun()
    with st.form(f"f_edit_{iid}", border=False):
        nom, prix, lien, pour, secret = _idea_fields(user, dests(it), it)
        ok = st.form_submit_button("Enregistrer", type="primary", width="stretch")
    if ok and _check_idea(nom, pour):
        fields = {"nom": nom.strip(), "prix": prix.strip(), "lien": join_links(lien),
                  "destinataire": SEP.join(pour), "prive": "oui" if secret else ""}
        it.update(fields)
        submit("update", "idees", iid, fields)
        st.rerun()


def _wish_form(key, w=None):
    with st.form(key, border=False):
        nom = st.text_input("Nom", value=(w or {}).get("nom", ""), placeholder="ex. Un pull en laine")
        prix = st.text_input("Prix (optionnel)", value=(w or {}).get("prix", ""), placeholder="ex. 40")
        lien = st.text_area("Liens (optionnel, un par ligne)", value=links_field_value((w or {}).get("lien", "")),
                            placeholder="https://…", height=80)
        ok = st.form_submit_button("Enregistrer" if w else "Ajouter", type="primary", width="stretch")
    if ok and not nom.strip():
        st.error("Le nom est obligatoire.")
        return None
    return (nom.strip(), prix.strip(), join_links(lien)) if ok else None


@st.dialog("Ajouter un souhait")
def dlg_wish_add(user):
    res = _wish_form("f_wish_add")
    if res:
        rec = {"id": new_id(), "personne": user, "nom": res[0], "prix": res[1], "lien": res[2],
               "cree_le": now_iso()}
        ss.db.setdefault("souhaits", []).append(rec)
        submit("append", "souhaits", rec)
        st.rerun()


@st.dialog("Modifier le souhait")
def dlg_wish_edit(wid):
    w = next((w for w in ss.db.get("souhaits", []) if w["id"] == wid), None)
    if not w:
        st.rerun()
    res = _wish_form(f"f_wish_{wid}", w)
    if res:
        w["nom"], w["prix"], w["lien"] = res
        submit("update", "souhaits", wid, {"nom": res[0], "prix": res[1], "lien": res[2]})
        st.rerun()


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def page_picker(people):
    st.markdown(f'<div class="hero">{motif("ecureuil")}</div>', unsafe_allow_html=True)
    title("Qui êtes-vous ?")
    st.write("")
    if not people:
        st.markdown('<div class="vide">Aucune personne dans l\'onglet « personnes ».</div>', unsafe_allow_html=True)
    with st.container(key="picker"):
        for i, name in enumerate(people):
            st.button(name, key=f"pick_{i}", type="tertiary", width="stretch",
                      on_click=go, kwargs={"user": name})


def anchor(name, people):
    return f"p{people.index(name)}"


def page_home(user, people, db):
    deco = "".join(motif(m) for m in ("branche_orange", "bonhomme", "houx", "bougie_rouge", "paquet"))
    st.markdown(f'<div id="haut"></div><div class="deco">{deco}</div>', unsafe_allow_html=True)
    title(TITRE)
    with st.container(gap=None, key="who"):
        st.markdown(f'<div class="sub">Vous êtes <b class="me">{esc(user)}</b></div>', unsafe_allow_html=True)
        st.button("Changer de personne", key="switch", type="tertiary", on_click=go)

    others = [p for p in people if p != user]
    # Ordre des tuiles : d'abord ceux qui ont le moins d'idées. Figé pour la session
    # pour qu'une tuile ne saute pas ailleurs pendant qu'on y ajoute quelque chose.
    order_key = (user, tuple(others))
    if ss.get("order_key") != order_key:
        counts = Counter(d for i in db["idees"] if visible(i, user) for d in dests(i))
        ss.order = sorted(others, key=lambda p: counts[p])
        ss.order_key = order_key
    order = ss.order

    links = ['<a href="#moi">Mes souhaits</a>'] + [
        f'<a href="#{anchor(p, people)}">{esc(p)}</a>' for p in order]
    with st.container(key="jump"):
        st.markdown(f'<nav class="jump">{"".join(links)}</nav>', unsafe_allow_html=True)

    render_my_wishes(user, db)
    for r in range(0, len(order), 2):
        cols = st.columns(2, gap="medium")
        for c, dest in zip(cols, order[r:r + 2]):
            with c:
                render_tile(people.index(dest), dest, db, user, anchor(dest, people))

    st.markdown('<div class="credits">Ce mini-site a été fait par Julie. '
                'Les illustrations sont de Och Monika.</div>', unsafe_allow_html=True)
    render_undo()


@st.fragment(run_every=3)  # se ré-affiche seule pour disparaître après UNDO_SECONDS
def render_undo():
    u = ss.get("undo")
    if not u:
        return
    if time.time() - u["at"] > UNDO_SECONDS:
        ss.pop("undo", None)
        return
    with st.container(key="undo", horizontal=True, vertical_alignment="center", gap="small"):
        st.markdown(f'<div class="ul">{esc(u["label"])}</div>', unsafe_allow_html=True)
        if st.button("Annuler", key="undo_btn", type="tertiary"):
            act_undo()
            st.rerun()  # toute la page, pour réafficher l'élément restauré


def _wish_html(w, extra=""):
    prix = fmt_prix(w.get("prix", ""))
    links = links_html(w.get("lien", ""))
    link = f'<div class="lnks">{links}</div>' if links else ""
    return (f'<div class="wish"><div><div class="nm">{esc(w["nom"])}</div>'
            f'{link}{extra}</div>' + (f'<div class="px">{esc(prix)}</div>' if prix else "") + "</div>")


def render_my_wishes(user, db):
    mine = sorted((w for w in db.get("souhaits", []) if w["personne"] == user),
                  key=lambda w: w.get("cree_le", ""))
    with st.container(key="mine"):
        st.markdown(f'<div id="moi" class="anchor"></div><div class="th"><span class="tl">{motif("paquet", "tm")}<span class="tn">Mes souhaits</span></span>'
                    '<span class="tc">visibles par la famille</span></div>', unsafe_allow_html=True)
        for w in mine:
            with st.container(horizontal=True, wrap=False, gap="small", vertical_alignment="center",
                              key=f"wrow_{w['id']}"):
                st.markdown(_wish_html(w), unsafe_allow_html=True)
                if st.button("Modifier le souhait", key=f"icoedit_{w['id']}", type="tertiary",
                             icon=":material/edit:"):
                    dlg_wish_edit(w["id"])
                st.button("Supprimer le souhait", key=f"icodel_{w['id']}", type="tertiary",
                          icon=":material/close:", on_click=act_delete_wish, args=(w["id"],))
        if st.button("Ajouter un souhait", key="add_wish", type="tertiary", icon=":material/add:"):
            dlg_wish_add(user)


def _plural(n, word):
    return f"{n} {word}{'s' if n > 1 else ''}"


def render_tile(i, dest, db, user, anchor_id):
    items = sorted((it for it in db["idees"] if dest in dests(it) and visible(it, user)), key=sort_key)
    wishes = sorted((w for w in db.get("souhaits", []) if w["personne"] == dest),
                    key=lambda w: w.get("cree_le", ""))
    n_bought = sum(it["statut"] == ACHETE for it in items)
    summary = _plural(len(items), "idée") if items else "aucune idée"
    if n_bought:
        summary += " · " + _plural(n_bought, "acheté")
    with st.container(key=f"tile_{i}"):
        st.markdown(f'<div id="{anchor_id}" class="anchor"></div>'
                    f'<div class="th"><span class="tl">{motif(MOTIFS_TUILES[i % len(MOTIFS_TUILES)], "tm")}'
                    f'<span class="tn">{esc(dest)}</span></span>'
                    f'<span class="tc">{summary}</span></div>', unsafe_allow_html=True)
        if wishes:
            st.markdown('<div class="lbl" style="margin:4px 0 2px">Ses souhaits</div>',
                        unsafe_allow_html=True)
            taken = {it["nom"].casefold() for it in items} | {u for it in items for u in links_of(it.get("lien"))}
            for w in wishes:
                with st.container(horizontal=True, wrap=False, gap="small", vertical_alignment="center",
                                  key=f"wrow_{w['id']}"):
                    already = w["nom"].casefold() in taken or any(u in taken for u in links_of(w.get("lien")))
                    st.markdown(_wish_html(w), unsafe_allow_html=True)
                    if already:
                        st.markdown('<div class="wdone">dans les idées</div>', unsafe_allow_html=True)
                    else:
                        st.button("idée", key=f"w2i_{w['id']}", type="tertiary", icon=":material/add:",
                                  help="Ajouter aux idées (pour suivre l'achat)",
                                  on_click=act_wish_to_idea, args=(w["id"], user))
            st.markdown('<div class="lbl" style="margin:12px 0 2px">Idées</div>', unsafe_allow_html=True)
        with st.container(key=f"liste_{i}"):
            for it in items:
                render_item(it, db, user, i, dest)
        if st.button("Ajouter une idée", key=f"add_{i}", type="tertiary", icon=":material/add:"):
            dlg_add(dest, user)
        render_note(i, dest, db, user)


def render_note(i, dest, db, user):
    note = next((n for n in db.get("notes", []) if n["destinataire"] == dest), {})
    txt = note.get("texte", "")
    key, seen = f"note_{i}", f"noteseen_{i}"
    # Reprendre le texte enregistré, sauf si l'on est en train de le modifier.
    if key not in ss or (ss[key] == ss.get(seen) and txt != ss.get(seen)):
        ss[key] = txt
    ss[seen] = txt if ss[key] == txt else ss.get(seen, txt)
    st.markdown('<div class="lbl" style="margin:10px 0 6px">Notes</div>', unsafe_allow_html=True)
    st.text_area("Notes", key=key, label_visibility="collapsed", height="content",
                 on_change=act_note, args=(dest, key, seen, user))
    if note.get("modifie_par"):
        st.markdown(f'<div class="nby">modifié par {esc(note["modifie_par"])}</div>',
                    unsafe_allow_html=True)


def render_item(it, db, user, ti, dest):
    """ti/dest : la tuile où l'idée est affichée (une idée commune apparaît dans plusieurs tuiles)."""
    iid = it["id"]
    k = f"{iid}_{ti}"
    bought = it["statut"] == ACHETE
    secret = bool(it.get("prive"))
    tags = [f"suggéré par {esc(it['suggere_par'])}"]
    autres = [d for d in dests(it) if d != dest]
    if autres:
        tags.append("commun avec " + esc(", ".join(autres)))
    ups, downs = vote_counts(it, db)
    if ups or downs:
        tags.append(" ".join(x for x in (f"♥ {ups}" if ups else "", f"👎 {downs}" if downs else "") if x))
    if secret:
        tags.append('<span class="sec">secret · visible par vous seul</span>')
    with st.container(key=f"it_{k}" + ("_prv" if secret else "")):
        with st.container(horizontal=True, wrap=False, gap="small", key=f"row_{k}"):
            st.button(
                "Remettre en idée" if bought else "Marquer comme acheté",
                key=f"tog_{k}", type="tertiary",
                icon=":material/check_circle:" if bought else ":material/radio_button_unchecked:",
                on_click=act_toggle, args=(iid,),
            )
            with st.container(key=f"body_{k}"):
                prix = fmt_prix(it["prix"])
                st.markdown(
                    f'<div class="row{" done" if bought else ""}"><div>'
                    f'<div class="nm">{esc(it["nom"])}'
                    + (' <span class="lk" title="lien">↗</span>' if links_of(it.get("lien")) else "")
                    + '</div>'
                    f'<div class="by">{" · ".join(tags)}</div></div>'
                    + (f'<div class="px">{esc(prix)}</div>' if prix else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )
                st.button(f"Détails : {it['nom']}", key=f"open_{k}", type="tertiary",
                          on_click=act_open, args=(k,))
        if ss.open == k:
            render_details(it, db, user, k)


def vote_counts(it, db):
    vs = [v for v in db.get("votes", []) if v["idee_id"] == it["id"]]
    return sum(v["vote"] == "+1" for v in vs), sum(v["vote"] == "-1" for v in vs)


def render_votes(it, db, user, k):
    iid = it["id"]
    vs = [v for v in db.get("votes", []) if v["idee_id"] == iid]
    mine = next((v["vote"] for v in vs if v["personne"] == user), "")
    ups = [v["personne"] for v in vs if v["vote"] == "+1"]
    downs = [v["personne"] for v in vs if v["vote"] == "-1"]
    with st.container(horizontal=True, wrap=False, gap="small", vertical_alignment="center",
                      key=f"votes_{k}"):
        st.button(str(len(ups)), key=f"vup_{k}" + ("_on" if mine == "+1" else ""), type="tertiary",
                  icon=":material/favorite:", help="J'aime cette idée",
                  on_click=act_vote, args=(iid, user, "+1"))
        st.button(str(len(downs)), key=f"vdn_{k}" + ("_on" if mine == "-1" else ""), type="tertiary",
                  icon=":material/thumb_down:", help="Pas convaincu·e",
                  on_click=act_vote, args=(iid, user, "-1"))
        who = []
        if ups:
            who.append("♥ " + ", ".join(ups))
        if downs:
            who.append("👎 " + ", ".join(downs))
        if who:
            st.markdown(f'<div class="vwho">{esc(" · ".join(who))}</div>', unsafe_allow_html=True)


def render_details(it, db, user, k):
    iid = it["id"]
    with st.container(key=f"det_{k}"):
        links = links_html(it.get("lien", ""))
        if links:
            st.markdown(f'<div class="lnks">{links}</div>', unsafe_allow_html=True)
        render_votes(it, db, user, k)
        st.markdown('<div class="lbl">Commentaires</div>', unsafe_allow_html=True)
        coms = sorted((c for c in db["commentaires"] if c["idee_id"] == iid),
                      key=lambda c: c.get("cree_le", ""))
        for c in coms:
            with st.container(horizontal=True, wrap=False, gap="small", key=f"crow_{c['id']}_{k}"):
                st.markdown(f'<div class="com"><span class="au">{esc(c["auteur"])} :</span> '
                            f'{esc(c["texte"])}</div>', unsafe_allow_html=True)
                st.button("Supprimer le commentaire", key=f"delc_{c['id']}_{k}", type="tertiary",
                          icon=":material/close:", on_click=act_del_comment, args=(c["id"],))
        with st.form(f"cf_{k}", clear_on_submit=True, border=False):
            with st.container(horizontal=True, wrap=False, gap="small", key=f"cbar_{k}",
                              vertical_alignment="center"):
                st.text_input("Commentaire", key=f"ct_{k}", placeholder="Ajouter un commentaire…",
                              label_visibility="collapsed")
                st.form_submit_button("Envoyer", type="tertiary",
                                      on_click=act_add_comment, args=(iid, user, f"ct_{k}"))
        with st.container(horizontal=True, gap="medium", key=f"acts_{k}"):
            if st.button("Modifier", key=f"edit_{k}", type="tertiary"):
                dlg_edit(iid, user)
            st.button("Supprimer", key=f"del_{k}", type="tertiary",
                      on_click=act_delete_idee, args=(iid,))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
st.markdown(CSS, unsafe_allow_html=True)


check_access()
db = get_db()
people = db["personnes"]
user = st.query_params.get("user")

if user not in people:
    page_picker(people)
else:
    page_home(user, people, db)
