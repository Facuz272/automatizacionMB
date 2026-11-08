"""Florida DBPR Community Association Manager licences (individuals and firms).

Source: https://www2.myfloridalicense.com/community-association-managers-and-firms/public-records/
File lic38cam.csv (no header). Layout verified 2026-11 on the live file (28,907 rows):

  0 board ("38")          1 occupation: CAM (individual), CAB (firm), CRSE/PROV/PRE (education)
  2 licensee name ("LAST, FIRST MIDDLE" for people)       3 DBA name
  5-7 address lines       8 city   9 state   10 zip   11 county code (23 = Miami-Dade, 16 = Broward, 60 = Palm Beach)
  12 licence number       13 primary status (C = current)  14 secondary status (A = active, I = inactive)
  15 original licensure date   16 status effective date   17 expiration date (MM/DD/YYYY)
  20 licence id ("CAM12345" / "CAB678")

``verify_person`` / ``verify_firm`` say whether a name from the condo registry (or the
website) holds an active licence and return the normalised licensee.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import requests
from loguru import logger

from .config import settings
from .dbpr import BASE_URL, _title_case, connect
from .names import compact_key, firm_key, firm_match_score, person_key, split_person

FILE = "lic38cam.csv"
COUNTY = {"23": "Miami-Dade", "16": "Broward", "60": "Palm Beach", "44": "Monroe"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cam_licenses (
  license_id TEXT PRIMARY KEY, kind TEXT, name TEXT, dba TEXT, name_key TEXT, dba_key TEXT,
  city TEXT, zip TEXT, county_code TEXT, primary_status TEXT, secondary_status TEXT, expires TEXT, active INTEGER
);
CREATE INDEX IF NOT EXISTS cam_licenses_key ON cam_licenses(kind, name_key);
CREATE INDEX IF NOT EXISTS cam_licenses_dba ON cam_licenses(kind, dba_key);
"""


def _iso(d: str) -> str | None:
    try:
        return datetime.strptime(d.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def is_active(primary: str, secondary: str, expires_iso: str | None, today: date | None = None) -> bool:
    if primary != "C" or secondary == "I":
        return False
    return bool(expires_iso) and expires_iso >= (today or date.today()).isoformat()


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def load_rows(conn: sqlite3.Connection, rows: Iterable[list[str]]) -> int:
    ensure(conn)
    n = 0
    for r in rows:
        if len(r) < 21 or r[1] not in ("CAM", "CAB"):
            continue
        kind, name, dba = r[1], r[2].strip(), r[3].strip()
        key = person_key(name) if kind == "CAM" else firm_key(name)
        expires = _iso(r[17])
        conn.execute("INSERT OR REPLACE INTO cam_licenses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            r[20].strip() or f"{kind}{r[12].lstrip('0')}", kind, name, dba or None, key,
            firm_key(dba) if dba else None, r[8].strip(), r[10].strip()[:5], r[11].strip(),
            r[13].strip(), r[14].strip(), expires, int(is_active(r[13].strip(), r[14].strip(), expires))))
        n += 1
    conn.commit()
    return n


