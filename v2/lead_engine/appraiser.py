"""Miami-Dade Property Appraiser (official cadastre) + Florida milestone-inspection window.

Layer (verified live 2026-11-08, public, no key, updated monthly):
  https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/PaGISView_gdb/FeatureServer/0
  fields used: FOLIO, PARENT_FOLIO, CONDO_FLAG, TRUE_SITE_ADDR/UNIT/CITY/ZIP_CODE, TRUE_OWNER1,
  TRUE_MAILING_ADDR1..3/CITY/STATE, DOR_CODE_CUR, DOR_DESC, FLOOR_COUNT, UNIT_COUNT,
  BUILDING_ACTUAL_AREA, LOT_SIZE, YEAR_BUILT, ASSESSED_VAL_CUR

A condominium is one "REFERENCE FOLIO" plus one folio per unit (5555 Collins Ave: 298 unit
folios; DBPR says 299), so condo records are aggregated: units = unit folios, year = most
common YEAR_BUILT, living area and assessed value = sums, floors = highest floor read from
the unit numbers ("1205" -> 12, "3A" -> 3). Any other property is one folio with its own
FLOOR_COUNT / area / lot / owner / mailing address. Coverage: Miami-Dade only.

Milestone inspection (Florida s. 553.899, "SB 4-D", 2022; + Miami-Dade/Broward building
recertification): condo/co-op buildings of 3+ storeys need a structural milestone inspection
at 30 years (25 when within 3 miles of the coast) and every 10 years after; the counties
recertify at 30/40/50… years. A building whose age is within ±1 year of one of those
ages is "in the legal window" and gets a top-priority pain signal.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Any

import requests
from loguru import logger

from . import costs
from .config import settings

URL = "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/PaGISView_gdb/FeatureServer/0/query"
FIELDS = ("FOLIO,PARENT_FOLIO,CONDO_FLAG,TRUE_SITE_ADDR,TRUE_SITE_UNIT,TRUE_SITE_CITY,TRUE_SITE_ZIP_CODE,TRUE_OWNER1,"
          "TRUE_MAILING_ADDR1,TRUE_MAILING_ADDR2,TRUE_MAILING_ADDR3,TRUE_MAILING_CITY,TRUE_MAILING_STATE,"
          "DOR_CODE_CUR,DOR_DESC,FLOOR_COUNT,UNIT_COUNT,BUILDING_ACTUAL_AREA,LOT_SIZE,YEAR_BUILT,ASSESSED_VAL_CUR")

# Florida DOR land-use code (first two digits) -> business vertical.
VERTICAL_BY_DOR = {
    "03": "community", "04": "community", "05": "community", "08": "community",   # multi-family / condo / co-op
    "11": "plaza", "12": "plaza", "13": "plaza", "14": "plaza", "15": "plaza", "16": "plaza",   # stores, shopping centers
    "17": "commercial", "18": "commercial", "23": "commercial", "39": "commercial",       # offices, banks, hotels
    "19": "commercial",                                                                     # professional (medical → clinic)
    "73": "clinic", "85": "clinic",                                                         # hospitals
}
_MEDICAL = re.compile(r"(?i)medical|hospital|clinic|health care|nursing")
_CO = re.compile(r"(?i)^(?:C/O|ATTN:?|%)\s*")


def vertical_from_dor(code: str | None, desc: str | None) -> str | None:
    if desc and _MEDICAL.search(desc):
        return "clinic"
    return VERTICAL_BY_DOR.get((code or "")[:2])


def _floor_from_unit(unit: str | None) -> int | None:
    u = (unit or "").upper().strip()
    if not u or u.startswith(("PH", "LPH", "UPH")):
        return None
    m = re.match(r"^(\d{1,4})", u)
    if not m:
        return None
    digits = m.group(1)
    if len(digits) >= 3:                       # 1205 -> 12, 301 -> 3
        return int(digits[:-2]) or None
    if re.match(r"^\d{1,2}[A-Z]$", u):           # 3A -> 3
        return int(digits)
    return None


def _mailing(a: dict) -> str:
    parts = [a.get(k) for k in ("TRUE_MAILING_ADDR1", "TRUE_MAILING_ADDR2", "TRUE_MAILING_ADDR3")]
    city = " ".join(x for x in (a.get("TRUE_MAILING_CITY"), a.get("TRUE_MAILING_STATE")) if x)
    return ", ".join(x.strip() for x in (*parts, city) if x and x.strip())


def care_of(a: dict) -> str | None:
    """Management company named in the mailing address ('C/O TRIGATE MGMT')."""
    for k in ("TRUE_MAILING_ADDR1", "TRUE_MAILING_ADDR2", "TRUE_OWNER1"):
        v = (a.get(k) or "").strip()
        if _CO.match(v):
            return _CO.sub("", v).strip() or None
    return None


def aggregate(records: list[dict]) -> dict[str, Any] | None:
    """Building-level facts from the folios at one street address (pure)."""
    recs = [r for r in records if (r.get("DOR_DESC") or "") != "REFERENCE FOLIO"
            and not (r.get("DOR_DESC") or "").upper().startswith("VACANT")]
    if not recs:
        return None
    condo = [r for r in recs if r.get("CONDO_FLAG") == "Y"]
    if condo and len(condo) >= max(2, len(recs) // 2):
        years = [r["YEAR_BUILT"] for r in condo if r.get("YEAR_BUILT")]
        floors = [f for f in (_floor_from_unit(r.get("TRUE_SITE_UNIT")) for r in condo) if f]
        dor = Counter((r.get("DOR_CODE_CUR"), r.get("DOR_DESC")) for r in condo).most_common(1)[0][0]
        return {
            "kind": "condominium",
            "folio": condo[0].get("PARENT_FOLIO") or condo[0].get("FOLIO"),
            "units": len(condo),
            "year_built": Counter(years).most_common(1)[0][0] if years else None,
            "floors": max(floors) if floors else None,
            "floors_source": "unit_numbers" if floors else None,
            "building_sqft": sum(r.get("BUILDING_ACTUAL_AREA") or 0 for r in condo) or None,
            "assessed_value": sum(r.get("ASSESSED_VAL_CUR") or 0 for r in condo) or None,
            "lot_sqft": None,
            "owner_name": None,
            "dor_code": dor[0], "land_use": dor[1],
            "vertical": vertical_from_dor(dor[0], dor[1]),
            "city": condo[0].get("TRUE_SITE_CITY"),
        }
    main = max(recs, key=lambda r: (r.get("BUILDING_ACTUAL_AREA") or 0, r.get("ASSESSED_VAL_CUR") or 0))
    return {
        "kind": "property",
        "folio": main.get("FOLIO"),
        "units": main.get("UNIT_COUNT") or None,
        "year_built": main.get("YEAR_BUILT") or None,
        "floors": main.get("FLOOR_COUNT") or None,
        "floors_source": "appraiser" if main.get("FLOOR_COUNT") else None,
        "building_sqft": main.get("BUILDING_ACTUAL_AREA") or None,
        "assessed_value": main.get("ASSESSED_VAL_CUR") or None,
        "lot_sqft": main.get("LOT_SIZE") or None,
        "owner_name": (main.get("TRUE_OWNER1") or "").strip() or None,
        "owner_mailing": _mailing(main) or None,
        "care_of": care_of(main),
        "dor_code": main.get("DOR_CODE_CUR"), "land_use": main.get("DOR_DESC"),
        "vertical": vertical_from_dor(main.get("DOR_CODE_CUR"), main.get("DOR_DESC")),
        "city": main.get("TRUE_SITE_CITY"),
        "folios_at_address": len(recs),
    }


def _query(pattern: str, zip5: str | None) -> list[dict]:
    where = f"TRUE_SITE_ADDR LIKE '{pattern}'"
    if zip5:
        where += f" AND TRUE_SITE_ZIP_CODE LIKE '{zip5}%'"
    r = requests.get(URL, params={"where": where, "outFields": FIELDS, "returnGeometry": "false",
                                  "resultRecordCount": 2000, "f": "json"},
                     timeout=40, headers={"User-Agent": settings.user_agent})
    costs.tracker.add("arcgis")
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "arcgis error"))
    return [f.get("attributes", {}) for f in data.get("features", [])]


_disabled = False


def lookup(address: str | None) -> dict[str, Any] | None:
    """Official building facts for a Miami-Dade street address (None outside coverage / no match)."""
    global _disabled
    from .dbpr import zip5 as zip_of
    from .municipal_signals import county_of, like_pattern, same_street, street_parts

    if _disabled or not settings.appraiser_enabled or county_of(address) != "miami-dade":
        return None
    parts = street_parts(address)
    if not parts:
        return None
    try:
        recs = _query(like_pattern(parts), zip_of(address))
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        logger.warning("Property Appraiser desactivado en esta corrida ({})", exc)
        _disabled = True
        return None
    same = []
    for r in recs:
        site = (r.get("TRUE_SITE_ADDR") or "").strip()
        unit = (r.get("TRUE_SITE_UNIT") or "").strip()
        if unit and site.endswith(" " + unit):
            site = site[: -len(unit) - 1]
        if same_street(address, site):
            same.append(r)
    out = aggregate(same)
    if out:
        out["source"] = "miami_dade_property_appraiser"
    return out


def reset() -> None:
    global _disabled
    _disabled = False


# ── milestone inspection window (pure) ──────────────────────────────────────

def milestone_ages(coastal: bool, horizon: int = 80, condo: bool = True) -> list[int]:
    """Ages that trigger a structural milestone (condo/co-op, FS 553.899) or county recertification (any building)."""
    county = list(range(30, horizon + 1, 10))          # Miami-Dade / Broward recertification
    if not condo:
        return county
    first = 25 if coastal else 30
    statute = list(range(first, horizon + 1, 10))
    return sorted(set(statute) | set(county))


def milestone_signal(year_built: int | None, floors: int | None, coastal: bool, *, units: int | None = None,
                     today: date | None = None, condo: bool = True) -> dict[str, Any] | None:
    """Top-priority pain signal when the building is within ±1 year of a milestone age.

    condo=True: Florida milestone inspection (25/30 + every 10 years) and county recertification;
    condo=False (plazas, offices, hotels, hospitals): county recertification only (30, 40, 50…)."""
    if not year_built:
        return None
    if floors is not None and floors < 3 and (units or 0) < 20:
        return None                                       # the statute covers 3+ storey buildings
    year = (today or date.today()).year
    age = year - int(year_built)
    hits = [m for m in milestone_ages(coastal, condo=condo) if abs(age - m) <= 1]
    if not hits:
        return None
    m = hits[0]
    stage = ("inspección de hito inicial" if m in (25, 30) else f"recertificación de {m} años") if condo \
        else f"recertificación estructural del condado a los {m} años"
    if age < m:
        when = f"cumple {m} años en {int(year_built) + m}"
    elif age == m:
        when = f"cumple {m} años este año"
    else:
        when = f"cumplió {m} años en {int(year_built) + m}"
    zone = "a <3 millas de la costa" if coastal else "interior"
    if condo:
        quote = (f"Construido en {year_built} ({age} años, {zone}): {when} — ventana legal de Milestone Inspection "
                 f"(Ley SB 4-D, FS 553.899) / recertificación del condado; {stage}.")
        label = "ventana legal de Milestone Inspection / recertificación"
    else:
        quote = f"Construido en {year_built} ({age} años): {when} — {stage} (programa de recertificación de edificios)."
        label = "ventana de recertificación estructural del condado"
    data = {"year_built": int(year_built), "age": age, "milestone": m, "milestone_year": int(year_built) + m,
            "coastal": coastal, "condo": condo}
    return {"data": data, "category": "milestone", "label": label,
            "weight": 6.0, "quote": quote, "date": str(int(year_built) + m), "source": "municipal",
            "layer": "milestone_rule", "ref": f"age={age}"}
