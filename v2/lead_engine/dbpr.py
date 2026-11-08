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
    r"(?i)^(?:THE\s+)?(?:(?:MANAGEMENT|MGMT\.?|MGT\.?|MANAGER'?S?|ADMINISTRATION|ADMIN\.?|ASSOCIATION|ASSN\.?|CONDO|BUILDING)\s+OFFICE|"
    r"OFFICE|LOBBY|FRONT DESK|SECURITY DESK|"
    r"(?:PROPERTY|GENERAL|COMMUNITY|BUILDING|SITE|RESIDENT|ASSOCIATION|ASSN\.?|CONDO|ON[- ]?SITE)?\s*MANAGER)\b(?![' ]?S\b)")
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


_KEEP_UPPER = {"NE", "NW", "SE", "SW", "LLC", "INC", "LLP", "PA", "II", "III", "IV", "USA", "HOA", "CAM", "LCAM", "FL"}


def _title_case(s: str) -> str:
    small = {"Of", "And", "The", "De", "Del", "La"}
    s = re.sub(r"\b([NS])\.\s?([EW])\.", r"\1\2", s, flags=re.I)        # N.E. -> NE
    out = " ".join(w.upper() if w.strip(".,").upper() in _KEEP_UPPER else w.capitalize() for w in s.split())
    words = out.split()
    return " ".join(w if i == 0 or w not in small else w.lower() for i, w in enumerate(words))


