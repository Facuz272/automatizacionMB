"""Claude precalification: one structured-output call per lead.

Input: everything collected (Places, site text, labelled phones, building
profile from OpenStreetMap, pain quotes with dates, DBPR registry, external
search hits, Apollo). Output: decision maker, management, segment, strategy and
the 1–2 line hook the setter reads before dialing.

Guards against invented data: a contact name / management company is accepted
only if it appears in the evidence we sent, an email only if it is one of the
candidate emails, and a hook that names a surface (pool, garage, tennis, gate,
roof) or a review that is not in the evidence is replaced by the rule hook.
Phone and address are never touched by the model.
"""

from __future__ import annotations

import json
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field

from . import costs
from .config import settings
from .models import Lead
from .normalize import clean_email

SYSTEM = """You qualify B2B leads for MB Softwash Miami, an exterior soft-washing / pressure-cleaning \
company (building facades, garages, walkways, pool decks, roofs) selling to condominium associations, \
HOAs and property management companies in South Florida. A Spanish-speaking setter will phone each lead.

Rules:
- Use ONLY facts present in the evidence. Never invent people, emails, counts or companies; use null when unknown.
- contact_name: the person who decides on building maintenance (Property Manager, LCAM/CAM, General/Building \
Manager, Board President). Prefer on-site managers over corporate executives.
- management_type: "onsite" when there is a manager/office in the building, "third_party" when an external \
management company runs it, "self_managed" when the board runs it without a manager, else "unknown".
- segment: "Dolor Urgente" when there is visible pain (reviews about dirt, mold, stains, poor maintenance, \
recent violations); otherwise "Gran Comunidad" (large/valuable community without explicit pain).
- strategy_kind: "onsite" (building with its own management), "pain" (explicit pain signals), \
"portfolio" (management company with several properties).
- sales_strategy: 1-2 short lines IN SPANISH for the setter: who to ask for and a specific hook that cites \
concrete evidence: real washable surfaces from building.amenities / levels / coastal exposure (e.g. "deck de \
piscina y parking multinivel expuestos al salitre") and, when present, the strongest pain quote with its date \
(e.g. "reseña de 2026-03 menciona moho en los pasillos"). Mention ONLY surfaces listed in the evidence. \
No greetings, no generic marketing text, max 300 characters.
- public_records.dbpr is the official Florida condominium registry (association, units, care-of line): trust it \
for the management company / on-site office unless the website clearly says otherwise.
- vertical is the kind of property; adapt the hook's vocabulary to it:
  community (Gran Comunidad): board and residents, salt air and mold on the facade, recertification / milestone \
inspection, next board meeting;
  plaza (Shopping/Plaza Comercial): foot traffic, sidewalks and storefronts, oil/grease stains in the parking lot, \
dumpster pads, night work without closing stores;
  commercial (Edificio Comercial): corporate image, facade and glass, main entrance, garage, after-hours work;
  clinic (Clínica/Hospital): sanitary image at patient entrances, slippery walkways (fall risk), compliance, work \
outside patient hours.
- contacts lists every decision maker found with a role; when the primary contact is a manager, you may name the \
facilities/engineering contact as the second door ("si no está, pedir por el Chief Engineer …").
- cadastre is the official Miami-Dade Property Appraiser record (year built, floors, area, owner): prefer it over \
any estimate. A "milestone" pain signal means the building is in Florida's legal milestone-inspection window.
- municipal_signals are official city/county records (40-year recertification pending, unsafe structure, open \
maintenance code case, recent exterior permit): when present, open the hook with them — they are the most urgent pain.
- portfolio_cluster lists other condos run by the same management firm/manager: when count > 1, suggest a combined \
inspection naming one or two of them.
- search.hits are third-party web results that already mention this building; use them for management company \
or manager names only when the snippet clearly ties them to this building."""


