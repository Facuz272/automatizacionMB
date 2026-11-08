"""Orchestrates one run:

  1. collect     Google Places / OSM / seed CSV candidates for a strategy
  2. gate        phone E.164 + street address (a website crawl may fill either first)
  3. precheck    dry-run against the CRM: drop phones it already has (before paying enrichment)
  4. enrich      site crawl (people, mailto, managed-by) → Apollo → rules → Claude
  5. output      out/<batch_id>/{leads.jsonl, crm_payload.json, rejected.jsonl, summary.json}
  6. send        optional: POST to the CRM (dry_run unless --commit)

Stops as soon as `limit` leads are fully enriched, so `--limit 20` bounds spend.
"""

from __future__ import annotations

import json
import secrets
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

from loguru import logger

from . import ai_enrich, apollo, crm_importer
from .collectors import google_places, osm, seed_csv
from .config import settings
from .models import Lead
from .normalize import address_problem, best_email, phone_key
from .qualify import contactability_problem, pain_signals, rule_qualify
from .state import Ledger
from .web_extract import SiteFacts, crawl


def new_batch_id(strategy: str) -> str:
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d-%H%M")
    return f"le2-{now}-{strategy}-{secrets.token_hex(2)}"


def _candidates(strategy: str, sources: list[str], max_candidates: int, seed: str | None) -> Iterator[Lead]:
    for src in sources:
        if src == "places" and settings.google_places_api_key:
            yield from google_places.collect(strategy, max_candidates)
        elif src == "places":
            logger.warning("Fuente 'places' omitida: falta GOOGLE_PLACES_API_KEY")
        elif src == "osm":
            yield from osm.collect(strategy, max_candidates)
        elif src == "csv":
            if not seed:
                raise SystemExit("--source csv requiere --seed archivo.csv")
            yield from seed_csv.collect(seed, strategy, max_candidates)


def _fill_from_site(lead: Lead, facts: SiteFacts) -> None:
    if not lead.phone and facts.phones:
        lead.phone = facts.phones[0]
        lead.evidence["phone_from"] = "website"
    if address_problem(lead.address, settings.target_state) and facts.addresses:
        for a in facts.addresses:
            if not address_problem(a, settings.target_state):
                lead.evidence["address_from"] = "website_jsonld"
                lead.address = a
                break
    lead.web_text = facts.text


def _enrich(lead: Lead, facts: SiteFacts | None, use_apollo: bool, use_ai: bool) -> None:
    facts = facts or SiteFacts()
    lead.pain_signals = pain_signals(lead.reviews)
    # People from the site (first PM-like title wins), then Apollo.
    if not lead.contact_name and facts.people:
        lead.contact_name, lead.contact_title = facts.people[0]
    ap = None
    if use_apollo and lead.domain and (not lead.contact_name or not lead.email):
        ap = apollo.find_contact(lead.domain)
        if ap and not lead.contact_name:
            lead.contact_name, lead.contact_title = ap["name"], ap["title"]
            lead.evidence["contact_from"] = "apollo"
        if ap and ap.get("email") and not lead.email:
            lead.email, lead.email_source = ap["email"], "apollo"
    if not lead.management_company and facts.managed_by:
        lead.management_company = facts.managed_by[0]
    # Email: never required (primary channel is the phone), best candidate kept.
    if not lead.email:
        e = best_email(list(facts.emails), lead.domain, lead.contact_name or "")
        if e:
            lead.email, lead.email_source = e, facts.emails.get(e, "text")
    if use_ai and ai_enrich.available():
        candidate_emails = sorted(set(facts.emails) | ({lead.email} if lead.email else set()))
        evidence = ai_enrich.build_evidence(lead, facts.people, candidate_emails, facts.managed_by, ap)
        result = ai_enrich.enrich(lead, evidence)
        if result:
            ai_enrich.apply(lead, result, evidence)
    rule_qualify(lead)  # fills whatever is still empty
    lead.evidence["site_pages"] = facts.pages[:6]


