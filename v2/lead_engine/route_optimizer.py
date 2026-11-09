"""Visit router for on-site inspections (no paid API).

Input: leads.jsonl from a batch, or a CRM export (CSV/JSON) with at least company_name + address;
optional lat/lng (missing ones are geocoded with the free Census geocoder), inspection_at
(ISO datetime, America/New_York when naive) and inspector.

1. Each stop gets a Miami zone (nearest zone centre within 12 km: Brickell / Downtown,
   Miami Beach, Sunny Isles / Aventura, Coral Gables / Grove, Doral, Fort Lauderdale…).
2. Unassigned stops are split between the inspectors by whole zones: each zone goes to the
   inspector whose appointments that day are closest (balanced by visit count), so nobody
   crosses Miami twice.
3. Per inspector and day:
     * scheduled visits keep their appointment times (chronological); a leg whose travel doesn't
       fit the free time is flagged;
     * unscheduled visits are inserted (nearest-neighbour order) where they add the least distance
       without making any appointment late — so morning gaps before a 14:00 visit get filled — and
       the tail after the last appointment is improved with 2-opt. Suggested times start at
       DAY_START with VISIT_MINUTES per visit.
Travel time = straight-line distance × ROAD_FACTOR / AVG_KMH (urban Miami defaults).
Exports: CSV, a Markdown itinerary for WhatsApp, and Google Maps directions links (≤ 10 stops
per link, the limit of the URL API).
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .geo import haversine_m

TZ = ZoneInfo("America/New_York")
ROAD_FACTOR = float(os.getenv("ROUTE_ROAD_FACTOR", "1.35"))
AVG_KMH = float(os.getenv("ROUTE_AVG_KMH", "25"))
VISIT_MINUTES = int(os.getenv("ROUTE_VISIT_MINUTES", "60"))        # the CRM books inspections as 60 min
DAY_START = os.getenv("ROUTE_DAY_START", "09:00")
ZONE_MAX_KM = 12

ZONES: list[tuple[str, float, float]] = [
    ("Brickell / Downtown", 25.7680, -80.1936),
    ("Edgewater / Midtown / Wynwood", 25.8040, -80.1910),
    ("Miami Beach Sur", 25.7826, -80.1341),
    ("Miami Beach Centro / Norte", 25.8450, -80.1220),
    ("Surfside / Bal Harbour / Bay Harbor", 25.8840, -80.1230),
    ("Sunny Isles / Aventura / North Miami Beach", 25.9450, -80.1300),
    ("Key Biscayne", 25.6930, -80.1630),
    ("Coral Gables / Coconut Grove", 25.7330, -80.2600),
    ("Doral / Sweetwater", 25.8030, -80.3550),
    ("Kendall / Pinecrest", 25.6800, -80.3300),
    ("Hialeah / Miami Lakes", 25.8700, -80.3100),
    ("Hallandale / Hollywood", 26.0050, -80.1300),
    ("Fort Lauderdale", 26.1220, -80.1100),
]


@dataclass
class Stop:
    name: str
    address: str
    lat: float | None = None
    lng: float | None = None
    when: datetime | None = None          # scheduled appointment (aware, America/New_York)
    inspector: str | None = None
    phone: str | None = None
    contact: str | None = None
    ref: str | None = None                # source id / CRM id
    zone: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Leg:
    stop: Stop
    arrive: datetime
    depart: datetime
    travel_min: int
    km: float
    scheduled: bool
    warning: str | None = None


# ── geometry ────────────────────────────────────────────────────────────────

def zone_of(lat: float | None, lng: float | None) -> str:
    if lat is None or lng is None:
        return "Sin coordenadas"
    name, d = min(((z, haversine_m(lat, lng, zl, zg)) for z, zl, zg in ZONES), key=lambda t: t[1])
    return name if d <= ZONE_MAX_KM * 1000 else "Otra zona"


def road_km(a: Stop | tuple[float, float], b: Stop | tuple[float, float]) -> float:
    la, lo = (a.lat, a.lng) if isinstance(a, Stop) else a
    lb, lob = (b.lat, b.lng) if isinstance(b, Stop) else b
    return haversine_m(la, lo, lb, lob) / 1000 * ROAD_FACTOR


def travel_minutes(km: float) -> int:
    return int(math.ceil(km / AVG_KMH * 60)) if km > 0 else 0


def path_km(start: tuple[float, float], stops: list[Stop]) -> float:
    pts: list[Any] = [start, *stops]
    return sum(road_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def nearest_neighbour(start: tuple[float, float], stops: list[Stop]) -> list[Stop]:
    left, out, cur = list(stops), [], start
    while left:
        nxt = min(left, key=lambda s: road_km(cur, s))
        out.append(nxt)
        left.remove(nxt)
        cur = (nxt.lat, nxt.lng)
    return out


def two_opt(start: tuple[float, float], order: list[Stop], max_rounds: int = 50) -> list[Stop]:
    """Reverse segments while the open path from `start` gets shorter."""
    best, best_km = list(order), path_km(start, order)
    for _ in range(max_rounds):
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                cand = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                k = path_km(start, cand)
                if k + 1e-9 < best_km:
                    best, best_km, improved = cand, k, True
        if not improved:
            break
    return best


# ── assignment ──────────────────────────────────────────────────────────────

def _centroid(stops: list[Stop]) -> tuple[float, float]:
    return sum(s.lat for s in stops) / len(stops), sum(s.lng for s in stops) / len(stops)


def split_by_zone(stops: list[Stop], inspectors: list[str], start: tuple[float, float],
                  anchors: dict[str, list[Stop]] | None = None) -> dict[str, list[Stop]]:
    """Whole zones per inspector. Each zone goes to the inspector whose fixed appointments (or, without
    appointments, the start point) are closest, while keeping visit counts balanced (± 1)."""
    if not inspectors:
        inspectors = ["Inspector"]
    anchors = anchors or {}
    zones: dict[str, list[Stop]] = {}
    for s in stops:
        zones.setdefault(s.zone or "Otra zona", []).append(s)
    load = {i: len(anchors.get(i, [])) for i in inspectors}
    cap = math.ceil((len(stops) + sum(load.values())) / len(inspectors)) + 1
    out: dict[str, list[Stop]] = {i: [] for i in inspectors}

    def cost(insp: str, z: list[Stop]) -> float:
        c = _centroid(z)
        pts = [(a.lat, a.lng) for a in anchors.get(insp, [])] + [p for z2 in out[insp] for p in [(z2.lat, z2.lng)]]
        return min(road_km(c, p) for p in pts) if pts else road_km(c, start)

    for z in sorted(zones.values(), key=len, reverse=True):
        open_ = [i for i in inspectors if load[i] + len(z) <= cap] or inspectors
        best = min(open_, key=lambda i: (cost(i, z), load[i]))
        out[best].extend(z)
        load[best] += len(z)
    return out


# ── itinerary ───────────────────────────────────────────────────────────────

def _day_start(day: date) -> datetime:
    h, m = (int(x) for x in DAY_START.split(":"))
    return datetime.combine(day, time(h, m), TZ)


def _timeline(start: tuple[float, float], seq: list[Stop], day: date) -> tuple[list[Leg], int]:
    """Times for a sequence: appointments keep their time; free visits go as early as possible.
    Returns (legs, total minutes of lateness at appointments)."""
    legs: list[Leg] = []
    pos = start
    first = next((s.when for s in seq if s.when), None)
    t = _day_start(day)
    if first and seq and seq[0].when:
        t = min(t, first - timedelta(minutes=travel_minutes(road_km(start, seq[0]))))
    late_total = 0
    for s in seq:
        km = road_km(pos, s)
        mins = travel_minutes(km)
        ready = t + timedelta(minutes=mins)
        warn = None
        if s.when:
            if ready > s.when:
                late = int((ready - s.when).total_seconds() // 60)
                warn = f"no llega a tiempo: faltan {late} min de traslado"
                late_total += late
            arrive = s.when
            t = max(ready, s.when) + timedelta(minutes=VISIT_MINUTES)
        else:
            arrive = ready
            t = ready + timedelta(minutes=VISIT_MINUTES)
        legs.append(Leg(s, arrive, arrive + timedelta(minutes=VISIT_MINUTES), mins, round(km, 1), bool(s.when), warn))
        pos = (s.lat, s.lng)
    return legs, late_total


DAY_LENGTH_KM_PER_HOUR = float(os.getenv("ROUTE_KM_PER_HOUR_SAVED", "5"))   # 1 h shorter day ≈ 5 km more driving


def _cost(start: tuple[float, float], seq: list[Stop], legs: list[Leg], day: date) -> float:
    end = legs[-1].depart if legs else _day_start(day)
    hours = max(0.0, (end - _day_start(day)).total_seconds() / 3600)
    return path_km(start, seq) + hours * DAY_LENGTH_KM_PER_HOUR


def plan_day(stops: list[Stop], start: tuple[float, float], day: date) -> list[Leg]:
    """Appointments fixed in time; free visits inserted where they add the least distance without
    making any appointment late (morning gaps get filled), the rest after the last appointment."""
    seq = sorted([s for s in stops if s.when], key=lambda s: s.when)
    free = [s for s in stops if not s.when]
    for s in nearest_neighbour(start, free):
        base_late = _timeline(start, seq, day)[1]
        best: tuple[float, list[Stop]] | None = None
        for i in range(len(seq) + 1):
            cand = seq[:i] + [s] + seq[i:]
            legs, late = _timeline(start, cand, day)
            if late <= base_late:                       # never makes an appointment later than it already is
                c = _cost(start, cand, legs, day)
                if best is None or c < best[0]:
                    best = (c, cand)
        seq = best[1] if best else seq + [s]
    # 2-opt only on the free tail after the last appointment (appointments must keep their order).
    last_fixed = max((i for i, s in enumerate(seq) if s.when), default=-1)
    tail = seq[last_fixed + 1:]
    if len(tail) > 2:
        anchor = (seq[last_fixed].lat, seq[last_fixed].lng) if last_fixed >= 0 else start
        seq = seq[:last_fixed + 1] + two_opt(anchor, tail)
    return _timeline(start, seq, day)[0]


def maps_links(start: tuple[float, float], legs: list[Leg], per_link: int = 10) -> list[str]:
    """Google Maps directions URLs (origin + up to 8 waypoints + destination per link)."""
    pts = [f"{round(start[0], 6)},{round(start[1], 6)}"] + [f"{round(l.stop.lat, 6)},{round(l.stop.lng, 6)}" for l in legs]
    links = []
    i = 0
    while i < len(pts) - 1:
        chunk = pts[i: i + per_link]
        url = (f"https://www.google.com/maps/dir/?api=1&origin={chunk[0]}&destination={chunk[-1]}&travelmode=driving")
        if len(chunk) > 2:
            url += "&waypoints=" + quote("|".join(chunk[1:-1]), safe=",")
        links.append(url)
        i += per_link - 1
    return links


@dataclass
class Plan:
    day: date
    start: tuple[float, float]
    routes: dict[str, list[Leg]]
    skipped: list[Stop]

    def total_km(self, inspector: str) -> float:
        return round(sum(l.km for l in self.routes[inspector]), 1)


def optimize(stops: Iterable[Stop], *, inspectors: list[str], start: tuple[float, float], day: date | None = None,
             geocoder=None) -> Plan:
    day = day or datetime.now(TZ).date()
    ready, skipped = [], []
    for s in stops:
        if (s.lat is None or s.lng is None) and geocoder:
            hit = geocoder(s.address)
            if hit:
                s.lat, s.lng = hit
        if s.lat is None or s.lng is None:
            skipped.append(s)
            continue
        if s.when and s.when.date() != day:
            continue
        s.zone = zone_of(s.lat, s.lng)
        ready.append(s)
    fixed: dict[str, list[Stop]] = {i: [] for i in inspectors}
    free = []
    for s in ready:
        if s.inspector and s.inspector in fixed:
            fixed[s.inspector].append(s)
        else:
            free.append(s)
    split = split_by_zone(free, inspectors, start, anchors=fixed) if free else {i: [] for i in inspectors}
    routes = {i: plan_day(fixed[i] + split.get(i, []), start, day) for i in inspectors}
    return Plan(day, start, routes, skipped)


# ── input / output ──────────────────────────────────────────────────────────

def _parse_when(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)


def stop_from_record(r: dict[str, Any]) -> Stop | None:
    name = r.get("company_name") or r.get("name")
    addr = r.get("address")
    if not name or not addr:
        return None
    lat, lng = r.get("lat"), r.get("lng") or r.get("lon")
    return Stop(name=name, address=addr,
                lat=float(lat) if lat not in (None, "") else None, lng=float(lng) if lng not in (None, "") else None,
                when=_parse_when(r.get("inspection_at") or r.get("when")), inspector=r.get("inspector") or None,
                phone=r.get("phone") or None, contact=r.get("contact_name") or None,
                ref=r.get("source_id") or r.get("id") or None,
                extra={**{k: r.get(k) for k in ("lead_score", "estimated_deal") if r.get(k)},
                       # full engine record (leads.jsonl) → the inspector's field sheet has the building facts
                       **({"record": r} if r.get("building") or r.get("cadastre") else {})})


def load(path: str | Path) -> list[Stop]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    elif p.suffix == ".json":
        data = json.loads(text)
        rows = data.get("leads", data) if isinstance(data, dict) else data
    else:
        rows = list(csv.DictReader(text.splitlines()))
    return [s for s in (stop_from_record(r) for r in rows) if s]


def field_sheets(plan: Plan, out_dir: str | Path, stem: str) -> list[Path]:
    """One PDF per inspector with the field & costing sheet of each visit, in route order."""
    from . import inspector_sheet

    files = []
    for insp, legs in plan.routes.items():
        if not legs:
            continue
        recs = [l.stop.extra.get("record") or {"company_name": l.stop.name, "address": l.stop.address,
                                                "phone": l.stop.phone, "contact_name": l.stop.contact} for l in legs]
        whens = [l.arrive.strftime("%d/%m %H:%M") + ("" if l.scheduled else " (sugerida)") for l in legs]
        from .pdf_common import safe_filename
        files.append(inspector_sheet.build(recs, Path(out_dir) / f"{stem}_{safe_filename(insp)}_campo.pdf",
                                           inspector=insp, whens=whens))
    return files


def to_markdown(plan: Plan) -> str:
    out = [f"# Ruta de inspecciones · {plan.day.strftime('%d/%m/%Y')}"]
    for insp, legs in plan.routes.items():
        if not legs:
            continue
        zones = []
        for l in legs:
            if l.stop.zone not in zones:
                zones.append(l.stop.zone)
        out.append(f"\n## {insp} — {len(legs)} visitas · {plan.total_km(insp)} km · {' → '.join(zones)}")
        for n, l in enumerate(legs, 1):
            hora = l.arrive.strftime("%H:%M") + ("" if l.scheduled else " (sugerida)")
            line = f"{n}. {hora} · {l.stop.name} — {l.stop.address} · {l.travel_min} min de traslado ({l.km} km)"
            if l.stop.contact or l.stop.phone:
                line += f" · {l.stop.contact or ''} {l.stop.phone or ''}".rstrip()
            if l.warning:
                line += f" ⚠️ {l.warning}"
            out.append(line)
        for i, url in enumerate(maps_links(plan.start, legs), 1):
            out.append(f"Mapa {i}: {url}")
    if plan.skipped:
        out.append("\nSin coordenadas (revisar dirección): " + "; ".join(s.name for s in plan.skipped))
    return "\n".join(out) + "\n"


def to_csv(plan: Plan, path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["inspector", "orden", "hora", "agendada", "zona", "edificio", "direccion", "contacto", "telefono",
                    "traslado_min", "km", "aviso", "lat", "lng", "ref"])
        for insp, legs in plan.routes.items():
            for n, l in enumerate(legs, 1):
                w.writerow([insp, n, l.arrive.strftime("%Y-%m-%d %H:%M"), "sí" if l.scheduled else "no", l.stop.zone,
                            l.stop.name, l.stop.address, l.stop.contact or "", l.stop.phone or "", l.travel_min, l.km,
                            l.warning or "", l.stop.lat, l.stop.lng, l.stop.ref or ""])


def to_json(plan: Plan) -> dict[str, Any]:
    return {"day": plan.day.isoformat(), "start": plan.start,
            "routes": {i: [{**{k: v for k, v in asdict(l.stop).items() if k != "when"},
                            "when": l.stop.when.isoformat() if l.stop.when else None,
                            "arrive": l.arrive.isoformat(), "travel_min": l.travel_min, "km": l.km,
                            "scheduled": l.scheduled, "warning": l.warning} for l in legs]
                       for i, legs in plan.routes.items()},
            "skipped": [s.name for s in plan.skipped]}
