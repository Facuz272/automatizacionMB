"""Pre-sale PDF, zone competitor intelligence, visit router and Census geocoder (no network)."""

import csv
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from lead_engine import competitor_intel as ci
from lead_engine import geocode, pdf_generator
from lead_engine import route_optimizer as ro

pypdf = pytest.importorskip("pypdf")

LEAD = {
    "company_name": "Oceanside Plaza Condo", "address": "5555 Collins Ave, Miami Beach, FL 33140",
    "source_id": "A", "import_batch_id": "le2-test", "vertical": "community", "lead_score": 100,
    "contact_name": "Ana Diaz", "contact_title": "Property Manager", "management_company": "Castle Management LLC",
    "building": {"levels": 17, "units": 297, "coastal": True, "coast_distance_m": 150,
                 "amenities": {"parking_garage": "osm", "pool_deck": "osm"}},
    "cadastre": {"kind": "condominium", "folio": "0232140100001", "year_built": 1967, "units": 297, "floors": 17,
                 "building_sqft": 310037, "assessed_value": 116528282,
                 "land_use": "RESIDENTIAL - TOTAL VALUE : CONDOMINIUM - RESIDENTIAL"},
    "public_records": {"dbpr": {"association": "Oceanside Plaza Condo Assn INC", "units": 299}},
    "municipal": [{"category": "milestone", "label": "ventana legal de Milestone Inspection / recertificación",
                   "quote": "Construido en 1967 (59 años): cumple 60 años en 2027.", "source": "municipal"},
                  {"category": "municipal_permit", "label": "obra exterior con permiso reciente",
                   "quote": "Permiso 2026035798 (2026-03-31): WATERPROOFING", "source": "municipal"}],
    "pain": [{"category": "mold", "label": "moho/hongos", "quote": "Garage has black mold in the hallways.",
              "date": "hace 2 meses", "source": "review"}],
    "estimated_deal": {"low": 37000, "high": 68000,
                       "components": {"facade": [28000, 51000], "walkways": [3100, 5600], "garage": [5900, 11100]}},
    "competitor_intel": {"top_weakness": "recurrence",
                         "top_weakness_es": "el moho y las manchas vuelven a las pocas semanas después del lavado a presión",
                         "argument": "el softwash trata el moho con producto a baja presión"},
}


def _pages_and_text(path: Path) -> tuple[int, str]:
    r = pypdf.PdfReader(str(path))
    return len(r.pages), "\n".join(p.extract_text() for p in r.pages)


# ── PDF ─────────────────────────────────────────────────────────────────────

def test_pdf_has_two_pages_with_official_data_diagnosis_scope_and_range(tmp_path):
    path = pdf_generator.build(LEAD, tmp_path / "x.pdf", lang="es")
    n, text = _pages_and_text(path)
    assert n == 2
    for s in ("INFORME TÉCNICO PRELIMINAR DE PREVENTA", "Oceanside Plaza Condo", "Datos oficiales de la propiedad",
              "1967", "310.037 ft²", "US$ 116.528.282", "Miami-Dade Property Appraiser", "Ana Diaz (Property Manager)",
              "Diagnóstico técnico preliminar", "Milestone", "WATERPROOFING", "black mold", "salitre",
              "elastomérica",
              "Alcance propuesto del servicio Softwash", "Acceso por cuerdas", "US$ 37.000 – US$ 68.000",
              "Fachada (softwash)", "Inspección gratuita", "Página 2 de 2"):
        assert s in text, s


def test_pdf_stays_two_pages_with_a_lot_of_content_and_with_almost_nothing(tmp_path):
    heavy = {**LEAD, "municipal": LEAD["municipal"] * 6,
             "pain": [{**LEAD["pain"][0], "quote": "x " * 120}] * 6}
    assert _pages_and_text(pdf_generator.build(heavy, tmp_path / "h.pdf", lang="es"))[0] == 2
    bare = {"company_name": "Plaza Ñandú & Co <test>", "address": "1 Main St, Miami, FL 33101", "vertical": "plaza"}
    n, text = _pages_and_text(pdf_generator.build(bare, tmp_path / "b.pdf", lang="es"))
    assert n == 2 and "Plaza Ñandú & Co <test>" in text and "se define en la inspección" in text
    assert "aceras" in text.lower()                                  # plaza scope


