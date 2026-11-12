"""CRM feedback loop (no network): /api/public/lead-outcomes responses, bad phones, buildings reopened and
re-phoned with another number, external management → firm line, conversion metrics and mix weights."""

import pytest
import requests

from lead_engine import daily, feedback, phone_resolver, pipeline
from lead_engine.config import settings
from lead_engine.models import Lead
from lead_engine.normalize import phone_key
from lead_engine.state import CONSUMED, Ledger
from test_pipeline_e2e import wired  # noqa: F401  (fixture)

BAD = "+13054271100"
GOOD = "+13054271199"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def crm(monkeypatch):
    """Simulated CRM: `crm.pages` is the list of responses served in order; `crm.calls` the params seen."""
    object.__setattr__(settings, "crm_webhook_secret", "s")
    state = type("CRM", (), {"pages": [], "calls": []})()

    def get(url, params=None, headers=None, timeout=None):
        assert url.endswith("/api/public/lead-outcomes") and headers["x-webhook-secret"] == "s"
        state.calls.append(dict(params or {}))
        nxt = state.pages.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    monkeypatch.setattr(feedback.requests, "get", get)
    return state


def _tower(phone=BAD, **kw):
    lead = Lead(source="dbpr", source_id="dbpr:4410", strategy_kind="onsite", company_name="Olympus",
                address="5600 Collins Ave, Miami Beach, FL 33140", phone=phone)
    for k, v in kw.items():
        setattr(lead, k, v)
    return lead


def _imported(lead):
    Ledger().mark_seen(lead, "imported", "le2-1")
    assert Ledger().should_skip(lead)


def outcome(**kw):
    base = {"id": "crm-1", "batch_id": "le2-1", "source": "lead-engine-v2:dbpr", "segment": "Gran Comunidad",
            "phone": BAD, "status": "closed", "call_attempts": 3, "no_answer_streak": 0, "discard_reason": None,
            "objection": None, "inspection_scheduled": False, "closed_won": False}
    return {**base, **kw}


# ── endpoint tolerance ──────────────────────────────────────────────────────

def test_404_is_only_a_warning(crm):
    crm.pages = [Resp(404)]
    rep = feedback.sync()
    assert rep["ok"] is False and "404" in rep["why"]
    assert Ledger().get_state("outcomes_since") is None            # nothing advanced


@pytest.mark.parametrize("failure", [requests.ConnectionError("reset"), Resp(500), Resp(200, ValueError("html")),
                                     Resp(200, {"unexpected": True})])
def test_any_failure_keeps_the_run_going(crm, failure):
    crm.pages = [failure]
    assert feedback.sync()["ok"] is False


def test_no_secret_skips_without_calling():
    object.__setattr__(settings, "crm_webhook_secret", None)
    assert feedback.sync()["skipped"]


def test_crm_contract_pages_with_since_and_after_id(crm, monkeypatch):
    """docs/LEAD_OUTCOMES.md in the CRM: {ok, count, leads, next_since, next_after_id}; full page → next page."""
    monkeypatch.setattr(feedback, "PAGE", 1)
    u1, u2 = "27832d29-e23a-4be5-ab1d-8a5d7f37ec02", "bbb36e50-a76a-4994-b8c2-4331524937e5"
    crm.pages = [Resp(200, {"ok": True, "count": 1, "leads": [outcome(id="a", changed_at="2026-11-01T10:00:00+00:00")],
                            "next_since": "2026-11-01T10:00:00+00:00", "next_after_id": u1}),
                 Resp(200, {"ok": True, "count": 0, "leads": [], "next_since": "2026-11-01T10:00:00+00:00",
                            "next_after_id": u1})]
    rep = feedback.sync()
    assert rep["ok"] and rep["fetched"] == 1
    assert crm.calls == [{"limit": 1}, {"since": "2026-11-01T10:00:00+00:00", "after_id": u1, "limit": 1}]
    led = Ledger()
    assert (led.get_state("outcomes_since"), led.get_state("outcomes_after_id")) == ("2026-11-01T10:00:00+00:00", u1)
    assert led.conn.execute("SELECT updated_at, seed FROM crm_outcomes WHERE crm_id='a'").fetchone() == (
        "2026-11-01T10:00:00+00:00", "dbpr")
    crm.pages = [Resp(200, {"ok": True, "count": 1, "leads": [outcome(id="b")], "next_since": "2026-11-02T00:00:00Z",
                            "next_after_id": u2})]
    monkeypatch.setattr(feedback, "PAGE", 500)
    feedback.sync()                                                   # next run continues from the stored cursor
    assert crm.calls[-1] == {"since": "2026-11-01T10:00:00+00:00", "after_id": u1, "limit": 500}
    assert led.get_state("outcomes_after_id") == u2
    crm.pages = [Resp(503)]
    feedback.sync()
    assert led.get_state("outcomes_after_id") == u2                   # a failure never moves the cursor