def load_csv(path: str | Path, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    with open(path, newline="", encoding="latin-1") as fh:
        return load_rows(conn, csv.reader(fh))


def sync(conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    r = requests.get(BASE_URL + FILE, timeout=180, headers={"User-Agent": settings.user_agent})
    r.raise_for_status()
    n = load_rows(conn, csv.reader(io.StringIO(r.content.decode("latin-1"))))
    logger.info("DBPR {}: {} licencias CAM/CAB", FILE, n)
    return n


def load_and_reindex(loader, conn: sqlite3.Connection | None = None) -> int:
    """Load licences, then rebuild the condo route index so clusters group by licensed firm."""
    from .dbpr import index_routes

    conn = conn or connect()
    n = loader(conn)
    _FIRM_CACHE.pop(id(conn), None)
    index_routes(conn)
    return n


def _display_person(name: str) -> str:
    p = split_person(name)
    if not p:
        return _title_case(name)
    first_rest = name.split(",", 1)[1].strip() if "," in name else name
    return _title_case(f"{first_rest} {p[0]}")


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"license_id": r["license_id"], "kind": r["kind"], "active": bool(r["active"]), "expires": r["expires"],
            "county": COUNTY.get(r["county_code"], r["county_code"]), "city": _title_case(r["city"] or ""),
            "name": _display_person(r["name"]) if r["kind"] == "CAM" else _title_case(r["dba"] or r["name"]),
            "legal_name": r["name"]}


def _tables_ready(conn: sqlite3.Connection) -> bool:
    ensure(conn)
    return conn.execute("SELECT EXISTS(SELECT 1 FROM cam_licenses)").fetchone()[0] == 1


def verify_person(name: str | None, conn: sqlite3.Connection | None = None,
                  prefer_counties: tuple[str, ...] = ("23", "16", "60")) -> dict[str, Any] | None:
    """Licensed CAM with the same first + last name. Ambiguous when several active people share it."""
    key = person_key(name)
    if not key:
        return None
    conn = conn or connect()
    if not _tables_ready(conn):
        return None
    rows = conn.execute("SELECT * FROM cam_licenses WHERE kind = 'CAM' AND name_key = ?", (key,)).fetchall()
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (not r["active"], r["county_code"] not in prefer_counties, r["license_id"]))
    out = _row(rows[0])
    actives = [r for r in rows if r["active"]]
    out["homonyms"] = len(actives) - 1 if actives else len(rows) - 1
    return out


_FIRM_CACHE: dict[int, list[tuple[str, str, sqlite3.Row]]] = {}


def _firm_candidates(conn: sqlite3.Connection) -> list[tuple[str, str, sqlite3.Row]]:
    """(key, compact key, row) for every CAB legal name and DBA, built once per connection."""
    if id(conn) not in _FIRM_CACHE:
        out = []
        for r in conn.execute("SELECT * FROM cam_licenses WHERE kind = 'CAB'").fetchall():
            for raw in {r["name"], r["dba"]} - {None, ""}:
                out.append((firm_key(raw), compact_key(raw), r))
        _FIRM_CACHE[id(conn)] = out
    return _FIRM_CACHE[id(conn)]


def verify_firm(name: str | None, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Licensed CAM firm (CAB): exact normalised legal name/DBA, else same distinctive words, else a
    near-identical spelling of the distinctive words (catches 'Carribbean', 'Seacsrest', 'Mangement')."""
    key = firm_key(name)
    if not key:
        return None
    conn = conn or connect()
    if not _tables_ready(conn):
        return None
    rows = conn.execute("SELECT * FROM cam_licenses WHERE kind = 'CAB' AND (name_key = ? OR dba_key = ?)",
                        (key, key)).fetchall()
    how = "exact"
    if not rows:
        best: tuple[float, str, sqlite3.Row] | None = None
        weak_hits: set[str] = set()
        compact = compact_key(name)
        for k, ck, r in _firm_candidates(conn):
            if ck == compact:
                best = (1.0, "compact~1.00", r)
                break
            m = firm_match_score(key, k)
            if not m:
                continue
            if m[1] == "same_word":
                weak_hits.add(r["license_id"])
            if best is None or m[0] > best[0] or (m[0] == best[0] and r["active"] and not best[2]["active"]):
                best = (m[0], f"{m[1]}~{m[0]:.2f}", r)
        # A single shared common word only counts when it points to exactly one licensed firm.
        if not best or (best[1].startswith("same_word") and len(weak_hits) != 1):
            return None
        rows, how = [best[2]], best[1]
    rows = sorted(rows, key=lambda r: (not r["active"], r["county_code"] not in ("23", "16", "60")))
    out = _row(rows[0])
    out["matched_by"] = how
    return out