class Enrichment(BaseModel):
    contact_name: str | None = Field(description="Decision maker full name, or null")
    contact_title: str | None = Field(description="Their title, e.g. Property Manager, LCAM, Board President")
    email: str | None = Field(description="Best email, copied exactly from candidate_emails, or null")
    management_company: str | None = Field(description="External management company name, or null")
    management_type: Literal["onsite", "third_party", "self_managed", "unknown"]
    properties_count: int | None = Field(description="Number of managed properties (portfolio only), or null")
    segment: Literal["Dolor Urgente", "Gran Comunidad"]
    strategy_kind: Literal["onsite", "pain", "portfolio"]
    sales_strategy: str
    confidence: Literal["high", "medium", "low"]


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic

        # Explicit budget: a hung request costs at most timeout × (max_retries + 1), never the SDK's 10 min.
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=settings.anthropic_timeout_s,
                                      max_retries=2)
    return _client


def available() -> bool:
    return bool(settings.anthropic_api_key)


def build_evidence(lead: Lead, people: list[tuple[str, str]], emails: list[str], managed_by: list[str],
                   apollo: dict | None) -> dict:
    b = lead.building or {}
    return {
        "company_name": lead.company_name,
        "address": lead.address,
        "website": lead.website_url,
        "google": {"rating": lead.google_rating, "reviews": lead.total_reviews, "types": lead.place_types[:6]},
        "strategy_hint": lead.strategy_kind,
        "phones": [{"number": p["number"], "label": p["label"], "context": p.get("context", "")[:80]} for p in lead.phones[:6]],
        "building": {
            "levels": b.get("levels"), "footprint_m2": b.get("footprint_m2"),
            "complex_footprint_m2": b.get("complex_footprint_m2"), "units": b.get("units"), "roof": b.get("roof"),
            "amenities": sorted((b.get("amenities") or {}).keys()), "coastal": b.get("coastal"),
            "coast_distance_m": b.get("coast_distance_m"), "large": b.get("large"),
        },
        "people_found_on_site": [{"name": n, "title": t} for n, t in people[:8]],
        "apollo_contact": apollo,
        "candidate_emails": emails[:10],
        "managed_by_mentions": managed_by[:5],
        "public_records": {"dbpr": lead.public_records.get("dbpr")},
        "search": {"hits": lead.search_hits[:6], "companies": lead.evidence.get("search_companies", [])[:5],
                   "people": lead.evidence.get("search_people", [])[:5]},
        "pain_signals": [{"label": p["label"], "quote": p["quote"], "date": p.get("date"), "source": p["source"]}
                         for p in lead.pain[:4]],
        "pain_score": lead.pain_score,
        "municipal_signals": [{"label": m["label"], "quote": m["quote"], "date": m.get("date")} for m in lead.municipal[:3]],
        "vertical": lead.vertical,
        "contacts": lead.contacts[:6],
        "office_hours": lead.office_hours,
        "cadastre": {k: lead.cadastre.get(k) for k in ("year_built", "floors", "building_sqft", "units", "owner_name",
                                                       "care_of", "land_use", "assessed_value")} if lead.cadastre else None,
        "lodging": lead.lodging or None,
        "management_domain": lead.management_domain or None,
        "licenses": lead.licenses,
        "portfolio_cluster": {"count": lead.portfolio_cluster.get("count"), "by": lead.portfolio_cluster.get("by"),
                              "siblings": [x["name"] for x in lead.portfolio_cluster.get("siblings", [])]}
        if lead.portfolio_cluster else None,
        "estimated_annual_contract_usd": {"low": lead.estimated_deal.get("low"), "high": lead.estimated_deal.get("high")}
        if lead.estimated_deal else None,
        "reviews": [{"text": r["text"][:400], "date": r.get("published") or r.get("relative"), "rating": r.get("rating")}
                    for r in lead.reviews[:5]],
        "site_text": (lead.web_text or "")[:5000],
    }


