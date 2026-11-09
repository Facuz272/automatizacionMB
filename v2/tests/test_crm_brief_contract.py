"""Contract with the CRM card parser (mbguiaclientes src/lib/lead-brief.ts, vendored in tests/contract/).

Every sales_strategy the engine produces is parsed by the CRM's own TypeScript (run with Node's type
stripping) and must come back whole: the hook (with the consolidation line), Zona with finding and
argument, and every Ficha fact in exactly one field — nothing dropped, nothing merged.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lead_engine import competitor_intel, portfolio_consolidation as pc
from lead_engine.models import SALES_STRATEGY_MAX, Lead

CONTRACT = Path(__file__).parent / "contract"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node 22+ is needed to run the CRM's lead-brief.ts")


def parse(texts: list[str]) -> list[dict]:
    r = subprocess.run([NODE, "--experimental-strip-types", "--no-warnings", str(CONTRACT / "parse_brief.mjs")],
                       input=json.dumps(texts), capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


ZONA_SUMMARY = {"neighbors_scanned": 4, "top_weakness": "recurrence",
                "top_weakness_es": "el moho y las manchas vuelven a las pocas semanas después del lavado a presión",
                "argument": "el softwash trata el moho con producto a baja presión",
                "mentions": [{"weakness": "recurrence", "building": "Bay One"},
                             {"weakness": "recurrence", "building": "Bay Two"}]}


def full_lead(**over) -> Lead:
    l = Lead(source="dbpr", source_id="dbpr:1", strategy_kind="onsite", company_name="Oceanside Plaza Condo",
             address="5555 Collins Ave, Miami Beach, FL 33140", phone="+13058661300")
    l.sales_strategy = "Registro municipal: ventana legal de Milestone Inspection / recertificación — Construido en 1967."
    l.contact_name, l.contact_title = "Ana Diaz", "Property Manager"
    l.add_contact("Ana Diaz", "Property Manager", "website")
    l.add_contact("Carlos Ruiz", "Chief Engineer", "website", phone="+13055550133")
    l.office_hours = "L–V 9–17"
    l.phones = [{"number": "+13058661300", "label": "management", "source": "site", "context": ""},
                {"number": "+13058661301", "label": "front_desk", "source": "places", "context": ""},
                {"number": "+13058661302", "label": "maintenance", "source": "site", "context": ""}]
    l.building = {"levels": 17, "units": 297, "footprint_m2": 1200, "coastal": True, "coast_distance_m": 150,
                  "roof": "flat", "amenities": {"pool_deck": "osm", "parking_garage": "osm"}}
    l.cadastre = {"year_built": 1967, "floors": 17, "units": 297}
    l.estimated_deal = {"low": 37000, "high": 68000}
    l.portfolio_cluster = {"count": 3, "by": "company", "siblings": [{"name": "Bay One"}, {"name": "Bay Two"}]}
    l.management_company = "Castle Management LLC"
    l.licenses = {"contact": {"active": True, "license_id": "CAM12345"},
                  "firm": {"active": True, "license_id": "CAB3838", "matched_by": "exact"}}
    l.management_domain = {"domain": "castlegroup.com", "status": "mx", "email": "info@castlegroup.com"}
    l.lead_score = 92
    for k, v in over.items():
        setattr(l, k, v)
    return l


def with_zona(l: Lead) -> Lead:
    line = competitor_intel.hook_line(ZONA_SUMMARY)
    assert line and line.startswith("Zona: ")
    l.sales_strategy = f"{l.sales_strategy} {line}"
    return l


def with_consolidation(l: Lead, n: int = 3) -> Lead:
    l.phone = "+19549262921"
    l.phones.append({"number": "+19549262921", "label": "firm_central", "source": "firm_website", "context": ""})
    l.portfolio_siblings = [{"name": f"Tower {i}", "address": f"{i}00 Collins Ave, Miami Beach, FL 33140",
                             "units": 100 + i, "signals": [{"category": "municipal_permit", "label": "x"}],
                             "est_high": 20000 + i} for i in range(2, 2 + n)]
    pc.apply(l)
    return l


def facts_of(brief: str) -> list[str]:
    """The engine's own fact list (administradora + its email is one fact, as both sides agree)."""
    out: list[str] = []
    for p in brief.removeprefix("Ficha: ").split(" · "):
        if "@" in p and " " not in p and out and out[-1].startswith("administradora:"):
            out[-1] += f" · {p}"
        else:
            out.append(p)
    return out


def rebuilt(ficha: dict) -> list[str]:
    """Every fact the CRM kept, back in the engine's wording."""
    got = list(ficha["facts"])
    if ficha["vertical"]:
        got.append(ficha["vertical"])
    if ficha["estimate"]:
        got.append(ficha["estimate"])
    if ficha["score"] is not None:
        got.append(f"score {ficha['score']}/100")
    if ficha["planB"]:
        got.append("Plan B: " + ficha["planB"] + (f" {ficha['planBPhone']}" if ficha["planBPhone"] else ""))
    if ficha["officeHours"]:
        got.append(f"oficina {ficha['officeHours']}")
    if ficha["otherPhones"]:
        got.append("otros tel.: " + "; ".join(f"{p['label']} {p['number']}" for p in ficha["otherPhones"]))
    return got


