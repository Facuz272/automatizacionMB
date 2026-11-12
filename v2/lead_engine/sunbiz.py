"""Florida Division of Corporations (Sunbiz) → board officers and registered agent of a condo association.

Why: DBPR names the association that runs each condominium by its exact legal name (`OLYMPUS ASSOC., INC.`),
but its `Managing Entity Number` (MA00000037) is a DBPR id, not the Sunbiz document number, so the link is
made by normalised legal name + ZIP / city.

Vía A — local bulk file (`python -m lead_engine sunbiz-sync`): the Corporate Data File (quarterly full file
and daily updates) is downloaded by hand from the Division's data-download service and dropped in
`data/sunbiz/` (`.txt` / `.dat`, also inside `.zip`). Layout = the official definition page
(dos.sunbiz.org/data-definitions/cor.html): fixed-width ASCII records of 1440 characters, up to six
officer blocks of 128 characters from column 669. CSV exports with headers are read too (see CSV_ALIASES).
Only associations are indexed (non-profit filings, or names with ASSOCIATION / CONDOMINIUM / HOMEOWNERS /
OWNERS / COOPERATIVE). Stored per association: document number, legal name, status, filing type, principal
ZIP / city (for matching), registered agent name + kind, officers' title + name + kind.
**No address of any officer or agent is ever stored** (they are often residents' homes).

Vía B — the online search (search.sunbiz.org) answers 403 to automated clients, so it is not scraped; once
`data/sunbiz.db` has rows, every enriched lead with a DBPR association gets its officers as contacts:
President / VP become the contact when the lead has none, the rest feed the Ficha's Plan B.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from loguru import logger

from .config import settings

RECORD_LEN = 1440
# (start column 1-based, length) per the official Corporate Data File definition.
FIELDS = {
    "doc_number": (1, 12), "name": (13, 192), "status": (205, 1), "filing_type": (206, 15),
    "city": (305, 28), "state": (333, 2), "zip": (335, 10),
    "file_date": (473, 8), "more_officers": (495, 1), "last_tx": (496, 8),
    "ra_name": (545, 42), "ra_type": (587, 1),
}
OFFICER_START, OFFICER_LEN, OFFICERS = 669, 128, 6
OFFICER_FIELDS = {"title": (0, 4), "kind": (4, 1), "name": (5, 42)}      # address / city / zip are skipped

SCHEMA = """
CREATE TABLE IF NOT EXISTS sunbiz_corps (
  doc_number TEXT PRIMARY KEY, name TEXT, name_key TEXT, status TEXT, filing_type TEXT, city TEXT, zip TEXT,
  file_date TEXT, last_tx TEXT, ra_name TEXT, ra_kind TEXT, source_file TEXT
);
CREATE INDEX IF NOT EXISTS sunbiz_corps_key ON sunbiz_corps(name_key);
CREATE INDEX IF NOT EXISTS sunbiz_corps_zip ON sunbiz_corps(zip);
CREATE TABLE IF NOT EXISTS sunbiz_officers (
  doc_number TEXT, position INTEGER, title_code TEXT, title TEXT, name TEXT, kind TEXT,
  PRIMARY KEY (doc_number, position)
);
"""

# ── legal name normalisation ────────────────────────────────────────────────

CANON = {
    "ASSOC": "ASSOCIATION", "ASSN": "ASSOCIATION", "ASSO": "ASSOCIATION", "ASOC": "ASSOCIATION",
    "ASSOCIATIONS": "ASSOCIATION", "ASS": "ASSOCIATION",
    "CONDO": "CONDOMINIUM", "CONDOS": "CONDOMINIUM", "CONDOMINIUMS": "CONDOMINIUM", "COND": "CONDOMINIUM",
    "HOMEOWNER": "HOMEOWNERS", "HOMEOWNERS": "HOMEOWNERS", "HOA": "HOMEOWNERS ASSOCIATION",
    "APTS": "APARTMENTS", "APT": "APARTMENTS", "TWR": "TOWER", "TWRS": "TOWERS", "COOP": "COOPERATIVE",
    "CO-OP": "COOPERATIVE", "NUMBER": "NO", "NBR": "NO", "MSTR": "MASTER",
    "INTL": "INTERNATIONAL", "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
}
DROP = {"INC", "INCORPORATED", "CORP", "CORPORATION", "THE", "A", "AN"}
GENERIC = {"ASSOCIATION", "CONDOMINIUM", "OF", "AT", "AND", "HOMEOWNERS", "OWNERS", "COOPERATIVE", "NO"}
ASSOC_NAME = re.compile(r"\b(ASSOC|ASSN|ASS'N|ASSOCIATION|CONDO|CONDOMINIUM|HOMEOWNERS?|OWNERS|COOPERATIVE|CO-OP|HOA)\b")


def name_tokens(name: str | None) -> list[str]:
    s = (name or "").upper().replace("&", " AND ").replace("'", "")
    s = re.sub(r"[^A-Z0-9-]+", " ", s).replace(" - ", " ")
    out: list[str] = []
    for t in s.split():
        t = t.strip("-")
        if not t:
            continue
        t = CANON.get(t, t)
        out += [x for x in t.split() if x not in DROP]
    return out


def legal_key(name: str | None) -> str:
    """'OLYMPUS ASSOC., INC.' and 'Olympus Association, Inc.' → 'OLYMPUS ASSOCIATION'."""
    return " ".join(name_tokens(name))


def distinctive(name: str | None) -> set[str]:
    return {t for t in name_tokens(name) if t not in GENERIC}


def similarity(a: str | None, b: str | None) -> float:
    """Jaccard of the distinctive words (generic association / condominium words ignored)."""
    da, db = distinctive(a), distinctive(b)
    if not da or not db:
        return 0.0
    return len(da & db) / len(da | db)


# ── officer titles ──────────────────────────────────────────────────────────

TITLES = {
    "P": "President", "PRES": "President", "PRESIDENT": "President", "V": "Vice President", "VP": "Vice President",
    "VPRE": "Vice President", "T": "Treasurer", "TR": "Treasurer", "TRES": "Treasurer", "TREA": "Treasurer",
    "S": "Secretary", "SEC": "Secretary", "SECR": "Secretary", "D": "Director", "DIR": "Director",
    "C": "Chairman", "CHAI": "Chairman", "CHAIR": "Chairman", "MGR": "Manager", "MGRM": "Manager",
    "MANA": "Manager", "PM": "Property Manager", "PMGR": "Property Manager", "CEO": "CEO",
}
RANK = ("President", "Vice President", "Treasurer", "Secretary", "Chairman", "Director", "Property Manager", "Manager")


def officer_title(code: str | None) -> str | None:
    c = re.sub(r"[^A-Z]", "", (code or "").upper())
    if not c:
        return None
    if c in TITLES:
        return TITLES[c]
    if set(c) <= set("PTCVSD"):            # combined codes: PD, VPD, STD …
        parts, i = [], 0
        while i < len(c):
            if c[i:i + 2] == "VP":
                parts.append("Vice President")
                i += 2
            else:
                parts.append(TITLES[c[i]])
                i += 1
        return " / ".join(dict.fromkeys(parts))
    return code.strip()


def person_name(raw: str | None) -> str | None:
    """'SMITH, JOHN A' → 'John A Smith'; other orders are kept as filed (title-cased)."""
    from .dbpr import _title_case

    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return None
    if s.count(",") == 1:
        last, first = (x.strip() for x in s.split(","))
        if first and last:
            s = f"{first} {last}"
    return _title_case(s)


def _rank(title: str | None) -> int:
    for i, r in enumerate(RANK):
        if title and title.startswith(r):
            return i
    return len(RANK)


# ── parsing ─────────────────────────────────────────────────────────────────

def _cut(line: str, start: int, length: int) -> str:
    return line[start - 1:start - 1 + length].strip()


def parse_fixed(line: str) -> dict[str, Any] | None:
    line = line.rstrip("\r\n")
    if len(line) < OFFICER_START:
        return None
    line = line.ljust(RECORD_LEN)
    rec: dict[str, Any] = {k: _cut(line, *pos) for k, pos in FIELDS.items()}
    officers = []
    for n in range(OFFICERS):
        base = OFFICER_START + n * OFFICER_LEN
        o = {k: _cut(line, base + off, ln) for k, (off, ln) in OFFICER_FIELDS.items()}
        if o["name"]:
            officers.append({"position": n + 1, "title_code": o["title"], "kind": o["kind"] or None, "name": o["name"]})
    rec["officers"] = officers
    return rec if rec["doc_number"] and rec["name"] else None


CSV_ALIASES = {
    "doc_number": ("doc_number", "document_number", "corporation_number", "cor_number", "number", "document"),
    "name": ("name", "corporation_name", "entity_name", "cor_name", "legal_name"),
    "status": ("status", "cor_status"),
    "filing_type": ("filing_type", "cor_filing_type", "type"),
    "city": ("city", "principal_city", "cor_princ_city"),
    "state": ("state", "principal_state", "cor_princ_state"),
    "zip": ("zip", "principal_zip", "cor_princ_zip", "zip_code"),
    "file_date": ("file_date", "cor_file_date"),
    "last_tx": ("last_tx", "last_transaction_date"),
    "ra_name": ("ra_name", "registered_agent_name", "registered_agent"),
    "ra_type": ("ra_type", "registered_agent_type"),
}


def _key(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def parse_csv_row(row: dict[str, str]) -> dict[str, Any] | None:
    r = {_key(k): (v or "").strip() for k, v in row.items() if k}
    rec: dict[str, Any] = {f: next((r[a] for a in aliases if r.get(a)), "") for f, aliases in CSV_ALIASES.items()}
    officers = []
    for n in range(1, 21):
        name = r.get(f"officer{n}_name") or r.get(f"officer_{n}_name")
        if name:
            officers.append({"position": n, "title_code": r.get(f"officer{n}_title") or r.get(f"officer_{n}_title") or "",
                             "kind": r.get(f"officer{n}_type") or r.get(f"officer_{n}_type") or None, "name": name})
    rec["officers"] = officers
    return rec if rec["doc_number"] and rec["name"] else None


def is_association(rec: dict[str, Any]) -> bool:
    ft = (rec.get("filing_type") or "").upper()
    return ft.endswith("NP") or bool(ASSOC_NAME.search((rec.get("name") or "").upper()))


def _texts(path: Path) -> Iterator[tuple[str, Iterable[str]]]:
    """(member name, lines) for a .txt/.dat/.csv file or each such member of a .zip."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for m in z.namelist():
                if m.lower().endswith((".txt", ".dat", ".csv")):
                    with z.open(m) as fh:
                        yield f"{path.name}:{m}", io.TextIOWrapper(fh, encoding="latin-1", newline="")
    else:
        with open(path, encoding="latin-1", newline="") as fh:
            yield path.name, fh


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    for label, lines in _texts(path):
        it = iter(lines)
        first = next(it, "")
        if label.lower().endswith(".csv") or ("," in first and len(first) < OFFICER_START):
            for row in csv.DictReader([first, *it]):
                rec = parse_csv_row(row)
                if rec:
                    rec["source_file"] = label
                    yield rec
            continue
        for line in _chain(first, it):
            rec = parse_fixed(line)
            if rec:
                rec["source_file"] = label
                yield rec


