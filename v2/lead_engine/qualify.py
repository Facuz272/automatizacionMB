"""Deterministic gates and precalification rules (pure, tested).

The AI step improves these, but every lead is fully qualified even with no
ANTHROPIC_API_KEY: segment, management type and an evidence-based hook (real
surfaces from the building profile + the strongest pain quote with its date).
"""

from __future__ import annotations

import re

from .models import AMENITY_LABEL, Lead
from .normalize import address_problem, to_e164
from .pain import VERIFIED_PAIN, detect

ONSITE_HINTS = re.compile(r"(?i)\b(on[- ]?site (?:manag|office|staff)|management office|manager'?s office|building office|"
                          r"oficina de administraci[oó]n)\b")


def contactability_problem(lead: Lead, state: str | None = None) -> str | None:
    """None when the lead can be called and visited; otherwise the reason it is held back."""
    if not lead.company_name.strip():
        return "sin nombre"
    if not lead.phone or to_e164(lead.phone) != lead.phone:
        return "sin teléfono E.164 válido"
    return address_problem(lead.address, state)


def has_pain(lead: Lead) -> bool:
    """Any pain hit at all (used by the `pain` strategy gate)."""
    signals, _ = detect(lead.reviews)
    return bool(signals)


def detect_pain(lead: Lead) -> None:
    lead.pain, lead.pain_score = detect(lead.reviews, lead.web_text)


def infer_management_type(lead: Lead) -> str:
    if lead.management_company:
        return "third_party"
    if lead.strategy_kind == "portfolio":
        return "third_party"
    reviews = " ".join(r.get("text", "") for r in lead.reviews)
    if ONSITE_HINTS.search(lead.web_text or "") or ONSITE_HINTS.search(reviews):
        return "onsite"
    if any(p["label"] == "management" and p["source"] != "google_places" for p in lead.phones):
        return "onsite"
    return lead.management_type or "unknown"


def rule_qualify(lead: Lead) -> None:
    """Fill segment / management_type / sales_strategy with rules (AI may override later)."""
    if not lead.pain and lead.reviews:
        detect_pain(lead)
    if lead.management_type == "unknown":
        lead.management_type = infer_management_type(lead)
    if not lead.segment:
        urgent = lead.pain_score >= VERIFIED_PAIN or (lead.strategy_kind == "pain" and lead.pain)
        lead.segment = "Dolor Urgente" if urgent else "Gran Comunidad"
    if not lead.sales_strategy:
        lead.sales_strategy = default_hook(lead)


def surfaces_phrase(lead: Lead, max_items: int = 3) -> str:
    """'deck de piscina, garaje/parking multinivel y fachada de 32 pisos expuesta al salitre'."""
    b = lead.building or {}
    items = [AMENITY_LABEL[a] for a in AMENITY_LABEL if a in (b.get("amenities") or {})][:max_items]
    facade = "fachada"
    if b.get("levels"):
        facade += f" de {b['levels']} pisos"
    if b.get("coastal"):
        facade += " expuesta al salitre"
    items.append(facade)
    return ", ".join(items[:-1]) + (" y " if len(items) > 1 else "") + items[-1]


def default_hook(lead: Lead) -> str:
    who = lead.contact_name or ("el/la Property Manager" if lead.strategy_kind != "portfolio" else "el director de operaciones")
    if lead.contact_name and lead.contact_title:
        who = f"{lead.contact_name} ({lead.contact_title})"
    surfaces = surfaces_phrase(lead)
    if lead.pain:
        top = lead.pain[0]
        when = f" ({top['date']})" if top.get("date") else ""
        src = "Reseña" if top["source"] == "review" else "Su web"
        quote = top["quote"] if len(top["quote"]) <= 110 else top["quote"][:107] + "…"
        quote = quote.rstrip(" .")
        return (f"{src}{when} menciona {top['label']}: «{quote}». Pedir por {who} y ofrecer "
                f"inspección gratis de {surfaces}.")
    if lead.strategy_kind == "portfolio":
        n = f"{lead.properties_count} propiedades" if lead.properties_count else "varias comunidades"
        return (f"Administradora con {n}: pedir por {who} y proponer un proveedor único de softwash "
                f"con precio por portafolio y una prueba en un edificio.")
    return f"Pedir por {who} y ofrecer inspección gratis de {surfaces} para el próximo presupuesto de mantenimiento."
