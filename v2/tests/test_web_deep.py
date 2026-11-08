"""Labelled phones, hydrated JSON state (SPA), Playwright rendering, email MX, AI hook guard."""

import pytest

from lead_engine import email_verify, render
from lead_engine.ai_enrich import unsupported_hook
from lead_engine.models import Lead
from lead_engine.pipeline import choose_primary_phone, merge_phones, pick_email
from lead_engine.web_extract import label_phone, looks_like_spa, parse_page

PHONES_HTML = """
<html><body>
<div class="contact">
  <p>Front Desk / Concierge: <a href="tel:3059321000">(305) 932-1000</a></p>
  <p>Management Office <a href="tel:+1 305 932 1001">305-932-1001</a></p>
  <p>Fax: 305-932-1002</p>
  <p>Leasing &amp; Sales (305) 932-1003</p>
  <p>Oficina de Administración: 305.932.1004</p>
</div>
</body></html>
"""


def test_phone_labels_from_context():
    f = parse_page(PHONES_HTML)
    labels = {p["number"]: p["label"] for p in f.phones}
    assert labels["+13059321000"] == "front_desk"
    assert labels["+13059321001"] == "management"
    assert labels["+13059321002"] == "fax"
    assert labels["+13059321003"] == "leasing"
    assert labels["+13059321004"] == "management"
    assert f.best_phone()["number"] in ("+13059321001", "+13059321004")
    assert label_phone("Call our HOA board at") == "hoa"
    assert label_phone("After-hours emergency maintenance line") == "maintenance"


def test_primary_phone_prefers_management_over_google_and_front_desk():
    lead = Lead(source="google_places", source_id="x", strategy_kind="onsite", company_name="Tower",
                phone="+13059321000", address="1 Collins Ave, Miami Beach, FL 33139",
                phones=[{"number": "+13059321000", "label": "google_main", "source": "google_places", "context": ""}])
    merge_phones(lead, parse_page(PHONES_HTML))
    choose_primary_phone(lead)
    assert lead.phone in ("+13059321001", "+13059321004")
    assert lead.evidence["phone_replaced"]["from"] == "+13059321000"
    # Google's number turned out to be the front desk: kept as secondary with that label, fax never kept.
    assert {p["label"] for p in lead.secondary_phones()} >= {"front_desk", "leasing"}
    assert all(p["label"] != "fax" for p in lead.phones)
    assert "front desk +13059321000" in lead.brief()


NEXT_HTML = """
<html><head><title>Bay Tower</title></head><body><div id="__next"></div>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"building":{"name":"Bay Tower","managedBy":"Managed by KW Property Management.",
 "staff":[{"name":"Laura Gomez","title":"Community Association Manager","email":"lgomez@baytower.org"},
          {"name":"Front Desk","phone":"305-555-1212"}],
 "contacts":[{"label":"Management Office","phone":"(786) 220-3300"}],
 "amenities":["Resort-style pool deck","Tennis courts"]}}}}
</script>
<script src="/_next/static/chunks/main.js"></script></body></html>
"""


def test_next_data_is_read_when_html_is_an_empty_shell():
    f = parse_page(NEXT_HTML, "https://baytower.org")
    assert f.embedded_json
    assert ("Laura Gomez", "Community Association Manager") in f.people
    assert "lgomez@baytower.org" in f.emails
    assert {"number": "+17862203300", "label": "management"}.items() <= next(
        p for p in f.phones if p["number"] == "+17862203300").items()
    assert "KW Property Management" in f.managed_by
    assert "Tennis courts" in f.text


def test_window_state_and_spa_detection():
    html = ('<div id="root"></div><script>window.__INITIAL_STATE__ = {"office":{"type":"Property Manager",'
            '"phone":"3056660142"}};</script><script src=a.js></script>')
    f = parse_page(html)
    assert f.embedded_json and f.phones[0]["label"] == "management"
    assert looks_like_spa('<div id="root"></div><script src=1></script>', parse_page("<div id=root></div>"))
    assert not looks_like_spa("<p>" + "word " * 200 + "</p>", parse_page("<p>" + "word " * 200 + "</p>"))


