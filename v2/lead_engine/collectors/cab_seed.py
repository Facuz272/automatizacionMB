"""Seed: community association management firms with an active CAB licence (DBPR registry, local).

`python -m lead_engine run --seed cab --strategy portfolio`. Every active CAB firm in the coverage counties
(Miami-Dade 23, Broward 16 by default; CAB_COUNTIES overrides) becomes one portfolio lead: the firm decides the
exterior-cleaning vendor for every condo it runs. Ranked by the size of its DBPR portfolio in the coverage
counties (firms whose name appears on the most condos first); several licences of one firm are one lead.

The registry has no phone or website: `resolve` finds the firm's own listing (verified catalog domain first,
then one Places lookup — in PLACES_MODE=details a free IDs-only search plus at most PLACES_DETAILS_MAX Place
Details calls). A listing is the firm only when the name is clearly the same and it sits in South Florida,
never a condo or a business inside one.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterator

from loguru import logger

from ..dbpr import _title_case, connect
from ..models import Lead

COUNTIES = ("23", "16")                         # cam.COUNTY: 23 Miami-Dade, 16 Broward
SOUTH_FLORIDA = {"miami-dade", "broward", "palm-beach"}
BUILDING_TYPES = {"condominium_complex", "apartment_building", "apartment_complex", "housing_complex"}
MIN_NAME_SIMILARITY = 0.5


def counties() -> tuple[str, ...]:
    raw = os.getenv("CAB_COUNTIES")
    return tuple(c.strip() for c in raw.split(",") if c.strip()) if raw else COUNTIES


def candidates(conn: sqlite3.Connection | None = None, county_codes: tuple[str, ...] | None = None) -> list[dict]:
    """One row per firm (several licences of one firm collapse on the name key), largest DBPR portfolio first."""
    from ..backfill import firm_portfolio

    conn = conn or connect()
    codes = county_codes or counties()
    rows = conn.execute(
        f"SELECT license_id, name, dba, name_key, city, zip, county_code FROM cam_licenses WHERE kind = 'CAB' "
        f"AND active = 1 AND county_code IN ({','.join('?' * len(codes))}) ORDER BY license_id",
        codes).fetchall()
    firms: dict[str, dict] = {}
    for r in rows:
        key = r[3] or r[1]
        if key in firms:
            firms[key]["licenses"].append(r[0])
            continue
        name = _title_case(r[2] or r[1] or "")
        firms[key] = {"license_id": r[0], "licenses": [r[0]], "name": name, "legal_name": r[1],
                      "city": _title_case(r[4] or ""), "zip": (r[5] or "")[:5], "county_code": r[6]}
    for f in firms.values():
        pf = firm_portfolio(f["name"]) or firm_portfolio(f["legal_name"])
        f["portfolio"] = pf["count"] if pf else 0
    return sorted(firms.values(), key=lambda f: (-f["portfolio"], f["name"]))


def to_lead(f: dict[str, Any], strategy: str = "portfolio") -> Lead:
    lead = Lead(source="cab", source_id=f"cab:{f['license_id']}", strategy_kind=strategy,  # type: ignore[arg-type]
                company_name=f["name"], address=", ".join(x for x in (f["city"], f"FL {f['zip']}".strip()) if x),
                management_company=f["name"], management_type="third_party",
                evidence={"seed": "dbpr_cab", "cab_licenses": f["licenses"], "lead_kind": "management_firm",
                          "dbpr_portfolio": f["portfolio"]})
    lead.licenses["firm"] = {"license_id": f["license_id"], "name": f["name"], "legal_name": f["legal_name"],
                             "active": True, "matched_by": "exact"}
    return lead


def collect(strategy: str, max_candidates: int, conn: sqlite3.Connection | None = None) -> Iterator[Lead]:
    for f in candidates(conn)[: max(0, max_candidates * 3)]:     # spare rows: firms seen earlier are skipped
        yield to_lead(f, strategy)


def accept_listing(firm: str, place: dict) -> bool:
    """The Places listing is this firm's office: same firm name, in South Florida, not a building / sub-listing."""
    from ..municipal_signals import county_of
    from ..names import firm_similarity
    from .google_places import sub_listing_reason

    name = ((place.get("displayName") or {}).get("text") or "")
    if set(place.get("types") or []) & BUILDING_TYPES or sub_listing_reason(place, firm):
        return False
    if firm_similarity(firm, name) < MIN_NAME_SIMILARITY:
        return False
    return county_of(place.get("formattedAddress")) in SOUTH_FLORIDA


def resolve(lead: Lead) -> bool:
    """Phone / website / office address of a CAB firm lead. True when a listing was accepted."""
    from .. import mgmt_domains
    from ..config import settings
    from ..normalize import extract_domain
    from . import google_places as gp

    hit = mgmt_domains.from_catalog(lead.company_name)
    if hit and not lead.website_url:
        lead.website_url, lead.domain = f"https://{hit[1]}/", hit[1]
        lead.evidence["website_from"] = "mgmt_catalog"
    if not settings.google_places_api_key:
        return False
    city = lead.address.split(",")[0] if lead.address else ""
    try:
        for place in gp.candidate_places(f"{lead.company_name}, {city}, FL", page_size=5):
            if not accept_listing(lead.company_name, place):
                continue
            found = gp._to_lead(place, lead.strategy_kind)
            if not found:
                continue
            for attr in ("phone", "website_url", "domain", "maps_url", "google_rating", "total_reviews", "lat", "lng",
                         "place_types"):
                if getattr(found, attr) and not getattr(lead, attr):
                    setattr(lead, attr, getattr(found, attr))
            lead.phones = found.phones + [p for p in lead.phones if p["number"] not in {x["number"] for x in found.phones}]
            lead.address = found.address or lead.address
            if lead.website_url and not lead.domain:
                lead.domain = extract_domain(lead.website_url)
            lead.evidence.update(places_match="firm_name", place_id=found.source_id)
            return True
    except gp.PlacesError as exc:
        logger.warning("Places firma {}: {}", lead.company_name, exc)
    return False
