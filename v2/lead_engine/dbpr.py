"""Florida DBPR official condominium registry: download, load and match by address.

Source: Division of Florida Condominiums, Timeshares and Mobile Homes, public
records extracts (https://www2.myfloridalicense.com/condos-timeshares-mobile-homes/public-records/).
Columns (verified 2026-11 on Condo_MD.csv):
  Project Number, File Number, Condo Name, County, Street City State Zip, Units,
  Recorded Date, Primary Status, Secondary Status, Managing Entity Number,
  Managing Entity Name, Managing Entity Route, Managing Entity Street,
  Managing Entity City, Managing Entity State, Managing Entity Zip

"Managing Entity Name" is the association; "Managing Entity Route" is the
care-of line and is where the management company / on-site office / board
officer usually appears ("C/O CASTLE MANAGEMENT LLC", "C/O MANAGEMENT OFFICE",
"C/O ISEL ZILKA / RITZ MGMT, INC", "C/O PETER BATTY, PRESIDENT").

Matching key = street number + normalised street name + ZIP5, so
"16375 Collins Ave, Sunny Isles Beach, FL 33160" (Google) matches
"16375 COLLINS AVE, MIAMI BEACH, FL 33160-4537" (DBPR). Falls back to name
similarity inside the same ZIP when the registry address has no number.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import requests
from loguru import logger

from .config import settings

BASE_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts/"
# County group code -> file (the site mixes case in names).
FILES = {
    "MD": "Condo_MD.csv",   # Dade + Monroe
    "PB": "condo_PB.csv",   # Broward + Palm Beach
    "NF": "Condo_NF.csv",
    "CE": "condo_CE.csv",
    "CW": "Condo_CW.csv",
}

# ── address normalisation (pure) ────────────────────────────────────────────

STREET_TYPES = {
    "AVENUE": "AVE", "AV": "AVE", "STREET": "ST", "ROAD": "RD", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "PLACE": "PL", "COURT": "CT", "LANE": "LN", "TERRACE": "TER", "TERR": "TER", "PARKWAY": "PKWY",
    "CIRCLE": "CIR", "HIGHWAY": "HWY", "TRAIL": "TRL", "CAUSEWAY": "CSWY", "PLAZA": "PLZ",
    "ISLAND": "IS", "POINT": "PT", "SQUARE": "SQ", "WAY": "WAY",
}
DIRECTIONS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
_UNIT = re.compile(r"\s(?:#|UNIT|UNITS|APT|STE|SUITE|PH|BLDG)\b.*$")
_ORDINAL = re.compile(r"\b(\d+)(?:ST|ND|RD|TH)\b")


def _norm_street(street: str) -> str:
    s = street.upper().replace(".", "").replace("&", " AND ")
    s = _UNIT.sub("", " " + s).strip()
    s = _ORDINAL.sub(r"\1", s)
    words = [DIRECTIONS.get(w, STREET_TYPES.get(w, w)) for w in re.split(r"[\s,]+", s) if w]
    return " ".join(words)


def address_key(address: str | None) -> str | None:
    """'6000 Collins Avenue, Miami Beach, FL 33140' -> '6000|COLLINS AVE|33140' (None if no number/zip)."""
    if not address:
        return None
    a = address.upper().strip()
    zm = re.search(r"\b(\d{5})(?:-\d{4})?\b(?!.*\b\d{5}\b)", a)
    first = a.split(",")[0]
    nm = re.match(r"\s*(\d{1,6})[A-Z]?(?:\s*-\s*\d+)?\s+(.+)$", first)
    if not zm or not nm:
        return None
    return f"{nm.group(1)}|{_norm_street(nm.group(2))}|{zm.group(1)}"


def zip5(address: str | None) -> str | None:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b(?!.*\b\d{5}\b)", (address or "").upper())
    return m.group(1) if m else None


_NAME_STOP = {
    "A", "THE", "OF", "AT", "CONDO", "CONDOMINIUM", "CONDOMINIUMS", "ASSN", "ASSOCIATION", "INC", "NO",
    "HOA", "HOMEOWNERS", "APARTMENTS", "APTS", "RESIDENCES", "LLC", "AND",
}


def name_tokens(name: str | None) -> set[str]:
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return {w for w in words if w not in _NAME_STOP and len(w) > 1}


def name_similarity(a: str | None, b: str | None) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── managing-entity route parsing (pure) ────────────────────────────────────

_COMPANY_WORDS = re.compile(
    r"(?i)\b(MANAGEMENT|MGMT|MGT|PROPERTY|PROPERTIES|SERVICES|SERVICE|RESIDENTIAL|GROUP|REALTY|LLC|INC|CORP|"
    r"ASSOCIATES|SOLUTIONS|CONSULTANTS|PARTNERS|ADVISORS|COMPANY|CO)\b\.?")
_ONSITE = re.compile(
    r"(?i)^(?:THE\s+)?(?:(?:MANAGEMENT|MGMT|MANAGER'?S?|ADMINISTRATION|ADMIN|ASSOCIATION|ASSN|CONDO|BUILDING)\s+OFFICE|"
    r"OFFICE|LOBBY|FRONT DESK|SECURITY DESK)\b")
_BOARD = re.compile(r"(?i)^BOARD(?:\s+OF\s+DIRECTORS)?\b")
# Single-word firms that carry no company word in the name.
KNOWN_SHORT_FIRMS = {"AKAM": "AKAM", "REALMANAGE": "RealManage", "CIRACONNECT": "CiraConnect", "ASSOCIA": "Associa",
                     "FIRSTSERVICE": "FirstService Residential", "CASTLE": "Castle Group"}
_OFFICER = re.compile(
    r"(?i)[,\-–]\s*(?:ASSN\.?\s+|ASSOCIATION\s+|BOARD\s+)?(PRESIDENT|PRES|VICE PRESIDENT|VP|TREASURER|SECRETARY|SEC|DIRECTOR|MANAGER|"
    r"PROPERTY MANAGER|LCAM|CAM)\b\.?$")
_AGENT = re.compile(r"(?i)\b((?:REGISTERED|REGD|REG|RGD)\.?\s+AG(?:EN)?T|ATTORNEY|ESQ|P\.?A\.?|LAW (?:FIRM|OFFICE|GROUP)S?)\b")
_AGENT_SUFFIX = re.compile(r"(?i)[, ]*(?:REGISTERED|REGD|REG|RGD)\.?\s+AG(?:EN)?T\.?")
OFFICER_TITLES = {
    "PRESIDENT": "Board President", "PRES": "Board President", "VICE PRESIDENT": "Board Vice President",
    "VP": "Board Vice President", "TREASURER": "Board Treasurer", "SECRETARY": "Board Secretary", "SEC": "Board Secretary",
    "DIRECTOR": "Board Director", "MANAGER": "Manager", "PROPERTY MANAGER": "Property Manager", "LCAM": "LCAM", "CAM": "LCAM",
}


def _title_case(s: str) -> str:
    small = {"Of", "And", "The", "De", "Del", "La"}
    out = " ".join(w.capitalize() if not re.fullmatch(r"(?:LLC|INC|LLP|PA|II|III|IV|USA|HOA|CAM|LCAM)", w.strip(".,"))
                   else w for w in s.split())
    return " ".join(w if w not in small else w.lower() for w in out.split())


def parse_route(route: str | None) -> dict[str, Any]:
    """Split a DBPR care-of line into management company / person / title / type."""
    out: dict[str, Any] = {"management_company": None, "contact_name": None, "contact_title": None,
                           "management_type": None, "registered_agent": None}
    r = " ".join((route or "").split()).strip()
    r = re.sub(r"(?i)^(?:C/O|C\\O|CO|ATTN:?|ATTENTION:?)\s+", "", r).strip(" ,")
    r = re.sub(r"(?i)\s+C/O\s+", " / ", r)               # "SOBE MANAGEMENT C/O PHILIPPE LAMERY"
    if not r:
        return out
    if _ONSITE.match(r):
        out["management_type"] = "onsite"
        tail = r.split(",", 1)[1].strip() if "," in r else ""
        if 2 <= len(tail.split()) <= 4 and not any(ch.isdigit() for ch in tail):
            out["contact_name"] = _title_case(tail)
            out["contact_title"] = "Oficina de administración (DBPR)"
        return out
    if _BOARD.match(r):
        out["management_type"] = "self_managed"
        return out
    if _AGENT.search(r):
        out["registered_agent"] = _title_case(_AGENT_SUFFIX.sub("", r).strip(" ,"))
        return out
    if r.upper() in KNOWN_SHORT_FIRMS:
        out["management_company"] = KNOWN_SHORT_FIRMS[r.upper()]
        out["management_type"] = "third_party"
        return out
    officer = _OFFICER.search(r)
    if officer:
        name = r[: officer.start()].strip(" ,-–")
        out["contact_name"] = _title_case(name)
        out["contact_title"] = OFFICER_TITLES.get(officer.group(1).upper(), officer.group(1).title())
        out["management_type"] = "self_managed" if out["contact_title"].startswith("Board") else None
        return out
    parts = [p.strip(" ,") for p in re.split(r"\s+/\s+|\s*/\s*", r) if p.strip(" ,")]
    # "PREMIER ASSN SERVICES, ORLY IGLESIAS": company, then a trailing person after a comma.
    expanded: list[str] = []
    for p in parts:
        segs = [x.strip() for x in p.split(",")]
        tail = segs[-1] if len(segs) > 1 else ""
        if (tail and _COMPANY_WORDS.search(",".join(segs[:-1])) and not _COMPANY_WORDS.search(tail)
                and 2 <= len(tail.split()) <= 4 and not any(ch.isdigit() for ch in tail)):
            expanded += [", ".join(segs[:-1]), tail]
        else:
            expanded.append(p)
    parts = expanded
    for p in parts:
        if _COMPANY_WORDS.search(p) and not out["management_company"]:
            out["management_company"] = _title_case(p)
        elif not out["contact_name"] and len(p.split()) >= 2 and not any(ch.isdigit() for ch in p):
            out["contact_name"] = _title_case(p)
    if out["management_company"]:
        out["management_type"] = "third_party"
    return out


# ── storage ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS dbpr_condos (
  project_number TEXT PRIMARY KEY, condo_name TEXT, county TEXT, address TEXT, address_key TEXT, zip TEXT,
  units INTEGER, primary_status TEXT, secondary_status TEXT, managing_entity_number TEXT,
  managing_entity_name TEXT, managing_entity_route TEXT, managing_entity_address TEXT
);
CREATE INDEX IF NOT EXISTS dbpr_condos_key ON dbpr_condos(address_key);
CREATE INDEX IF NOT EXISTS dbpr_condos_zip ON dbpr_condos(zip);
"""