def test_other_shapes_are_accepted(crm):
    crm.pages = [Resp(200, [outcome(id="x", import_batch_id="le2-9", batch_id=None, source="lead_engine_v2")])]
    assert feedback.sync()["fetched"] == 1
    assert Ledger().conn.execute("SELECT batch_id, seed FROM crm_outcomes").fetchone() == ("le2-9", None)


@pytest.mark.parametrize("raw,canon", [("wrong_number", "wrong_number"), ("Número equivocado", "wrong_number"),
                                       ("No es administrador", "not_management"),
                                       ("Administración remota / externa", "external_management"),
                                       ("Fuera de zona", "out_of_area"), (None, None)])
def test_discard_reasons_accept_crm_labels(raw, canon):
    assert feedback.discard_reason(raw) == canon


# ── bad phones ──────────────────────────────────────────────────────────────

def test_wrong_number_marks_the_phone_and_reopens_the_building(crm):
    lead = _tower()
    _imported(lead)
    crm.pages = [Resp(200, [outcome(discard_reason="wrong_number")])]
    rep = feedback.sync()
    assert rep["reopened"] == 1 and rep["bad_phones"][0]["reason"] == "wrong_number"
    led = Ledger()
    assert led.bad_phones_for(lead) == {phone_key(BAD)}
    assert led.seen_status(lead) not in CONSUMED and not led.should_skip(lead)     # works it again
    crm.pages = [Resp(200, [outcome(discard_reason="wrong_number")])]
    assert feedback.sync()["reopened"] == 0                                         # idempotent


def test_six_unanswered_calls_count_as_a_dead_line_but_a_booking_does_not(crm):
    _imported(_tower())
    crm.pages = [Resp(200, [outcome(no_answer_streak=5)])]
    assert feedback.sync()["bad_phones"] == []
    crm.pages = [Resp(200, [outcome(no_answer_streak=6, inspection_scheduled=True)])]
    assert feedback.sync()["bad_phones"] == []
    crm.pages = [Resp(200, [outcome(no_answer_streak=6)])]
    assert "no contesta" in feedback.sync()["bad_phones"][0]["reason"]


def test_unknown_phone_is_stored_but_not_applied(crm):
    crm.pages = [Resp(200, [outcome(phone="+17865550000", discard_reason="wrong_number")])]
    rep = feedback.sync()
    assert rep["stored"] == 1 and rep["unmatched"] == 1 and rep["bad_phones"] == []


