"""Orchestrates one run:

  1. collect     Google Places / OSM / seed CSV candidates for a strategy
  2. gate        phone E.164 + street address (a website crawl may fill either first)
  3. precheck    dry-run against the CRM: drop phones it already has (before paying enrichment)
  4. enrich      per lead, in this order:
                   site crawl (+Playwright for SPAs, hydrated JSON) → labelled phones, people, emails
                   DBPR registry (association, units, care-of management/officer)
                   external search API when the site/registry leave gaps
                   Apollo → OSM building profile → pain detector → email MX
                   → Claude (evidence-guarded) → rules → lead_score
  5. rank        sort by lead_score (CRM appends in order → 6 AM distribution gives the best first)
  6. output      out/<batch_id>/{leads.jsonl, crm_payload.json, rejected.jsonl, quality_report.json}
  7. send        optional: POST to the CRM (dry_run unless --commit)

Stops as soon as `limit` leads are fully enriched, so `--limit 20` bounds spend.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

from loguru import logger

from . import (ai_enrich, apollo, appraiser, cam, costs, crm_importer, dbpr, email_verify, geo, lodging, mgmt_domains,
               municipal_signals, npi, quality, render, scoring, search, valuation, verticals)
from .roles import pick_primary
from .collectors import dbpr_seed, google_places, osm, seed_csv
from .config import settings
from .models import Lead
from .normalize import address_problem, clean_email, phone_key, rank_email
from .qualify import contactability_problem, detect_pain, has_pain, rule_qualify
from .state import Ledger
from .web_extract import LABEL_PRIORITY, SiteFacts, better_label, crawl


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
        elif src == "dbpr":
            yield from dbpr_seed.collect(strategy, max_candidates)
        elif src == "npi":
            from .config import cities_for
            yield from npi.collect(strategy, max_candidates, cities_for(strategy))
        elif src == "pa":
            from .collectors import appraiser_seed
            yield from appraiser_seed.collect(strategy, max_candidates, vertical=settings.seed_vertical)
        elif src == "csv":
            if not seed:
                raise SystemExit("--source csv requiere --seed archivo.csv")
            yield from seed_csv.collect(seed, strategy, max_candidates)


# ── phones ──────────────────────────────────────────────────────────────────

def merge_phones(lead: Lead, facts: SiteFacts) -> None:
    for p in facts.phones:
        if p["label"] == "fax":
            continue
        existing = next((x for x in lead.phones if x["number"] == p["number"]), None)
        if existing is None:
            lead.phones.append(dict(p))
        elif better_label(existing["label"], p["label"]):
            existing.update(label=p["label"], context=p.get("context", ""), source=p["source"])


def choose_primary_phone(lead: Lead) -> None:
    """Management office / property manager line beats Google's main number; front desk/leasing never win."""
    usable = [p for p in lead.phones if p["label"] != "fax"]
    if not usable:
        return
    best = sorted(usable, key=lambda p: LABEL_PRIORITY.get(p["label"], 9))[0]
    current = next((p for p in usable if p["number"] == lead.phone), None)
    if current is None or LABEL_PRIORITY.get(best["label"], 9) < LABEL_PRIORITY.get(current["label"], 9):
        if lead.phone and lead.phone != best["number"]:
            lead.evidence["phone_replaced"] = {"from": lead.phone, "to": best["number"], "label": best["label"]}
        lead.phone = best["number"]


def _fill_from_site(lead: Lead, facts: SiteFacts) -> None:
    merge_phones(lead, facts)
    if not lead.phone and facts.best_phone():
        lead.phone = facts.best_phone()["number"]
        lead.evidence["phone_from"] = "website"
    if address_problem(lead.address, settings.target_state) and facts.addresses:
        for a in facts.addresses:
            if not address_problem(a, settings.target_state):
                lead.evidence["address_from"] = "website_jsonld"
                lead.address = a
                break
    lead.web_text = facts.text


# ── DBPR seed: find the contact channel for a known building ───────────────

