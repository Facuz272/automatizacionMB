from lead_engine.models import Lead
from lead_engine.qualify import contactability_problem, rule_qualify
from lead_engine.web_extract import parse_page

HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ApartmentComplex","name":"Ocean Villas",
 "telephone":"(305) 861-4000",
 "address":{"@type":"PostalAddress","streetAddress":"6000 Collins Ave","addressLocality":"Miami Beach",
            "addressRegion":"FL","postalCode":"33140"}}
</script></head>
<body>
<nav><a href="/contact-us">Contact Us</a><a href="tel:+1-305-861-4001">Call</a></nav>
<p>Our community is professionally managed by FirstService Residential.</p>
<h3>Management Office</h3>
<p>Maria Perez, Property Manager</p>
<p>Board President: John Smith</p>
<a href="mailto:manager@oceanvillas.org">Email the office</a>
<p>Questions? info@oceanvillas.org</p>
</body></html>
"""


def test_parse_page_extracts_everything():
    f = parse_page(HTML, "https://oceanvillas.org")
    assert f.emails == {"manager@oceanvillas.org": "mailto", "info@oceanvillas.org": "text"}
    assert f.phone_numbers[0] == "+13058614001"     # tel: link first
    assert "+13058614000" in f.phone_numbers       # JSON-LD
    assert f.addresses == ["6000 Collins Ave, Miami Beach, FL 33140"]
    assert ("Maria Perez", "Property Manager") in f.people
    assert ("John Smith", "Board President") in f.people
    assert f.managed_by == ["FirstService Residential"]


def _lead(**kw):
    base = dict(source="google_places", source_id="p1", strategy_kind="onsite", company_name="Ocean Villas",
                phone="+13058614000", address="6000 Collins Ave, Miami Beach, FL 33140")
    base.update(kw)
    return Lead(**base)


def test_gate_requires_e164_and_street_address():
    assert contactability_problem(_lead()) is None
    assert contactability_problem(_lead(phone=None)) == "sin teléfono E.164 válido"
    assert contactability_problem(_lead(phone="(305) 861-4000")) == "sin teléfono E.164 válido"
    assert contactability_problem(_lead(address="Miami Beach, FL")) == "dirección sin número de calle"


def test_pain_signals_and_segment():
    reviews = [{"text": "Nice pool but the garage has black mold everywhere and nobody cleans it.",
                "rating": 2, "published": None, "relative": "a month ago"},
               {"text": "Great staff", "rating": 5}]
    lead = _lead(reviews=reviews)
    rule_qualify(lead)
    assert lead.pain[0]["category"] == "mold" and lead.pain_score >= 3
    assert lead.segment == "Dolor Urgente"
    assert "moho" in lead.sales_strategy and "black mold" in lead.sales_strategy

    calm = _lead()
    rule_qualify(calm)
    assert calm.segment == "Gran Comunidad"
    assert calm.sales_strategy


def test_crm_payload_contract():
    lead = _lead(contact_name="Maria Perez", contact_title="Property Manager", email="manager@oceanvillas.org",
                 management_company="FirstService Residential", management_type="third_party",
                 google_rating=4.4, total_reviews=120, website_url="https://oceanvillas.org")
    rule_qualify(lead)
    p = lead.crm_payload("le2-test")
    allowed = {"company_name", "phone", "segment", "contact_name", "contact_title", "management_company", "email",
               "address", "website_url", "maps_url", "google_rating", "total_reviews", "strategy_kind",
               "sales_strategy", "properties_count", "source", "import_batch_id"}
    assert set(p) <= allowed
    assert p["phone"] == "+13058614000" and p["segment"] in ("Dolor Urgente", "Gran Comunidad")
    assert (p["contact_title"], p["management_company"]) == ("Property Manager", "FirstService Residential")
    assert (p["source"], p["import_batch_id"], p["strategy_kind"]) == ("lead-engine-v2:google_places", "le2-test", "onsite")
    # hook first; no trace tags or folded contact facts (the brief only adds physical/phone/score facts)
    assert p["sales_strategy"].startswith(lead.sales_strategy)
    assert "[LE2" not in p["sales_strategy"] and "Administra:" not in p["sales_strategy"]
    assert lead.crm_payload("le2-test", include_brief=False)["sales_strategy"] == lead.sales_strategy


def test_onsite_without_company_gets_label():
    lead = _lead(management_type="onsite")
    rule_qualify(lead)
    assert lead.crm_payload("b")["management_company"] == "Administración en el edificio"


def test_cities_and_state_from_env(monkeypatch):
    from lead_engine import config

    monkeypatch.setenv("TARGET_CITIES", "Naples, Fort Myers")
    assert config.cities_for("onsite") == ["Naples", "Fort Myers"]
    monkeypatch.setenv("TARGET_CITIES_PORTFOLIO", "Tampa")
    assert config.cities_for("portfolio") == ["Tampa"]
    monkeypatch.delenv("TARGET_CITIES")
    assert config.cities_for("onsite")[0] == "Miami Beach"
    monkeypatch.setenv("TARGET_STATE", "ga")
    assert config.Settings().target_state == "GA"
    assert contactability_problem(_lead(), "GA") == "dirección fuera de GA"


def test_ai_apply_drops_unsupported_facts():
    from lead_engine.ai_enrich import Enrichment, apply

    lead = _lead()
    evidence = {"site_text": "Contact Maria Perez, LCAM", "candidate_emails": ["manager@oceanvillas.org"]}
    apply(lead, Enrichment(contact_name="Maria Perez", contact_title="LCAM", email="manager@oceanvillas.org",
                           management_company=None, management_type="onsite", properties_count=None,
                           segment="Gran Comunidad", strategy_kind="onsite", sales_strategy="Pedir por Maria.",
                           confidence="high"), evidence)
    assert (lead.contact_name, lead.email, lead.sales_strategy) == ("Maria Perez", "manager@oceanvillas.org", "Pedir por Maria.")

    lead2 = _lead()
    apply(lead2, Enrichment(contact_name="Ghost Person", contact_title="CAM", email="ghost@nowhere.com",
                            management_company="Made Up Mgmt", management_type="third_party", properties_count=40,
                            segment="Gran Comunidad", strategy_kind="onsite", sales_strategy="Pedir por Ghost.",
                            confidence="low"), evidence)
    assert lead2.contact_name is None and lead2.email is None and lead2.management_company is None
    assert lead2.management_type == "unknown" and lead2.properties_count is None
    assert lead2.sales_strategy is None  # rule_qualify will write the template hook
