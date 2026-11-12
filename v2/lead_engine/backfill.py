"""Enrich leads that already live in the CRM (`python -m lead_engine backfill-crm`).

CRM contract (mbguiaclientes docs/ENRIQUECER_LEADS.md):
  GET  /api/public/leads-unenriched?after_position=&limit=&include_attempted=  → open leads missing a Ficha,
       management company, address or ZIP (paged by position).
  POST /api/public/leads-enrich {dry_run, batch_id, leads: [{id, phone, name, sales_strategy, …}]}
       → the CRM keeps the setter's hook, adds Zona / Ficha only when the lead has no Ficha yet and fills
       only empty fields (a value a setter typed meanwhile always wins). State, owner, call dates, notes,
       mails and history are never touched. A committed send stamps `enriched_at` (30 days without retry);
       a dry run writes nothing.

Older CRM leads are of two kinds, and each is worked like the engine works its own:
  · management firms ("Keystone Property Management"): strategy portfolio; the firm is its own
    management_company; DBPR tells how many condos it runs nearby and its CAM licence; Plan B asks for the
    operations / regional manager.
  · buildings ("Turnberry On the Green Condominium"): onsite; DBPR association + managing firm, cadastre,
    site contacts, verified email, Plan B, zone hook from Places Nearby, estimated ticket and score.
Identity before anything is used: a Places result is accepted only when its phone or website domain is
the lead's own, or (buildings without either) when the name is clearly the same and DBPR / Places agree
on one address. Sub-listings (garages, venues, shops) are never a match. The lead's phone is never
changed: it is the key the CRM already dials; other lines only go to the Ficha.
Rows with nothing useful to add are not sent, so a lead the engine could not identify comes back in a
later run instead of being stamped as attempted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from . import costs, dbpr
from .config import settings
from .models import Lead, clip
from .normalize import extract_domain, is_company_site, phone_key, to_e164

UNENRICHED = "/api/public/leads-unenriched"
ENRICH = "/api/public/leads-enrich"
ENRICH_MAX = 200
# Papers sent and live calls first, then the rest of the open leads.
PRIORITY = ("papers_sent", "portal", "interested", "on_list", "scheduled", "callback", "no_contact", "future", "new")
ACTIVE = ("papers_sent", "portal", "interested", "on_list", "scheduled", "callback", "no_contact")

FIRM_RX = re.compile(r"(?i)\b(?:management|mgmt|managers?|realty|real estate|properties|property|services|"
                     r"group|partners|consult\w*|associates|company|co\.?|inc\.?|llc|corp\.?|pmi|hoa)\b"
                     r"|\bpm\b|\bcam\b|condomaster|\bsfpma\b|residential\b")
BUILDING_RX = re.compile(r"(?i)\b(?:condo\w*|towers?|apartments?|apts|residences?|village|club|clubhouse|lakes|"
                         r"plaza|place|park|point|landing|harbou?r|isles?|bay|beach|homeowners|villas?|at|"
                         r"of\s+\w+|gardens?|estates|leasing office)\b")
FIRM_TYPES = {"real_estate_agency", "corporate_office", "consultant", "insurance_agency", "accounting"}
BUILDING_TYPES = {"condominium_complex", "apartment_building", "apartment_complex", "housing_complex"}


class CrmError(RuntimeError):
    pass


# ── CRM ─────────────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    if not settings.crm_webhook_secret:
        raise CrmError("falta CRM_WEBHOOK_SECRET")
    return {"x-webhook-secret": settings.crm_webhook_secret}


def fetch_unenriched(include_attempted: bool = False, page: int = 200, timeout: float = 30) -> list[dict[str, Any]]:
    """Every open lead the CRM says lacks intelligence, by position."""
    rows: list[dict[str, Any]] = []
    after = 0
    for _ in range(100):
        r = requests.get(settings.crm_base_url.rstrip("/") + UNENRICHED, headers=_headers(), timeout=timeout,
                         params={"after_position": after, "limit": page,
                                 "include_attempted": "true" if include_attempted else "false"})
        if r.status_code != 200:
            raise CrmError(f"{UNENRICHED}: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        got = body.get("leads") or []
        rows += got
        if not body.get("count") or not got:
            break
        after = body.get("next_after_position") or max(int(x.get("position") or 0) for x in got)
    return rows


def post_enrich(leads: list[dict[str, Any]], batch_id: str, dry_run: bool, timeout: float = 60) -> dict[str, Any]:
    """POST in chunks of 200; results are merged with indexes into the full list."""
    merged: dict[str, Any] = {"ok": True, "dry_run": dry_run, "received": 0, "updated": 0, "unchanged": 0,
                              "errors": 0, "not_found": [], "ambiguous": [], "rejected": [], "results": []}
    for start in range(0, len(leads), ENRICH_MAX):
        chunk = leads[start:start + ENRICH_MAX]
        r = requests.post(settings.crm_base_url.rstrip("/") + ENRICH, headers=_headers(), timeout=timeout,
                          json={"dry_run": dry_run, "batch_id": batch_id, "leads": chunk})
        if r.status_code != 200:
            raise CrmError(f"{ENRICH}: HTTP {r.status_code} {r.text[:300]}")
        body = r.json()
        for k in ("received", "updated", "unchanged", "errors"):
            merged[k] += int(body.get(k) or 0)
        for k in ("not_found", "ambiguous", "rejected", "results"):
            merged[k] += [{**x, "index": x.get("index", 0) + start} for x in body.get(k) or []]
    return merged


# ── selection ───────────────────────────────────────────────────────────────

def select(rows: list[dict[str, Any]], statuses: list[str] | None, limit: int | None) -> list[dict[str, Any]]:
    """Papers sent first, then live calls, then the rest; by CRM position inside each state."""
    order = {s: i for i, s in enumerate(PRIORITY)}
    picked = [r for r in rows if not statuses or r.get("status") in statuses]
    picked.sort(key=lambda r: (order.get(r.get("status"), len(order)), r.get("position") or 0))
    return picked[:limit] if limit else picked


STRONG_FIRM = re.compile(r"(?i)manag|\bmgmt\b|\brealty\b|real estate|\bassociates\b|\bproperties\b|"
                         r"\bproperty\s+(?:group|services|solutions)\b|\bservices\b|consult|\bpmi?\b|\bhoa\b")
HOA_NAME = re.compile(r"(?i)\bassociation\b|\bassn\b|\bhomeowners\b|\bclub\b|\bapartments?\b|\btowers?\b|"
                      r"\bleasing office\b|\bclubhouse\b|\bvillage\b")


def is_firm(name: str | None, place_types: list[str] | None = None) -> bool:
    """A management company lead (not a building). Places building types win; then firm vocabulary
    ("RealManage", "Condominium Associates", "Isram Realty") unless the name is an association / building."""
    types = set(place_types or [])
    if types & BUILDING_TYPES:
        return False
    n = name or ""
    if STRONG_FIRM.search(n) and not HOA_NAME.search(n):
        return True
    if FIRM_RX.search(n) and not re.search(r"(?i)\bcondo\w*|\btowers?\b|\bapartments?\b|\bclubhouse\b|"
                                           r"\bhomeowners\b|\bleasing office\b", n):
        return True
    return bool(types & FIRM_TYPES) and not BUILDING_RX.search(n)


# ── identity ────────────────────────────────────────────────────────────────

def _place_leads(query: str, strategy: str, name: str) -> list[Lead]:
    from .collectors import google_places as gp

    try:
        data = gp._search(query, None, with_reviews=settings.places_reviews, page_size=5)
    except gp.PlacesError as exc:
        logger.warning("Places {}: {}", query, exc)
        return []
    out = []
    for p in data.get("places") or []:
        reason = gp.sub_listing_reason(p, name)
        if reason:
            continue
        lead = gp._to_lead(p, strategy)
        if lead:
            out.append(lead)
    return out


def identify(row: dict[str, Any], strategy: str) -> Lead | None:
    """The Places listing of this very lead: same phone, same website domain, or (no phone / site match) the
    same name with exactly one candidate in the service area. None when unsure."""
    name = row.get("name") or ""
    pkey = phone_key(row.get("phone") or "")
    dom = extract_domain(row.get("website_url")) if is_company_site(row.get("website_url")) else ""
    where = row.get("city") or (row.get("zip") and f"FL {row['zip']}") or "Miami-Dade Broward FL"
    cands = _place_leads(f"{name}, {where}", strategy, name)
    for c in cands:
        if pkey and c.phone and phone_key(c.phone) == pkey:
            c.evidence["places_match"] = "phone"
            return c
    for c in cands:
        if dom and c.domain and c.domain == dom:
            c.evidence["places_match"] = "website"
            return c
    from .config import service_area
    area = {x.upper() for x in service_area()}
    close = [c for c in cands if dbpr.name_similarity(name, c.company_name) >= 0.8
             and (not area or (dbpr.city_of(c.address) or "") in area)]
    if len(close) == 1:
        close[0].evidence["places_match"] = "name"
        return close[0]
    return None


def dbpr_by_name(name: str | None, conn=None) -> dict[str, Any] | None:
    """DBPR condo with clearly the same name in the service counties (only when unique)."""
    if not name:
        return None
    try:
        conn = conn or dbpr.connect()
        rows = conn.execute("SELECT * FROM dbpr_condos WHERE UPPER(county) IN ('DADE','MIAMI-DADE','BROWARD')").fetchall()
    except Exception:                                  # noqa: BLE001 — registry missing: no match
        return None
    scored = sorted(((dbpr.name_similarity(name, r["condo_name"]), r) for r in rows), key=lambda t: -t[0])
    if not scored or scored[0][0] < 0.85 or (len(scored) > 1 and scored[1][0] >= scored[0][0]):
        return None
    return dict(scored[0][1])


# ── enrichment ──────────────────────────────────────────────────────────────

NOT_A_PERSON = re.compile(
    r"(?i)\b(?:links?|management|mgmt|group|propert(?:y|ies)|services?|association|assn|team|office|department|"
    r"contact|info|important|home|about|portal|residents?|board|staff|support|customer|realty|inc|llc|corp|"
    r"company|admin|login|page|news|menu|click|here|welcome|community|communities|condo\w*|hoa|services|"
    r"maintenance|accounting|leasing|sales|owners?|apply|online|payments?|forms?|faq|careers?|our|your|free|"
    r"rental|analysis|order|estoppel|request|quote|schedule|book|view|learn|more|read|start|sign|pay|submit|"
    r"download|corporate|profiles?|resources?|testimonials?|reviews?|blog|gallery|events?|amenities)\b")


def plausible_person(name: str | None, company: str | None = None) -> bool:
    """A real person's name (two or three words, no web / company vocabulary, not the firm's own name)."""
    n = (name or "").strip()
    words = n.split()
    if not 2 <= len(words) <= 4 or NOT_A_PERSON.search(n) or any(ch.isdigit() for ch in n):
        return False
    if company and n.lower() in company.lower():
        return False
    return all(w[:1].isupper() for w in words if w.isalpha())


def drop_implausible_contacts(lead: Lead) -> None:
    from .models import clean_person_name

    lead.contact_name = clean_person_name(lead.contact_name)
    for c in lead.contacts:
        c["name"] = clean_person_name(c.get("name")) or c.get("name")
    lead.contacts = [c for c in lead.contacts if plausible_person(c.get("name"), lead.company_name)]
    if lead.contact_name and not plausible_person(lead.contact_name, lead.company_name):
        lead.evidence["contact_dropped"] = lead.contact_name
        lead.contact_name = lead.contact_title = None


def firm_hook(lead: Lead) -> str:
    """Portfolio hook for a management-firm lead, from facts only (DBPR portfolio, a real contact)."""
    who = f"{lead.contact_name} ({lead.contact_title})" if lead.contact_name and lead.contact_title else \
        "el director de operaciones / regional manager"
    cl = lead.portfolio_cluster or {}
    names = [x["name"] for x in cl.get("siblings", [])[:2]]
    if cl.get("count"):
        n = cl["count"]
        incl = f" (incl. {' y '.join(names)})" if names else ""
        trial = f"una prueba en {names[0]}" if names else "una prueba en uno de sus edificios"
        return (f"Administradora con {n} condominio{'s' if n != 1 else ''} registrado{'s' if n != 1 else ''} en DBPR "
                f"en la zona{incl}: pedir por {who} y proponer un proveedor único de softwash con precio por "
                f"portafolio y {trial}.")
    return (f"Administradora de condominios / HOA: pedir por {who} y proponer un proveedor único de softwash con "
            "precio por portafolio y una prueba en uno de sus edificios.")


FIRM_PLAN_B = "pedir por el Director de Operaciones / Regional Manager (decide proveedores del portafolio)"


def to_lead(row: dict[str, Any], firm: bool) -> Lead:
    phone = to_e164(row.get("phone"))
    lead = Lead(source="crm", source_id=f"crm:{row['id']}", strategy_kind="portfolio" if firm else "onsite",
                company_name=row.get("name") or "", phone=phone, address=row.get("address") or None,
                website_url=row.get("website_url") if is_company_site(row.get("website_url")) else None,
                contact_name=row.get("contact_name") or None,
                phones=[{"number": phone, "label": "unlabeled", "source": "crm", "context": ""}] if phone else [])
    if lead.website_url:
        lead.domain = extract_domain(lead.website_url)
    lead.evidence["crm_id"] = row["id"]
    lead.evidence["lead_kind"] = "management_firm" if firm else "building"
    return lead


def _merge_place(lead: Lead, found: Lead) -> None:
    for attr in ("address", "website_url", "domain", "maps_url", "google_rating", "total_reviews", "reviews",
                 "lat", "lng", "place_types"):
        if getattr(found, attr) and not getattr(lead, attr):
            setattr(lead, attr, getattr(found, attr))
    for p in found.phones:                            # other lines only for the Ficha; the CRM phone stays
        if not any(phone_key(x["number"]) == phone_key(p["number"]) for x in lead.phones):
            lead.phones.append(dict(p))
        else:
            for x in lead.phones:
                if phone_key(x["number"]) == phone_key(p["number"]) and x["label"] == "unlabeled":
                    x.update(label=p["label"], source=p["source"])
    lead.evidence["places_match"] = found.evidence.get("places_match")
    lead.evidence["place_id"] = found.source_id


def _enrich_firm(lead: Lead, facts, use_ai: bool, check_mx: bool = True) -> None:
    """Management-firm lead: people and emails from its site, CAM firm licence, how many condos it runs in
    the coverage counties (DBPR), portfolio hook and Plan B. No cadastre / building profile: its address is
    an office, not a property to wash."""
    from . import ai_enrich, email_verify, pipeline, scoring
    from .qualify import rule_qualify
    from .roles import pick_primary

    for name, title in facts.people:
        lead.add_contact(name, title, "website")
    lead.office_hours = lead.office_hours or facts.office_hours
    if not lead.contact_name and lead.contacts:
        best = pick_primary(lead.contacts)
        if best:
            lead.contact_name, lead.contact_title = best["name"], best["title"]
            lead.evidence["contact_from"] = best["source"]
    pipeline.apply_licenses(lead)
    cl = dbpr.cluster(company=lead.management_company, near_zip=dbpr.zip5(lead.address),
                      near_city=dbpr.city_of(lead.address))
    if cl:
        cl = {**cl, "count": cl["count"] - 1}          # cluster() counts the lead itself as one building
        lead.portfolio_cluster = cl
        lead.properties_count = cl["count"]
    emails: dict[str, str] = dict(facts.emails)
    pipeline.pick_email(lead, emails, check_mx)
    if use_ai and ai_enrich.available():
        cands = sorted(set(emails) | ({lead.email} if lead.email else set()))
        if check_mx:
            cands = [e for e in cands if email_verify.email_status(e) not in email_verify.drop_statuses()]
        evidence = ai_enrich.build_evidence(lead, facts.people, cands, [], None)
        result = ai_enrich.enrich(lead, evidence)
        if result:
            before = lead.email
            ai_enrich.apply(lead, result, evidence)
            if lead.email != before and check_mx:
                lead.email_status = email_verify.email_status(lead.email)
    lead.management_company = lead.company_name
    lead.management_type = "third_party"
    lead.pain, lead.pain_score = [], 0.0          # the firm's own reviews / marketing are not a building's pain
    drop_implausible_contacts(lead)
    rule_qualify(lead)
    lead.sales_strategy = firm_hook(lead)
    if not lead.plan_b():
        lead.plan_b_fallback = {"text": FIRM_PLAN_B, "source": "tactical"}
    scoring.apply(lead)


def enrich_row(row: dict[str, Any], *, use_ai: bool = True, nearby: bool = True) -> Lead:
    """Identity (Places / DBPR) → site → buildings: the pipeline's enrichment (DBPR, cadastre, licences,
    cluster, AI, email, Plan B, ticket, score) + zone hook; firms: _enrich_firm. The CRM phone stays primary."""
    from . import pipeline, scoring
    from .web_extract import SiteFacts

    firm = is_firm(row.get("name"))
    if firm and not is_company_site(row.get("website_url")):
        # No site on the lead: our verified catalog of management firms may know its official domain.
        from . import mgmt_domains
        hit = mgmt_domains.from_catalog(row.get("name"))
        if hit:
            row = {**row, "website_url": f"https://{hit[1]}/"}
            logger.info("{}: dominio del catálogo verificado {}", row.get("name"), hit[1])
    found = identify(row, "portfolio" if firm else "onsite") if settings.google_places_api_key else None
    if found:
        firm = is_firm(row.get("name"), found.place_types)
    lead = to_lead(row, firm)
    if found:
        _merge_place(lead, found)
    if not firm and not dbpr.zip5(lead.address):
        rec = dbpr_by_name(lead.company_name)
        if rec:
            lead.address = rec["address"]
            lead.evidence["address_from"] = "dbpr_name"
    facts = SiteFacts()
    if lead.website_url:
        try:
            facts = pipeline.crawl(lead.website_url)
            pipeline._fill_from_site(lead, facts)
        except Exception as exc:                        # noqa: BLE001 — a dead site never stops the batch
            logger.warning("sitio de {} falló: {}", lead.company_name, exc)
    crm_phone = to_e164(row.get("phone")) or lead.phone
    if firm:
        lead.management_company = lead.company_name     # the firm is its own management company
        _enrich_firm(lead, facts, use_ai)
    else:
        pipeline.enrich(lead, facts, use_apollo=False, use_ai=use_ai, use_search=False, use_geo=True,
                        use_dbpr=True, check_mx=True)
    drop_implausible_contacts(lead)
    if crm_phone and lead.phone != crm_phone:          # the CRM line stays the primary
        lead.phone = crm_phone
        lead.evidence.pop("phone_replaced", None)
    if nearby and not firm and lead.lat is not None:
        pipeline.apply_competitor_intel([lead], [])
    scoring.apply(lead)
    return lead


UNIT_RX = re.compile(r"(?i),?\s+(?:apt|apartment|unit|#)\s*[\w-]+(?=,)")
LISTING_DOMAINS = {"condomatch.com", "zillow.com", "realtor.com", "apartments.com", "trulia.com", "redfin.com",
                   "condo.com", "keeq.io"}


def building_address(address: str | None) -> str | None:
    """A building's street address without the unit of the listing it came from ('… APT 603, Miami' → '…, Miami')."""
    return UNIT_RX.sub("", address) if address else address