def test_alternative_phone_is_chosen_and_the_bad_one_never_comes_back(crm, monkeypatch):
    lead = _tower()
    _imported(lead)
    crm.pages = [Resp(200, [outcome(discard_reason="wrong_number")])]
    feedback.sync()

    again = _tower(phone=None)
    places = [{"number": BAD, "label": "google_main", "source": "google_places", "context": ""},
              {"number": GOOD, "label": "unlabeled", "source": "website", "context": ""}]
    again.phones = [dict(p) for p in places]
    again.phone = BAD
    removed = pipeline.drop_bad_phones(again, Ledger().bad_phones_for(again))
    assert set(removed) == {BAD}
    assert again.phone == GOOD and all(p["number"] != BAD for p in again.phones)
    assert again.evidence["bad_phones_excluded"] == [BAD]
    # the firm backup line / Plan B never re-offer it either
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": BAD, "firm": "X", "source": "s"})
    again.management_company, again.management_type = "X Management", "third_party"
    assert pipeline.firm_line(again) is None


def test_gate_rephones_a_reopened_building(wired, crm, monkeypatch):  # noqa: F811
    """End-to-end through pipeline.run: Places still lists the wrong number; the site has another one."""
    led = pipeline.Ledger()                                   # the wired run's own state.db
    lead = _tower()
    led.mark_seen(lead, "imported", "le2-1")
    crm.pages = [Resp(200, [outcome(discard_reason="wrong_number")])]
    assert feedback.sync(ledger=led)["reopened"] == 1

    seed = _tower(phone=None, website_url="https://olympus.example")
    monkeypatch.setattr(pipeline, "_candidates", lambda *a, **k: iter([seed]))

    def resolve(l, use_search):
        l.phone = BAD
        l.phones.append({"number": BAD, "label": "google_main", "source": "google_places", "context": ""})
    monkeypatch.setattr(pipeline, "resolve_seed", resolve)
    from lead_engine.web_extract import SiteFacts
    facts = SiteFacts()
    facts.phones = [{"number": BAD, "label": "unlabeled", "source": "text", "context": ""},
                    {"number": GOOD, "label": "management", "source": "text", "context": "Management Office"}]
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: facts)
    out = pipeline.run(strategy="onsite", limit=1, sources=["dbpr"], use_ai=False, use_apollo=False, precheck=False,
                       send=False, use_search=False, use_geo=False, check_mx=False, out_dir=wired / "o")
    import json
    rows = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines() if l.strip()]
    assert rows and rows[0]["phone"] == GOOD
    assert BAD not in json.dumps(rows[0]["phones"])


# ── external management ─────────────────────────────────────────────────────

def test_external_management_prefers_the_firm_line(crm, monkeypatch):
    lead = _tower(phone="+13054271123")
    _imported(lead)
    crm.pages = [Resp(200, [outcome(phone="+13054271123", discard_reason="Administración remota / externa")])]
    rep = feedback.sync()
    assert rep["prefer_firm"] == ["Olympus"] and rep["reopened"] == 1
    again = _tower(phone="+13054271123", management_company="Castle Group",
                   phones=[{"number": "+13054271123", "label": "google_main", "source": "google_places", "context": ""}])
    assert Ledger().flags_for(again) == {"prefer_firm": "external_management"}
    monkeypatch.setattr(phone_resolver, "firm_phone",
                        lambda firm: {"number": "+19544200000", "firm": "Castle Group", "source": "catalog"})
    assert pipeline.apply_prefer_firm(again, set())
    pipeline.choose_primary_phone(again)                       # enrich re-ranks: the firm line must stay
    assert again.phone == "+19544200000" and any(p["number"] == "+13054271123" for p in again.secondary_phones())
    nofirm = _tower(phone="+13054271123")
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: None)
    assert not pipeline.apply_prefer_firm(nofirm, set()) and nofirm.phone == "+13054271123"


def test_out_of_area_and_not_management_are_reported_only(crm):
    crm.pages = [Resp(200, [outcome(id="1", discard_reason="out_of_area", address="1 Main St, Homestead, FL 33030"),
                            outcome(id="2", discard_reason="Fuera de zona", zip="33030"),
                            outcome(id="3", discard_reason="No es administrador")])]
    rep = feedback.sync()
    assert rep["out_of_area_zips"] == {"33030": 2} and rep["not_management"] == 1 and rep["reopened"] == 0
    assert "fuera de zona: 33030 (2)" in feedback.report_text(rep)


