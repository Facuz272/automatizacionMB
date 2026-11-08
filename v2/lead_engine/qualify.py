"""Deterministic gates and precalification rules (pure, tested).

The AI step improves these, but every lead is fully qualified even with no
ANTHROPIC_API_KEY: segment, management type and an evidence-based hook (real
surfaces from the building profile + the strongest pain quote with its date).
"""

from __future__ import annotations

import re

from .models import AMENITY_LABEL, Lead
from .verticals import VOCAB
from .dbpr import _title_case
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
    """Review/site pains plus municipal signals; municipal records always lead (highest priority)."""
    signals, score = detect(lead.reviews, lead.web_text)
    # A real municipal record outranks the computed milestone window at the same weight.
    muni = sorted(lead.municipal, key=lambda s: (-s["weight"], s.get("layer") == "milestone_rule"))
    lead.pain = muni + signals
    lead.pain_score = min(10.0, round(score + sum(s["weight"] for s in muni), 2))


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
    """Washable surfaces in the vertical's words, e.g. community: 'deck de piscina, garaje/parking multinivel y
    fachada de 32 pisos expuesta al salitre'; plaza: 'aceras, frentes de locales, estacionamiento… y dumpster pads'."""
    b = lead.building or {}
    if lead.vertical != "community":
        base = VOCAB[lead.vertical]["surfaces"]
        if b.get("levels") and lead.vertical == "commercial":
            base = base.replace("fachada", f"fachada de {b['levels']} pisos", 1)
        return base + (" expuestas al salitre" if b.get("coastal") else "")
    items = [AMENITY_LABEL[a] for a in AMENITY_LABEL if a in (b.get("amenities") or {})][:max_items]
    facade = "fachada"
    if b.get("levels"):
        facade += f" de {b['levels']} pisos"
    if b.get("coastal"):
        facade += " expuesta al salitre"
    items.append(facade)
    return ", ".join(items[:-1]) + (" y " if len(items) > 1 else "") + items[-1]


def cluster_phrase(lead: Lead) -> str:
    cl = lead.portfolio_cluster or {}
    sibs = [x["name"] for x in cl.get("siblings", [])[:2]]
    if cl.get("count", 0) <= 1 or not sibs:
        return ""
    by = "misma administradora" if cl.get("by") == "company" else "mismo administrador"
    return f" Proponer inspección combinada con {' y '.join(sibs)} ({by}, {cl['count']} condominios en la zona)."


def default_hook(lead: Lead) -> str:
    v = VOCAB.get(lead.vertical, VOCAB["community"])
    if lead.source == "dbpr" and lead.management_company:
        default_who = "el/la CAM asignado/a al edificio"
    elif lead.vertical != "community":
        default_who = v["ask_for"]
    else:
        default_who = "el/la Property Manager" if lead.strategy_kind != "portfolio" else "el director de operaciones"
    who = lead.contact_name or default_who
    if lead.contact_name and lead.contact_title:
        who = f"{lead.contact_name} ({lead.contact_title})"
    surfaces = surfaces_phrase(lead)
    multi = cluster_phrase(lead)
    if lead.pain:
        top = lead.pain[0]
        limit = 240 if top["source"] == "municipal" else 130     # official records are quoted whole
        quote = top["quote"] if len(top["quote"]) <= limit else top["quote"][: limit - 3] + "…"
        quote = quote.rstrip(" .")
        if top["source"] == "municipal":
            return (f"Registro municipal: {top['label']} — {quote}. Pedir por {who} y ofrecer inspección gratis "
                    f"de {surfaces} antes del plazo.{multi}")
        if lead.vertical != "community":
            when = f" ({top['date']})" if top.get("date") else ""
            return (f"Reseña{when} de {v['audience']} menciona {top['label']}: «{quote}». Pedir por {who}: "
                    f"{v['close']} ({surfaces}).")
        when = f" ({top['date']})" if top.get("date") else ""
        src = "Reseña" if top["source"] == "review" else "Su web"
        return (f"{src}{when} menciona {top['label']}: «{quote}». Pedir por {who} y ofrecer "
                f"inspección gratis de {surfaces}.{multi}")
    if lead.vertical != "community":
        owner = (lead.cadastre or {}).get("owner_name")
        lead_in = f"Propiedad de {_title_case(owner)}" + (f", administrada por {lead.management_company}" if lead.management_company else "") \
            + ". " if owner else ""
        return f"{lead_in}Pedir por {who}: {v['angle']}. Ofrecer {v['close']} ({surfaces})."
    if lead.source == "dbpr" and lead.management_company:
        return f"Edificio administrado por {lead.management_company}: pedir por {who} y ofrecer inspección gratis de {surfaces}.{multi}"
    if lead.strategy_kind == "portfolio":
        n = f"{lead.properties_count} propiedades" if lead.properties_count else "varias comunidades"
        return (f"Administradora con {n}: pedir por {who} y proponer un proveedor único de softwash "
                f"con precio por portafolio y una prueba en un edificio.{multi}")
    return f"Pedir por {who} y ofrecer inspección gratis de {surfaces} para el próximo presupuesto de mantenimiento.{multi}"