def run(strategy: str = "onsite", limit: int = 20, sources: Iterable[str] = ("places",), seed: str | None = None,
        use_ai: bool = True, use_apollo: bool = True, precheck: bool = True, send: bool = False,
        commit: bool = False, max_candidates: int | None = None, out_dir: Path | None = None) -> Path:
    batch_id = new_batch_id(strategy)
    out = Path(out_dir or settings.out_dir) / batch_id
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()
    max_candidates = max_candidates or limit * 6
    logger.info("Lote {} · estrategia={} · límite={} · fuentes={}", batch_id, strategy, limit, list(sources))

    rejected: list[dict] = []
    gated: list[tuple[Lead, SiteFacts | None]] = []
    seen_keys: set[str] = set()

    def reject(lead: Lead, reason: str) -> None:
        lead.rejected_reason = reason
        rejected.append({"company_name": lead.company_name, "source": lead.source, "source_id": lead.source_id,
                         "phone": lead.phone, "address": lead.address, "website": lead.website_url, "reason": reason})
        ledger.mark(lead.source_id, lead.source, lead.phone, "rejected", reason, batch_id)

    # ── 1-2: collect + contactability gate ────────────────────────────────────
    for lead in _candidates(strategy, list(sources), max_candidates, seed):
        if ledger.outcome(lead.source_id) in ("accepted", "imported", "in_crm"):
            continue
        facts: SiteFacts | None = None
        needs_site = not lead.phone or address_problem(lead.address, settings.target_state)
        if needs_site and lead.website_url:
            facts = crawl(lead.website_url)
            _fill_from_site(lead, facts)
        problem = contactability_problem(lead, settings.target_state)
        if problem:
            reject(lead, problem)
            continue
        if strategy == "pain" and not pain_signals(lead.reviews):
            reject(lead, "sin señales de dolor en reseñas")
            continue
        key = phone_key(lead.phone or "")
        if key in seen_keys or ledger.is_imported(lead.phone):
            reject(lead, "teléfono repetido")
            continue
        seen_keys.add(key)
        gated.append((lead, facts))
        if len(gated) >= limit * 2:  # margin for CRM duplicates
            break

    # ── 3: CRM precheck (drop what the CRM already has) ───────────────────────
    client = crm_importer.CrmClient()
    if precheck and client.configured and gated:
        try:
            in_crm = crm_importer.existing_phone_keys(
                [{"company_name": l.company_name, "phone": l.phone} for l, _ in gated], client)
        except crm_importer.CrmError as exc:
            logger.warning("Precheck CRM falló ({}); se sigue sin él", exc)
            in_crm = set()
        kept = []
        for lead, facts in gated:
            if phone_key(lead.phone or "") in in_crm:
                reject(lead, "ya existe en el CRM")
                ledger.mark(lead.source_id, lead.source, lead.phone, "in_crm", None, batch_id)
            else:
                kept.append((lead, facts))
        gated = kept

    # ── 4: enrichment ─────────────────────────────────────────────────────────
    accepted: list[Lead] = []
    for lead, facts in gated[:limit]:
        if facts is None and lead.website_url:
            facts = crawl(lead.website_url)
            _fill_from_site(lead, facts)
        _enrich(lead, facts, use_apollo, use_ai)
        accepted.append(lead)
        ledger.mark(lead.source_id, lead.source, lead.phone, "accepted", None, batch_id)
        logger.success("{} · {} · {} · contacto={} · email={}", lead.company_name, lead.phone, lead.segment,
                       lead.contact_name or "—", lead.email or "—")

    # ── 5: output ─────────────────────────────────────────────────────────────
    payloads = [l.crm_payload(batch_id) for l in accepted]
    with open(out / "leads.jsonl", "w", encoding="utf-8") as fh:
        for l in accepted:
            fh.write(json.dumps({**l.internal_record(), "import_batch_id": batch_id}, ensure_ascii=False) + "\n")
    with open(out / "rejected.jsonl", "w", encoding="utf-8") as fh:
        for r in rejected:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "crm_payload.json").write_text(json.dumps(
        {"import_batch_id": batch_id, "source": "lead-engine-v2", "dry_run": True, "leads": payloads},
        ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize(batch_id, strategy, accepted, rejected)

    # ── 6: send ───────────────────────────────────────────────────────────────
    if send and payloads:
        if not client.configured:
            logger.warning("No se envía al CRM: falta CRM_WEBHOOK_SECRET")
        else:
            report = crm_importer.import_leads(payloads, batch_id, dry_run=not commit, client=client)
            name = "crm_response_commit.json" if commit else "crm_response_dry.json"
            (out / name).write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            summary["crm"] = {k: v for k, v in report.as_dict().items() if k != "responses"}
            if commit:
                ledger.mark_imported(payloads, report)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(summary, out)
    return out


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "—"


def summarize(batch_id: str, strategy: str, accepted: list[Lead], rejected: list[dict]) -> dict:
    n = len(accepted)
    return {
        "import_batch_id": batch_id,
        "strategy": strategy,
        "accepted": n,
        "rejected": len(rejected),
        "rejected_reasons": dict(Counter(r["reason"] for r in rejected).most_common()),
        "coverage": {
            "phone_e164": _pct(sum(bool(l.phone) for l in accepted), n),
            "address_street": _pct(sum(not address_problem(l.address, None) for l in accepted), n),
            "contact_name": _pct(sum(bool(l.contact_name) for l in accepted), n),
            "contact_title": _pct(sum(bool(l.contact_title) for l in accepted), n),
            "email": _pct(sum(bool(l.email) for l in accepted), n),
            "management_known": _pct(sum(l.management_type != "unknown" for l in accepted), n),
            "ai_qualified": _pct(sum(l.ai_used for l in accepted), n),
        },
        "segments": dict(Counter(l.segment for l in accepted)),
        "strategy_kinds": dict(Counter(l.strategy_kind for l in accepted)),
    }


def print_summary(summary: dict, out: Path) -> None:
    print("\n=== Lead Engine v2 ===")
    print(f"Lote: {summary['import_batch_id']}  ({summary['strategy']})")
    print(f"Aceptados: {summary['accepted']}  ·  Rechazados: {summary['rejected']}")
    for k, v in summary["coverage"].items():
        print(f"  {k:<16} {v}")
    if summary["rejected_reasons"]:
        print("Motivos de rechazo:", summary["rejected_reasons"])
    if "crm" in summary:
        c = summary["crm"]
        print(f"CRM (dry_run={c['dry_run']}): insertaría={c['would_insert']} insertados={c['inserted']} "
              f"duplicados={len(c['duplicates'])} rechazados={len(c['rejected'])}")
    print(f"Archivos: {out}/  (crm_payload.json = lo que se enviaría)")
