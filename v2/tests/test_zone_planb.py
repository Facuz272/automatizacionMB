"""Zone hook levels (vendor weakness → physical problems → proximity) and the tactical Plan B (no network)."""

import pytest

from lead_engine import competitor_intel as ci, phone_resolver, pipeline
from lead_engine.models import Lead


@pytest.mark.parametrize("text,labels", [
    ("The pool deck is filthy and the walkways have mold everywhere.",
     {"moho y algas", "suciedad y manchas", "pool deck", "pasillos y aceras"}),
    ("Beautiful pool deck and lovely walkways.", set()),
    ("Maintenance is terrible, the garage leaks.", {"mantenimiento", "garaje"}),
    ("Special assessment for the 40-year recertification, facade needs paint.",
     {"recertificación / inspección", "fachada"}),
    ("La piscina está sucia y hay moho en los pasillos.", {"suciedad y manchas", "moho y algas", "pool deck",
                                                          "pasillos y aceras"}),
])
def test_scan_conditions(text, labels):
    assert {c["label"] for c in ci.scan_conditions(text)} == labels


ADDR = "5875 Collins Ave, Miami Beach, FL 33140"


def test_vendor_weakness_still_wins():
    s = ci.analyze([{"name": "Akoya", "reviews": [
        {"text": "After the pressure washing the mold came back within two weeks. Pool deck is dirty."}]}])
    assert ci.hook_line(s, address=ADDR).startswith("Zona: reseñas de 1 edificio(s) vecino(s) dicen que el moho")


def test_physical_problems_name_two_real_neighbours_and_the_street():
    s = ci.analyze([
        {"name": "Akoya Condominium", "distance_m": 200, "reviews": [{"text": "Black mold in the stairs."}]},
        {"name": "Blue Diamond", "distance_m": 300, "reviews": [{"text": "Mold on the balconies, walkways are dirty."}]},
        {"name": "Third Tower", "distance_m": 900, "reviews": [{"text": "Mold."}]}])
    line = ci.hook_line(s, address=ADDR)
    assert line.startswith("Zona: reseñas de edificios vecinos sobre Collins Ave (Akoya Condominium y Blue Diamond) "
                           "mencionan moho y algas")
    assert "Third Tower" not in line and "→ argumento: el softwash elimina moho y algas" in line


def test_proximity_hook_never_names_the_building_itself():
    s = ci.analyze([{"name": "MEi Condominium Association, Inc.", "distance_m": 0, "reviews": []},
                    {"name": "Akoya Condominium", "distance_m": 250, "reviews": [{"text": "Great staff"}]},
                    {"name": "Blue Diamond", "distance_m": 400, "reviews": []}])
    line = ci.hook_line(s, address=ADDR, own_name="MEi Condominium Association, Inc.")
    assert line == ("Zona: estuvimos relevando propiedades sobre Collins Ave cerca de Akoya Condominium y Blue Diamond "
                    "→ argumento: ofrecer la inspección gratuita aprovechando el relevamiento de la zona.")


def test_street_of():
    assert ci.street_of(ADDR) == "Collins Ave" and ci.street_of("Collins Ave, Miami") is None


def _lead(**kw):
    l = Lead(source="places", source_id="p:1", strategy_kind="onsite", company_name="Blue Tower",
             address=ADDR, phone="+13058678222")
    for k, v in kw.items():
        setattr(l, k, v)
    return l


def test_plan_b_uses_the_firm_line_when_it_differs(monkeypatch):
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": "+19549262921", "firm": "Castle Group"})
    l = _lead(management_company="Castle Management LLC", management_type="third_party")
    pipeline.apply_plan_b_fallback(l)
    assert l.plan_b_fallback == {"text": "administradora Castle Group (pedir por el Property Manager del edificio)",
                                 "phone": "+19549262921", "source": "management_firm"}
    assert "Plan B: administradora Castle Group (pedir por el Property Manager del edificio) +19549262921" in l.brief()


def test_plan_b_tactical_when_the_firm_line_is_the_lead_phone_or_unknown(monkeypatch):
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": "+13058678222", "firm": "X"})
    l = _lead(management_company="X Mgmt", management_type="third_party")
    pipeline.apply_plan_b_fallback(l)
    assert l.plan_b_fallback["source"] == "tactical"
    assert "Plan B: pedir en recepción/conserjería por el Chief Engineer / Director of Facilities / In-House Maintenance" \
        in l.brief()
    plaza = _lead(vertical="plaza", management_type="onsite")
    pipeline.apply_plan_b_fallback(plaza)
    assert plaza.plan_b_fallback["text"].endswith("Property Manager / Facilities Manager del centro")


def test_plan_b_commercial_operator_and_real_contact_first(monkeypatch):
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: pytest.fail("operator line is enough"))
    l = _lead(vertical="plaza", management_company="RK Centers", management_type="third_party",
              evidence={"commercial_operator": {"name": "RK Centers", "phone": "+13059494110", "phone_scope": "florida"}})
    pipeline.apply_plan_b_fallback(l)
    assert l.plan_b_fallback["phone"] == "+13059494110"
    named = _lead()
    named.contact_name = "Ana Diaz"
    named.add_contact("Ana Diaz", "Property Manager", "website")
    named.add_contact("Carlos Ruiz", "Chief Engineer", "website")
    pipeline.apply_plan_b_fallback(named)
    assert named.plan_b_fallback == {} and "Plan B: Chief Engineer Carlos Ruiz" in named.brief()