def test_filenames_and_batch_order(tmp_path):
    assert pdf_generator.safe_filename("Tides On Hollywood Beach, The / #2") == "Tides_On_Hollywood_Beach_The_2"
    assert pdf_generator.safe_filename("Ñandú") == "Nandu"
    recs = [{**LEAD, "lead_score": 40}, {**LEAD, "lead_score": 90}, {**LEAD, "company_name": "Otro", "lead_score": 10}]
    files = pdf_generator.generate_batch(recs, tmp_path / "pdf", top=2, lang="es")
    assert [f.name for f in files] == ["Oceanside_Plaza_Condo.pdf", "Oceanside_Plaza_Condo_2.pdf"]
    assert all(f.exists() for f in files)


# ── competitor intel ────────────────────────────────────────────────────────

NEIGHBORS = [
    {"name": "Seacoast Tower", "distance_m": 300, "reviews": [
        {"text": "The pressure washing company came in March and the black mold came back within a few weeks. Waste of money.",
         "published": "2026-06-01T00:00:00Z"},
        {"text": "Nice pool, friendly staff."}]},
    {"name": "Blue Bay Condo", "distance_m": 900, "reviews": [
        {"text": "Building hired Sunshine Pressure Washing, they stripped the paint on our balconies. Too much pressure.",
         "relative": "3 months ago"},
        {"text": "Algae is back again two months after the power washing guys left."}]},
    {"name": "Quiet Place", "distance_m": 1200, "reviews": [{"text": "The mold came back in my unit after a leak."}]},
]


def test_scan_requires_vendor_context_and_detects_weaknesses():
    assert ci.scan_review("The mold came back in my unit after a leak.") == []        # no cleaning vendor involved
    hits = ci.scan_review("Hired Sunshine Pressure Washing; they stripped the paint. Too much pressure.")
    assert hits[0]["weakness"] == "pressure_damage" and hits[0]["vendor"] == "Sunshine Pressure Washing"
    es = ci.scan_review("La empresa de limpieza hizo hidrolavado y a los 2 meses volvió el moho.")
    assert es and es[0]["weakness"] == "recurrence"


def test_analyze_summary_and_hook_line():
    s = ci.analyze(NEIGHBORS)
    assert s["neighbors_scanned"] == 3 and s["weaknesses"]["recurrence"] == 2
    assert s["top_weakness"] == "recurrence" and s["buildings_with_top"] == 2
    assert s["vendors"] == ["Sunshine Pressure Washing"]
    line = ci.hook_line(s)
    assert line.startswith("Zona: reseñas de 2 edificio(s) vecino(s) dicen que el moho") and "softwash" in line
    assert "Sunshine" not in line                                                     # never names a competitor
    # no vendor weakness and no physical problem: the proximity hook names the neighbour
    assert ci.hook_line(ci.analyze([{"name": "x", "reviews": [{"text": "great"}]}])).startswith(
        "Zona: estuvimos relevando propiedades cerca de x")
    assert ci.hook_line(ci.analyze([])) is None


def test_neighbors_from_pool_radius_and_exclusion():
    pool = [{"id": "self", "name": "Me", "lat": 25.8357, "lng": -80.1207, "reviews": [{"text": "a"}]},
            {"id": "n1", "name": "Near", "lat": 25.8380, "lng": -80.1210, "reviews": [{"text": "b"}]},
            {"id": "n2", "name": "Far", "lat": 25.9000, "lng": -80.1200, "reviews": [{"text": "c"}]},
            {"id": "n3", "name": "NoReviews", "lat": 25.8360, "lng": -80.1205, "reviews": []}]
    nb = ci.neighbors_from_pool(25.8357, -80.1207, pool, exclude_id="self", radius_m=1500)
    assert [n["name"] for n in nb] == ["Near"] and 200 < nb[0]["distance_m"] < 300


