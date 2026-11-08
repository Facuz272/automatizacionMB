from lead_engine import costs, quality, scoring
from lead_engine.models import Lead


def _lead(**kw):
    base = dict(source="google_places", source_id="p", strategy_kind="onsite", company_name="X Condo",
                phone="+13058614000", address="1 Collins Ave, Miami Beach, FL 33139")
    base.update(kw)
    return Lead(**base)


def test_full_score_100():
    lead = _lead(contact_name="Maria Perez", contact_title="LCAM", management_type="onsite",
                 email="maria@xcondo.com", email_status="mx", building={"coastal": True},
                 phones=[{"number": "+13058614000", "label": "management", "source": "tel_link", "context": ""}])
    assert scoring.score(lead) == (100, {"decision_maker": 25, "onsite_or_pain": 25, "email_mx": 15,
                                         "exposure_size": 20, "mgmt_phone": 15})


def test_partial_credit():
    lead = _lead(contact_name="John Smith", contact_title="Board President", strategy_kind="onsite",
                 email="info@x.com", email_status="a_only", building={"levels": 5},
                 phones=[{"number": "+13058614000", "label": "hoa", "source": "tel_link", "context": ""}])
    total, parts = scoring.score(lead)
    assert parts == {"decision_maker": 15, "onsite_or_pain": 10, "email_mx": 0, "exposure_size": 10, "mgmt_phone": 8}
    assert total == 43
    assert scoring.score(_lead(contact_name="Madonna"))[1]["decision_maker"] == 0
    assert scoring.score(_lead(strategy_kind="pain", pain_score=4.0))[1]["onsite_or_pain"] == 25


def test_rank_orders_best_first():
    a, b, c = _lead(source_id="a"), _lead(source_id="b"), _lead(source_id="c")
    a.lead_score, b.lead_score, c.lead_score = 40, 90, 40
    c.pain_score = 3.5
    assert [l.source_id for l in scoring.rank([a, b, c])] == ["b", "c", "a"]


def test_quality_report_and_costs(monkeypatch):
    costs.tracker.reset()
    costs.tracker.add("places_text_search_reviews", 2)
    costs.tracker.add("claude_input_token", 10_000)
    costs.tracker.add("claude_output_token", 1_000)
    costs.tracker.add("serpapi")
    monkeypatch.setenv("PRICE_SERPAPI", "0.02")
    good = _lead(contact_name="Maria Perez", contact_title="LCAM", management_company="Castle Group",
                 email="m@x.com", email_status="mx", pain=[{"category": "mold", "label": "moho", "weight": 3, "quote": "q",
                                                           "date": None, "source": "review"}], pain_score=3.0,
                 public_records={"dbpr": {"units": 10}}, building={"levels": 12, "large": True})
    scoring.apply(good)
    bare = _lead(source_id="2")
    scoring.apply(bare)
    r = quality.build("b1", "onsite", [good, bare], [{"reason": "sin teléfono E.164 válido"}])
    assert r["pct"]["decision_maker"] == 50.0 and r["pct"]["email_verified_mx"] == 50.0
    assert r["pct"]["management_company_identified"] == 50.0 and r["pct"]["pain_signals"] == 50.0
    assert r["pct"]["dbpr_match"] == 50.0 and r["pct"]["coastal_or_large"] == 50.0
    assert r["lead_score"]["max"] == good.lead_score and r["lead_score"]["avg"] == (good.lead_score + bare.lead_score) / 2
    c = r["cost_estimate_usd"]
    assert c["by_api"]["serpapi"]["usd"] == 0.02
    assert abs(c["total_usd"] - (2 * 0.04 + 10_000 * 2e-6 + 1_000 * 1e-5 + 0.02)) < 1e-6
    assert r["cost_per_accepted_lead_usd"] == round(c["total_usd"] / 2, 4)