CASES = {
    "full": lambda: full_lead(),
    "zona": lambda: with_zona(full_lead()),
    "consolidated+zona": lambda: with_zona(with_consolidation(full_lead())),
    "no_ficha_extras": lambda: full_lead(contacts=[], office_hours=None, phones=[], portfolio_cluster={},
                                         licenses={}, management_domain={}),
    "plaza": lambda: full_lead(vertical="plaza", cadastre={"kind": "property", "building_sqft": 85000,
                                                             "owner_name": "SUNNY PLAZA LLC", "care_of": "ACME MGMT"}),
}


@pytest.mark.parametrize("case", list(CASES))
def test_every_engine_output_is_parsed_whole_by_the_crm(case):
    lead = CASES[case]()
    text = lead.crm_payload("le2-x")["sales_strategy"]
    assert len(text) <= SALES_STRATEGY_MAX
    hook_line, ficha_line = text.split("\n")
    parsed = parse([text])[0]
    brief = lead.brief()
    assert ficha_line == brief                                       # own line, never collapsed or cut
    f = parsed["ficha"]
    assert f is not None and f["raw"] == brief.removeprefix("Ficha: ")
    assert sorted(rebuilt(f)) == sorted(facts_of(brief))             # nothing lost, nothing merged
    assert f["score"] == lead.lead_score
    zona = competitor_intel.hook_line(ZONA_SUMMARY)
    if "zona" in case:
        assert parsed["zona"]["finding"] == ("reseñas de 2 edificio(s) vecino(s) dicen que "
                                             + ZONA_SUMMARY["top_weakness_es"])
        assert parsed["zona"]["argument"] == ZONA_SUMMARY["argument"]
        assert parsed["hook"] == hook_line[: hook_line.index("Zona:")].strip()
        assert zona.removeprefix("Zona: ") == parsed["zona"]["text"]
    else:
        assert parsed["zona"] is None and parsed["hook"] == hook_line
    if "consolidated" in case:
        assert parsed["hook"].startswith("Teléfono = central de Castle Management LLC: al llamar, cotizar también")
        assert "Tower 4" in parsed["hook"]
    if case in ("full", "zona"):
        assert f["planB"] == "Chief Engineer Carlos Ruiz" and f["planBPhone"] == "+13055550133"
        assert [p["label"] for p in f["otherPhones"]] == ["front desk", "mantenimiento"]
        assert (f["floors"], f["units"], f["yearBuilt"], f["officeHours"]) == (17, 297, 1967, "L–V 9–17")
        assert f["portfolio"]["count"] == 3 and f["vertical"] == "Gran Comunidad"


def test_long_hooks_are_clipped_before_the_ficha_and_the_ficha_survives():
    lead = with_zona(with_consolidation(full_lead(), n=12))
    head, zona = lead.sales_strategy.split(" Zona: ")
    lead.sales_strategy = f"{head} {'detalle ' * 400}Zona: {zona}"        # absurdly long hook before the Zona
    text = lead.crm_payload("le2-x")["sales_strategy"]
    assert len(text) <= SALES_STRATEGY_MAX
    parsed = parse([text])[0]
    assert parsed["ficha"]["score"] == 92 and parsed["ficha"]["raw"] == lead.brief().removeprefix("Ficha: ")
    assert parsed["hook"].endswith("…") and parsed["hook"].startswith("Teléfono = central")
    assert parsed["zona"]["argument"] == ZONA_SUMMARY["argument"]          # the Zona sentence survives whole


def test_old_single_line_payloads_would_have_lost_the_ficha():
    """Regression: clip() used to join hook and Ficha into one line; the CRM then saw no Ficha."""
    lead = with_zona(full_lead())
    old = " ".join(f"{lead.sales_strategy}\n{lead.brief()}".split())
    assert parse([old])[0]["ficha"] is None
    assert parse([lead.crm_payload("x")["sales_strategy"]])[0]["ficha"] is not None


def test_vendored_parser_matches_the_crm_when_its_checkout_is_available():
    crm = Path(__file__).resolve().parents[3] / "mbguiaclientes"
    if not (crm / ".git").exists():
        pytest.skip("CRM checkout not next to this repo")
    r = subprocess.run(["git", "-C", str(crm), "show", "origin/main:src/lib/lead-brief.ts"], capture_output=True,
                       text=True)
    if r.returncode:
        pytest.skip("origin/main of the CRM not fetched")
    assert r.stdout == (CONTRACT / "lead-brief.ts").read_text(encoding="utf-8"), \
        "lead-brief.ts changed in the CRM: copy it to tests/contract/ and re-run this contract"
