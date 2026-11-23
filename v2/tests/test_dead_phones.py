"""Dead phones (☎️) → new_phone (no network): which CRM leads qualify, free sources before Places, exclusions
(the dead number, known bad numbers, numbers another CRM lead has), the exact leads-enrich rows, the CRM's
per-row answers, and the auto-replenish step that runs it first."""

import json

import pytest

from lead_engine import backfill, crm_importer, dead_phones, phone_resolver, pipeline
from lead_engine import lodging
from lead_engine.config import settings
from lead_engine.state import Ledger, building_keys
from lead_engine.web_extract import parse_page

DEAD = "+13054271100"


def row(**kw):
    base = {"id": "0b9a7f5e-1111-4c2a-9d1e-000000000001", "position": 1, "name": "Oceanside Plaza", "phone": DEAD,
            "address": "5555 Collins Ave, Miami Beach, FL 33140", "status": "callback", "segment": "Gran Comunidad",
            "management_company": None, "website_url": "https://www.oceansideplaza.org/", "phone_dead": True,
            "missing": ["phone"]}
    return {**base, **kw}


SITE = """<html><body><p>Front desk 305-866-1301</p>
<p>Management Office: <a href="tel:3058661300">305-866-1300</a></p><p>Fax 305-866-1399</p></body></html>"""


@pytest.fixture
def offline(monkeypatch):
    """Every source simulated: site, lodging, firm line, Places; the CRM precheck and leads-enrich recorded."""
    state = type("S", (), {"site": SITE, "lodging": None, "firm": None, "places": [], "in_crm": set(), "posts": [],
                           "places_calls": 0})()
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: parse_page(state.site, url))
    monkeypatch.setattr(lodging, "building_phone", lambda address, names, **k: state.lodging)
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: state.firm)

    def identify(r, strategy):
        state.places_calls += 1
        if not state.places:
            return None
        from lead_engine.models import Lead
        return Lead(source="google_places", source_id="P", strategy_kind="onsite", company_name=r["name"],
                    phones=[{"number": n, "label": "google_main", "source": "google_places", "context": ""}
                            for n in state.places])
    monkeypatch.setattr(backfill, "identify", identify)
    monkeypatch.setattr(crm_importer, "existing_phone_keys",
                        lambda leads, client=None: {k for k in state.in_crm})

    def post(leads, batch_id, dry_run):
        state.posts.append((dry_run, leads))
        results = []
        for i, l in enumerate(leads):
            if l["new_phone"] == "+13054279999":
                results.append({"index": i, "id": l["id"], "status": "unchanged", "fields": [],
                                "new_phone": "phone_duplicate"})
            else:
                results.append({"index": i, "id": l["id"], "status": "updated", "fields": ["new_phone"]})
        return {"ok": True, "dry_run": dry_run, "received": len(leads), "updated": len(leads), "unchanged": 0,
                "errors": 0, "not_found": [], "ambiguous": [], "rejected": [], "results": results}
    monkeypatch.setattr(backfill, "post_enrich", post)
    object.__setattr__(settings, "google_places_api_key", "k")
    return state


def test_only_dead_leads_qualify():
    rows = [row(), row(id="2", phone_dead=False, missing=["decider"]), row(id="3", phone_dead=None, missing=["phone"])]
    assert [r["id"] for r in dead_phones.dead_rows(rows)] == [row()["id"], "3"]


def test_the_office_line_from_the_site_wins_and_never_the_dead_or_fax(offline, tmp_path):
    rep = dead_phones.run(rows=[row()], out_dir=tmp_path)
    payload = json.loads((tmp_path / rep["batch_id"] / "dead_phone_payload.json").read_text())["leads"]
    assert payload == [{"id": row()["id"], "phone": DEAD, "name": "Oceanside Plaza", "new_phone": "+13058661300"}]
    assert offline.places_calls == 0                                       # a free source answered
    assert rep["by_source"] == {"website": 1} and not rep["committed"]
    assert [p[0] for p in offline.posts] == [True]                          # dry run only


def test_numbers_already_in_the_crm_or_known_bad_are_skipped(offline, tmp_path):
    from lead_engine.models import Lead
    lead = Lead(source="crm", source_id=f"crm:{row()['id']}", strategy_kind="onsite", company_name="Oceanside Plaza",
                address=row()["address"])
    for key in building_keys(lead):
        Ledger().add_bad_phone(key, "3058661300", "wrong_number")          # the office line failed before
    offline.in_crm = {"3058661301"}                                          # the front desk is another lead's phone
    offline.lodging = {"phone": "+13054270000", "label": "front_desk"}
    rep = dead_phones.run(rows=[row()], out_dir=tmp_path)
    pick = json.loads((tmp_path / rep["batch_id"] / "dead_phone_report.json").read_text())["rows"][0]["pick"]
    assert pick["number"] == "+13054270000" and pick["source"] == "dbpr_lodging"


def test_places_only_when_free_sources_give_nothing(offline, tmp_path):
    offline.site = "<html><body>No phones here</body></html>"
    offline.places = [DEAD, "+13057778888"]
    rep = dead_phones.run(rows=[row()], out_dir=tmp_path)
    assert offline.places_calls == 1 and rep["by_source"] == {"google_places": 1}
    rep2 = dead_phones.run(rows=[row()], out_dir=tmp_path, use_places=False)
    assert offline.places_calls == 1 and rep2["no_candidate"] == 1 and rep2["with_candidate"] == 0


def test_commit_sends_the_same_rows_and_reads_the_crm_answers(offline, tmp_path):
    rows = [row(), row(id="0b9a7f5e-1111-4c2a-9d1e-000000000002", name="Ocean Tower", website_url=None,
                       management_company="Castle Group", address=None)]
    offline.firm = {"number": "+13054279999", "firm": "Castle Group"}
    rep = dead_phones.run(rows=rows, commit=True, out_dir=tmp_path)
    assert [p[0] for p in offline.posts] == [True, False] and offline.posts[0][1] == offline.posts[1][1]
    assert rep["committed"] and rep["crm"] == {"written": 1, "refused": {"phone_duplicate": 1}}
    from lead_engine.models import Lead
    l = Lead(source="crm", source_id=f"crm:{row()['id']}", strategy_kind="onsite", company_name="x",
             address=row()["address"])
    assert Ledger().bad_phones_for(l) == {"3054271100"}                   # the dead number is never offered again


def test_the_cost_ceiling_stops_places(offline, tmp_path, monkeypatch):
    from lead_engine import costs
    offline.site = "<html></html>"
    offline.places = ["+13057778888"]
    monkeypatch.setattr(costs.CostTracker, "over_budget", lambda self: True)
    rep = dead_phones.run(rows=[row()], out_dir=tmp_path)
    assert offline.places_calls == 0 and rep["no_candidate"] == 1


def test_only_us_numbers_are_offered():
    cands = [{"number": "+44 20 7946 0958", "label": "management", "source": "website"},
             {"number": "(305) 866-1300", "label": "unlabeled", "source": "website"}]
    assert [c["number"] for c in dead_phones.rank(cands, set())] == ["+13058661300"]


def test_cli_dead_phones(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(dead_phones, "run", lambda **kw: seen.update(kw) or {"dead_leads": 0})
    from lead_engine.__main__ import main
    assert main(["backfill-crm", "--dead-phones", "--limit", "20", "--max-cost", "0.5", "--no-places"]) == 0
    assert seen == {"limit": 20, "commit": False, "max_cost": 0.5, "use_places": False}
