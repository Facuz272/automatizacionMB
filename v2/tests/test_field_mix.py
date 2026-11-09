"""East-Broward corridor + Broward tax roll, inspector field sheet, daily multi-vertical mix (no network)."""

import json
from datetime import date

import pytest
import requests

from lead_engine import appraiser, broward, daily, inspector_sheet as sheet, pipeline, tenant, valuation
from lead_engine import route_optimizer as ro
from lead_engine.collectors import appraiser_seed, dbpr_seed
from lead_engine.config import settings
from lead_engine.models import Lead

from test_pdf_route_intel import LEAD, _pages_and_text

pypdf = pytest.importorskip("pypdf")


# ── 1. east Broward ─────────────────────────────────────────────────────────

def test_mb_softwash_corridor_keeps_east_broward_and_drops_the_far_west():
    tenant.use("mb_softwash")
    area = dbpr_seed.area_filter(["Fort Lauderdale", "Hollywood FL", "Hallandale Beach", "Miami Beach"])
    assert dbpr_seed.in_area("FORT LAUDERDALE", "33316", area) and dbpr_seed.in_area("FT LAUDERDALE", "33304", area)
    assert not dbpr_seed.in_area("FORT LAUDERDALE", "33326", area)        # Bonaventure / Weston
    assert not dbpr_seed.in_area("FORT LAUDERDALE", "33324", area)        # not in the corridor list
    assert dbpr_seed.in_area("HOLLYWOOD", "33019", area) and dbpr_seed.in_area("HOLLYWOOD BEACH", "33019", area)
    assert not dbpr_seed.in_area("HOLLYWOOD", "33024", area)
    assert dbpr_seed.in_area("HALLANDALE", "33009", area)
    assert dbpr_seed.in_area("MIAMI BEACH", "33140", area)                # unrestricted cities unchanged
    assert not dbpr_seed.in_area("MIAMI BEACH", "33326", area)            # excluded ZIPs never pass


def test_without_restrictions_the_city_name_still_matches():
    area = dbpr_seed.area_filter(["Fort Lauderdale"], restrictions={}, exclude=[])
    assert dbpr_seed.in_area("FORT LAUDERDALE", "33326", area)


def test_appraiser_seed_where_clause_uses_the_restricted_zips():
    tenant.use("mb_softwash")
    w = appraiser_seed.where_clause("plaza", ["Fort Lauderdale", "Doral"], 20000)
    assert "TRUE_SITE_CITY = 'Doral'" in w and "Fort Lauderdale'" not in w and "33316" in w and "33326" not in w


BROWARD_ROWS = [{"USE_CODE": "04", "BLDG_YEAR_BUILT": 1983, "SITUS_UNIT_NUMBER": u, "BLDG_TOT_SQ_FOOTAGE": 1100,
                 "JUST_BUILDING_VALUE": 300000, "JUST_LAND_VALUE": 50000, "FOLIO_NUMBER": "5142"}
                for u in ("101", "1204", "2501", "PH1")] + [{"USE_CODE": "00", "BLDG_YEAR_BUILT": 0}]


def test_broward_aggregate_condo_shape():
    a = broward.aggregate(BROWARD_ROWS)
    assert (a["kind"], a["units"], a["year_built"], a["floors"], a["building_sqft"]) == ("condominium", 4, 1983, 25, 4400)


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_broward_lookup_only_in_the_corridor_and_never_slows_the_run(monkeypatch):
    tenant.use("mb_softwash")
    calls = []

    def fake_get(url, params, timeout, headers):
        calls.append((params["where"], timeout))
        return _Resp({"features": [{"attributes": r} for r in BROWARD_ROWS]})
    monkeypatch.setattr(broward.requests, "get", fake_get)
    hit = appraiser.lookup("1201 S Ocean Dr, Hollywood, FL 33019")
    assert hit["year_built"] == 1983 and hit["source"] == "broward_property_appraiser"
    assert calls[0] == ("SITUS_STREET_NUMBER = '1201' AND SITUS_STREET_NAME LIKE '%OCEAN%' AND SITUS_ZIP_CODE = 33019",
                        5.0)
    assert appraiser.lookup("16 Isla Bahia Dr, Fort Lauderdale, FL 33326") is None and len(calls) == 1

    def slow(*a, **k):
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(broward.requests, "get", slow)
    broward.reset()
    assert appraiser.lookup("1201 S Ocean Dr, Hollywood, FL 33019") is None
    monkeypatch.setattr(broward.requests, "get", lambda *a, **k: pytest.fail("disabled after the first failure"))
    assert appraiser.lookup("3901 S Ocean Dr, Hollywood, FL 33019") is None


# ── 2. inspector field sheet ────────────────────────────────────────────────