# ── conversion and mix weights ──────────────────────────────────────────────

def _samples(seed, n, booked, segment="Gran Comunidad", zip5="33140"):
    return [outcome(id=f"{seed}-{i}", source=f"lead-engine-v2:{seed}", phone=f"+1305{seed[:1] == 'd' and 6 or 7}{i:06d}",
                    segment=segment, zip=zip5, inspection_scheduled=i < booked, call_attempts=2) for i in range(n)]


def test_conversion_by_seed_segment_county():
    feedback.ingest([*map(feedback.normalize_outcome, _samples("dbpr", 40, 8)),
                     *map(feedback.normalize_outcome, _samples("pa", 10, 0, zip5="33301")),
                     feedback.normalize_outcome(outcome(id="untouched", call_attempts=0, phone="+13050000001"))])
    rows = {(r["seed"], r["county"]): r for r in feedback.conversion(by=("seed", "county"))}
    assert rows[("dbpr", "miami-dade")]["n"] == 40 and rows[("dbpr", "miami-dade")]["inspection_rate"] == 0.2
    assert rows[("pa", "broward")]["inspections"] == 0
    assert sum(r["n"] for r in rows.values()) == 50                  # the untouched lead is not a sample


def test_mix_weights_move_only_with_30_samples():
    slices = [{"name": "condos", "seed": "dbpr", "share": 0.7}, {"name": "plazas", "seed": "pa", "share": 0.3,
                                                                "vertical": "plaza"}]
    feedback.ingest([*map(feedback.normalize_outcome, _samples("dbpr", 40, 12)),
                     *map(feedback.normalize_outcome, _samples("pa", 20, 0))])
    new, notes = feedback.weighted_mix(slices)
    rate_all = 12 / 60
    assert notes[0]["multiplier"] == min(2.0, round((12 / 40) / rate_all, 2)) == 1.5
    assert notes[1]["multiplier"] == 1.0 and notes[1]["samples"] == 20     # < 30: untouched
    feedback.ingest(map(feedback.normalize_outcome, _samples("pa", 35, 0)))
    new, notes = feedback.weighted_mix(slices)
    assert notes[1]["multiplier"] == 0.5 and new[1]["share"] == pytest.approx(0.15)


# ── daily ───────────────────────────────────────────────────────────────────

def test_daily_reads_feedback_first_and_survives_a_404(crm, monkeypatch):
    crm.pages = [Resp(404)]
    monkeypatch.setattr(pipeline, "resend_pending", lambda: {"resent": [], "still_pending": []})
    s = daily.run(limit=0, slack=False)
    assert s["skipped"] and s["feedback"]["ok"] is False and "404" in s["feedback"]["why"]


