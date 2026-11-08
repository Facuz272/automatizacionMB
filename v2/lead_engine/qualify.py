"""Deterministic gates and precalification rules (pure, tested).

The AI step improves these, but every lead is fully qualified even with no
ANTHROPIC_API_KEY: segment, strategy_kind and a sales hook always exist.
"""

from __future__ import annotations

import re

from .models import Lead
from .normalize import address_problem, to_e164

PAIN_TERMS = {
    "mold": "moho", "mildew": "moho", "algae": "algas", "stain": "manchas", "dirty": "suciedad",
    "filthy": "suciedad", "grime": "suciedad", "disgusting": "suciedad", "black streak": "manchas negras",
    "pressure wash": "lavado a presión", "power wash": "lavado a presión", "peeling": "pintura pelada",
    "rust": "óxido", "bird droppings": "excremento de aves", "pigeon": "palomas", "smell": "olor",
    "cobweb": "telarañas", "dirty windows": "ventanas sucias", "facade": "fachada",
    "poorly maintained": "mal mantenimiento", "not maintained": "mal mantenimiento",
    "maintenance is": "quejas de mantenimiento", "needs cleaning": "falta limpieza",
}
_PAIN_RE = re.compile("|".join(re.escape(t) for t in sorted(PAIN_TERMS, key=len, reverse=True)), re.I)
ONSITE_HINTS = re.compile(r"(?i)\b(on[- ]?site|management office|manager'?s office|front desk|concierge|building office)\b")


def contactability_problem(lead: Lead, state: str | None = None) -> str | None:
    """None when the lead can be called and visited; otherwise the reason it is held back."""
    if not lead.company_name.strip():
        return "sin nombre"
    if not lead.phone or to_e164(lead.phone) != lead.phone:
        return "sin teléfono E.164 válido"
    return address_problem(lead.address, state)


def pain_signals(reviews: list[str], max_items: int = 3) -> list[str]:
    """Short evidence snippets like 'moho: "...black mold on the balconies..."'."""
    out: list[str] = []
    for rv in reviews:
        m = _PAIN_RE.search(rv)
        if not m:
            continue
        start = max(0, m.start() - 50)
        snippet = " ".join(rv[start : m.end() + 60].split())
        label = PAIN_TERMS[m.group(0).lower()] if m.group(0).lower() in PAIN_TERMS else m.group(0).lower()
        out.append(f'{label}: "…{snippet}…"')
        if len(out) >= max_items:
            break
    return out


def infer_management_type(lead: Lead) -> str:
    if lead.management_company:
        return "third_party"
    if lead.strategy_kind == "portfolio":
        return "third_party"
    if ONSITE_HINTS.search(lead.web_text or "") or "on-site" in " ".join(lead.reviews).lower():
        return "onsite"
    return lead.management_type or "unknown"


def rule_qualify(lead: Lead) -> None:
    """Fill segment / management_type / sales_strategy with rules (AI may override later)."""
    lead.pain_signals = lead.pain_signals or pain_signals(lead.reviews)
    if lead.management_type == "unknown":
        lead.management_type = infer_management_type(lead)
    if not lead.segment:
        lead.segment = "Dolor Urgente" if lead.pain_signals or lead.strategy_kind == "pain" else "Gran Comunidad"
    if not lead.sales_strategy:
        lead.sales_strategy = default_hook(lead)


def default_hook(lead: Lead) -> str:
    who = lead.contact_name or ("el/la Property Manager" if lead.strategy_kind != "portfolio" else "el director de operaciones")
    if lead.pain_signals:
        issue = lead.pain_signals[0].split(":", 1)[0]
        return (f"Reseñas mencionan {issue}: pedir por {who} y ofrecer una inspección gratis de fachada/áreas "
                f"comunes antes de que la junta reciba más quejas.")
    if lead.strategy_kind == "portfolio":
        n = f"{lead.properties_count} propiedades" if lead.properties_count else "varias comunidades"
        return (f"Administradora con {n}: pedir por {who} y proponer un proveedor único de softwash "
                f"con precio por portafolio y una prueba en un edificio.")
    return (f"Edificio de condominio: pedir por {who} y ofrecer inspección gratis de fachada, "
            f"garajes y áreas comunes para el próximo presupuesto de mantenimiento.")
