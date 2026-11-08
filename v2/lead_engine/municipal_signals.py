"""Municipal urgency signals from official open data (ArcGIS REST), by street address.

Layers verified reachable on 2026-11-08 (no key, public):
  miami_recert   City of Miami · 40_Year_Recertification (9,157 buildings; 1,907 "Pending")
  mdc_bldg_viol  Miami-Dade · Open_Building_Violations (25,057 open; 3,074 "Unsafe Structure")
  mdc_code       Miami-Dade · CodeComplianceViolation_Open_View (9,459 open)
  mdc_permits    Miami-Dade · miamidade_permit_data (489,896 permits, incl. municipal ones:
                 painting, waterproofing, concrete restoration, facade, roofing, pool deck…)
Miami Beach, Broward and Fort Lauderdale GIS servers did not answer from our network
at that date, so leads there get no municipal signal (never an error).

Each signal becomes a pain entry with source "municipal" and a weight above any
review (recert pending 6, unsafe structure 5, maintenance code case 4, other open
building violation 3, recent exterior permit 2.5), so it leads the setter hook.
A source that errors is switched off for the rest of the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests
from loguru import logger

from . import costs
from .config import settings
from .dbpr import _norm_street
from .geo import haversine_m

MDC = "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services"
COM = "https://services1.arcgis.com/CvuPhqcTQpZPT9qY/arcgis/rest/services"
STREET_TYPES = {"AVE", "ST", "RD", "DR", "BLVD", "PL", "CT", "LN", "TER", "PKWY", "CIR", "HWY", "TRL", "CSWY", "PLZ",
                "WAY", "AV", "TERR", "STREET", "AVENUE", "DRIVE", "ROAD", "COURT", "PLACE", "LANE", "BOULEVARD"}
# Matched on the work description only (the application type "ALTER - EXTERIOR" also covers doors, awnings…).
PERMIT_KEYWORDS = ("PAINT", "WATERPROOF", "RESTOR", "SPALL", "RECERT", "STUCCO", "FACADE", "ROOF", "SEALANT",
                   "BALCON", "RAILING", "CONCRETE REPAIR", "POOL DECK", "POOLDECK", "POOL/P", "MILESTONE", "PRESSURE CLEAN",
                   "PAVER", "WALKWAY")
CODE_RELEVANT = re.compile(r"(?i)maintenance|upkeep|graffiti|trash|overgrowth|minimum housing|premise")
PERMIT_MONTHS = 24


# ── address helpers (pure) ──────────────────────────────────────────────────

def street_parts(address: str | None) -> tuple[str, str] | None:
    """('1865', 'BRICKELL') / ('250', 'NW 108') — number and street core without the type or unit."""
    first = (address or "").upper().split(",")[0].strip()
    m = re.match(r"(\d{1,6})[A-Z]?(?:\s*-\s*\d+)?\s+(.+)$", first)
    if not m:
        return None
    toks = _norm_street(m.group(2)).split()
    core = [t for t in toks if t not in STREET_TYPES]
    # Unit numbers left at the end ("COLLINS AVE 402") are not part of the street.
    while len(core) > 1 and core[-1].isdigit() and toks.index(core[-1]) > 0 and toks[toks.index(core[-1]) - 1] in STREET_TYPES:
        core.pop()
    return (m.group(1), " ".join(core)) if core else None


def same_street(lead_address: str | None, record_address: str | None) -> bool:
    a, b = street_parts(lead_address), street_parts(record_address)
    return bool(a and b and a == b)


def county_of(address: str | None, dbpr_county: str | None = None) -> str | None:
    if dbpr_county:
        return {"DADE": "miami-dade", "MIAMI-DADE": "miami-dade", "BROWARD": "broward",
                "PALM BEACH": "palm-beach", "MONROE": "monroe"}.get(dbpr_county.upper())
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b(?!.*\b\d{5}\b)", (address or "").upper())
    if not m:
        return None
    z = int(m.group(1))
    broward = {33004, 33009} | set(range(33019, 33030)) | set(range(33060, 33077)) | set(range(33301, 33395)) | {33441, 33442}
    if z in broward:
        return "broward"
    if 33010 <= z <= 33199 or 33030 <= z <= 33039:
        return "miami-dade"
    if 33401 <= z <= 33499:
        return "palm-beach"
    return None


def _date(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
    return str(v)[:10]


# ── classification of layer records (pure) ──────────────────────────────────

def signal(category: str, label: str, weight: float, quote: str, when: str | None, src: str, ref: str | None) -> dict:
    return {"category": category, "label": label, "weight": weight, "quote": quote, "date": when,
            "source": "municipal", "layer": src, "ref": ref}


def classify_recert(a: dict) -> dict | None:
    status = (a.get("CertificationStatus") or "").strip()
    if status != "Pending":
        return None
    detail = (a.get("RecertificationProcessStatus") or "").strip()
    year = a.get("RecertificateYear")
    plan = (a.get("PlanStatus") or "").strip()
    q = f"City of Miami: {a.get('Address', '').strip()} — recertificación {year} pendiente ({detail}" + (f", plan {plan}" if plan else "") + ")"
    return signal("municipal_recert", "recertificación 40 años pendiente", 6.0, q, str(year) if year else None,
                  "miami_recert", a.get("FolioNumber"))


def classify_building_violation(a: dict) -> dict | None:
    kind = (a.get("CASE_TYPE") or "").strip()
    when = _date(a.get("OPEN_DATE"))
    addr = (a.get("PROP_ADDR") or "").strip()
    if kind == "Unsafe Structure":
        return signal("municipal_unsafe", "caso abierto de estructura insegura", 5.0,
                      f"Miami-Dade: {addr} — Unsafe Structure abierto (caso {a.get('CASE_NUM')})", when, "mdc_bldg_viol", a.get("CASE_NUM"))
    if kind == "All Other Code Violations":
        return signal("municipal_violation", "violación de edificación abierta", 3.0,
                      f"Miami-Dade: {addr} — violación de edificación abierta (caso {a.get('CASE_NUM')})", when, "mdc_bldg_viol", a.get("CASE_NUM"))
    return None


def classify_code_case(a: dict) -> dict | None:
    problem = " ".join((a.get("PROBLEM_DESC") or "").split())
    if not CODE_RELEVANT.search(problem):
        return None
    return signal("municipal_code", "caso de código abierto por mantenimiento", 4.0,
                  f"Miami-Dade Code Compliance: {(a.get('ADDRESS') or '').strip()} — {problem} (caso {a.get('CASE_NUM')})",
                  _date(a.get("CASE_DATE")), "mdc_code", a.get("CASE_NUM"))


def classify_permit(a: dict, today: date | None = None) -> dict | None:
    desc = str(a.get("DetailDescriptionComments") or "").upper()
    hit = next((k for k in PERMIT_KEYWORDS if k in desc), None)
    when = _date(a.get("PermitIssuedDate"))
    if not hit or not when:
        return None
    if when < ((today or date.today()) - timedelta(days=PERMIT_MONTHS * 30)).isoformat():
        return None
    what = " ".join((a.get("DetailDescriptionComments") or "").split())
    return signal("municipal_permit", "obra exterior con permiso reciente", 2.5,
                  f"Permiso {a.get('PermitNumber') or ''} ({when}): {what}".strip(), when, "mdc_permits", a.get("PermitNumber"))


# ── sources ─────────────────────────────────────────────────────────────────

@dataclass
class Layer:
    key: str
    url: str
    address_field: str
    fields: str
    county: str
    classify: Callable[[dict], dict | None]
    extra_where: str = ""
    order_by: str | None = None
    city: str | None = None          # postal city filter when the layer covers one municipality
    coords: tuple[str, str] | None = None  # (lon, lat) attribute names for a distance check


LAYERS = [
    Layer("miami_recert", f"{COM}/40_Year_Recertification/FeatureServer/0/query", "Address",
          "RecertificateYear,CertificationStatus,RecertificationProcessStatus,Address,FolioNumber,PlanStatus,XLongitude,YLatitude",
          "miami-dade", classify_recert, city="MIAMI", coords=("XLongitude", "YLatitude")),
    Layer("mdc_bldg_viol", f"{MDC}/Open_Building_Violations/FeatureServer/0/query", "PROP_ADDR",
          "CASE_NUM,CASE_TYPE,PROP_ADDR,OPEN_DATE", "miami-dade", classify_building_violation),
    Layer("mdc_code", f"{MDC}/CodeComplianceViolation_Open_View/FeatureServer/0/query", "ADDRESS",
          "CASE_NUM,CASE_DATE,STAT_DESC,ADDRESS,PROBLEM_DESC", "miami-dade", classify_code_case),
    Layer("mdc_permits", f"{MDC}/miamidade_permit_data/FeatureServer/0/query", "PropertyAddress",
          "PermitNumber,PermitIssuedDate,ApplicationTypeDescription,DetailDescriptionComments,PropertyAddress",
          "miami-dade", classify_permit,
          extra_where="(" + " OR ".join(f"DetailDescriptionComments LIKE '%{k}%'" for k in PERMIT_KEYWORDS) + ")",
          order_by="PermitIssuedDate DESC"),
]

_disabled: set[str] = set()


def reset() -> None:
    _disabled.clear()


def _query(layer: Layer, prefix: str) -> list[dict]:
    where = f"{layer.address_field} LIKE '{prefix}%'"
    if layer.extra_where:
        where += f" AND {layer.extra_where}"
    params = {"where": where, "outFields": layer.fields, "returnGeometry": "false", "resultRecordCount": 25, "f": "json"}
    if layer.order_by:
        params["orderByFields"] = layer.order_by
    r = requests.get(layer.url, params=params, timeout=25, headers={"User-Agent": settings.user_agent})
    costs.tracker.add("arcgis")
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "arcgis error"))
    return [f.get("attributes", {}) for f in data.get("features", [])]


def lookup(address: str | None, *, lat: float | None = None, lng: float | None = None,
           dbpr_county: str | None = None, layers: list[Layer] | None = None) -> list[dict]:
    """Municipal signals for the building at `address` (empty list when none or no coverage)."""
    if not settings.municipal_signals:
        return []
    parts = street_parts(address)
    county = county_of(address, dbpr_county)
    if not parts or not county:
        return []
    postal_city = (address or "").upper().split(",")[1].strip() if "," in (address or "") else ""
    prefix = f"{parts[0]} {parts[1]}".replace("'", "''")
    out: list[dict] = []
    for layer in layers or LAYERS:
        if layer.key in _disabled or layer.county != county:
            continue
        if layer.city and layer.city != postal_city:
            continue
        try:
            records = _query(layer, prefix)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            logger.warning("Señales municipales: {} desactivada en esta corrida ({})", layer.key, exc)
            _disabled.add(layer.key)
            continue
        seen: set[str] = set()
        for a in records:
            if not same_street(address, a.get(layer.address_field)):
                continue
            if layer.coords and lat is not None and lng is not None:
                x, y = a.get(layer.coords[0]), a.get(layer.coords[1])
                if isinstance(x, (int, float)) and isinstance(y, (int, float)) and haversine_m(lat, lng, y, x) > 250:
                    continue
            sig = layer.classify(a)
            if sig and sig["category"] not in seen:     # strongest/most recent per category is enough
                seen.add(sig["category"])
                out.append(sig)
    return sorted(out, key=lambda s: -s["weight"])
