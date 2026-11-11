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


# ── physical condition of neighbouring buildings (second level of the zone hook) ──────────────

# Terms that are a problem by themselves, and terms that need a negative cue in the same sentence
# ("pool deck is filthy" counts, "beautiful pool deck" does not).
CONDITION_SELF = {
    "moho y algas": re.compile(r"(?i)\b(?:mold|mould|mildew|algae|moho|hongos|verd[ií]n)\b"),
    "suciedad y manchas": re.compile(r"(?i)\b(?:dirty|filthy|grime|grimy|stains?|stained|sucio|sucia|manchas?)\b"),
}
CONDITION_AREA = {
    "fachada": re.compile(r"(?i)\b(?:facade|fa[cç]ade|exterior|paint(?:ing)?|stucco|fachada|pintura)\b"),
    "pool deck": re.compile(r"(?i)\bpool ?(?:deck|area)\b|\bpiscina\b"),
    "pasillos y aceras": re.compile(r"(?i)\b(?:walkways?|hallways?|sidewalks?|catwalks?|pasillos?|aceras?)\b"),
    "garaje": re.compile(r"(?i)\b(?:parking|garage|estacionamiento|garaje)\b"),
    "mantenimiento": re.compile(r"(?i)\b(?:maintenance|maintained|upkeep|mantenimiento)\b"),
    "obras y renovación": re.compile(r"(?i)\b(?:renovation|construction|repairs?|scaffold\w*|obras?|renovaci[oó]n)\b"),
    "recertificación / inspección": re.compile(
        r"(?i)\b(?:40[- ]year|recertification|milestone|structural inspection|special assessments?|recertificaci[oó]n)\b"),
}
NEGATIVE_CUE = re.compile(
    r"(?i)\b(?:bad|poor(?:ly)?|terrible|awful|horrible|worst|dirty|filthy|broken|needs?|needed|never|issues?|problems?|"
    r"leak\w*|crack\w*|rust\w*|peel\w*|neglect\w*|disgusting|slow|closed|under|falling|old|outdated|lack|"
    r"assessments?|mal|malo|p[eé]simo|roto|nunca|falta|problemas?)\b")
CONDITION_ARGUMENT = {
    "moho y algas": "el softwash elimina moho y algas con producto a baja presión y frena que vuelvan",
    "suciedad y manchas": "limpieza a baja presión que saca manchas y suciedad sin dañar pintura ni sellos",
    "recertificación / inspección": "preparar fachada y áreas comunes antes de la inspección y de la pintura",
    "obras y renovación": "dejar fachada y áreas comunes listas al cerrar las obras",
}
DEFAULT_CONDITION_ARGUMENT = "una limpieza exterior programada mantiene fachada y áreas comunes al nivel de la zona"
PROXIMITY_ARGUMENT = "ofrecer la inspección gratuita aprovechando el relevamiento de la zona"


def scan_conditions(text: str) -> list[dict[str, str]]:
    """Physical / exterior-maintenance problems in one review (pure). One hit per label."""
    out, seen = [], set()
    for label, rx in CONDITION_SELF.items():
        m = rx.search(text or "")
        if m and label not in seen:
            seen.add(label)
            out.append({"label": label, "quote": _sentence_around(text, m.start(), m.end(), max_len=160)})
    for label, rx in CONDITION_AREA.items():
        for m in rx.finditer(text or ""):
            sentence = _sentence_around(text, m.start(), m.end(), max_len=160)
            if label not in seen and (NEGATIVE_CUE.search(sentence) or any(r.search(sentence)
                                                                            for r in CONDITION_SELF.values())):
                seen.add(label)
                out.append({"label": label, "quote": sentence})
                break
    return out


def street_of(address: str | None) -> str | None:
    """'5875 Collins Ave, Miami Beach, FL 33140' → 'Collins Ave'."""
    first = (address or "").split(",")[0].strip()
    m = re.match(r"^\d+[A-Za-z]?(?:-\d+)?\s+(.+)$", first)
    return m.group(1).strip() if m else None