def test_competitor_intel_reaches_the_hook(monkeypatch):
    from lead_engine import pipeline
    from lead_engine.models import Lead
    lead = Lead(source="google_places", source_id="me", strategy_kind="onsite", company_name="Me", lat=25.8357,
                lng=-80.1207, sales_strategy="Pedir por Ana y ofrecer inspección.")
    pool = [{"id": "n1", "name": "Seacoast Tower", "lat": 25.8380, "lng": -80.1210, "reviews": NEIGHBORS[0]["reviews"]}]
    pipeline.apply_competitor_intel([lead], pool)
    assert lead.competitor_intel["top_weakness"] == "recurrence"
    assert lead.sales_strategy.startswith("Pedir por Ana") and "Zona: reseñas de 1 edificio(s) vecino(s)" in lead.sales_strategy


def test_nearby_from_places_parses(monkeypatch):
    from lead_engine import config
    monkeypatch.setattr(ci, "settings", type(config.settings)(**{**config.settings.__dict__,
                                                                 "google_places_api_key": "k", "competitor_nearby": True}))

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"places": [{"id": "p", "displayName": {"text": "Seacoast"}, "location": {"latitude": 25.838, "longitude": -80.121},
                                "reviews": [{"text": {"text": "mold came back after the pressure washing"},
                                             "publishTime": "2026-05-01T00:00:00Z"}]}]}

    sent = {}
    monkeypatch.setattr(ci.requests, "post", lambda url, json, headers, timeout: sent.update(json=json, headers=headers) or R())
    out = ci.nearby_from_places(25.8357, -80.1207, 800)
    assert out[0]["name"] == "Seacoast" and out[0]["reviews"][0]["published"] == "2026-05-01T00:00:00Z"
    assert sent["json"]["locationRestriction"]["circle"]["radius"] == 800.0
    assert "places.reviews" in sent["headers"]["X-Goog-FieldMask"]


# ── route optimizer ─────────────────────────────────────────────────────────

def _s(name, lat, lng, **kw):
    return ro.Stop(name=name, address=f"{name} addr", lat=lat, lng=lng, **kw)


def test_zones():
    assert ro.zone_of(25.7650, -80.1920) == "Brickell / Downtown"
    assert ro.zone_of(25.9500, -80.1220) == "Sunny Isles / Aventura / North Miami Beach"
    assert ro.zone_of(25.7400, -80.2600) == "Coral Gables / Coconut Grove"
    assert ro.zone_of(27.0, -80.0) == "Otra zona" and ro.zone_of(None, None) == "Sin coordenadas"