def test_daily_survives_a_crashing_feedback(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    monkeypatch.setattr(feedback, "fetch_outcomes", lambda cursor: 1 / 0)
    monkeypatch.setattr(pipeline, "resend_pending", lambda: {"resent": [], "still_pending": []})
    assert daily.run(limit=0, slack=False)["feedback"]["ok"] is False


def test_slack_mentions_the_feedback():
    t = daily.slack_text({"batch": "le2-x", "committed": True, "injected": 3, "accepted": 3, "annual_usd_low": 1,
                          "annual_usd_high": 2, "needs_phone": 0, "consolidated_siblings": 0, "top": [],
                          "pdf_dir": "p", "outreach_pack": "o",
                          "feedback": {"ok": True, "fetched": 9, "bad_phones": [{}], "prefer_firm": [], "reopened": 1,
                                       "out_of_area_zips": {}, "not_management": 0},
                          "mix_weights": [{"slice": "condos", "multiplier": 1.5, "samples": 40}]}, "x")
    assert "Feedback del CRM: 9 resultados · 1 teléfonos descartados" in t and "condos x1.5 (40 muestras)" in t


# ── post-visit quote (CRM Block C) ──────────────────────────────────────────

def test_post_visit_fields_are_stored(crm):
    crm.pages = [Resp(200, {"ok": True, "count": 3, "leads": [
        outcome(id="q1", phone="+13054270001", visit_completed=True, quote_amount_usd=18500, quote_status="won",
                quote_loss_reason=None, closed_won=True),
        outcome(id="q2", phone="+13054270002", visit_completed=True, quote_amount_usd="12,400.50", quote_status="lost",
                quote_loss_reason="Price"),
        outcome(id="q3", phone="+13054270003")]}),            # an older CRM: the four keys are absent
                 ]
    assert feedback.sync()["stored"] == 3
    rows = {r[0]: r[1:] for r in Ledger().conn.execute(
        "SELECT crm_id, visit_completed, quote_amount_usd, quote_status, quote_loss_reason FROM crm_outcomes")}
    assert rows["q1"] == (1, 18500.0, "won", None)
    assert rows["q2"] == (1, 12400.5, "lost", "price")
    assert rows["q3"] == (None, None, None, None)                 # absent ≠ false


@pytest.mark.parametrize("raw,amount", [(18500, 18500.0), ("12,400.50", 12400.5), ("$900", 900.0), (None, None),
                                        ("n/a", None), (-5, None), (True, None)])
def test_quote_amounts(raw, amount):
    assert feedback._amount(raw) == amount


def test_visit_without_calls_is_a_conversion_sample():
    o = feedback.normalize_outcome(outcome(call_attempts=0, visit_completed=True))
    assert feedback.worked(o)


def test_old_state_db_gains_the_columns_and_keeps_its_rows(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE crm_outcomes (crm_id TEXT PRIMARY KEY, batch_id TEXT, source TEXT, seed TEXT, segment TEXT, "
              "county TEXT, zip TEXT, phone_key TEXT, building_key TEXT, status TEXT, call_attempts INTEGER, "
              "no_answer_streak INTEGER, discard_reason TEXT, objection TEXT, inspection_scheduled INTEGER, "
              "closed_won INTEGER, updated_at TEXT, synced_at TEXT)")
    c.execute("INSERT INTO crm_outcomes (crm_id, status) VALUES ('old', 'new')")
    c.commit()
    c.close()
    led = Ledger(path)
    cols = {r[1] for r in led.conn.execute("PRAGMA table_info(crm_outcomes)")}
    assert {"visit_completed", "quote_amount_usd", "quote_status", "quote_loss_reason"} <= cols
    assert led.conn.execute("SELECT status FROM crm_outcomes WHERE crm_id = 'old'").fetchone() == ("new",)
    feedback.ingest([feedback.normalize_outcome(outcome(id="new", visit_completed=True, quote_status="board_review",
                                                        quote_amount_usd=7300))], led)
    assert led.conn.execute("SELECT quote_status, quote_amount_usd FROM crm_outcomes WHERE crm_id='new'").fetchone() == (
        "board_review", 7300.0)
    Ledger(path)                                                  # idempotent


def test_conversion_counts_visits_quotes_and_loss_reasons():
    rows = [outcome(id=f"v{i}", phone=f"+1305427{i:04d}", call_attempts=2, visit_completed=True,
                    quote_amount_usd=10000, quote_status=s, quote_loss_reason=r, closed_won=s == "won")
            for i, (s, r) in enumerate([("won", None), ("won", None), ("lost", "price"), ("lost", None),
                                        ("board_review", None)])]
    feedback.ingest(map(feedback.normalize_outcome, rows))
    g = feedback.conversion(by=("seed",))[0]
    assert (g["visits"], g["quotes"], g["quotes_won"], g["quotes_lost"]) == (5, 5, 2, 2)
    assert g["quoted_usd"] == 50000 and g["won_usd"] == 20000 and g["quote_win_rate"] == 0.5
    assert feedback.loss_reasons() == {"price": 1, "sin motivo": 1}