def test_surfaces_operation_and_good_better_best():
    surf = sheet.surfaces(LEAD)
    assert set(surf) == {"facade", "walkways", "pool_deck", "dumpster", "garage"}
    assert surf["facade"]["rope"] and surf["walkways"]["ft2"] == 2000 + 20 * 297 and surf["pool_deck"]["ft2"] == 6000
    ops = sheet.operation(surf)
    r = sheet.rates()
    assert ops["crew_hours"] == pytest.approx(sum(x["crew_hours"] for x in ops["rows"]), abs=0.3)
    assert ops["days"] >= ops["crew_hours"] / (r["hours_per_day"] - r["setup_hours_per_day"])
    assert ops["sh_gal"] > 0 and ops["rope"]
    o1, o2, o3 = sheet.options(surf)
    assert set(o1["keys"]) == {"walkways", "pool_deck", "dumpster"} and set(o2["keys"]) == set(surf)
    assert o1["range"][0] < o1["range"][1] < o2["range"][0]
    disc = 1 - r["annual_discount_pct"] / 100
    assert o3["range"][0] == round(o2["range"][0] + 2 * o1["range"][0] * disc, -1)
    assert o3["range"][0] > o2["range"][0] and o3["range"][1] > o2["range"][1]      # never cheaper than option 2
    assert o3["monthly"] == round(o3["suggested"] / 12, -1)


def test_small_jobs_hit_the_floor_price():
    tiny = {"company_name": "Tiny HOA", "address": "1 Main St, Miami, FL 33101", "building": {"units": 4, "levels": 2}}
    o1 = sheet.options(sheet.surfaces(tiny))[0]
    assert o1["range"] == (1500, 1500) and "precio piso" in o1["scope"]


def test_tenant_field_rates_override_defaults():
    object.__setattr__(settings, "field_rates", {"min_job_usd": 900, "annual_discount_pct": 20})
    assert sheet.rates()["min_job_usd"] == 900 and sheet.options(sheet.surfaces(LEAD))[2]["scope"].endswith(
        "con 20% de descuento")


def test_field_sheet_pdf_one_page_per_building(tmp_path):
    p = sheet.build([LEAD, {**LEAD, "company_name": "Otro"}], tmp_path / "x.pdf", inspector="Tomás",
                    whens=["10/11 10:00", None])
    n, text = _pages_and_text(p)
    assert n == 2
    for s in ("HOJA DE CAMPO Y COTIZACIÓN", "Inspector: Tomás", "Visita: 10/11 10:00", "Superficies y operación",
              "Fachada y balcones", "Garaje", "Hipoclorito", "acceso por cuerdas", "1. Entrada / Horizontal",
              "2. Completa / Milestone", "3. Contrato Anual", "Notas de la visita"):
        assert s in text, s


def test_route_writes_one_field_sheet_pdf_per_inspector(tmp_path):
    recs = [{**LEAD, "company_name": f"B{i}", "lat": 25.83 + i / 100, "lng": -80.12,
             "inspection_at": f"2026-11-10T{10 + i}:00:00-05:00"} for i in range(3)]
    stops = [ro.stop_from_record(r) for r in recs]
    plan = ro.optimize(stops, inspectors=["Tomás", "Mateo"], start=(25.8, -80.13), day=date(2026, 11, 10))
    files = ro.field_sheets(plan, tmp_path, "ruta_2026-11-10")
    pages = sum(_pages_and_text(f)[0] for f in files)
    assert pages == sum(len(l) for l in plan.routes.values()) == 3
    assert all(f.name.endswith("_campo.pdf") for f in files)
    assert "Fachada y balcones" in _pages_and_text(files[0])[1]          # full engine record reached the sheet


# ── 3. daily multi-vertical mix ─────────────────────────────────────────────

MIX = [{"name": "condos", "share": 0.6, "seed": "dbpr", "strategy": "onsite"},
       {"name": "plazas", "share": 0.25, "seed": "pa", "vertical": "plaza"},
       {"name": "clinicas", "share": 0.15, "seed": "npi"}]


def test_quotas_and_validation():
    assert daily.quotas(10, daily.validate_mix(MIX)) == [6, 3, 1]
    assert daily.quotas(1, daily.validate_mix(MIX)) == [1, 0, 0]
    assert sum(daily.quotas(37, daily.validate_mix(MIX))) == 37
    with pytest.raises(ValueError, match="vertical"):
        daily.validate_mix([{"seed": "pa", "share": 1}])
    with pytest.raises(ValueError, match="seed"):
        daily.validate_mix([{"seed": "yelp", "share": 1}])