def analyze(neighbors: Iterable[dict[str, Any]], *, own_reviews: list[dict] | None = None) -> dict[str, Any]:
    """neighbors: {"name", "distance_m", "reviews": [{"text","published","relative"}]} → zone summary (pure)."""
    mentions: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    names: list[dict[str, Any]] = []
    scanned = 0
    for nb in neighbors:
        scanned += 1
        if nb.get("name"):
            names.append({"name": nb["name"], "distance_m": nb.get("distance_m")})
        for rv in nb.get("reviews") or []:
            for c in scan_conditions(rv.get("text") or ""):
                conditions.append({**c, "building": nb.get("name"), "distance_m": nb.get("distance_m"),
                                   "date": rv.get("published", "")[:7] if rv.get("published") else rv.get("relative")})
            for hit in scan_review(rv.get("text") or ""):
                mentions.append({**hit, "building": nb.get("name"), "distance_m": nb.get("distance_m"),
                                 "date": rv.get("published", "")[:7] if rv.get("published") else rv.get("relative")})
    for rv in own_reviews or []:
        for hit in scan_review(rv.get("text") or ""):
            mentions.append({**hit, "building": "este edificio", "distance_m": 0,
                             "date": rv.get("published", "")[:7] if rv.get("published") else rv.get("relative")})
    counts = Counter(m["weakness"] for m in mentions)
    summary: dict[str, Any] = {"neighbors_scanned": scanned, "mentions": mentions[:12], "weaknesses": dict(counts),
                               "vendors": sorted({m["vendor"] for m in mentions if m.get("vendor")}),
                               "conditions": conditions[:12],
                               "neighbor_names": sorted(names, key=lambda n: n["distance_m"] if n["distance_m"] is not None
                                                        else 1e9)[:6]}
    if counts:
        top, n = counts.most_common(1)[0]
        buildings = {m["building"] for m in mentions if m["weakness"] == top}
        summary.update({"top_weakness": top, "top_weakness_es": WEAKNESSES[top][1], "argument": WEAKNESSES[top][2],
                        "buildings_with_top": len(buildings)})
    return summary


def _names(items: list[str], n: int = 2) -> str:
    uniq = list(dict.fromkeys(i for i in items if i))[:n]
    return " y ".join(uniq)


def hook_line(summary: dict[str, Any], *, address: str | None = None, own_name: str | None = None) -> str | None:
    """Zone line for the setter, always in the CRM's "Zona: <finding> → argumento: <argument>." shape:

      1. neighbours' reviews show a weakness of a cleaning vendor (never names the vendor);
      2. neighbours' reviews describe physical / exterior-maintenance problems (mold, stains, facade, pool deck,
         walkways, parking, renovation, 40-year inspection) — names up to two of those buildings;
      3. otherwise, proximity: up to two real neighbouring buildings found by Places in the same radius.
    """
    if not summary:
        return None
    if summary.get("top_weakness"):
        nb = [m for m in summary["mentions"] if m["weakness"] == summary["top_weakness"] and m["building"] != "este edificio"]
        where = (f"reseñas de {len({m['building'] for m in nb})} edificio(s) vecino(s)" if nb
                 else "reseñas de este edificio")
        return f"Zona: {where} dicen que {summary['top_weakness_es']} → argumento: {summary['argument']}."
    own = (own_name or "").strip().lower()
    street = street_of(address)
    conds = [c for c in summary.get("conditions") or [] if c.get("building") and c["building"].strip().lower() != own]
    if conds:
        top = Counter(c["label"] for c in conds).most_common(2)
        labels = " y ".join(l for l, _ in top)
        who = _names([c["building"] for c in conds])           # the two nearest buildings with a problem
        where = f"sobre {street} " if street else ""
        arg = CONDITION_ARGUMENT.get(top[0][0], DEFAULT_CONDITION_ARGUMENT)
        return f"Zona: reseñas de edificios vecinos {where}({who}) mencionan {labels} → argumento: {arg}."
    near = _names([n["name"] for n in summary.get("neighbor_names") or [] if n["name"].strip().lower() != own])
    if near:
        where = f"sobre {street} " if street else ""
        return f"Zona: estuvimos relevando propiedades {where}cerca de {near} → argumento: {PROXIMITY_ARGUMENT}."
    return None


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