def _site_phone(p: dict) -> bool:
    return p.get("source") not in ("crm", "google_places")


def drop_shared_boilerplate(leads: list[Lead]) -> dict[str, list[str]]:
    """A phone or email that the websites of two or more different leads print (a realtor's or listing portal's
    page about each building) belongs to that agent, not to any of the buildings: dropped everywhere, and so
    is any email on a listing-portal domain."""
    from collections import Counter

    phone_n = Counter(k for l in leads for k in {phone_key(x["number"]) for x in l.phones if _site_phone(x)})
    email_n = Counter(l.email.lower() for l in leads if l.email)
    bad_phones = {k for k, n in phone_n.items() if n >= 2}
    bad_emails = {e for e, n in email_n.items() if n >= 2} | {
        l.email.lower() for l in leads if l.email and l.email.split("@")[-1].lower() in LISTING_DOMAINS}
    for l in leads:
        l.phones = [p for p in l.phones if not (_site_phone(p) and phone_key(p["number"]) in bad_phones)]
        if l.email and l.email.lower() in bad_emails:
            l.evidence["email_dropped"] = l.email
            l.email, l.email_status = None, None
    return {"phones": sorted(bad_phones), "emails": sorted(bad_emails)}


def identified(lead: Lead) -> bool:
    """Enough identity to print facts about this lead (never a Ficha built on a guess)."""
    return bool(lead.evidence.get("places_match") or lead.public_records.get("dbpr")
                or lead.evidence.get("address_from") or lead.web_text)


