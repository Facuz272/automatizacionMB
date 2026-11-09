"""Broward County Property Appraiser tax roll (public ArcGIS layer of Broward County GIS).

Used only for the east/coastal corridor the tenant serves (BROWARD_ZIPS / tenant
`zip_restrictions`: Hallandale, Hollywood, Fort Lauderdale beach side). Same building-level
shape as appraiser.lookup (year built, units, floors from unit numbers, area), so the milestone
window, the PDF and the dossiers work the same as in Miami-Dade.

Every call has a 5-second budget; the first timeout or error disables the layer for the rest of
the run, so a slow county server never delays a batch.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import requests
from loguru import logger

from . import costs
from .config import settings

# Every folio of the tax roll, condo units included (781k rows; the PARCEL_POLY layer only has drawn parcels).
URL = ("https://services.arcgis.com/JMAJrTsHNLrSsWf5/arcgis/rest/services/"
       "QAlertBCPA_Parcel_TaxRoll/FeatureServer/3/query")
FIELDS = ("FOLIO_NUMBER,SITUS_STREET_NUMBER,SITUS_STREET_DIRECTION,SITUS_STREET_NAME,SITUS_STREET_TYPE,"
          "SITUS_UNIT_NUMBER,SITUS_ZIP_CODE,BLDG_YEAR_BUILT,ACTUAL_YEAR_BUILT,BLDG_UNITS,BLDG_TOT_SQ_FOOTAGE,"
          "USE_CODE,USE_TYPE,JUST_BUILDING_VALUE,JUST_LAND_VALUE")
TIMEOUT = 5.0
CONDO_USE = {"04"}              # FDOR use code: condominium unit
_disabled = False


def reset() -> None:
    global _disabled
    _disabled = False


def allowed_zips() -> set[str]:
    zips: set[str] = set()
    for v in (settings.zip_restrictions or {}).values():
        zips |= {str(z)[:5] for z in v}
    return zips


def aggregate(records: list[dict]) -> dict[str, Any] | None:
    """Building-level facts from the parcels at one street address (pure)."""
    from .appraiser import _floor_from_unit

    recs = [r for r in records if (r.get("BLDG_YEAR_BUILT") or 0) > 0 or r.get("USE_CODE") in CONDO_USE]
    if not recs:
        return None
    condo = [r for r in recs if r.get("USE_CODE") in CONDO_USE]
    if len(condo) >= 2:
        years = [r["BLDG_YEAR_BUILT"] for r in condo if r.get("BLDG_YEAR_BUILT")]
        floors = [f for f in (_floor_from_unit(r.get("SITUS_UNIT_NUMBER")) for r in condo) if f]
        return {"kind": "condominium", "units": len(condo),
                "year_built": Counter(years).most_common(1)[0][0] if years else None,
                "floors": max(floors) if floors else None, "floors_source": "unit_numbers" if floors else None,
                "building_sqft": sum(r.get("BLDG_TOT_SQ_FOOTAGE") or 0 for r in condo) or None,
                "assessed_value": sum((r.get("JUST_BUILDING_VALUE") or 0) + (r.get("JUST_LAND_VALUE") or 0)
                                      for r in condo) or None,
                "land_use": "CONDOMINIUM", "vertical": "community", "folio": condo[0].get("FOLIO_NUMBER")}
    main = max(recs, key=lambda r: r.get("BLDG_TOT_SQ_FOOTAGE") or 0)
    return {"kind": "property", "units": main.get("BLDG_UNITS") or None,
            "year_built": main.get("BLDG_YEAR_BUILT") or None, "floors": None, "floors_source": None,
            "building_sqft": main.get("BLDG_TOT_SQ_FOOTAGE") or None,
            "assessed_value": (main.get("JUST_BUILDING_VALUE") or 0) + (main.get("JUST_LAND_VALUE") or 0) or None,
            "land_use": main.get("USE_TYPE"), "folio": main.get("FOLIO_NUMBER")}


def lookup(address: str | None) -> dict[str, Any] | None:
    """Official facts for a Broward address inside the tenant's corridor (None otherwise or on any failure)."""
    global _disabled
    from .dbpr import zip5
    from .municipal_signals import _DIRS, street_parts

    zipc = zip5(address)
    if _disabled or not zipc or zipc not in allowed_zips():
        return None
    parts = street_parts(address)
    if not parts:
        return None
    words = [t for t in parts[1].split() if t not in _DIRS] or parts[1].split()
    name = max(words, key=len).replace("'", "''")
    where = (f"SITUS_STREET_NUMBER = '{parts[0]}' AND SITUS_STREET_NAME LIKE '%{name}%' "
             f"AND SITUS_ZIP_CODE = {int(zipc)}")
    try:
        r = requests.get(URL, params={"where": where, "outFields": FIELDS, "returnGeometry": "false",
                                      "resultRecordCount": 2000, "f": "json"},
                         timeout=TIMEOUT, headers={"User-Agent": settings.user_agent})
        costs.tracker.add("arcgis")
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "arcgis error"))
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        logger.warning("Catastro de Broward desactivado en esta corrida ({})", exc)
        _disabled = True
        return None
    out = aggregate([f.get("attributes", {}) for f in data.get("features", [])])
    if out:
        out["source"] = "broward_property_appraiser"
    return out
