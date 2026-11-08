"""Claude precalification: one structured-output call per lead.

Input: everything collected (Places, site text, people/emails found, Apollo,
pain snippets). Output: decision maker, management, segment, strategy and the
1–2 line hook the setter reads before dialing.

Guards against invented data: a contact name is accepted only if it appears in
the evidence we sent, an email only if it is one of the candidate emails, and
the phone/address are never touched by the model.
"""

from __future__ import annotations

import json
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field

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
- sales_strategy: 1-2 short lines IN SPANISH for the setter: who to ask for and the specific hook based on \
the evidence. No greetings, no generic marketing text, max 280 characters."""


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

        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def available() -> bool:
    return bool(settings.anthropic_api_key)


def build_evidence(lead: Lead, people: list[tuple[str, str]], emails: list[str], managed_by: list[str],
                   apollo: dict | None) -> dict:
    return {
        "company_name": lead.company_name,
        "address": lead.address,
        "website": lead.website_url,
        "google": {"rating": lead.google_rating, "reviews": lead.total_reviews, "types": lead.place_types[:6]},
        "strategy_hint": lead.strategy_kind,
        "people_found_on_site": [{"name": n, "title": t} for n, t in people[:8]],
        "apollo_contact": apollo,
        "candidate_emails": emails[:10],
        "managed_by_mentions": managed_by[:5],
        "pain_snippets": lead.pain_signals[:3],
        "reviews": [r[:400] for r in lead.reviews[:5]],
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
    except anthropic.APIConnectionError as exc:
        logger.warning("Claude sin conexión en {}: {}", lead.company_name, exc)
        return None
    if response.stop_reason == "refusal" or response.parsed_output is None:
        logger.warning("Claude no devolvió precalificación para {} ({})", lead.company_name, response.stop_reason)
        return None
    return response.parsed_output


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
    if result.management_company and _mentioned(result.management_company, evidence):
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
    # A hook built on an unsupported name/company would mislead the setter: keep the rule-based one.
    if not invented:
        lead.sales_strategy = result.sales_strategy.strip()[:400]
    lead.evidence["ai_confidence"] = result.confidence
    lead.ai_used = True
