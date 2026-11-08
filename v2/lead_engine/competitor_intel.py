"""Zone competitor intelligence: what neighbours' reviews say about exterior-cleaning vendors.

Sources (cheapest first):
  1. reviews of every building collected in the same run (free: already paid for), within
     COMPETITOR_RADIUS_M of the lead;
  2. optionally (COMPETITOR_NEARBY=true + GOOGLE_PLACES_API_KEY) a Places API (New) Nearby
     Search around the lead with reviews (one request per lead);
  3. the lead's own reviews.

A sentence only counts when it talks about a cleaning vendor or job (pressure/power washing,
cleaning company/crew, window washers, hidrolavado…) AND shows a weakness:
  recurrence       mold / stains come back weeks after a pressure wash
  pressure_damage  high pressure damaged paint, sealant, stucco, screens
  streaks          left streaks / zebra marks
  unreliable       no-show, cancelled, left the job half done
  mess             killed plants, water/chemicals got inside
  cost             overcharged / rip-off
The summary names the dominant weakness and a closing argument the setter can use. Nothing
here names or disparages a specific competitor in the hook: vendor names stay in the evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

import requests
from loguru import logger

from . import costs
from .config import settings
from .geo import haversine_m
from .pain import _sentence_around

VENDOR_CONTEXT = re.compile(
    r"(?i)(?:pressure|power)[- ]?wash\w*|cleaning (?:company|crew|service|guys|team|vendor)|exterior cleaning|"
    r"window (?:washing|washers|cleaners|cleaning)|soft ?wash\w*|the (?:vendor|contractor|cleaners)|"
    r"hidrolavad\w*|lavado a presi[oó]n|empresa de limpieza|limpieza de (?:fachada|vidrios|ventanas)|los de la limpieza")
WEAKNESSES: dict[str, tuple[re.Pattern[str], str, str]] = {
    "recurrence": (re.compile(
        r"(?i)(?:mold|mildew|algae|stains?|black streaks?|green)\W+(?:\w+\W+){0,3}(?:came|comes|coming|is|are|was) back|"
        r"came back (?:within|after|in) (?:a few|a couple of|\d+|two|three|four|six) (?:days|weeks|months)|"
        r"(?:volvi[oó]|regres[oó]|reapareci[oó]) (?:el moho|las manchas|el verd[ií]n)|a l[oa]s? \d+ (?:semanas|meses) (?:volvi|ya estaba)"),
        "el moho y las manchas vuelven a las pocas semanas después del lavado a presión",
        "el softwash trata el moho con producto a baja presión en vez de solo arrastrarlo, por eso dura más"),
    "pressure_damage": (re.compile(
        r"(?i)(?:damag|ruin|strip|etch|peel|chipp|blast)\w* (?:\w+ ){0,3}(?:paint|sealant|caulk|stucco|screens?|pavers?|wood|finish)|"
        r"(?:paint|sealant|stucco|screens?) (?:was|were|got) (?:damaged|ruined|stripped|peeled)|too much pressure|"
        r"(?:da[nñ]aron|pelaron|arruinaron) (?:la pintura|el sellador|las mallas)|demasiada presi[oó]n"),
        "la alta presión dañó pintura, selladores o stucco",
        "la limpieza a baja presión no daña pintura, selladores ni stucco"),
    "streaks": (re.compile(r"(?i)(?:left|leaving|with) (?:\w+ )?(?:streaks|stripes|lines|marks|zebra)|zebra stripes|"
                           r"(?:rayas|marcas|manchas) (?:en la fachada|en el piso|por todos lados)"),
                "dejan rayas y marcas en fachada y pisos",
                "aplicación uniforme y enjuague controlado, sin rayas"),
    "unreliable": (re.compile(r"(?i)never (?:showed|came|finished)|no[- ]show|didn'?t show up|cancel+ed (?:twice|again|last)|"
                              r"(?:left|leave) (?:it|the job|the work) (?:half|unfinished)|half[- ]done|"
                              r"no vinieron|dejaron (?:el trabajo )?a medias|nunca terminaron"),
                   "incumplen fechas o dejan trabajos a medias",
                   "agenda confirmada por escrito y trabajo terminado por zonas"),
    "mess": (re.compile(r"(?i)(?:killed|burned|damaged) (?:the |our |all the )?(?:plants|landscaping|grass|garden)|"
                        r"(?:water|chemicals?|bleach) (?:got|went|leaked|came) (?:into|inside|in|through)|"
                        r"(?:mataron|quemaron) las plantas|entr[oó] agua"),
             "dañan plantas o filtran agua y químicos",
             "protección de plantas y control de agua y producto"),
    "cost": (re.compile(r"(?i)overcharg\w*|rip[- ]?off|way too expensive|charged us (?:extra|more|double)|cobraron de m[aá]s"),
             "hay quejas de sobreprecios",
             "presupuesto cerrado después de la inspección gratis"),
}
VENDOR_NAME = re.compile(
    r"\b([A-Z][\w&'.]+(?: [A-Z][\w&'.]+){0,3}) (Pressure Washing|Power Washing|Exterior Cleaning|Cleaning Services?|"
    r"Window Cleaning|Soft ?Wash(?:ing)?)\b")


_NAME_LEAD = {"Hired", "Used", "Called", "The", "We", "Our", "They", "Building", "Management", "Board", "Avoid",
              "Never", "Recently", "Last", "This", "That", "Contracted", "Using"}


def _vendor(m: re.Match | None) -> str | None:
    if not m:
        return None
    words = m.group(1).split()
    while words and words[0] in _NAME_LEAD:
        words = words[1:]
    return " ".join([*words, m.group(2)]) if words else None


def scan_review(text: str) -> list[dict[str, Any]]:
    """Weakness hits in one review: only sentences that mention a cleaning vendor/job (pure)."""
    out = []
    for key, (rx, _, _) in WEAKNESSES.items():
        for m in rx.finditer(text or ""):
            sentence = _sentence_around(text, m.start(), m.end(), max_len=200)
            ctx = text[max(0, m.start() - 160): m.end() + 160]
            if not VENDOR_CONTEXT.search(sentence) and not VENDOR_CONTEXT.search(ctx):
                continue
            vendor = VENDOR_NAME.search(ctx)
            out.append({"weakness": key, "quote": sentence, "vendor": _vendor(vendor)})
            break
    return out


def analyze(neighbors: Iterable[dict[str, Any]], *, own_reviews: list[dict] | None = None) -> dict[str, Any]:
    """neighbors: {"name", "distance_m", "reviews": [{"text","published","relative"}]} → zone summary (pure)."""
    mentions: list[dict[str, Any]] = []
    scanned = 0
    for nb in neighbors:
        scanned += 1
        for rv in nb.get("reviews") or []:
            for hit in scan_review(rv.get("text") or ""):
                mentions.append({**hit, "building": nb.get("name"), "distance_m": nb.get("distance_m"),
                                 "date": rv.get("published", "")[:7] if rv.get("published") else rv.get("relative")})
    for rv in own_reviews or []:
        for hit in scan_review(rv.get("text") or ""):
            mentions.append({**hit, "building": "este edificio", "distance_m": 0,
                             "date": rv.get("published", "")[:7] if rv.get("published") else rv.get("relative")})
    counts = Counter(m["weakness"] for m in mentions)
    summary: dict[str, Any] = {"neighbors_scanned": scanned, "mentions": mentions[:12], "weaknesses": dict(counts),
                               "vendors": sorted({m["vendor"] for m in mentions if m.get("vendor")})}
    if counts:
        top, n = counts.most_common(1)[0]
        buildings = {m["building"] for m in mentions if m["weakness"] == top}
        summary.update({"top_weakness": top, "top_weakness_es": WEAKNESSES[top][1], "argument": WEAKNESSES[top][2],
                        "buildings_with_top": len(buildings)})
    return summary


def hook_line(summary: dict[str, Any]) -> str | None:
    """'Zona: en 3 edificios vecinos … → argumento: …' (never names a competitor)."""
    if not summary or not summary.get("top_weakness"):
        return None
    nb = [m for m in summary["mentions"] if m["weakness"] == summary["top_weakness"] and m["building"] != "este edificio"]
    where = (f"reseñas de {len({m['building'] for m in nb})} edificio(s) vecino(s)" if nb
             else "reseñas de este edificio")
    return f"Zona: {where} dicen que {summary['top_weakness_es']} → argumento: {summary['argument']}."


def neighbors_from_pool(lead_lat: float | None, lead_lng: float | None, pool: Iterable[dict[str, Any]],
                        exclude_id: str | None = None, radius_m: float | None = None) -> list[dict[str, Any]]:
    """Buildings seen in this run (with reviews and coordinates) within the radius, nearest first."""
    if lead_lat is None or lead_lng is None:
        return []
    radius_m = radius_m or settings.competitor_radius_m
    out = []
    for b in pool:
        if b.get("id") == exclude_id or b.get("lat") is None or not b.get("reviews"):
            continue
        d = haversine_m(lead_lat, lead_lng, b["lat"], b["lng"])
        if d <= radius_m:
            out.append({**b, "distance_m": round(d)})
    return sorted(out, key=lambda b: b["distance_m"])


NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"


def nearby_from_places(lat: float, lng: float, radius_m: float | None = None) -> list[dict[str, Any]]:
    """Places API (New) Nearby Search with reviews (optional, billable)."""
    if not (settings.google_places_api_key and settings.competitor_nearby):
        return []
    body = {"includedTypes": ["condominium_complex", "apartment_building", "apartment_complex", "shopping_mall",
                              "hospital", "corporate_office"],
            "maxResultCount": 20,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                               "radius": float(radius_m or settings.competitor_radius_m)}}}
    headers = {"X-Goog-Api-Key": settings.google_places_api_key,
               "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.reviews"}
    try:
        r = requests.post(NEARBY_URL, json=body, headers=headers, timeout=settings.http_timeout_s)
        costs.tracker.add("places_nearby_reviews")
        r.raise_for_status()
        places = r.json().get("places") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Places Nearby falló: {}", exc)
        return []
    out = []
    for p in places:
        loc = p.get("location") or {}
        out.append({"id": p.get("id"), "name": (p.get("displayName") or {}).get("text"),
                    "lat": loc.get("latitude"), "lng": loc.get("longitude"),
                    "distance_m": round(haversine_m(lat, lng, loc["latitude"], loc["longitude"])) if loc else None,
                    "reviews": [{"text": ((rv.get("originalText") or rv.get("text") or {}).get("text") or ""),
                                 "published": rv.get("publishTime"), "relative": rv.get("relativePublishTimeDescription")}
                                for rv in p.get("reviews") or []]})
    return out