def _lead(name, phone, score, high, vertical="community"):
    l = Lead(source="x", source_id=f"x:{name}", strategy_kind="onsite", company_name=name,
             address=f"{abs(hash(name)) % 900 + 100} Collins Ave, Miami Beach, FL 33140", phone=phone)
    l.lead_score, l.estimated_deal, l.vertical = score, {"low": high // 2, "high": high}, vertical
    l.building = {"levels": 10, "units": 120}
    return l


def test_mix_runs_each_slice_merges_ranks_and_finalizes_one_batch(tmp_path, monkeypatch):
    object.__setattr__(settings, "seed_vertical", "commercial")
    produced = {
        "dbpr": [[_lead("Condo A", "+13055550101", 80, 50000), _lead("Condo B", "+13055550102", 95, 30000)],
                 [_lead("Condo C", "+13055550103", 60, 20000)]],                       # second call = top-up
        "pa": [[_lead("Plaza X", "+13055550201", 95, 90000, "plaza"),
                _lead("Dup of A", "+13055550101", 50, 1000, "plaza")]],                 # same phone as Condo A
        "npi": [[]],
    }
    seen_vertical = []

    def fake_run(*, strategy, limit, sources, use_ai, send, out_dir, make_pdfs, **kw):
        assert not send and not make_pdfs
        seen_vertical.append((sources[0], pipeline.settings.seed_vertical, limit))
        leads = produced[sources[0]].pop(0)
        o = out_dir / f"slice-{sources[0]}-{len(seen_vertical)}"
        o.mkdir(parents=True)
        (o / "leads.jsonl").write_text("".join(json.dumps(l.internal_record()) + "\n" for l in leads))
        (o / "rejected.jsonl").write_text(json.dumps({"company_name": "R", "reason": "sin teléfono"}) + "\n")
        return o
    monkeypatch.setattr(pipeline, "run", fake_run)
    out = daily.run_mix(5, MIX, send=False, commit=False, use_ai=False, out_dir=tmp_path)
    assert [(s, n) for s, _, n in seen_vertical] == [("dbpr", 3), ("pa", 1), ("npi", 1), ("dbpr", 1)]
    assert seen_vertical[1][1] == "plaza"                               # the pa slice ran with its vertical
    assert settings.seed_vertical == "commercial"                       # and the setting was restored
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines()]
    # score first, then ticket; the plaza duplicate of Condo A's phone is dropped
    assert [l["company_name"] for l in leads] == ["Plaza X", "Condo B", "Condo A", "Condo C"]
    payload = json.loads((out / "crm_payload.json").read_text())
    assert payload["import_batch_id"] == out.name and len(payload["leads"]) == 4
    report = json.loads((out / "quality_report.json").read_text())
    assert report["mix"]["condos"] == {"quota": 4, "accepted": 3, "batches": report["mix"]["condos"]["batches"]}
    assert report["mix"]["plazas"]["accepted"] == 2 and report["mix"]["clinicas"]["accepted"] == 0
    assert len(list((out / "pdf").glob("*.pdf"))) == 4 and len(list((out / "campo").glob("*_campo.pdf"))) == 4
    assert (out / "outreach_pack.md").exists()
    assert len((out / "rejected.jsonl").read_text().splitlines()) == 4


def test_daily_uses_the_tenant_mix_unless_a_seed_is_given(tmp_path, monkeypatch):
    object.__setattr__(settings, "daily_mix", MIX)
    calls = []
    monkeypatch.setattr(daily, "run_mix", lambda n, sl, **kw: calls.append(("mix", n)) or _fake_out(tmp_path))
    monkeypatch.setattr(pipeline, "run", lambda **kw: calls.append(("single", kw["sources"])) or _fake_out(tmp_path))
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    daily.run(limit=5, slack=False)
    daily.run(limit=5, slack=False, sources=["dbpr"])
    daily.run(limit=5, slack=False, mix=False)
    assert calls == [("mix", 5), ("single", ["dbpr"]), ("single", None)]


def _fake_out(tmp_path):
    o = tmp_path / "le2-fake"
    o.mkdir(exist_ok=True)
    (o / "leads.jsonl").write_text("")
    (o / "quality_report.json").write_text(json.dumps({"mix": {"condos": {"quota": 3, "accepted": 3}}}))
    return o


def test_mb_softwash_profile_carries_the_mix_and_corridor():
    data = tenant.use("mb_softwash")
    mix = daily.validate_mix(data["daily_mix"])
    assert round(sum(s["share"] for s in mix), 2) == 1.0
    assert {s["seed"] for s in mix} == {"dbpr", "pa", "npi"}
    assert "33326" in settings.exclude_zips and "33316" in settings.zip_restrictions["Fort Lauderdale"]
    assert valuation.RATES["facade"] == tuple(data["valuation_rates"]["facade"])


def test_option_scopes_only_name_detected_surfaces():
    bare = {"company_name": "Small Villas", "address": "9280 SW 150th Ave, Miami, FL 33196",
            "building": {"units": 40, "levels": 3}}                     # below the scale thresholds: nothing inferred
    o1, o2, _ = sheet.options(sheet.surfaces(bare))
    assert o1["scope"].startswith("Aceras y entradas, dumpster pads") and "pool" not in o1["scope"]
    assert o2["scope"] == "Fachada completa + áreas comunes"
