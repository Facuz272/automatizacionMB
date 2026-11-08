"""DBPR seed collector: candidates straight from the official condo registry.

No Google Places needed to *discover* buildings. Filters:
  * target area: TARGET_CITIES / TARGET_CITIES_<STRATEGY> (DBPR postal city or ZIP aliases,
    e.g. "Brickell Miami" -> 33129/33130/33131, "Sunny Isles Beach" -> 33160)
  * MIN_UNITS (default 50); buildings with PRIORITY_UNITS+ (default 80) come first
  * management type from the care-of line:
      onsite     "C/O MANAGEMENT OFFICE" or the care-of address is the building itself
      portfolio  an external management company
  * one candidate per street address (projects sharing a tower are summed)

Each candidate carries the DBPR record, so the pipeline only has to find a phone:
Places by name + address when GOOGLE_PLACES_API_KEY exists (one cheap lookup per
building instead of sweeping zones), else OpenStreetMap tags / web search / website.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterator

from ..config import cities_for, settings
from ..dbpr import _short_condo_name, _title_case, address_key, connect, match
from ..models import Lead

# Neighbourhood / municipality names that DBPR files under another postal city.
CITY_ZIPS: dict[str, set[str]] = {
    "BRICKELL": {"33129", "33130", "33131"},
    "BRICKELL MIAMI": {"33129", "33130", "33131"},
    "DOWNTOWN MIAMI": {"33128", "33130", "33131", "33132"},
    "EDGEWATER": {"33137"},
    "SUNNY ISLES BEACH": {"33160"},
    "SUNNY ISLES": {"33160"},
    "BAL HARBOUR": {"33154"},
    "BAY HARBOR ISLANDS": {"33154"},
    "SURFSIDE": {"33154"},
    "KEY BISCAYNE": {"33149"},
    "AVENTURA": {"33180"},
    "NORTH MIAMI BEACH": {"33160", "33162", "33179", "33181"},
    "MIAMI BEACH": {"33109", "33139", "33140", "33141"},
    "HALLANDALE BEACH": {"33009"},
    "HOLLYWOOD": {"33019", "33020", "33021", "33023", "33024"},
    "HOLLYWOOD FL": {"33019", "33020", "33021", "33023", "33024"},
    "FORT LAUDERDALE": {"33301", "33304", "33305", "33306", "33308", "33316"},
    "DORAL": {"33122", "33126", "33166", "33172", "33178"},
    "CORAL GABLES": {"33133", "33134", "33143", "33146", "33156"},
}


# Postal-city spellings a ZIP alias may match: the umbrella postal cities and the alias neighbourhoods
# themselves. A record filed under another municipality (Davie or Pembroke Pines inside a Hollywood ZIP,
# North Miami inside 33181, Sweetwater inside a Doral ZIP) only counts when that municipality is
# configured by name.
ZIP_POSTAL_OK = {
    "MIAMI", "MIAMI BEACH", "NORTH MIAMI BEACH", "HOLLYWOOD", "FORT LAUDERDALE", "FT LAUDERDALE", "FT. LAUDERDALE",
    "HALLANDALE", "HALLANDALE BEACH", "SUNNY ISLES", "SUNNY ISLES BEACH", "BAY HARBOR ISLANDS", "BAY HARBOR ISLAND",
    "TOWN OF BAY HARBOR", "BAL HARBOUR", "SURFSIDE", "KEY BISCAYNE", "FISHER ISLAND", "CORAL GABLES", "DORAL",
    "AVENTURA", "COCONUT GROVE", "NORTH BAY VILLAGE", "BRICKELL", "EDGEWATER",
}


def in_area(postal_city: str | None, zip_code: str | None, area: tuple[set[str], set[str]]) -> bool:
    names, zips = area
    city = _norm_city(postal_city or "")
    if city in names:
        return True
    return (zip_code or "")[:5] in zips and (not city or city in ZIP_POSTAL_OK)


def _norm_city(c: str) -> str:
    c = re.sub(r"\s+(?:FL|FLORIDA)$", "", c.upper().strip())
    return re.sub(r"\s+", " ", c)


def area_filter(cities: list[str]) -> tuple[set[str], set[str]]:
    """(DBPR postal cities, ZIPs) that cover the target names."""
    names, zips = set(), set()
    for c in cities:
        n = _norm_city(c)
        names.add(n)
        zips |= CITY_ZIPS.get(n, set())
    return names, zips


def classify(route_type: str) -> str | None:
    return {"onsite": "onsite", "portfolio": "portfolio"}.get(route_type)


def _clean_address(addr: str) -> str:
    a = re.sub(r"-\d{4}\b", "", addr.strip())             # ZIP+4 -> ZIP
    parts = [p.strip() for p in a.split(",")]
    if len(parts) >= 3:
        state_zip = parts[-1]
        return ", ".join([_title_case(p) for p in parts[:-1]] + [state_zip])
    return _title_case(a)


def candidates(strategy: str, conn: sqlite3.Connection | None = None, cities: list[str] | None = None,
               min_units: int | None = None) -> list[dict]:
    """Ranked DBPR rows for the strategy (dicts with condo + route fields)."""
    conn = conn or connect()
    area = area_filter(cities or cities_for(strategy))
    min_units = settings.min_units if min_units is None else min_units
    rows = conn.execute(
        "SELECT c.*, r.route_type, r.company, r.person, r.person_title, r.city AS postal_city "
        "FROM dbpr_condos c JOIN dbpr_routes r USING (project_number) "
        "WHERE c.primary_status = 'Approved' AND c.address_key IS NOT NULL").fetchall()
    by_addr: dict[str, dict] = {}
    for r in rows:
        if not in_area(r["postal_city"], r["zip"], area):
            continue
        kind = classify(r["route_type"])
        if strategy in ("onsite", "portfolio") and kind != strategy:
            continue
        key = r["address_key"]
        d = dict(r)
        if key in by_addr:
            prev = by_addr[key]
            total = (prev["units_total"] or 0) + (r["units"] or 0)
            # Same rule as dbpr.match: the project with a care-of line represents the tower.
            if (r["managing_entity_route"] or "").strip() and not (prev["managing_entity_route"] or "").strip():
                d["units_total"] = total
                by_addr[key] = d
            else:
                prev["units_total"] = total
            continue
        d["units_total"] = r["units"] or 0
        by_addr[key] = d
    out = [d for d in by_addr.values() if d["units_total"] >= min_units]
    pri = settings.priority_units
    out.sort(key=lambda d: (d["units_total"] < pri, -d["units_total"], d["condo_name"] or ""))
    return out


def collect(strategy: str, max_candidates: int, conn: sqlite3.Connection | None = None,
            cities: list[str] | None = None) -> Iterator[Lead]:
    conn = conn or connect()
    # No slicing here: the pipeline skips buildings worked on in earlier runs and stops at max_candidates.
    for d in candidates(strategy, conn, cities):
        address = _clean_address(d["address"])
        lead = Lead(
            source="dbpr",
            source_id=f"dbpr:{d['project_number']}",
            strategy_kind=strategy if strategy in ("onsite", "portfolio", "pain") else "onsite",  # type: ignore[arg-type]
            company_name=_short_condo_name(d["condo_name"]),
            address=address,
            evidence={"seed": "dbpr", "address_key": address_key(d["address"])},
        )
        rec = match(d["address"], d["condo_name"], conn)
        if rec:
            lead.public_records["dbpr"] = {k: rec[k] for k in (
                "project_number", "condo_name", "association", "units", "projects_at_address", "status", "route_raw",
                "management_company", "contact_name", "contact_title", "management_type", "registered_agent", "matched_by")}
            lead.public_records["dbpr"]["county"] = d["county"]
        if d["route_type"] == "onsite":
            lead.management_type = "onsite"
            lead.evidence["management_from"] = "dbpr_onsite" if rec and rec.get("management_type") == "onsite" else "dbpr_same_address"
        yield lead