def payload_row(row: dict[str, Any], lead: Lead, batch_id: str) -> dict[str, Any] | None:
    """Exactly what POST /api/public/leads-enrich takes for one lead (always by id); None when there is
    nothing worth sending."""
    if not identified(lead):
        return None
    p = lead.crm_payload(batch_id)
    out: dict[str, Any] = {"id": row["id"], "phone": to_e164(row.get("phone")) or row.get("phone"),
                           "name": clip(row.get("name"), 200)}
    if p.get("sales_strategy") and "Ficha:" in p["sales_strategy"]:
        out["sales_strategy"] = p["sales_strategy"]      # already line-preserving and <= 2000 (never clip(): it joins lines)
    for k in ("management_company", "contact_name", "contact_title", "email", "website_url", "maps_url"):
        if p.get(k):
            out[k] = p[k]
    addr = building_address(p.get("address")) if lead.evidence.get("lead_kind") == "building" else p.get("address")
    if addr and dbpr.zip5(addr) and "@" not in addr:
        out["address"] = addr
    if out.get("contact_name") and not out.get("contact_title"):
        out.pop("contact_name")                        # a bare name without a role misleads the setter
    useful = set(out) - {"id", "phone", "name"}
    return out if useful else None


# ── run ─────────────────────────────────────────────────────────────────────

