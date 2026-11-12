"""Phase 1 hardening: run lock, resend queue in daily, Places/Claude resilience, preflight, rendering fallback,
firm names, firm-phone cache TTL, firm backup line and the extended management catalog (no network)."""

import fcntl
from datetime import datetime, timedelta, timezone

import pytest

from lead_engine import (ai_enrich, costs, daily, mgmt_domains, phone_resolver, pipeline, preflight, render, runlock,
                         web_extract)
from lead_engine.collectors import google_places as gp
from lead_engine.config import settings
from lead_engine.models import Lead
from lead_engine.state import Ledger


# ── run lock ────────────────────────────────────────────────────────────────

def test_lock_is_reentrant_and_blocks_another_run():
    with runlock.hold("daily"):
        with runlock.hold("run onsite"):              # daily → pipeline.run in the same process
            assert runlock.lock_path().exists()
    # another process holding the lock (a second open file description behaves like one)
    other = open(runlock.lock_path(), "a+")
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    other.write("pid 999 · daily · 2026-11-12")
    other.flush()
    try:
        assert runlock.holder() == "pid 999 · daily · 2026-11-12"
        with pytest.raises(runlock.RunLocked, match="pid 999"):
            with runlock.hold("daily"):
                pass
        from lead_engine.__main__ import main
        assert main(["daily", "--limit", "0", "--no-slack"]) == 3
        assert preflight.run_state_checks()[0].status == preflight.ERROR
    finally:
        fcntl.flock(other, fcntl.LOCK_UN)
        other.close()
    assert runlock.holder() is None
    with runlock.hold("daily"):
        pass


# ── daily: resend queue first ───────────────────────────────────────────────

def test_daily_resends_pending_batches_before_a_new_one(monkeypatch, tmp_path):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    monkeypatch.setattr(pipeline, "resend_pending", lambda: {"resent": [], "still_pending": [
        {"batch_id": "le2-old", "attempts": 3, "error": "HTTP 503"}]})
    monkeypatch.setattr(pipeline, "run", lambda **kw: pytest.fail("no new batch while the CRM keeps failing"))
    monkeypatch.setattr(daily, "fetch_health", lambda: pytest.fail("not even the pool is read"))
    s = daily.run(limit=5, slack=False)
    assert s["skipped"] and "sigue sin confirmar 1 lote" in s["why"] and s["still_pending"][0]["batch_id"] == "le2-old"


def test_daily_reports_resent_batches_and_goes_on(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    monkeypatch.setattr(pipeline, "resend_pending", lambda: {"resent": [{"batch_id": "le2-old", "inserted": 7,
                                                                        "duplicates": 0}], "still_pending": []})
    s = daily.run(limit=0, slack=False)
    assert s["skipped"] and s["resent_batches"][0]["inserted"] == 7


def test_slack_text_says_nothing_was_lost_when_the_crm_failed():
    t = daily.slack_text({"batch": "le2-x", "pending_resend": True, "crm_error": "HTTP 503", "accepted": 8}, "x")
    assert "8 leads quedan en cola" in t and "nada se perdió" in t


# ── Places / Claude / costs ─────────────────────────────────────────────────

def test_places_collect_skips_a_failed_query_and_stops_after_three(monkeypatch):
    object.__setattr__(settings, "google_places_api_key", "k")
    calls = []

    first = f"{gp.QUERIES['onsite'][0]} Miami Beach"

    def search(query, token, with_reviews, page_size=20):
        calls.append(query)
        if query == first:
            raise gp.PlacesError("Places HTTP 503")
        return {"places": [{"id": query, "displayName": {"text": query}, "formattedAddress": "1 Ocean Dr, Miami, FL 33139",
                            "nationalPhoneNumber": "(305) 427-8770", "types": ["condominium_complex"]}]}
    monkeypatch.setattr(gp, "_search", search)
    got = list(gp.collect("onsite", 50, cities=["Miami Beach", "Aventura"]))
    names = {l.company_name for l in got}
    assert first not in names and any("Aventura" in n for n in names)           # the run went on
    calls.clear()
    monkeypatch.setattr(gp, "_search", lambda *a, **k: (_ for _ in ()).throw(gp.PlacesError("403 key")))
    assert list(gp.collect("onsite", 50, cities=["Miami Beach", "Aventura", "Doral"])) == []


def test_claude_client_has_an_explicit_timeout(monkeypatch):
    import anthropic
    seen = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: seen.update(kw) or object())
    monkeypatch.setattr(ai_enrich, "_client", None)
    object.__setattr__(settings, "anthropic_api_key", "sk-test")
    ai_enrich._get_client()
    assert seen["timeout"] == settings.anthropic_timeout_s == 60.0 and seen["max_retries"] == 2
    monkeypatch.setattr(ai_enrich, "_client", None)


def test_nearby_search_is_priced():
    assert costs.DEFAULT_PRICES["places_nearby_reviews"] == 0.040


# ── preflight ───────────────────────────────────────────────────────────────

def test_preflight_lists_pending_batches_and_dns(monkeypatch, tmp_path):
    Ledger().add_pending("le2-x", tmp_path, "CRM no respondió: HTTP 503")
    rows = {c.name: c for c in preflight.run_state_checks()}
    assert rows["lock de corrida"].status == preflight.OK
    assert rows["lotes pendientes de CRM"].status == preflight.PENDING and "le2-x" in rows["lotes pendientes de CRM"].detail
    from lead_engine import email_verify
    monkeypatch.setattr(email_verify, "backend", type("B", (), {"__call__": lambda s: "none", "cache_clear": lambda s: None})())
    assert preflight.run_probe("red", "DNS", preflight._dns_mx, "").status == preflight.ERROR