def resolve_seed(lead: Lead, use_search: bool) -> None:
    """Phone / website / coordinates / reviews for a DBPR seed lead.

    Places by name + address (when the key exists) → OpenStreetMap address tags → web search
    for the official site. The website crawl in the gate then fills a missing phone.
    """
    if settings.google_places_api_key:
        found = google_places.find_place(lead.company_name, lead.address or "", lead.strategy_kind)
        if found:
            for attr in ("phone", "website_url", "domain", "maps_url", "google_rating", "total_reviews", "place_types",
                         "reviews", "lat", "lng", "phones"):
                if getattr(found, attr) and not getattr(lead, attr):
                    setattr(lead, attr, getattr(found, attr))
            lead.evidence["places_match"] = found.evidence.get("places_match")
            if lead.source == "pa" and found.company_name:      # cadastre has the owner, Places has the business name
                lead.evidence["owner_as_name"] = lead.company_name
                lead.company_name = found.company_name
            lead.evidence["place_id"] = found.source_id
            lead.evidence["phone_raw"] = found.evidence.get("phone_raw")
    if lead.lat is None or not lead.phone:
        hit = osm.lookup_address(lead.address, lead.company_name) if settings.geo_enabled else None
        if hit:
            lead.lat = lead.lat if lead.lat is not None else hit["lat"]
            lead.lng = lead.lng if lead.lng is not None else hit["lng"]
            if hit["phone"] and not lead.phone:
                lead.phone = hit["phone"]
                lead.phones.append({"number": hit["phone"], "label": "unlabeled", "source": "osm", "context": ""})
            if hit["website"] and not lead.website_url:
                lead.website_url = hit["website"]
            lead.evidence["osm_address_match"] = hit["osm_id"]
    if not lead.website_url and use_search:
        url = search.find_website(lead.company_name, search.city_of(lead.address))
        if url:
            lead.website_url = url
            lead.evidence["website_from"] = "search"
    if lead.website_url and not lead.domain:
        from .normalize import extract_domain
        lead.domain = extract_domain(lead.website_url)


# ── enrichment steps ────────────────────────────────────────────────────────

def apply_licenses(lead: Lead) -> None:
    """Verify the contact (CAM) and the management firm (CAB) against the DBPR licence registry."""
    try:
        person = cam.verify_person(lead.contact_name) if lead.contact_name else None
        firm = cam.verify_firm(lead.management_company) if lead.management_company else None
    except Exception as exc:  # noqa: BLE001 - registry missing/corrupt must not stop the run
        logger.debug("CAM registry: {}", exc)
        return
    if person:
        lead.licenses["contact"] = person
        if person["active"] and (not lead.contact_title or "DBPR" in lead.contact_title or "Contacto" in lead.contact_title):
            lead.contact_title = "LCAM (licencia activa)"
    if firm:
        lead.licenses["firm"] = firm


# ZIPs within ~3 miles of the Atlantic / Biscayne Bay in Miami-Dade and Broward (milestone at 25 years).
COASTAL_3MI_ZIPS = {
    "33109", "33139", "33140", "33141", "33154", "33160", "33180", "33181", "33162", "33149", "33129", "33130",
    "33131", "33132", "33133", "33137", "33138", "33136", "33125", "33128", "33156", "33157", "33158", "33176",
    "33009", "33019", "33020", "33004", "33062", "33064", "33060", "33301", "33304", "33305", "33306", "33308",
    "33316", "33315", "33441", "33432", "33431", "33480", "33062", "33308",
}


def coastal_3mi(lead: Lead) -> bool:
    b = lead.building or {}
    if b.get("coast_distance_m") is not None:
        return b["coast_distance_m"] <= 4828
    return bool(b.get("coastal")) or (dbpr.zip5(lead.address) or "") in COASTAL_3MI_ZIPS