@pytest.mark.skipif(not render.available(), reason="playwright not installed")
def test_playwright_renders_js_content(monkeypatch):
    import os

    from lead_engine import config
    path = os.getenv("PLAYWRIGHT_CHROMIUM_PATH") or ("/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None)
    monkeypatch.setattr(render, "settings", type(config.settings)(**{**config.settings.__dict__, "playwright_chromium_path": path}))
    html = ('<div id="app"></div><script>document.getElementById("app").innerHTML = '
            '"<p>Professionally managed by Castle Group.</p><p>Ana Diaz, Property Manager</p>";</script>')
    out = render.render(html=html)
    render.close()
    if out is None:
        pytest.skip("no Chromium available")
    f = parse_page(out)
    assert "Castle Group" in f.managed_by and ("Ana Diaz", "Property Manager") in f.people


def test_email_mx_selection(monkeypatch):
    statuses = {"deadtypo.cmo": "nxdomain", "oceanvillas.org": "mx", "nomail.com": "no_mail"}
    monkeypatch.setattr(email_verify, "domain_status", lambda d, timeout=4.0: statuses.get(d, "a_only"))
    lead = Lead(source="s", source_id="x", strategy_kind="onsite", company_name="Ocean Villas", domain="oceanvillas.org",
                email="pm@deadtypo.cmo", email_source="apollo")
    pick_email(lead, {"info@oceanvillas.org": "text", "x@nomail.com": "text"}, check_mx=True)
    assert (lead.email, lead.email_status, lead.email_verified) == ("info@oceanvillas.org", "mx", True)
    assert lead.email_source == "text" and lead.evidence["emails_dropped_dns"] == ["pm@deadtypo.cmo"]
    dead = Lead(source="s", source_id="y", strategy_kind="onsite", company_name="Z", email="a@nomail.com")
    pick_email(dead, {}, check_mx=True)
    assert dead.email is None and dead.email_status == "no_mail"
    kept = Lead(source="s", source_id="z", strategy_kind="onsite", company_name="Z", email="a@other.com")
    pick_email(kept, {}, check_mx=True)
    assert kept.email_status == "a_only" and kept.crm_payload("b")["email"] == "a@other.com"


def test_email_status_real_resolver_shapes(monkeypatch):
    dns = pytest.importorskip("dns.resolver")
    email_verify.domain_status.cache_clear()

    class R:
        lifetime = 0

        def resolve(self, domain, rtype):
            if domain == "gone.example":
                raise dns.NXDOMAIN()
            if rtype == "MX" and domain == "web-only.example":
                raise dns.NoAnswer()
            if rtype == "MX":
                return [type("A", (), {"exchange": "mx1.mail.example."})()]
            return ["1.2.3.4"]

    monkeypatch.setattr(dns, "Resolver", R)
    email_verify.mx_supported.cache_clear()
    assert email_verify.domain_status("mail.example") == "mx"
    assert email_verify.domain_status("web-only.example") == "a_only"
    assert email_verify.domain_status("gone.example") == "nxdomain"
    email_verify.domain_status.cache_clear()
    email_verify.mx_supported.cache_clear()

    class Blind(R):   # resolver that never answers MX (captive DNS)
        def resolve(self, domain, rtype):
            if rtype == "MX" and domain != "gone.example":
                raise dns.NoAnswer()
            return super().resolve(domain, rtype)

    monkeypatch.setattr(dns, "Resolver", Blind)
    assert email_verify.domain_status("web-only.example") == "unchecked"
    assert email_verify.domain_status("gone.example") == "nxdomain"
    email_verify.domain_status.cache_clear()
    email_verify.mx_supported.cache_clear()


def test_ai_hook_guard():
    ev = {"building": {"amenities": ["pool_deck"], "coastal": True}, "pain_signals": [], "reviews": [], "site_text": ""}
    assert unsupported_hook("Pedir por Ana; deck de piscina expuesto al salitre.", ev) is None
    assert unsupported_hook("Ofrecer limpieza del garaje multinivel.", ev) == "parking_garage"
    assert unsupported_hook("Reseña reciente menciona moho.", ev) == "review"
    ev2 = {"building": {"amenities": []}, "site_text": "covered parking garage", "pain_signals": [], "reviews": []}
    assert unsupported_hook("Ofrecer limpieza del garage.", ev2) is None
    assert unsupported_hook("Fachada expuesta al salitre.", ev2) == "coastal"