def enrich(lead: Lead, evidence: dict) -> Enrichment | None:
    if not available():
        return None
    import anthropic

    try:
        response = _get_client().messages.parse(
            model=settings.anthropic_model,
            max_tokens=2048,
            system=SYSTEM,
            output_config={"effort": settings.anthropic_effort},
            messages=[{"role": "user", "content": "Evidence (JSON):\n" + json.dumps(evidence, ensure_ascii=False)}],
            output_format=Enrichment,
        )
    except anthropic.RateLimitError as exc:
        logger.warning("Claude rate limit en {}: {}", lead.company_name, exc)
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("Claude HTTP {} en {}: {}", exc.status_code, lead.company_name, exc.message)
        return None
    except anthropic.APIConnectionError as exc:      # includes APITimeoutError
        logger.warning("Claude sin conexión o sin respuesta a tiempo en {}: {}", lead.company_name, exc)
        return None
    usage = getattr(response, "usage", None)
    if usage is not None:
        costs.tracker.add("claude_input_token", int(getattr(usage, "input_tokens", 0) or 0))
        costs.tracker.add("claude_output_token", int(getattr(usage, "output_tokens", 0) or 0))
    if response.stop_reason == "refusal" or response.parsed_output is None:
        logger.warning("Claude no devolvió precalificación para {} ({})", lead.company_name, response.stop_reason)
        return None
    return response.parsed_output


# Surface words the hook may only use when the evidence supports them.
SURFACE_GROUPS = {
    "pool_deck": ("piscina", "pool"),
    "parking_garage": ("garaje", "garage", "parking", "estacionamiento"),
    "tennis_court": ("tenis", "tennis", "pickleball"),
    "gatehouse": ("garita", "gatehouse", "guard gate"),
    "roof": ("techo", "roof"),
}


def unsupported_hook(hook: str, evidence: dict) -> str | None:
    """Name of the first claim in the hook that the evidence does not support, else None."""
    h = hook.lower()
    amenities = set((evidence.get("building") or {}).get("amenities") or [])
    blob = json.dumps({k: v for k, v in evidence.items() if k not in ("building",)}, ensure_ascii=False).lower()
    for key, words in SURFACE_GROUPS.items():
        if any(w in h for w in words) and key not in amenities and not any(w in blob for w in words):
            return key
    if ("reseña" in h or "review" in h) and not evidence.get("pain_signals") and not evidence.get("reviews"):
        return "review"
    if "salitre" in h and not (evidence.get("building") or {}).get("coastal"):
        return "coastal"
    return None


def _mentioned(name: str, evidence: dict) -> bool:
    blob = json.dumps(evidence, ensure_ascii=False).lower()
    parts = [p for p in name.lower().split() if len(p) > 1]
    return bool(parts) and all(p in blob for p in parts)


def apply(lead: Lead, result: Enrichment, evidence: dict) -> None:
    """Copy AI output onto the lead, dropping anything not backed by evidence."""
    invented = False
    if result.contact_name and _mentioned(result.contact_name, evidence):
        lead.contact_name = result.contact_name.strip()
        lead.contact_title = (result.contact_title or lead.contact_title or "").strip() or None
    elif result.contact_name:
        lead.evidence["ai_dropped_contact"] = result.contact_name
        invented = True
    if result.email:
        e = clean_email(result.email)
        if e and e in evidence.get("candidate_emails", []):
            lead.email = e
            lead.email_source = lead.email_source or "ai_choice"
    from .web_extract import firm_like

    if result.management_company and not firm_like(result.management_company):
        # "a volunteer board" / "the owners": a description, not a company — keep the management type only
        lead.evidence["ai_management_description"] = result.management_company
        lead.management_type = result.management_type
    elif result.management_company and _mentioned(result.management_company, evidence):
        lead.management_company = result.management_company.strip()
        lead.management_type = result.management_type
    elif result.management_company:
        lead.evidence["ai_dropped_management"] = result.management_company
        invented = True
    else:
        lead.management_type = result.management_type
    if result.properties_count and result.properties_count > 0 and str(result.properties_count) in evidence["site_text"]:
        lead.properties_count = result.properties_count
    lead.segment = result.segment
    lead.strategy_kind = result.strategy_kind
    # A hook built on an unsupported name/company/surface would mislead the setter: keep the rule-based one.
    bad = unsupported_hook(result.sales_strategy, evidence)
    if bad:
        lead.evidence["ai_dropped_hook"] = {"reason": bad, "hook": result.sales_strategy}
    if not invented and not bad:
        lead.sales_strategy = result.sales_strategy.strip()[:400]
    lead.evidence["ai_confidence"] = result.confidence
    lead.ai_used = True