def parse_route(route: str | None) -> dict[str, Any]:
    """Split a DBPR care-of line into management company / person / title / type."""
    out: dict[str, Any] = {"management_company": None, "contact_name": None, "contact_title": None,
                           "management_type": None, "registered_agent": None}
    r = " ".join((route or "").split()).strip()
    prev = None
    while prev != r:                                        # "C/O ATTENTION MANAGEMENT OFFICE"
        prev = r
        r = re.sub(r"(?i)^(?:C/O|C\\O|CO|ATTN\.?:?|ATTENTION:?)\s+", "", r).strip(" ,")
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
-- Derived from managing_entity_route (rebuilt by index_routes after every load).
CREATE TABLE IF NOT EXISTS dbpr_routes (
  project_number TEXT PRIMARY KEY, city TEXT, route_type TEXT, company TEXT, company_key TEXT,
  person TEXT, person_key TEXT, person_title TEXT, same_address INTEGER, firm_id TEXT
);
CREATE INDEX IF NOT EXISTS dbpr_routes_company ON dbpr_routes(company_key);
CREATE INDEX IF NOT EXISTS dbpr_routes_person ON dbpr_routes(person_key);
CREATE INDEX IF NOT EXISTS dbpr_routes_city ON dbpr_routes(city);
"""


def _db_path() -> Path:
    return settings.state_db.with_name("dbpr.db")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema upgrades for databases created by earlier versions."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dbpr_routes)")}
    if "firm_id" not in cols:
        conn.execute("ALTER TABLE dbpr_routes ADD COLUMN firm_id TEXT")
        conn.execute("DELETE FROM dbpr_routes")       # rebuilt below with firm ids
    conn.execute("CREATE INDEX IF NOT EXISTS dbpr_routes_firm ON dbpr_routes(firm_id)")
    conn.commit()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or _db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    # Databases loaded before dbpr_routes existed get it built once.
    if conn.execute("SELECT EXISTS(SELECT 1 FROM dbpr_condos) AND NOT EXISTS(SELECT 1 FROM dbpr_routes)").fetchone()[0]:
        index_routes(conn)
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
    index_routes(conn)
    return n


def city_of(address: str | None) -> str | None:
    """'5555 COLLINS AVE, MIAMI BEACH, FL 33140-2559' -> 'MIAMI BEACH'."""
    parts = [p.strip() for p in (address or "").upper().split(",")]
    if len(parts) >= 3:
        return parts[-2] or None
    return None


def route_type(route: dict[str, Any], same_address: bool) -> str:
    """onsite | portfolio | self_managed | person | agent | unknown (for the DBPR seed filter)."""
    if route.get("management_type") == "onsite" or same_address:
        return "onsite"
    if route.get("management_company"):
        return "portfolio"
    if route.get("management_type") == "self_managed":
        return "self_managed"
    if route.get("registered_agent"):
        return "agent"
    if route.get("contact_name"):
        return "person"
    return "unknown"


def firm_identity(company: str | None, conn: sqlite3.Connection, cache: dict[str, str] | None = None) -> str | None:
    """One id per real firm: its CAB licence when the registry knows it, else the spacing-free name key
    ('FIRST SERVICE RESIDENTIAL' and 'FIRSTSERVICE RESIDENTIAL' -> same id)."""
    from .names import firm_key

    key = firm_key(company) if company else ""
    if not key:
        return None
    if cache is not None and key in cache:
        return cache[key]
    fid = "KEY:" + key.replace(" ", "")
    try:
        from . import cam

        lic = cam.verify_firm(company, conn)
        if lic:
            fid = "LIC:" + lic["license_id"]
    except sqlite3.Error:
        pass
    if cache is not None:
        cache[key] = fid
    return fid


def index_routes(conn: sqlite3.Connection) -> int:
    from .names import firm_key, person_key

    _migrate(conn)
    cache: dict[str, str] = {}
    conn.execute("DELETE FROM dbpr_routes")
    rows = conn.execute("SELECT project_number, address, address_key, managing_entity_route, managing_entity_address "
                        "FROM dbpr_condos").fetchall()
    for r in rows:
        route = parse_route(r["managing_entity_route"])
        me_key = address_key(r["managing_entity_address"])
        same = bool(me_key and r["address_key"] and me_key.split("|")[:2] == r["address_key"].split("|")[:2])
        company = route.get("management_company")
        person = route.get("contact_name")
        conn.execute("INSERT OR REPLACE INTO dbpr_routes VALUES (?,?,?,?,?,?,?,?,?,?)", (
            r["project_number"], city_of(r["address"]), route_type(route, same), company,
            firm_key(company) or None if company else None, person, person_key(person), route.get("contact_title"),
            int(same), firm_identity(company, conn, cache)))
    conn.commit()
    return len(rows)


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


# ── portfolio cluster ───────────────────────────────────────────────────────

def _coverage_counties() -> list[str]:
    return [c.upper() for c in settings.cluster_counties]


def cluster(*, company: str | None = None, person: str | None = None, exclude_project: str | None = None,
            near_zip: str | None = None, near_city: str | None = None, conn: sqlite3.Connection | None = None,
            max_siblings: int = 3) -> dict[str, Any] | None:
    """Other condos in the coverage counties managed by the same firm (or, failing that, the same person).

    Siblings are ordered by proximity proxy — same ZIP, then same city — and then by units.
    """
    from .names import person_key

    try:
        conn = conn or connect()
    except sqlite3.Error:
        return None
    counties = _coverage_counties()
    marks = ",".join("?" * len(counties))
    for by, key in (("company", firm_identity(company, conn) if company else None),
                    ("person", person_key(person) if person else None)):
        if not key:
            continue
        col = "firm_id" if by == "company" else "person_key"
        rows = conn.execute(
            f"SELECT c.project_number, c.condo_name, c.address, c.zip, c.units, r.city FROM dbpr_routes r "
            f"JOIN dbpr_condos c USING (project_number) WHERE r.{col} = ? AND UPPER(c.county) IN ({marks}) "
            f"AND c.primary_status = 'Approved'", (key, *counties)).fetchall()
        # One building per street address (several projects can share it).
        seen: dict[str, sqlite3.Row] = {}
        for r in rows:
            k = address_key(r["address"]) or r["project_number"]
            if k not in seen or (r["units"] or 0) > (seen[k]["units"] or 0):
                seen[k] = r
        own = None
        if exclude_project:
            own_row = conn.execute("SELECT address FROM dbpr_condos WHERE project_number = ?", (exclude_project,)).fetchone()
            own = address_key(own_row["address"]) if own_row else None
        others = [r for k, r in seen.items() if r["project_number"] != exclude_project and k != own]
        if not others:
            continue
        others.sort(key=lambda r: (r["zip"] != near_zip, (r["city"] or "") != (near_city or "").upper(), -(r["units"] or 0)))
        return {
            "by": by,
            "key": key,
            "count": len(others) + 1,
            "total_units": sum(r["units"] or 0 for r in others),
            "siblings": [{"name": _short_condo_name(r["condo_name"]), "address": r["address"], "units": r["units"],
                          "zip": r["zip"], "project_number": r["project_number"]} for r in others[:max_siblings]],
        }
    return None


_CONDO_SUFFIX = re.compile(r"(?i)[,\s]+(?:A\s+)?(?:CONDOMINIUM|CONDO)\.?(?:\s+(?:ASSOCIATION|ASSN)\.?)?(?:,?\s+INC\.?)?\s*$")


def _short_condo_name(name: str | None) -> str:
    """'OCEANIA II, A CONDO' -> 'Oceania II'; 'TIDES ON HOLLYWOOD BEACH CONDO, THE' -> 'The Tides On Hollywood Beach'."""
    n = (name or "").strip()
    the = bool(re.search(r"(?i)(?:,|\bCONDO(?:MINIUM)?)\s*THE\s*$", n))
    n = re.sub(r"(?i),?\s*THE\s*$", "", n)
    n = _CONDO_SUFFIX.sub("", n) or n
    n = re.sub(r"(?i)\s+(?:CONDO(?:MINIUM)?)\s*$", "", n).strip(" ,") or n
    return _title_case(("THE " if the else "") + n)