def apply_official_building(lead: Lead) -> None:
    """Miami-Dade Property Appraiser facts override estimates (floors, units, area, year)."""
    p, b = lead.cadastre, lead.building
    if not p:
        return
    if p.get("floors") and (p.get("floors_source") == "appraiser" or not b.get("levels")):
        if b.get("levels") and b["levels"] != p["floors"]:
            b["levels_osm"] = b["levels"]
        b["levels"] = p["floors"]
        b["levels_source"] = p.get("floors_source")
    if p.get("units") and not b.get("units"):
        b["units"] = p["units"]
    if p.get("building_sqft"):
        b["building_sqft"] = p["building_sqft"]
        if not b.get("footprint_m2") and p.get("kind") == "property" and b.get("levels"):
            b["footprint_m2"] = round(p["building_sqft"] / b["levels"] / 10.7639)
            b["footprint_source"] = "appraiser"
        if p["building_sqft"] >= 100_000:
            b["large"] = True
    if p.get("year_built"):
        b["year_built"] = p["year_built"]
    if p.get("lot_sqft"):
        b["lot_sqft"] = p["lot_sqft"]
    b["vertical"] = lead.vertical


def apply_property_owner(lead: Lead) -> None:
    """Plazas / office buildings: the registered owner and the c/o management firm from the cadastre."""
    p = lead.cadastre
    if not p or lead.vertical == "community":
        return
    if p.get("care_of") and not lead.management_company:
        lead.management_company = p["care_of"].title()
        lead.management_type = "third_party"
        lead.evidence["management_from"] = "appraiser_care_of"
    if p.get("owner_name"):
        lead.evidence["owner"] = {"name": p["owner_name"], "mailing": p.get("owner_mailing")}


def apply_lodging(lead: Lead) -> None:
    lg = lead.lodging
    if not lg or not lg.get("phone"):
        return
    if not any(x["number"] == lg["phone"] for x in lead.phones):
        lead.phones.append({"number": lg["phone"], "label": "front_desk", "source": "dbpr_lodging",
                            "context": f"{lg.get('kind')} {lg.get('business_name')}"})
    if not lead.phone:
        lead.phone = lg["phone"]


def apply_cluster(lead: Lead) -> None:
    rec = lead.public_records.get("dbpr") or {}
    cl = dbpr.cluster(company=lead.management_company, person=lead.contact_name,
                      exclude_project=rec.get("project_number"), near_zip=dbpr.zip5(lead.address),
                      near_city=dbpr.city_of(lead.address))
    if cl:
        lead.portfolio_cluster = cl



def apply_dbpr(lead: Lead, rec: dict | None) -> None:
    if not rec:
        return
    lead.public_records["dbpr"] = {k: rec[k] for k in (
        "project_number", "condo_name", "association", "units", "projects_at_address", "status", "route_raw",
        "management_company", "contact_name", "contact_title", "management_type", "registered_agent", "matched_by")}
    lead.association_name = lead.association_name or rec.get("association")
    if rec.get("management_company") and not lead.management_company:
        lead.management_company = rec["management_company"]
        lead.management_type = "third_party"
    elif rec.get("management_type") in ("onsite", "self_managed") and lead.management_type == "unknown":
        lead.management_type = rec["management_type"]
    if rec.get("contact_name") and not lead.contact_name:
        lead.contact_name = rec["contact_name"]
        lead.contact_title = rec.get("contact_title") or "Contacto registrado de la asociación (DBPR)"
        lead.evidence["contact_from"] = "dbpr"


def apply_search(lead: Lead, found: dict) -> None:
    if not found:
        return
    lead.search_hits = found.get("hits", [])[:8]
    lead.evidence["search_query"] = found.get("query")
    lead.evidence["search_companies"] = found.get("companies", [])
    lead.evidence["search_people"] = found.get("people", [])
    companies = found.get("companies") or []
    if companies and not lead.management_company:
        lead.management_company = companies[0][0]
        lead.management_type = "third_party"
        lead.evidence["management_from"] = "search"