# ── rendering ───────────────────────────────────────────────────────────────

def test_installed_chromium_builds_are_found(tmp_path, monkeypatch):
    exe = tmp_path / "chromium-1243" / "chrome-linux64" / "chrome"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert str(exe) in render.fallback_executables()
    assert settings.render_js == "auto"                                        # default for normal runs


# ── management firm names ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("The building is managed by a volunteer board of directors.", []),
    ("Managed by the owners association.", []),
    ("Professionally managed by Castle Group.", ["Castle Group"]),
    ("managed by: The Continental Group\n", ["The Continental Group"]),
    ("Managed by our board.", []),
])
def test_managed_by_never_stores_descriptions(text, expected):
    assert web_extract.parse_page(f"<html><body><p>{text}</p></body></html>", "https://x.org").managed_by == expected


def test_ai_management_descriptions_are_not_companies():
    lead = Lead(source="s", source_id="x", strategy_kind="onsite", company_name="Point East One")
    result = ai_enrich.Enrichment.model_construct(
        **{**{f: None for f in ai_enrich.Enrichment.model_fields}, "management_company": "a volunteer",
           "management_type": "self_managed", "contact_name": None, "email": None, "properties_count": None})
    try:
        ai_enrich.apply(lead, result, {"site_text": "managed by a volunteer board", "candidate_emails": []})
    except Exception:                                                        # noqa: BLE001 - other fields irrelevant
        pass
    assert lead.management_company is None and lead.evidence.get("ai_management_description") == "a volunteer"


# ── firm phones ─────────────────────────────────────────────────────────────

def test_firm_phone_cache_expires_negatives_after_a_week():
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    ago = lambda d: (now - timedelta(days=d)).isoformat()  # noqa: E731
    assert phone_resolver.fresh(None, ago(3), now) and not phone_resolver.fresh(None, ago(8), now)
    assert phone_resolver.fresh("+19549262921", ago(60), now) and not phone_resolver.fresh("+19549262921", ago(95), now)
    assert not phone_resolver.fresh(None, None, now) and not phone_resolver.fresh(None, "garbage", now)


def _lead(**kw):
    l = Lead(source="dbpr", source_id="dbpr:1", strategy_kind="portfolio", company_name="Opera Tower",
             address="1750 N Bayshore Dr, Miami, FL 33132", phone="+13055031000")
    for k, v in kw.items():
        setattr(l, k, v)
    return l


def test_firm_line_is_a_backup_phone_never_the_primary(monkeypatch):
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": "+19549262921", "firm": "FirstService"})
    l = _lead(management_company="FirstService Residential", management_type="third_party",
              phones=[{"number": "+13055031000", "label": "google_main", "source": "google_places", "context": ""}])
    pipeline.apply_firm_backup_phone(l)
    pipeline.choose_primary_phone(l)
    assert l.phone == "+13055031000" and l.phones[-1]["label"] == "firm_central"
    pipeline.apply_plan_b_fallback(l)
    brief = l.brief()
    assert "Plan B: administradora FirstService (pedir por el Property Manager del edificio) +19549262921" in brief
    assert brief.count("+19549262921") == 1                                   # not repeated in "otros tel."
    onsite = _lead(management_type="onsite")
    pipeline.apply_firm_backup_phone(onsite)
    assert onsite.phones == []


def test_office_lines_beat_google_and_concierge():
    l = _lead(phones=[{"number": "+13055031000", "label": "google_main", "source": "google_places", "context": ""},
                      {"number": "+13055031001", "label": "front_desk", "source": "text", "context": "Concierge"},
                      {"number": "+13055031002", "label": "management", "source": "text", "context": "Management Office"}])
    pipeline.choose_primary_phone(l)
    assert l.phone == "+13055031002"
    assert web_extract.label_phone("Concierge desk") == "front_desk"


# ── catalog ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,domain", [
    ("Gold Property Management & Associates INC", "goldpropertymanagement.com"),
    ("GRS Community Management", "grsmgt.com"),
    ("Unlimited Property Management", "unlimitedpm.com"),
    ("Benchmark Property Management, INC.", "benchmarkpm.com"),
    ("L&C Royal Management Corporation", "lcroyal.com"),
    ("Skrld, INC.", None), ("Fp Management INC", None), ("GRS Management, INC.", None),
])
def test_catalog_only_holds_verified_firms(name, domain):
    hit = mgmt_domains.from_catalog(name)
    assert (hit[1] if hit else None) == domain


def test_manual_import_commit_consumes_the_batch_and_clears_the_queue(tmp_path, monkeypatch):
    import json as _json

    from lead_engine import crm_importer
    batch = tmp_path / "le2-x"
    batch.mkdir()
    lead = Lead(source="dbpr", source_id="dbpr:7", strategy_kind="onsite", company_name="Blue Tower",
                address="1 Ocean Dr, Miami Beach, FL 33139", phone="+13054278770")
    (batch / "leads.jsonl").write_text(_json.dumps(lead.internal_record()) + "\n")
    (batch / "crm_payload.json").write_text(_json.dumps({"import_batch_id": "le2-x", "leads": [lead.crm_payload("le2-x")]}))
    Ledger().add_pending("le2-x", batch, "HTTP 503")
    monkeypatch.setattr(crm_importer, "import_leads", lambda leads, batch_id, dry_run, batch_size=200, **k:
                        crm_importer.ImportReport(batch_id=batch_id, dry_run=dry_run, sent=1, inserted=1))
    assert crm_importer.main([str(batch / "crm_payload.json"), "--commit"]) == 0
    assert Ledger().pending() == [] and Ledger().should_skip(lead)
