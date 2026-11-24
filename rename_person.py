"""Rename a family member everywhere in the sheet (or the local test file).

Updates the name in `personnes` and in every column that refers to a person:
recipients (including joint gifts like `Bruno + Victoire`), authors, wishes,
notes and votes. Without --apply it only shows what would change.

Usage
-----
  python rename_person.py "Georges" "Jojo"            # dry run on the real sheet
  python rename_person.py "Georges" "Jojo" --apply    # do it
  python rename_person.py "Georges" "Jojo" --local --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

SEP = " + "
# tab -> columns holding one person's name
SINGLE = {
    "personnes": ["nom"],
    "idees": ["suggere_par"],
    "commentaires": ["auteur"],
    "notes": ["destinataire", "modifie_par"],
    "souhaits": ["personne"],
    "votes": ["personne"],
}
# tab -> columns that may hold several names joined by " + "
MULTI = {"idees": ["destinataire"]}


def renamed(value: str, col_multi: bool, old: str, new: str) -> str:
    if col_multi:
        parts = [p.strip() for p in str(value).split("+")]
        if old in parts:
            return SEP.join(new if p == old else p for p in parts)
        return value
    return new if str(value).strip() == old else value


def col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def run_google(old, new, apply):
    import gspread

    secrets = tomllib.loads(Path(".streamlit/secrets.toml").read_text(encoding="utf-8"))
    gc = gspread.service_account_from_dict(dict(secrets["gcp_service_account"]))
    sh = gc.open_by_key(secrets["sheet_id"])
    tabs = list(SINGLE)
    data = sh.values_batch_get([f"'{t}'" for t in tabs])["valueRanges"]
    updates, existing = [], set()
    for tab, vr in zip(tabs, data):
        rows = vr.get("values", [])
        if not rows:
            continue
        header = rows[0]
        if tab == "personnes":
            existing = {r[0].strip() for r in rows[1:] if r}
        for col in SINGLE.get(tab, []) + MULTI.get(tab, []):
            if col not in header:
                continue
            c = header.index(col)
            for r, row in enumerate(rows[1:], start=2):
                if c < len(row) and row[c]:
                    nv = renamed(row[c], col in MULTI.get(tab, []), old, new)
                    if nv != row[c]:
                        updates.append({"range": f"'{tab}'!{col_letter(c)}{r}", "values": [[nv]]})
                        print(f"{tab}!{col_letter(c)}{r}: {row[c]!r} -> {nv!r}")
    check(old, new, existing)
    if apply and updates:
        sh.values_batch_update({"valueInputOption": "RAW", "data": updates})
    return len(updates)


def run_local(old, new, apply, path="local_db.json"):
    p = Path(path)
    db = json.loads(p.read_text(encoding="utf-8"))
    check(old, new, set(db["personnes"]))
    n = 0
    db["personnes"] = [new if x == old else x for x in db["personnes"]]
    n += db["personnes"].count(new)
    for tab in SINGLE:
        if tab == "personnes":
            continue
        for rec in db.get(tab, []):
            for col in SINGLE[tab] + MULTI.get(tab, []):
                if rec.get(col):
                    nv = renamed(rec[col], col in MULTI.get(tab, []), old, new)
                    if nv != rec[col]:
                        print(f"{tab}.{col}: {rec[col]!r} -> {nv!r}")
                        rec[col] = nv
                        n += 1
    if apply:
        p.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    return n


def check(old, new, existing):
    if old not in existing:
        sys.exit(f"{old!r} is not in the personnes tab.")
    if new in existing:
        sys.exit(f"{new!r} already exists in the personnes tab.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--local", action="store_true", help="use local_db.json instead of the sheet")
    a = ap.parse_args()
    old, new = a.old.strip(), a.new.strip()
    if not new or "+" in new:
        sys.exit("The new name must not be empty or contain '+'.")
    n = run_local(old, new, a.apply) if a.local else run_google(old, new, a.apply)
    print(f"{n} cell(s) {'updated' if a.apply else 'would change (dry run, add --apply)'}")


if __name__ == "__main__":
    main()
