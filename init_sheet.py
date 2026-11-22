"""Initialise the Google Sheet (or the local test file) for the gift app.

Creates the six tabs `personnes`, `idees`, `commentaires`, `notes`, `souhaits`, `votes` with their headers
(if missing) and adds the family names to `personnes`.
Existing data is never deleted: names already present are skipped.

Usage
-----
  # Real sheet, using .streamlit/secrets.toml (sheet_id + [gcp_service_account])
  python init_sheet.py --names "Alice, Bob, Chloé"

  # Or with a JSON key file and an explicit sheet ID
  python init_sheet.py --key service-account.json --sheet-id 1AbC... --names-file names.txt

  # Local JSON backend for testing (no Google needed); --demo adds sample ideas
  python init_sheet.py --local --names "Alice, Bob, Chloé" --demo
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADERS = {
    "personnes": ["nom"],
    "idees": ["id", "destinataire", "nom", "prix", "statut", "suggere_par", "cree_le", "lien", "prive"],
    "commentaires": ["id", "idee_id", "auteur", "texte", "cree_le"],
    "notes": ["destinataire", "texte", "modifie_par", "modifie_le"],
    "souhaits": ["id", "personne", "nom", "prix", "lien", "cree_le"],
    "votes": ["idee_id", "personne", "vote", "modifie_le"],
}
DEFAULT_TABS = {"Sheet1", "Feuille 1", "Feuille1", "Hoja 1", "Tabelle1"}


def parse_names(args) -> list[str]:
    raw = []
    if args.names:
        raw += args.names.split(",")
    if args.names_file:
        raw += Path(args.names_file).read_text(encoding="utf-8").splitlines()
    names, seen = [], set()
    for n in (x.strip() for x in raw):
        if n and n not in seen:
            names.append(n)
            seen.add(n)
    return names


def init_google(args, names):
    import gspread

    if args.key:
        gc = gspread.service_account(filename=args.key)
        sheet_id = args.sheet_id
    else:
        secrets = tomllib.loads(Path(".streamlit/secrets.toml").read_text(encoding="utf-8"))
        gc = gspread.service_account_from_dict(dict(secrets["gcp_service_account"]))
        sheet_id = args.sheet_id or secrets["sheet_id"]
    if not sheet_id:
        sys.exit("Missing sheet ID (--sheet-id or sheet_id in secrets.toml).")

    sh = gc.open_by_key(sheet_id)
    existing = {ws.title: ws for ws in sh.worksheets()}

    for tab, headers in HEADERS.items():
        ws = existing.get(tab)
        if ws is None:
            ws = sh.add_worksheet(title=tab, rows=500, cols=len(headers))
            print(f"created tab {tab}")
        first = ws.row_values(1)
        if not first:
            ws.update(range_name="A1", values=[headers], value_input_option="RAW")
            print(f"wrote headers in {tab}")
        elif first != headers[: len(first)] and first[: len(headers)] != headers:
            sys.exit(f"Tab {tab} has unexpected headers {first}; expected {headers}. Fix it by hand.")
        elif len(first) < len(headers):
            # Older sheet: add the new columns (e.g. `lien`) after the existing ones.
            if ws.col_count < len(headers):
                ws.add_cols(len(headers) - ws.col_count)
            ws.update(range_name="A1", values=[headers], value_input_option="RAW")
            print(f"added columns {headers[len(first):]} to {tab}")
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        existing[tab] = ws

    # Remove the default empty tab created with a new spreadsheet.
    for title, ws in list(existing.items()):
        if title in DEFAULT_TABS and title not in HEADERS and not ws.get_all_values():
            sh.del_worksheet(ws)
            print(f"removed empty default tab {title}")

    ws = existing["personnes"]
    present = {v.strip() for v in ws.col_values(1)[1:]}
    to_add = [[n] for n in names if n not in present]
    if to_add:
        ws.append_rows(to_add, value_input_option="RAW", table_range="A1")
    print(f"personnes: {len(to_add)} added, {len(present)} already present")


def init_local(args, names):
    path = Path(args.local_path)
    db = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
          else {"personnes": [], "idees": [], "commentaires": [], "notes": [], "souhaits": [], "votes": []})
    db.setdefault("notes", [])
    db.setdefault("souhaits", [])
    for n in names:
        if n not in db["personnes"]:
            db["personnes"].append(n)
    if args.demo and len(db["personnes"]) >= 3 and not db["idees"]:
        p = db["personnes"]
        t0 = datetime.now(timezone.utc) - timedelta(days=3)
        samples = [
            (p[1], "Un livre de cuisine italienne", "25", "Idée", p[0]),
            (p[1], "Écharpe en laine mérinos très douce, couleur bleu nuit", "45 €", "Acheté", p[2]),
            (p[1], "Places de concert", "", "Idée", p[2]),
            (p[0], "Montre connectée", "199", "Idée", p[1]),
            (p[2], "Puzzle 1000 pièces", "15-20", "Acheté", p[0]),
        ]
        for k, (dest, nom, prix, statut, by) in enumerate(samples):
            db["idees"].append({
                "id": uuid.uuid4().hex[:10], "destinataire": dest, "nom": nom, "prix": prix,
                "statut": statut, "suggere_par": by,
                "cree_le": (t0 + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        first = db["idees"][0]
        db["commentaires"].append({
            "id": uuid.uuid4().hex[:10], "idee_id": first["id"], "auteur": p[2],
            "texte": "Elle adore les pâtes fraîches !", "cree_le": first["cree_le"],
        })
    path.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {path} ({len(db['personnes'])} people, {len(db['idees'])} ideas)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--names", help="comma-separated family names")
    ap.add_argument("--names-file", help="text file, one name per line")
    ap.add_argument("--key", help="service account JSON key file (else .streamlit/secrets.toml)")
    ap.add_argument("--sheet-id", help="Google Sheet ID (else sheet_id from secrets.toml)")
    ap.add_argument("--local", action="store_true", help="initialise the local JSON test backend")
    ap.add_argument("--local-path", default="local_db.json")
    ap.add_argument("--demo", action="store_true", help="(local only) add sample ideas")
    args = ap.parse_args()
    names = parse_names(args)
    if args.local:
        init_local(args, names)
    else:
        init_google(args, names)


if __name__ == "__main__":
    main()
