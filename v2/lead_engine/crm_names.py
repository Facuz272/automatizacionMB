"""Leads already in the CRM by name / address (the CRM itself deduplicates only by phone).

A management firm or a building can be in the CRM under another phone (manual leads loaded before the engine,
a firm's other office line). `data/crm_names.json` (or CRM_NAMES_FILE) is a read-only snapshot of the CRM's
leads as {"leads": [[company_name, address], ...]}; the gate rejects a candidate that is clearly one of them
before any paid lookup. Missing file → no check (the phone check still runs).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from loguru import logger

from .config import settings

FIRM_SIMILARITY = 0.8
BUILDING_SIMILARITY = 0.85


def path() -> Path:
    return Path(os.getenv("CRM_NAMES_FILE") or settings.state_db.with_name("crm_names.json"))


@lru_cache(maxsize=4)
def _load(p: str, mtime: float) -> tuple[tuple[str, str | None], ...]:
    try:
        rows = json.loads(Path(p).read_text(encoding="utf-8")).get("leads") or []
    except (OSError, ValueError) as exc:
        logger.warning("No se pudo leer {} ({}); sin control de nombres contra el CRM", p, exc)
        return ()
    return tuple((r[0], r[1] if len(r) > 1 else None) for r in rows if r and r[0])


def rows() -> tuple[tuple[str, str | None], ...]:
    p = path()
    if not p.exists():
        return ()
    return _load(str(p), p.stat().st_mtime)


LEGAL = {"INC", "LLC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PA", "PL", "PLLC", "LP", "LLP", "THE", "OF",
         "AND", "L", "C"}
ABBREV = {"MGMT": "MANAGEMENT", "MGT": "MANAGEMENT", "MANAGMENT": "MANAGEMENT", "MANGEMENT": "MANAGEMENT",
          "ASSN": "ASSOCIATION", "ASSOC": "ASSOCIATION", "SVCS": "SERVICES", "SVC": "SERVICES", "SERVICE": "SERVICES",
          "PROP": "PROPERTY", "PROPERTIES": "PROPERTY", "INTL": "INTERNATIONAL", "GRP": "GROUP"}
# Words a branch adds to the same firm's name ("FirstService Residential Miami", "… of Florida").
BRANCH = {"MIAMI", "FLORIDA", "FL", "SOUTH", "NORTH", "DADE", "BROWARD", "FORT", "LAUDERDALE", "METRO", "BEACH",
          "CORAL", "GABLES", "DORAL", "AVENTURA", "HIALEAH", "BOCA", "RATON", "PALM", "WEST", "EAST", "COUNTY"}


def _words(name: str | None) -> set[str]:
    import re

    out = set()
    for w in re.findall(r"[A-Z0-9]+", (name or "").upper().replace("&", " AND ")):
        w = ABBREV.get(w, w)
        if w not in LEGAL:
            out.add(w)
    return out


def same_firm(a: str | None, b: str | None) -> bool:
    """The same firm: identical words up to legal suffixes and abbreviations (Mgmt / Management, Inc / LLC),
    word sets 80% alike, or one name plus only a branch / city ("FirstService Residential" ~ "… Miami").
    "GRS Management" and "GRS Community Management" are different firms."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if len(wa & wb) / len(wa | wb) >= FIRM_SIMILARITY:
        return True
    small, big = sorted((wa, wb), key=len)
    return len(small) >= 2 and small <= big and (big - small) <= BRANCH


def same_building(name: str | None, address: str | None, crm_name: str | None, crm_address: str | None) -> bool:
    from .dbpr import address_key, name_similarity, numbers_conflict, zip5

    ka, kb = address_key(address), address_key(crm_address)
    if ka and kb:
        return ka == kb
    if numbers_conflict(name, crm_name) or name_similarity(name, crm_name) < BUILDING_SIMILARITY:
        return False
    za, zb = zip5(address), zip5(crm_address)
    return not (za and zb and za != zb)


def known(lead) -> str | None:
    """The CRM lead this candidate already is (its name), or None."""
    from .backfill import is_firm

    firm = lead.source == "cab" or lead.evidence.get("lead_kind") == "management_firm"
    for crm_name, crm_address in rows():
        if firm:
            if same_firm(lead.company_name, crm_name):
                return crm_name
        elif not is_firm(crm_name) and same_building(lead.company_name, lead.address, crm_name, crm_address):
            return crm_name                    # (a firm's office can share a tower's street address)
    return None