def pick_email(lead: Lead, candidates: dict[str, str], check_mx: bool) -> None:
    """Best email whose domain is alive. Order: current, then ranked candidates."""
    ordered: list[str] = []
    original, original_source = lead.email, lead.email_source
    if lead.email:
        ordered.append(lead.email)
    ranked = sorted({c for c in (clean_email(e) for e in candidates) if c},
                    key=lambda e: rank_email(e, lead.domain, lead.contact_name or ""))
    ordered += [e for e in ranked if e not in ordered]
    if not ordered:
        lead.email, lead.email_status = None, "unchecked"
        return
    if not check_mx:
        lead.email, lead.email_status = ordered[0], "unchecked"
    else:
        costs.tracker.add("dns", len(ordered))
        chosen, status = email_verify.first_deliverable(ordered)
        dropped = [e for e in ordered[: ordered.index(chosen)]] if chosen else ordered
        if dropped:
            lead.evidence["emails_dropped_dns"] = dropped
        lead.email, lead.email_status = chosen, (email_verify.email_status(chosen) if chosen else status)
    if lead.email:
        lead.email_source = original_source if lead.email == original and original_source else candidates.get(lead.email, "text")


def enrich(lead: Lead, facts: SiteFacts | None, *, use_apollo: bool, use_ai: bool, use_search: bool,
           use_geo: bool, use_dbpr: bool, check_mx: bool) -> None:
    facts = facts or SiteFacts()
    choose_primary_phone(lead)

    # Site: every person with a title (PM, facilities/engineering, board…), office hours, managed-by.
    for name, title in facts.people:
        lead.add_contact(name, title, "website")
    lead.office_hours = lead.office_hours or facts.office_hours
    if not lead.contact_name and lead.contacts:
        best = pick_primary(lead.contacts)
        if best:
            lead.contact_name, lead.contact_title = best["name"], best["title"]
            lead.evidence["contact_from"] = best["source"]
    if not lead.management_company and facts.managed_by:
        lead.management_company = facts.managed_by[0]
        lead.management_type = "third_party"

    # Official registry (seed leads already carry their record).
    if use_dbpr:
        if lead.public_records.get("dbpr"):
            apply_dbpr(lead, {**lead.public_records["dbpr"]})
        else:
            apply_dbpr(lead, dbpr.match(lead.address, lead.company_name))

    # External search when the site and registry left gaps.
    emails: dict[str, str] = dict(facts.emails)
    if use_search and (not lead.website_url or not (lead.contact_name and lead.management_company)):
        found = search.discover(lead.company_name, search.city_of(lead.address))
        apply_search(lead, found)
        for e in found.get("emails", []) if found else []:
            emails.setdefault(e, "search")

    # Apollo by domain.
    ap = None
    if use_apollo and lead.domain and (not lead.contact_name or not lead.email):
        ap = apollo.find_contact(lead.domain)
        if ap and not lead.contact_name:
            lead.contact_name, lead.contact_title = ap["name"], ap["title"]
            lead.evidence["contact_from"] = "apollo"
        if ap and ap.get("email") and not lead.email:
            lead.email, lead.email_source = ap["email"], "apollo"

    # Official cadastre + lodging licence, then the business vertical.
    if lead.source != "pa":                      # cadastre seeds already carry their exact folio
        lead.cadastre = appraiser.lookup(lead.address) or lead.cadastre
    if use_dbpr:
        lead.lodging = lodging.match(lead.address) or {}
    lead.vertical = verticals.classify(appraiser=lead.cadastre.get("vertical"), source=lead.source,
                                       places_types=lead.place_types, name=lead.company_name,
                                       lodging_kind=lead.lodging.get("kind"))
    apply_property_owner(lead)
    apply_lodging(lead)
    if lead.vertical == "clinic" and lead.source != "npi" and settings.npi_enabled:
        npi.enrich(lead)
    if not lead.contact_name and lead.contacts:
        best = pick_primary(lead.contacts)
        if best:
            lead.contact_name, lead.contact_title = best["name"], best["title"]
            lead.evidence["contact_from"] = best["source"]

    # Licences (CAM person / CAB firm) and the firm's other buildings in the area.
    if use_dbpr:
        apply_licenses(lead)
        apply_cluster(lead)

    # Physical profile (official cadastre overrides estimates) + annual contract estimate.
    osm_profile = geo.fetch_profile(lead.lat, lead.lng) if use_geo else {}
    units = (lead.public_records.get("dbpr") or {}).get("units") or lead.cadastre.get("units")
    lead.building = geo.merge_profile(osm_profile, lead.web_text, lead.address, units)
    lead.building["vertical"] = lead.vertical
    apply_official_building(lead)
    lead.estimated_deal = valuation.estimate(lead.building) or {}

    # Municipal urgency (open data) + milestone window, then pain + email.
    lead.municipal = municipal_signals.lookup(lead.address, lat=lead.lat, lng=lead.lng,
                                              dbpr_county=(lead.public_records.get("dbpr") or {}).get("county"))
    ms = appraiser.milestone_signal(lead.cadastre.get("year_built"), lead.building.get("levels"),
                                    coastal_3mi(lead), units=lead.building.get("units"),
                                    condo=lead.vertical == "community")
    if ms and lead.vertical in ("community", "commercial", "clinic", "plaza"):
        lead.municipal.insert(0, ms)
    detect_pain(lead)
    pick_email(lead, emails, check_mx)

    # No direct email but a known management firm: its verified corporate domain/inbox.
    if lead.management_company and not lead.email:
        md = mgmt_domains.resolve(lead.management_company, use_search=use_search, check_mx=check_mx)
        if md:
            lead.management_domain = md

    # Claude, guarded by the evidence.
    if use_ai and ai_enrich.available():
        candidate_emails = sorted(set(emails) | ({lead.email} if lead.email else set()))
        if check_mx:
            candidate_emails = [e for e in candidate_emails if email_verify.email_status(e) not in email_verify.DROP]
        evidence = ai_enrich.build_evidence(lead, facts.people, candidate_emails, facts.managed_by, ap)
        result = ai_enrich.enrich(lead, evidence)
        if result:
            before = lead.email
            ai_enrich.apply(lead, result, evidence)
            if lead.email != before and check_mx:
                lead.email_status = email_verify.email_status(lead.email)
    rule_qualify(lead)  # fills whatever is still empty
    scoring.apply(lead)
    lead.evidence["site_pages"] = facts.pages[:6]
    if facts.rendered:
        lead.evidence["rendered_pages"] = facts.rendered[:4]


