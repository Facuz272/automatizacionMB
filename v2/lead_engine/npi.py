"""NPI Registry (CMS, US federal, public, no key) for the Clínica/Hospital vertical.

API (verified live 2026-11-08): https://npiregistry.cms.hhs.gov/api/?version=2.1
  enumeration_type=NPI-2 (organizations), taxonomy_description, city/postal_code (trailing * wildcard),
  state, organization_name, limit ≤ 200, skip.
Each organization has its practice LOCATION address + phone and an Authorized Official
(first/last name, title, direct phone).

Many NPI organizations are billing groups or tenants inside office towers ("STE 630"); those
don't decide on the building's exterior, so locations with a suite are skipped by default and
officials with billing/credentialing titles rank last.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterator

import requests
from loguru import logger

from . import costs
from .config import settings
from .models import Lead
from .normalize import to_e164

API = "https://npiregistry.cms.hhs.gov/api/"
TAXONOMIES = ("General Acute Care Hospital", "Clinic/Center", "Ambulatory Surgical", "Urgent Care")
SUITE = re.compile(r"(?i)\b(?:STE|SUITE|UNIT|APT|RM|ROOM|FL|FLOOR|BLDG)\b\.?\s*[\w-]+|#\s*\w+")
# Best decision makers for facility services first; billing / clinical roles last.
TITLE_RANK = [
    (re.compile(r"(?i)facilit|plant|engineer|maintenance|operations"), 0),
    (re.compile(r"(?i)administrator|practice manager|office manager|clinic manager|director|manager"), 1),
    (re.compile(r"(?i)\bceo\b|chief executive|coo\b|chief operating|president|owner|principal|partner"), 2),
    (re.compile(r"(?i)cfo|chief financial|controller"), 3),
    (re.compile(r"(?i)credential|billing|payer|rcm|coding|analyst|compliance|risk"), 5),
]


def title_rank(title: str | None) -> int:
    t = title or ""
    for rx, rank in TITLE_RANK:
        if rx.search(t):
            return rank
    return 4


def _location(rec: dict) -> dict:
    return next((a for a in rec.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})


def has_suite(loc: dict) -> bool:
    return bool(SUITE.search(f"{loc.get('address_1', '')} {loc.get('address_2', '')}"))


def _name(first: str | None, last: str | None) -> str | None:
    n = " ".join(x.strip().title() for x in (first, last) if x and x.strip() and x.strip() != "--")
    return n or None


def parse(rec: dict) -> dict[str, Any] | None:
    """Flatten one NPI-2 record (pure)."""
    b = rec.get("basic") or {}
    loc = _location(rec)
    if not loc or b.get("status") not in (None, "A"):
        return None
    zipc = (loc.get("postal_code") or "")[:5]
    street = " ".join(x for x in (loc.get("address_1"), loc.get("address_2")) if x)
    tax = next((t.get("desc") for t in rec.get("taxonomies", []) if t.get("primary")), None)
    return {
        "npi": rec.get("number"),
        "organization": (b.get("organization_name") or "").strip(),
        "address": f"{loc.get('address_1', '').title()}, {loc.get('city', '').title()}, {loc.get('state', 'FL')} {zipc}",
        "address_full": street,
        "suite": has_suite(loc),
        "phone": to_e164(loc.get("telephone_number")),
        "official_name": _name(b.get("authorized_official_first_name"), b.get("authorized_official_last_name")),
        "official_title": (b.get("authorized_official_title_or_position") or "").strip().title() or None,
        "official_phone": to_e164(b.get("authorized_official_telephone_number")),
        "taxonomy": tax,
        "last_updated": b.get("last_updated"),
    }


def search(*, taxonomy: str, city: str | None = None, postal_code: str | None = None,
           organization_name: str | None = None, limit: int = 200, skip: int = 0) -> list[dict]:
    if not settings.npi_enabled:
        return []
    params = {"version": "2.1", "enumeration_type": "NPI-2", "state": "FL", "limit": min(limit, 200), "skip": skip}
    if taxonomy:
        params["taxonomy_description"] = taxonomy
    if city:
        params["city"] = city.upper()
    if postal_code:
        params["postal_code"] = postal_code
    if organization_name:
        params["organization_name"] = organization_name
    try:
        r = requests.get(API, params=params, timeout=30, headers={"User-Agent": settings.user_agent})
        costs.tracker.add("npi")
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("NPI Registry no respondió: {}", exc)
        return []
    if data.get("Errors"):
        logger.warning("NPI Registry: {}", data["Errors"])
        return []
    return [p for p in (parse(x) for x in data.get("results", [])) if p]


def collect(strategy: str, max_candidates: int, cities: list[str], include_suites: bool = False) -> Iterator[Lead]:
    """Clinics / hospitals in the target cities (one lead per street address, best official first)."""
    from .dbpr import address_key

    seen: set[str] = set()
    emitted = 0
    for city in cities:
        for tax in TAXONOMIES:
            for org in sorted(search(taxonomy=tax, city=city), key=lambda o: (o["suite"], title_rank(o["official_title"]))):
                if org["suite"] and not include_suites:
                    continue
                key = address_key(org["address"]) or org["npi"]
                if key in seen or not org["phone"]:
                    continue
                seen.add(key)
                lead = Lead(source="npi", source_id=f"npi:{org['npi']}", strategy_kind=strategy,  # type: ignore[arg-type]
                            company_name=org["organization"].title(), phone=org["phone"], address=org["address"],
                            phones=[{"number": org["phone"], "label": "unlabeled", "source": "npi", "context": "location"}])
                apply(lead, org)
                yield lead
                emitted += 1
                if emitted >= max_candidates:
                    return
            time.sleep(0.2)


def apply(lead: Lead, org: dict) -> None:
    lead.vertical = "clinic"
    lead.public_records["npi"] = org
    if org.get("official_name"):
        lead.add_contact(org["official_name"], org.get("official_title") or "Authorized Official", "npi",
                         phone=org.get("official_phone"))


def enrich(lead: Lead) -> dict | None:
    """Find the NPI organization at the lead's ZIP with a similar name (clinic leads from Places)."""
    from .dbpr import name_similarity, zip5

    z = zip5(lead.address)
    if not z:
        return None
    best = None
    for tax in TAXONOMIES:
        for org in search(taxonomy=tax, postal_code=z):
            sim = name_similarity(lead.company_name, org["organization"])
            if sim >= 0.5 and (best is None or sim > best[0]):
                best = (sim, org)
    if best:
        apply(lead, best[1])
        return best[1]
    return None