def summarize(rows: list[dict[str, Any]], payloads: list[dict[str, Any]], response: dict[str, Any] | None) -> dict:
    by_id = {p["id"]: p for p in payloads}
    fields: dict[str, set] = {}
    for res in (response or {}).get("results") or []:
        fields[res.get("id")] = set(res.get("fields") or [])
    written = lambda f: sum(1 for i, fs in fields.items() if f in fs)  # noqa: E731
    return {
        "selected": len(rows), "sent": len(payloads), "not_sent_unidentified": len(rows) - len(payloads),
        "payload": {"ficha": sum("sales_strategy" in p for p in payloads),
                    "plan_b": sum("Plan B:" in (p.get("sales_strategy") or "") for p in payloads),
                    "management_company": sum("management_company" in p for p in payloads),
                    "address_zip": sum("address" in p for p in payloads),
                    "email": sum("email" in p for p in payloads),
                    "contact": sum("contact_name" in p for p in payloads)},
        "crm": None if response is None else {
            "updated": response.get("updated"), "unchanged": response.get("unchanged"),
            "errors": response.get("errors"), "not_found": len(response.get("not_found") or []),
            "ambiguous": len(response.get("ambiguous") or []), "rejected": len(response.get("rejected") or []),
            "ficha": written("sales_strategy"),
            "plan_b": sum(1 for i, fs in fields.items() if "sales_strategy" in fs
                          and "Plan B:" in (by_id.get(i, {}).get("sales_strategy") or "")),
            "management_company": written("management_company"), "address_zip": written("address"),
            "email": written("email"), "contact": written("contact_name")},
    }