def write_needs_phone(path: Path, leads: list[Lead]) -> None:
    """DBPR buildings that passed every filter but no source gave a phone: a ready list for manual lookup."""
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["condo", "address", "units", "association", "management", "dbpr_project", "website"])
        for l in leads:
            rec = l.public_records.get("dbpr") or {}
            w.writerow([l.company_name, l.address, rec.get("units"), rec.get("association"),
                        rec.get("management_company") or rec.get("management_type") or "", rec.get("project_number"),
                        l.website_url or ""])


def default_sources() -> list[str]:
    """Google Places when its key exists; otherwise the DBPR seed (zero Places cost)."""
    return ["places"] if settings.google_places_api_key else ["dbpr"]


# ── run ─────────────────────────────────────────────────────────────────────

def run(strategy: str = "onsite", limit: int = 20, sources: Iterable[str] | None = None, seed: str | None = None,
        use_ai: bool = True, use_apollo: bool = True, precheck: bool = True, send: bool = False,
        commit: bool = False, max_candidates: int | None = None, out_dir: Path | None = None,
        use_search: bool = True, use_geo: bool = True, use_dbpr: bool = True, check_mx: bool | None = None) -> Path:
    sources = list(sources) if sources else default_sources()
    batch_id = new_batch_id(strategy)
    out = Path(out_dir or settings.out_dir) / batch_id
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()
    costs.tracker.reset()
    check_mx = settings.email_mx_check if check_mx is None else check_mx
    max_candidates = max_candidates or limit * 6
    logger.info("Lote {} · estrategia={} · límite={} · fuentes={} · búsqueda={} · render={}",
                batch_id, strategy, limit, list(sources), search.provider() or "off",
                settings.render_js if render.available() else "off")

    rejected: list[dict] = []
    needs_phone: list[Lead] = []
    municipal_signals.reset()
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
        if lead.source in ("dbpr", "pa"):
            resolve_seed(lead, use_search)
        facts: SiteFacts | None = None
        needs_site = not lead.phone or address_problem(lead.address, settings.target_state)
        if needs_site and lead.website_url:
            facts = crawl(lead.website_url)
            _fill_from_site(lead, facts)
        problem = contactability_problem(lead, settings.target_state)
        if problem:
            if lead.source in ("dbpr", "pa") and not lead.phone:
                needs_phone.append(lead)
                problem = "sin teléfono (semilla DBPR: sin Places/OSM/web con teléfono)"
            reject(lead, problem)
            continue
        if strategy == "pain" and not has_pain(lead):
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
    try:
        for lead, facts in gated[:limit]:
            if facts is None and lead.website_url:
                facts = crawl(lead.website_url)
                _fill_from_site(lead, facts)
            enrich(lead, facts, use_apollo=use_apollo, use_ai=use_ai, use_search=use_search,
                   use_geo=use_geo, use_dbpr=use_dbpr, check_mx=check_mx)
            accepted.append(lead)
            ledger.mark(lead.source_id, lead.source, lead.phone, "accepted", None, batch_id)
            logger.success("[{:>3}] {} · {} · {} · contacto={} · admin={} · email={}({})", lead.lead_score,
                           lead.company_name, lead.phone, lead.segment, lead.contact_name or "—",
                           lead.management_company or lead.management_type, lead.email or "—", lead.email_status)
    finally:
        render.close()

    # ── 5: rank ───────────────────────────────────────────────────────────────
    accepted = scoring.rank(accepted)

    # ── 6: output ─────────────────────────────────────────────────────────────
    payloads = [l.crm_payload(batch_id, include_brief=settings.crm_include_brief) for l in accepted]
    with open(out / "leads.jsonl", "w", encoding="utf-8") as fh:
        for l in accepted:
            fh.write(json.dumps({**l.internal_record(), "import_batch_id": batch_id}, ensure_ascii=False) + "\n")
    with open(out / "rejected.jsonl", "w", encoding="utf-8") as fh:
        for r in rejected:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if needs_phone:
        write_needs_phone(out / "needs_phone.csv", needs_phone)
    (out / "crm_payload.json").write_text(json.dumps(
        {"import_batch_id": batch_id, "source": "lead-engine-v2", "dry_run": True, "leads": payloads},
        ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 7: send ───────────────────────────────────────────────────────────────
    crm_summary = None
    if send and payloads:
        if not client.configured:
            logger.warning("No se envía al CRM: falta CRM_WEBHOOK_SECRET")
        else:
            report = crm_importer.import_leads(payloads, batch_id, dry_run=not commit, client=client)
            name = "crm_response_commit.json" if commit else "crm_response_dry.json"
            (out / name).write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            crm_summary = {k: v for k, v in report.as_dict().items() if k != "responses"}
            if commit:
                ledger.mark_imported(payloads, report)

    qr = quality.build(batch_id, strategy, accepted, rejected)
    if crm_summary:
        qr["crm"] = crm_summary
    (out / "quality_report.json").write_text(json.dumps(qr, ensure_ascii=False, indent=2), encoding="utf-8")
    quality.print_report(qr, out)
    if crm_summary:
        c = crm_summary
        print(f"CRM (dry_run={c['dry_run']}): insertaría={c['would_insert']} insertados={c['inserted']} "
              f"duplicados={len(c['duplicates'])} rechazados={len(c['rejected'])}")
    return out
