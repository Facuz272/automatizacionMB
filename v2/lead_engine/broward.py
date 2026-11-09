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


# ── cadastre seed for the east corridor (`--seed pa`, daily mix slices) ──────

# Florida DOR use codes (same scheme as Miami-Dade's DOR_CODE_CUR first two digits).
SEED_USE = {
    "plaza": ("11", "12", "13", "14", "15", "16"),     # stores, mixed store/office, dept. stores, supermarkets,
                                                       # regional & community shopping centers
    "commercial": ("17", "18", "19", "39"),            # offices 1 / multi-storey, professional buildings, hotels
    "clinic": ("73", "85"),                            # private / public hospitals
}
SEED_FIELDS = ("FOLIO_NUMBER,NAME_LINE_1,NAME_LINE_2,SITUS_STREET_NUMBER,SITUS_STREET_DIRECTION,SITUS_STREET_NAME,"
               "SITUS_STREET_TYPE,SITUS_ZIP_CODE,BLDG_YEAR_BUILT,BLDG_TOT_SQ_FOOTAGE,USE_CODE,GIS_SQUARE_FOOT,"
               "JUST_BUILDING_VALUE,JUST_LAND_VALUE,ADDRESS_LINE_1,CITY,ZIP")


def zip_city() -> dict[str, str]:
    """Corridor ZIP → display city, from the tenant's zip_restrictions keys."""
    out = {}
    for city, zips in (settings.zip_restrictions or {}).items():
        name = city.replace(" FL", "").strip()
        for z in zips:
            out[str(z)[:5]] = name
    return out


def seed_where(vertical: str, zips: list[str], min_sqft: int) -> str:
    codes = ",".join(f"'{c}'" for c in SEED_USE[vertical])
    return (f"SITUS_ZIP_CODE IN ({','.join(str(int(z)) for z in sorted(zips))}) AND USE_CODE IN ({codes}) "
            f"AND BLDG_TOT_SQ_FOOTAGE >= {int(min_sqft)}")


def seed_record(a: dict) -> dict[str, Any] | None:
    """Cadastre record (same keys as appraiser.aggregate's 'property' shape) for one tax-roll row."""
    if not a.get("SITUS_STREET_NUMBER") or not a.get("SITUS_STREET_NAME") or not a.get("SITUS_ZIP_CODE"):
        return None
    from .dbpr import _title_case

    street = " ".join(x for x in (a["SITUS_STREET_NUMBER"], a.get("SITUS_STREET_DIRECTION"), a["SITUS_STREET_NAME"],
                                  a.get("SITUS_STREET_TYPE")) if x)
    zipc = str(a["SITUS_ZIP_CODE"])[:5]
    care = (a.get("NAME_LINE_2") or "").strip()
    return {
        "kind": "property", "folio": a.get("FOLIO_NUMBER"),
        "address": f"{_title_case(street)}, {zip_city().get(zipc, 'Fort Lauderdale')}, FL {zipc}",
        "year_built": a.get("BLDG_YEAR_BUILT") or None, "building_sqft": a.get("BLDG_TOT_SQ_FOOTAGE") or None,
        "lot_sqft": a.get("GIS_SQUARE_FOOT") or None, "floors": None, "units": None,
        "assessed_value": (a.get("JUST_BUILDING_VALUE") or 0) + (a.get("JUST_LAND_VALUE") or 0) or None,
        "owner_name": (a.get("NAME_LINE_1") or "").strip() or None,
        "care_of": care.lstrip("%").strip() if care.startswith("%") else None,
        "owner_mailing": ", ".join(x for x in (a.get("ADDRESS_LINE_1"), a.get("CITY"), a.get("ZIP")) if x) or None,
        "dor_code": a.get("USE_CODE"), "source": "broward_property_appraiser",
    }


def seed(vertical: str, zips: list[str], min_sqft: int, limit: int) -> list[dict[str, Any]]:
    """Largest properties of the vertical in the corridor ZIPs (one record per folio); [] on any failure."""
    if vertical not in SEED_USE or not zips:
        return []
    try:
        r = requests.get(URL, params={"where": seed_where(vertical, zips, min_sqft), "outFields": SEED_FIELDS,
                                      "orderByFields": "BLDG_TOT_SQ_FOOTAGE DESC", "returnGeometry": "false",
                                      "resultRecordCount": max(1, min(limit, 2000)), "f": "json"},
                         timeout=TIMEOUT * 2, headers={"User-Agent": settings.user_agent})
        costs.tracker.add("arcgis")
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "arcgis error"))
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        logger.warning("Semilla del catastro de Broward falló: {}", exc)
        return []
    out, seen = [], set()
    for f in data.get("features", []):
        rec = seed_record(f.get("attributes", {}))
        if rec and rec["folio"] not in seen:
            seen.add(rec["folio"])
            out.append(rec)
    return out