def run(*, statuses: list[str] | None = None, limit: int | None = None, commit: bool = False, use_ai: bool = True,
        nearby: bool = True, include_attempted: bool = False, out_dir: Path | None = None) -> dict[str, Any]:
    """Read → enrich → dry run always → (commit) real send. Writes out/<batch>/ with every step."""
    from . import render, runlock

    with runlock.hold("backfill-crm"):
        costs.tracker.reset()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        batch_id = f"enrich-{stamp}"
        out = Path(out_dir or settings.out_dir) / batch_id
        out.mkdir(parents=True, exist_ok=True)
        rows = select(fetch_unenriched(include_attempted), statuses, limit)
        logger.info("backfill-crm {}: {} leads ({})", batch_id, len(rows), ", ".join(statuses or ["todos"]))
        payloads, worked, done = [], [], []
        try:
            for i, row in enumerate(rows, 1):
                try:
                    done.append((row, enrich_row(row, use_ai=use_ai, nearby=nearby)))
                except Exception as exc:                # noqa: BLE001 — one bad lead never stops the batch
                    logger.exception("backfill {}: {}", row.get("name"), exc)
                    worked.append({"id": row["id"], "name": row.get("name"), "error": str(exc)[:200]})
                    continue
                logger.info("[{}/{}] {} · {}", i, len(rows), row.get("name"), done[-1][1].evidence.get("lead_kind"))
        finally:
            render.close()
        shared = drop_shared_boilerplate([l for _, l in done])
        if shared["phones"] or shared["emails"]:
            logger.info("Teléfonos/emails de portales o agentes repetidos en varios sitios, descartados: {}", shared)
        for row, lead in done:
            p = payload_row(row, lead, batch_id)
            worked.append({"id": row["id"], "name": row.get("name"), "status": row.get("status"),
                           "kind": lead.evidence.get("lead_kind"), "match": lead.evidence.get("places_match"),
                           "sent": bool(p), "fields": sorted(set(p or {}) - {"id", "phone", "name"}),
                           "lead": lead.internal_record()})
            if p:
                payloads.append(p)
        (out / "backfill.jsonl").write_text("\n".join(json.dumps(w, ensure_ascii=False, default=str) for w in worked)
                                            + "\n", encoding="utf-8")
        (out / "enrich_payload.json").write_text(json.dumps({"batch_id": batch_id, "leads": payloads},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
        dry = post_enrich(payloads, batch_id, dry_run=True) if payloads else None
        (out / "crm_dry_run.json").write_text(json.dumps(dry, ensure_ascii=False, indent=2), encoding="utf-8")
        real = None
        if commit and payloads:
            if dry and (dry["ambiguous"] or dry["not_found"]):
                logger.warning("dry run: {} ambiguos / {} no encontrados (se envían igual por id; esos no cambian)",
                               len(dry["ambiguous"]), len(dry["not_found"]))
            real = post_enrich(payloads, batch_id, dry_run=False)
            (out / "crm_commit.json").write_text(json.dumps(real, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {"batch_id": batch_id, "out": str(out), "committed": bool(real),
                  **summarize(rows, payloads, real or dry), "costs": costs.tracker.report()}
        (out / "backfill_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def commit_saved(out: Path) -> dict[str, Any]:
    """Send the payload a previous run left in out/<batch>/ (the one reviewed in its dry run): a fresh dry run
    first, then the real write. Nothing is recomputed, so no API is paid twice."""
    out = Path(out)
    saved = json.loads((out / "enrich_payload.json").read_text(encoding="utf-8"))
    payloads, batch_id = saved["leads"], saved["batch_id"]
    report = json.loads((out / "backfill_report.json").read_text(encoding="utf-8"))
    if not payloads:
        return {**report, "committed": False}
    dry = post_enrich(payloads, batch_id, dry_run=True)
    (out / "crm_dry_run.json").write_text(json.dumps(dry, ensure_ascii=False, indent=2), encoding="utf-8")
    real = post_enrich(payloads, batch_id, dry_run=False)
    (out / "crm_commit.json").write_text(json.dumps(real, ensure_ascii=False, indent=2), encoding="utf-8")
    worked = [json.loads(l) for l in (out / "backfill.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    report = {**report, "committed": True, **summarize(worked, payloads, real)}
    (out / "backfill_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