def test_two_opt_never_longer_than_nearest_neighbour():
    start = (25.7617, -80.1918)
    pts = [_s(str(i), 25.76 + (i % 4) * 0.03, -80.19 - (i // 4) * 0.03) for i in range(10)]
    nn = ro.nearest_neighbour(start, pts)
    opt = ro.two_opt(start, nn)
    assert sorted(s.name for s in opt) == sorted(s.name for s in pts)
    assert ro.path_km(start, opt) <= ro.path_km(start, nn) + 1e-9


def test_optimize_splits_by_zone_and_keeps_appointments(monkeypatch):
    tz = ro.TZ
    day = date(2026, 11, 10)
    stops = [
        _s("Brickell A", 25.7650, -80.1920), _s("Brickell B", 25.7700, -80.1900),
        _s("Sunny A", 25.9450, -80.1220), _s("Sunny B", 25.9500, -80.1210), _s("Aventura C", 25.9560, -80.1390),
        _s("Gables A", 25.7400, -80.2600),
        _s("Fixed Beach", 25.8000, -80.1300, when=datetime(2026, 11, 10, 9, 30, tzinfo=tz), inspector="Tomás"),
        _s("Fixed Far", 26.1220, -80.1100, when=datetime(2026, 11, 10, 10, 35, tzinfo=tz), inspector="Tomás"),
        _s("Other day", 25.77, -80.19, when=datetime(2026, 11, 11, 9, 0, tzinfo=tz)),
        ro.Stop(name="No coords", address="Nowhere"),
    ]
    plan = ro.optimize(stops, inspectors=["Tomás", "Mateo"], start=(25.7617, -80.1918), day=day,
                       geocoder=lambda a: None)
    names = {i: [l.stop.name for l in legs] for i, legs in plan.routes.items()}
    fixed = [l.stop.name for l in plan.routes["Tomás"] if l.scheduled]
    assert fixed == ["Fixed Beach", "Fixed Far"]                                      # appointments keep their order
    assert all(l.arrive == l.stop.when for l in plan.routes["Tomás"] if l.scheduled)   # …and their time
    far = next(l for l in plan.routes["Tomás"] if l.stop.name == "Fixed Far")
    assert far.warning and "no llega a tiempo" in far.warning                        # Miami Beach -> Fort Lauderdale in 5 min
    zones_by = {i: {l.stop.zone for l in legs if not l.scheduled} for i, legs in plan.routes.items()}
    assert not (zones_by["Tomás"] & zones_by["Mateo"])                              # whole zones per inspector
    assert "Other day" not in sum(names.values(), []) and [s.name for s in plan.skipped] == ["No coords"]
    free = [l for l in plan.routes["Mateo"] if not l.scheduled]
    assert all(free[i].arrive < free[i + 1].arrive for i in range(len(free) - 1))   # suggested times increase


def test_exports(tmp_path):
    plan = ro.optimize([_s(f"S{i}", 25.76 + i * 0.01, -80.19) for i in range(12)], inspectors=["Mateo"],
                       start=(25.7617, -80.1918), day=date(2026, 11, 10))
    md = ro.to_markdown(plan)
    assert md.startswith("# Ruta de inspecciones · 10/11/2026") and "## Mateo — 12 visitas" in md
    links = ro.maps_links(plan.start, plan.routes["Mateo"])
    assert len(links) == 2 and all(u.startswith("https://www.google.com/maps/dir/?api=1&origin=") for u in links)
    assert links[0].count("%7C") == 7                                     # 8 waypoints (7 separators) in the first link
    assert "25.790000000000003" not in links[0]
    ro.to_csv(plan, tmp_path / "r.csv")
    rows = list(csv.DictReader(open(tmp_path / "r.csv", encoding="utf-8")))
    assert len(rows) == 12 and rows[0]["inspector"] == "Mateo" and rows[0]["orden"] == "1"
    assert json.loads(json.dumps(ro.to_json(plan)))["routes"]["Mateo"][0]["scheduled"] is False


def test_load_jsonl_and_crm_csv(tmp_path):
    (tmp_path / "leads.jsonl").write_text(json.dumps({"company_name": "A", "address": "1 X St", "lat": 25.8, "lng": -80.1,
                                                      "source_id": "s1", "lead_score": 80}) + "\n", encoding="utf-8")
    assert ro.load(tmp_path / "leads.jsonl")[0].ref == "s1"
    (tmp_path / "crm.csv").write_text("company_name,address,inspection_at,inspector\nB,2 Y St,2026-11-10T14:00:00,Mateo\n",
                                      encoding="utf-8")
    s = ro.load(tmp_path / "crm.csv")[0]
    assert s.when.hour == 14 and s.when.tzinfo is not None and s.inspector == "Mateo" and s.lat is None


# ── Census geocoder ─────────────────────────────────────────────────────────

def test_census_parse():
    data = {"result": {"addressMatches": [{"matchedAddress": "5555 COLLINS AVE, MIAMI BEACH, FL, 33140",
                                           "coordinates": {"x": -80.120708858323, "y": 25.835754803442}}]}}
    assert geocode.parse(data) == (25.835755, -80.120709)
    assert geocode.parse({"result": {"addressMatches": []}}) is None



def test_free_visits_fill_the_morning_before_an_afternoon_appointment():
    tz = ro.TZ
    day = date(2026, 11, 10)
    stops = [_s("Appt Sunny", 25.9450, -80.1220, when=datetime(2026, 11, 10, 14, 0, tzinfo=tz)),
             _s("Aventura", 25.9400, -80.1420), _s("Hallandale", 25.9800, -80.1200)]
    legs = ro.plan_day(stops, (25.7617, -80.1918), day)
    names = [l.stop.name for l in legs]
    assert names[-1] == "Appt Sunny" and legs[-1].arrive.hour == 14          # free visits done before 14:00
    assert legs[0].arrive.hour < 12 and not any(l.warning for l in legs)
