"""Florida DBPR Division of Hotels & Restaurants — public lodging licences.

Files (verified live 2026-11-08, header row included):
  https://www2.myfloridalicense.com/sto/file_download/extracts/hrlodge1.csv  district 1: Miami-Dade + Monroe (25,303 rows)
  https://www2.myfloridalicense.com/sto/file_download/extracts/hrlodge2.csv  district 2: Broward + Palm Beach (18,795 rows)
Columns used: License Type Code, Rank Code (HOTL hotel, MOTL motel, NAPT non-transient apartments,
TAPT transient apartments, CNDO condo rental group, DWEL dwelling, BNB), Licensee Name, Business Name,
Mailing Name, Location Street Address / City / Zip, Primary Phone Number, Number of Seats or Rental Units,
Primary Status Code, License Number.

Whole-property operators (HOTL/MOTL/NAPT/TAPT) give rooms, operator and a phone for the property.
CNDO licences belong to rental groups inside a condo: they are counted, never used as the building's phone.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import requests
from loguru import logger

from .config import settings
from .dbpr import BASE_URL, _title_case, address_key, connect
from .normalize import to_e164

FILES = {"MD": "hrlodge1.csv", "PB": "hrlodge2.csv"}
OPERATOR_RANKS = {"HOTL": "hotel", "MOTL": "motel", "NAPT": "edificio de apartamentos", "TAPT": "apart-hotel"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS dbpr_lodging (
  license_number TEXT PRIMARY KEY, rank TEXT, licensee TEXT, business_name TEXT, mailing_name TEXT,
  address TEXT, address_key TEXT, city TEXT, zip TEXT, county TEXT, phone TEXT, units INTEGER, status TEXT
);
CREATE INDEX IF NOT EXISTS dbpr_lodging_key ON dbpr_lodging(address_key);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def load_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, str]]) -> int:
    ensure(conn)
    n = 0
    for raw in rows:
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        lic = r.get("License Number")
        if not lic:
            continue
        street = r.get("Location Street Address", "")
        zipc = r.get("Location Zip Code", "")[:5]
        addr = f"{street}, {r.get('Location City', '')}, FL {zipc}"
        units = r.get("Number of Seats or Rental Units", "")
        conn.execute("INSERT OR REPLACE INTO dbpr_lodging VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            lic, r.get("Rank Code"), r.get("Licensee Name"), r.get("Business Name"), r.get("Mailing Name") or None,
            addr, address_key(addr), r.get("Location City"), zipc, r.get("Location County"),
            to_e164(r.get("Primary Phone Number")), int(units) if units.isdigit() else None, r.get("Primary Status Code")))
        n += 1
    conn.commit()
    return n


def load_csv(path: str | Path, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    with open(path, newline="", encoding="latin-1") as fh:
        return load_rows(conn, csv.DictReader(fh))


def sync(groups: Iterable[str] | None = None, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    conn = conn or connect()
    out: dict[str, int] = {}
    for g in groups or settings.dbpr_groups:
        name = FILES.get(g.upper())
        if not name:
            continue
        r = requests.get(BASE_URL + name, timeout=180, headers={"User-Agent": settings.user_agent})
        r.raise_for_status()
        out[g] = load_rows(conn, csv.DictReader(io.StringIO(r.content.decode("latin-1"))))
        logger.info("DBPR {}: {} licencias de alojamiento", name, out[g])
    return out


def match(address: str | None, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Lodging licences at this street address: the operator (if any) and how many condo rental groups."""
    key = address_key(address)
    if not key:
        return None
    try:
        conn = conn or connect()
        ensure(conn)
        rows = conn.execute("SELECT * FROM dbpr_lodging WHERE address_key = ? AND status = '20'", (key,)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    ops = sorted((r for r in rows if r["rank"] in OPERATOR_RANKS), key=lambda r: -(r["units"] or 0))
    out: dict[str, Any] = {"condo_rental_licences": sum(1 for r in rows if r["rank"] == "CNDO")}
    if ops:
        o = ops[0]
        out.update({
            "kind": OPERATOR_RANKS[o["rank"]], "rank": o["rank"], "license_number": o["license_number"],
            "business_name": _title_case(o["business_name"] or ""), "licensee": _title_case(o["licensee"] or ""),
            "manager": _title_case(o["mailing_name"]) if o["mailing_name"] else None,
            "phone": o["phone"], "rooms": o["units"],
        })
    return out


def building_phone(address: str | None, names: list[str | None], conn: sqlite3.Connection | None = None,
                   min_similarity: float = 0.6) -> dict[str, Any] | None:
    """A phone that belongs to the building itself, from the lodging registry:
      * a whole-property operator licence (hotel/motel/apartments) at the address → front desk;
      * a condo rental licence (CNDO) held by the association itself (licensee ≈ association or condo name)
        → association office. CNDO licences held by owners or rental agents are never used."""
    from .dbpr import name_similarity

    key = address_key(address)
    if not key:
        return None
    try:
        conn = conn or connect()
        ensure(conn)
        rows = conn.execute("SELECT * FROM dbpr_lodging WHERE address_key = ? AND status = '20' AND phone IS NOT NULL",
                            (key,)).fetchall()
    except sqlite3.Error:
        return None
    ops = sorted((r for r in rows if r["rank"] in OPERATOR_RANKS), key=lambda r: -(r["units"] or 0))
    if ops:
        o = ops[0]
        return {"phone": o["phone"], "label": "front_desk", "license_number": o["license_number"],
                "licensee": _title_case(o["licensee"] or ""), "why": f"licencia {o['rank']} del edificio"}
    wanted = [n for n in names if n]
    for r in rows:
        if r["rank"] != "CNDO":
            continue
        sim = max((name_similarity(n, r["licensee"]) for n in wanted), default=0.0)
        sim = max(sim, max((name_similarity(n, r["business_name"]) for n in wanted), default=0.0))
        if sim >= min_similarity:
            return {"phone": r["phone"], "label": "hoa", "license_number": r["license_number"],
                    "licensee": _title_case(r["licensee"] or ""), "why": f"licencia CNDO a nombre de la asociación (~{sim:.2f})"}
    return None
