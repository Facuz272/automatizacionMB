"""Business verticals: classification and sales vocabulary per segment.

Vertical (what the property is) is independent of the CRM ``segment`` field, which today
only accepts "Dolor Urgente" / "Gran Comunidad" (mbguiaclientes src/lib/lead-import.ts).
With CRM_SEGMENT_MODE=vertical the label below is sent as ``segment`` instead — only once
the CRM enum includes the four names.
"""

from __future__ import annotations

import re
from typing import Any

LABEL = {
    "community": "Gran Comunidad",
    "plaza": "Shopping/Plaza Comercial",
    "commercial": "Edificio Comercial",
    "clinic": "Clínica/Hospital",
}

# Google Places types → vertical (first match wins).
PLACES_TYPES = [
    ({"hospital", "doctor", "medical_lab", "dental_clinic", "medical_clinic", "physiotherapist", "dentist"}, "clinic"),
    ({"shopping_mall", "department_store", "supermarket", "grocery_store", "store", "clothing_store"}, "plaza"),
    ({"lodging", "hotel", "corporate_office", "bank", "insurance_agency", "lawyer", "accounting"}, "commercial"),
    ({"condominium_complex", "apartment_building", "apartment_complex", "housing_complex"}, "community"),
]
_NAME_HINTS = [
    (re.compile(r"(?i)hospital|clinic|medical|health|urgent care|surgery|surgical|dental|imaging"), "clinic"),
    (re.compile(r"(?i)plaza|shopping|shoppes|mall|marketplace|town center|outlet|retail"), "plaza"),
    (re.compile(r"(?i)office|corporate|center|tower|financial|bank|hotel|resort|inn\b|suites"), "commercial"),
    (re.compile(r"(?i)condo|condominium|homeowners|hoa|association|apartments|residences|villas|towers?\b"), "community"),
]

# Vocabulary the rule hook and Claude use per vertical.
VOCAB: dict[str, dict[str, Any]] = {
    "community": {
        "audience": "la junta y los residentes",
        "ask_for": "el/la Property Manager (LCAM)",
        "surfaces": "fachada, balcones, garaje y áreas comunes",
        "angle": "salitre y moho en fachada, recertificación / Milestone Inspection y la próxima asamblea de la junta",
        "close": "inspección gratis y presupuesto para la próxima reunión de la junta",
    },
    "plaza": {
        "audience": "los inquilinos y el tráfico peatonal",
        "ask_for": "el/la Property Manager del centro comercial",
        "surfaces": "aceras, frentes de locales, estacionamiento (manchas de aceite y grasa) y dumpster pads",
        "angle": "imagen para el tráfico peatonal, chicle y grasa en aceras, manchas de aceite en el parking y olores en dumpster pads",
        "close": "limpieza nocturna sin cerrar locales y plan mensual de aceras",
    },
    "commercial": {
        "audience": "los inquilinos corporativos y visitantes",
        "ask_for": "el/la Property Manager o el Chief Engineer",
        "surfaces": "fachada y vidrios, entrada principal, garaje y plaza de acceso",
        "angle": "imagen corporativa de la fachada y el lobby exterior, garaje y entradas",
        "close": "plan trimestral de fachada/garaje con trabajo fuera de horario",
    },
    "clinic": {
        "audience": "los pacientes",
        "ask_for": "el/la administrador/a o el/la Facilities Manager",
        "surfaces": "entradas de pacientes, rampas y aceras, estacionamiento y fachada",
        "angle": "imagen sanitaria en entradas de pacientes, aceras resbalosas (riesgo de caídas) y cumplimiento normativo",
        "close": "limpieza fuera del horario de atención con productos aptos para áreas de salud",
    },
}


def from_places_types(types: list[str]) -> str | None:
    t = set(types or [])
    for keys, v in PLACES_TYPES:
        if t & keys:
            return v
    return None


def from_name(name: str | None) -> str | None:
    for rx, v in _NAME_HINTS:
        if rx.search(name or ""):
            return v
    return None


def classify(*, appraiser: str | None = None, source: str | None = None, places_types: list[str] | None = None,
             name: str | None = None, lodging_kind: str | None = None) -> str:
    """Official land use wins, then the source registry, then Places types, then the name; default community."""
    if appraiser:
        return appraiser
    if source in ("dbpr",):
        return "community"
    if source == "npi":
        return "clinic"
    if lodging_kind in ("hotel", "motel", "apart-hotel"):
        return "commercial"
    return from_places_types(places_types or []) or from_name(name) or "community"


def label(vertical: str | None) -> str:
    return LABEL.get(vertical or "community", LABEL["community"])