def _chain(first: str, rest: Iterable[str]) -> Iterator[str]:
    if first:
        yield first
    yield from rest


# ── storage ─────────────────────────────────────────────────────────────────

def data_dir() -> Path:
    return settings.state_db.parent / "sunbiz"


def _db_path() -> Path:
    return settings.state_db.with_name("sunbiz.db")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or _db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


_CONNS: dict[str, sqlite3.Connection] = {}


def _shared() -> sqlite3.Connection:
    key = str(_db_path())
    if key not in _CONNS:
        _CONNS[key] = connect()
    return _CONNS[key]


def store(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO sunbiz_corps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rec["doc_number"], rec["name"], legal_key(rec["name"]), (rec.get("status") or "").upper()[:1] or None,
                  rec.get("filing_type") or None, (rec.get("city") or "").upper() or None,
                  (rec.get("zip") or "")[:5] or None, rec.get("file_date") or None, rec.get("last_tx") or None,
                  rec.get("ra_name") or None, rec.get("ra_type") or None, rec.get("source_file")))
    conn.execute("DELETE FROM sunbiz_officers WHERE doc_number = ?", (rec["doc_number"],))
    for o in rec.get("officers") or []:
        conn.execute("INSERT OR REPLACE INTO sunbiz_officers VALUES (?,?,?,?,?,?)",
                     (rec["doc_number"], o["position"], o.get("title_code") or None, officer_title(o.get("title_code")),
                      o["name"], o.get("kind")))


