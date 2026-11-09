"""Seed from the Miami-Dade Property Appraiser for the non-residential verticals (`--seed pa`).

  plaza       DOR 11xx/15xx/16xx (stores, regional & community shopping centers)
  commercial  DOR 17xx/18xx (office buildings), 39xx (hotels) — whole-building folios only (no office condos)
  clinic      DOR 73xx/85xx (hospitals) or a land use mentioning medical
filtered by TARGET_CITIES (cadastre city name or the ZIP aliases of the DBPR seed) and a minimum
built area (SEED_MIN_SQFT, default 20,000 ft²), largest first. Each lead carries the cadastre
record (owner, mailing/care-of, floors, area, year, assessed value); the phone comes from Google
Places by address when the key exists, or from the property's website.

The east-Broward corridor (tenant `zip_restrictions`: Hallandale, Hollywood, Fort Lauderdale) is seeded
from the Broward County Property Appraiser tax roll with the same DOR use codes (broward.SEED_USE); both
counties are merged into one ranking by built area.
"""

from __future__ import annotations

import os
from typing import Iterator

import requests
from loguru import logger

from .. import appraiser, costs
from ..config import cities_for, settings
from ..dbpr import _title_case
from ..models import Lead
from .dbpr_seed import area_filter

DOR_WHERE = {
    "plaza": "(DOR_CODE_CUR LIKE '11%' OR DOR_CODE_CUR LIKE '15%' OR DOR_CODE_CUR LIKE '16%')",
    "commercial": "(DOR_CODE_CUR LIKE '17%' OR DOR_CODE_CUR LIKE '18%' OR DOR_CODE_CUR LIKE '39%')",
    "clinic": "(DOR_CODE_CUR LIKE '73%' OR DOR_CODE_CUR LIKE '85%' OR DOR_DESC LIKE '%MEDICAL%')",
}
NAME = {"plaza": "Shopping/Plaza", "commercial": "Edificio comercial", "clinic": "Centro médico"}


def where_clause(vertical: str, cities: list[str], min_sqft: int) -> str:
    area = area_filter(cities)
    names = {n.title().replace("'", "''") for n in area.names}
    corridor = {z for zs in area.restricted.values() for z in zs}       # east Broward: seeded from its own roll
    zips = set(area.zips) - area.exclude - corridor
    area = " OR ".join([f"TRUE_SITE_CITY = '{n}'" for n in sorted(names)] +
                       [f"TRUE_SITE_ZIP_CODE LIKE '{z}%'" for z in sorted(zips)])
    return (f"{DOR_WHERE[vertical]} AND CONDO_FLAG <> 'Y' AND BUILDING_ACTUAL_AREA >= {int(min_sqft)}"
            + (f" AND ({area})" if area else ""))


def to_lead(a: dict, vertical: str, strategy: str) -> Lead | None:
    site = (a.get("TRUE_SITE_ADDR") or "").strip()
    zipc = (a.get("TRUE_SITE_ZIP_CODE") or "")[:5]
    if not site or not zipc:
        return None
    address = f"{_title_case(site)}, {a.get('TRUE_SITE_CITY') or 'Miami'}, FL {zipc}".replace("Unincorporated County", "Miami")
    rec = appraiser.aggregate([a]) or {}
    owner = (a.get("TRUE_OWNER1") or "").strip()
    lead = Lead(source="pa", source_id=f"pa:{a.get('FOLIO')}", strategy_kind=strategy,  # type: ignore[arg-type]
                company_name=_title_case(owner) if owner else f"{NAME[vertical]} {_title_case(site)}",
                address=address, evidence={"seed": "appraiser", "folio": a.get("FOLIO")})
    lead.vertical = vertical
    lead.cadastre = {**rec, "source": "miami_dade_property_appraiser"}
    return lead


def broward_lead(rec: dict, vertical: str, strategy: str) -> Lead:
    owner = rec.get("owner_name")
    lead = Lead(source="pa", source_id=f"bcpa:{rec['folio']}", strategy_kind=strategy,  # type: ignore[arg-type]
                company_name=_title_case(owner) if owner else f"{NAME[vertical]} {rec['address'].split(',')[0]}",
                address=rec["address"], evidence={"seed": "broward_appraiser", "folio": rec["folio"]})
    lead.vertical = vertical
    lead.cadastre = {k: v for k, v in rec.items() if k != "address"}
    return lead


def collect(strategy: str, max_candidates: int, *, vertical: str = "plaza", cities: list[str] | None = None,
            min_sqft: int | None = None) -> Iterator[Lead]:
    if vertical not in DOR_WHERE:
        raise SystemExit(f"--vertical debe ser plaza, commercial o clinic (no '{vertical}')")
    min_sqft = min_sqft if min_sqft is not None else int(os.getenv("SEED_MIN_SQFT", "20000"))
    params = {"where": where_clause(vertical, cities or cities_for(strategy), min_sqft), "outFields": appraiser.FIELDS,
              "orderByFields": "BUILDING_ACTUAL_AREA DESC", "returnGeometry": "false",
              # extra rows so buildings seen in earlier runs can be skipped and the ranking still fills the batch
              "resultRecordCount": max(1, min(max_candidates * 5 + 200, 2000)), "f": "json"}
    try:
        # POST: a long city/ZIP list makes the GET URL too long for ArcGIS (404).
        r = requests.post(appraiser.URL, data=params, timeout=60, headers={"User-Agent": settings.user_agent})
        costs.tracker.add("arcgis")
        r.raise_for_status()
        rows = [f.get("attributes", {}) for f in r.json().get("features", [])]
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Semilla del Property Appraiser (Miami-Dade) falló: {}", exc)
        rows = []
    leads = [l for l in (to_lead(a, vertical, strategy) for a in rows) if l]
    # East-Broward corridor: same verticals from the Broward tax roll, for the restricted cities in the area.
    area = area_filter(cities or cities_for(strategy))
    restricted = {z for zs in area.restricted.values() for z in zs}
    corridor = sorted((area.zips & restricted) - area.exclude)      # only the corridor cities that were asked for
    if corridor:
        from .. import broward
        for rec in broward.seed(vertical, corridor, min_sqft, params["resultRecordCount"]):
            leads.append(broward_lead(rec, vertical, strategy))
    # One ranking for both counties: largest built area first.
    leads.sort(key=lambda l: -(l.cadastre.get("building_sqft") or 0))
    yield from leads