def _db_path() -> Path:
    return settings.state_db.with_name("dbpr.db")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or _db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, str]]) -> int:
    n = 0
    for row in rows:
        r = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        pn = r.get("Project Number")
        if not pn:
            continue
        addr = r.get("Street City State Zip", "")
        units = r.get("Units", "")
        me_addr = ", ".join(x for x in (r.get("Managing Entity Street"), r.get("Managing Entity City"),
                                        f"{r.get('Managing Entity State', '')} {r.get('Managing Entity Zip', '')}".strip()) if x)
        conn.execute(
            "INSERT OR REPLACE INTO dbpr_condos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pn, r.get("Condo Name"), r.get("County"), addr, address_key(addr), zip5(addr),
             int(units) if units.isdigit() else None, r.get("Primary Status"), r.get("Secondary Status"),
             r.get("Managing Entity Number"), r.get("Managing Entity Name"), r.get("Managing Entity Route"), me_addr),
        )
        n += 1
    conn.commit()
    return n


def load_csv(path: str | Path, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    with open(path, newline="", encoding="latin-1") as fh:
        return load_rows(conn, csv.DictReader(fh))


def sync(groups: Iterable[str] | None = None, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Download the official extracts (default DBPR_GROUPS=MD,PB) and load them."""
    conn = conn or connect()
    counts: dict[str, int] = {}
    for g in groups or settings.dbpr_groups:
        name = FILES.get(g.upper())
        if not name:
            logger.warning("Grupo DBPR desconocido: {}", g)
            continue
        r = requests.get(BASE_URL + name, timeout=120, headers={"User-Agent": settings.user_agent})
        r.raise_for_status()
        counts[g] = load_rows(conn, csv.DictReader(io.StringIO(r.content.decode("latin-1"))))
        logger.info("DBPR {}: {} condominios", name, counts[g])
    return counts


# ── matching ────────────────────────────────────────────────────────────────

def _pick(rows: list[sqlite3.Row]) -> sqlite3.Row:
    # Prefer active rows that say who manages them, then the biggest project.
    return sorted(rows, key=lambda r: (
        r["primary_status"] != "Approved", not (r["managing_entity_route"] or "").strip(), -(r["units"] or 0)))[0]


def match(address: str | None, name: str | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    try:
        conn = conn or connect()
    except sqlite3.Error:
        return None
    key = address_key(address)
    rows: list[sqlite3.Row] = []
    how = None
    if key:
        rows = conn.execute("SELECT * FROM dbpr_condos WHERE address_key = ?", (key,)).fetchall()
        how = "address" if rows else None
    if not rows and name and (z := zip5(address)):
        cands = conn.execute("SELECT * FROM dbpr_condos WHERE zip = ?", (z,)).fetchall()
        scored = sorted(((name_similarity(name, c["condo_name"]), c) for c in cands), key=lambda t: -t[0])
        if scored and scored[0][0] >= 0.6:
            rows, how = [scored[0][1]], f"name~{scored[0][0]:.2f}"
    if not rows:
        return None
    best = _pick(rows)
    total_units = sum(r["units"] or 0 for r in rows) or None
    route = parse_route(best["managing_entity_route"])
    return {
        "project_number": best["project_number"],
        "condo_name": best["condo_name"],
        "association": _title_case(best["managing_entity_name"] or "") or None,
        "units": total_units,
        "projects_at_address": len(rows),
        "status": f"{best['primary_status']}/{best['secondary_status']}",
        "route_raw": best["managing_entity_route"],
        "managing_entity_address": best["managing_entity_address"],
        "matched_by": how,
        **route,
    }