def sync(folder: Path | None = None, conn: sqlite3.Connection | None = None, everything: bool = False) -> dict[str, int]:
    """Index every file in data/sunbiz/ (oldest first, so daily updates overwrite the quarterly file)."""
    folder = Path(folder or data_dir())
    files = sorted((p for p in folder.glob("*") if p.suffix.lower() in (".txt", ".dat", ".csv", ".zip")),
                   key=lambda p: (p.stat().st_mtime, p.name)) if folder.exists() else []
    conn = conn or connect()
    stats = {"files": len(files), "read": 0, "kept": 0, "officers": 0}
    for path in files:
        for rec in read_records(path):
            stats["read"] += 1
            if not (everything or is_association(rec)):
                continue
            if (rec.get("state") or "FL").upper() not in ("FL", ""):
                continue
            store(conn, rec)
            stats["kept"] += 1
            stats["officers"] += len(rec.get("officers") or [])
            if stats["kept"] % 5000 == 0:
                conn.commit()
        logger.info("sunbiz-sync: {} leído ({} asociaciones acumuladas)", path.name, stats["kept"])
    conn.commit()
    return stats


def count(conn: sqlite3.Connection | None = None) -> int:
    try:
        conn = conn or connect()
        return conn.execute("SELECT COUNT(*) FROM sunbiz_corps").fetchone()[0]
    except sqlite3.Error:
        return 0


# ── matching ────────────────────────────────────────────────────────────────

def _better(row: sqlite3.Row, zip5: str | None, city: str | None) -> tuple:
    return (row["status"] != "A", not (zip5 and row["zip"] == zip5), not (city and row["city"] == city.upper()),
            -(int(row["file_date"]) if (row["file_date"] or "").isdigit() else 0))


def match(legal_name: str | None, zip5: str | None = None, city: str | None = None,
          conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Association record for a DBPR legal name. Exact normalised name first (Florida legal names are unique;
    ZIP / city / active status break ties); otherwise a close name inside the same ZIP or city."""
    key = legal_key(legal_name)
    if not key:
        return None
    if conn is None and not _db_path().exists():
        return None                       # no local index yet (sunbiz-sync never ran)
    try:
        conn = conn or _shared()
        rows = conn.execute("SELECT * FROM sunbiz_corps WHERE name_key = ?", (key,)).fetchall()
        how = "name"
        if not rows and (zip5 or city):
            cands = conn.execute("SELECT * FROM sunbiz_corps WHERE (zip = ? OR city = ?)",
                                 (zip5 or "", (city or "").upper())).fetchall()
            scored = sorted(((similarity(legal_name, c["name"]), c) for c in cands), key=lambda t: -t[0])
            if scored and scored[0][0] >= 0.8 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                rows, how = [scored[0][1]], f"name~{scored[0][0]:.2f}"
        if not rows:
            return None
        best = sorted(rows, key=lambda r: _better(r, zip5, city))[0]
        if how == "name" and zip5 and best["zip"] == zip5:
            how = "name+zip"
        officers = conn.execute("SELECT * FROM sunbiz_officers WHERE doc_number = ? ORDER BY position",
                                (best["doc_number"],)).fetchall()
    except sqlite3.Error:
        return None
    people = [{"title": o["title"] or o["title_code"], "name": person_name(o["name"]), "kind": o["kind"]}
              for o in officers if o["name"]]
    people.sort(key=lambda o: _rank(o["title"]))
    return {
        "doc_number": best["doc_number"], "name": best["name"], "status": best["status"],
        "active": best["status"] == "A", "filing_type": best["filing_type"], "matched_by": how,
        "officers": people,
        "registered_agent": {"name": person_name(best["ra_name"]) if best["ra_kind"] != "C" else best["ra_name"],
                             "kind": best["ra_kind"]} if best["ra_name"] else None,
    }


def board_title(title: str | None) -> str:
    if title and title.split(" /")[0] in ("President", "Vice President", "Treasurer", "Secretary", "Director",
                                          "Chairman"):
        return f"{title} of the Board (Sunbiz)"
    return f"{title or 'Officer'} (Sunbiz)"
