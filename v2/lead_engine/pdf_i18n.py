"""Texts for the client-facing PDFs (US commercial English by default, Spanish optional).

Florida terminology: SB 4-D Milestone Structural Inspection (F.S. 553.899), county 40-Year
Recertification, softwash biological treatment, surface preparation before elastomeric coating.
Official signals are rendered from their structured `data` (municipal_signals / appraiser), not
from the Spanish setter line, so the English brief never mixes languages.
"""

from __future__ import annotations

import re
from typing import Any

T: dict[str, dict[str, str]] = {
    "en": {
        "kicker": "PRELIMINARY EXTERIOR MAINTENANCE & MILESTONE COMPLIANCE BRIEF",
        "prepared_for": "Prepared for", "date": "Date", "admin_default": "Board of Directors / Property Management",
        "record_title": "Official property record",
        "property": "Property", "address": "Address", "type": "Property type", "year_built": "Year built",
        "years": "yrs", "stories": "Stories", "units": "Units", "area": "Building area", "lot": "Lot size",
        "land_use": "Land use", "assessed": "Assessed value", "association": "Association",
        "management": "Management", "folio": "Folio", "sources": "Sources", "ref": "Ref.",
        "src_pa": "Miami-Dade Property Appraiser", "src_dbpr": "Florida DBPR condominium registry",
        "src_none": "Public records and field survey",
        "s1": "1. Preliminary technical assessment", "s2": "2. Proposed softwash scope",
        "s3": "3. Estimated annual contract range", "s4": "4. Next step",
        "component": "Component", "range": "Estimated range / year", "total": "Total estimated annual",
        "no_range": "The range is set during the on-site inspection (surface data missing).",
        "siblings": "Other {firm} buildings to quote in the same visit",
        "sib_cols": ["Property", "Address", "Units", "Signal", "Est. / year"],
        "next": "Free on-site inspection (approx. 60 minutes) to measure surfaces, sample the staining and deliver a fixed quote.",
        "disclaimer": ("Preliminary estimate based on public records (Miami-Dade Property Appraiser, Florida DBPR, municipal "
                       "records) and public reviews. It is not a structural inspection, an engineering opinion or a binding "
                       "quote; amounts may change after the site visit. Milestone inspections must be performed by a "
                       "Florida-licensed engineer or architect; our service covers exterior cleaning and surface preparation."),
        "docs": "{docs} available upon request.",
        "qr": "Scan to book the inspection",
        "footer": "Preliminary brief", "page": "Page {n} of {total}",
        "review": "Review", "website": "Website",
        "coastal": "<b>Salt-air exposure.</b> The building is {where}: salt and humidity accelerate mold, algae and "
                   "corrosion on facades, railings and garages.",
        "coast_near": "about {m} m from the coastline", "coast_zone": "in the coastal zone",
        "zone": "<b>Area history.</b> Nearby buildings report that {weak}. Our approach: {arg}.",
        "surfaces": "<b>Surfaces noted.</b> ",
        "none": "No public findings on record: the assessment is completed during the on-site inspection.",
        "rope": "Rope access / certified equipment for {n} stories.",
        "freq": "Suggested frequency with salt exposure: facade every 6–8 months, traffic areas quarterly.",
        "elasto": "Surface preparation before elastomeric coating or repainting, coordinated with the recertification repairs.",
    },
    "es": {
        "kicker": "INFORME TÉCNICO PRELIMINAR DE PREVENTA",
        "prepared_for": "Preparado para", "date": "Fecha", "admin_default": "Administración del edificio",
        "record_title": "Datos oficiales de la propiedad",
        "property": "Propiedad", "address": "Dirección", "type": "Tipo", "year_built": "Año de construcción",
        "years": "años", "stories": "Pisos", "units": "Unidades", "area": "Superficie construida", "lot": "Lote",
        "land_use": "Uso de suelo", "assessed": "Valor tasado", "association": "Asociación",
        "management": "Administración", "folio": "Folio", "sources": "Fuente", "ref": "Ref.",
        "src_pa": "Miami-Dade Property Appraiser", "src_dbpr": "DBPR Florida (padrón de condominios)",
        "src_none": "Registros públicos y relevamiento propio",
        "s1": "1. Diagnóstico técnico preliminar", "s2": "2. Alcance propuesto del servicio Softwash",
        "s3": "3. Rango estimado del contrato anual", "s4": "4. Próximo paso",
        "component": "Componente", "range": "Rango estimado / año", "total": "Total anual estimado",
        "no_range": "El rango se define en la inspección en sitio (faltan datos de superficie).",
        "siblings": "Otros edificios de {firm} para cotizar en la misma visita",
        "sib_cols": ["Propiedad", "Dirección", "Unidades", "Señal", "Est. / año"],
        "next": "Inspección gratuita en sitio (aprox. 60 minutos) para medir superficies, tomar muestras de manchas y "
                "entregar una cotización cerrada.",
        "disclaimer": ("Estimación preliminar basada en registros públicos (Miami-Dade Property Appraiser, DBPR, registros "
                       "municipales) y reseñas públicas. No constituye una inspección estructural ni una cotización "
                       "vinculante; los importes pueden variar tras la inspección. Las inspecciones de hito (Milestone) "
                       "las realiza un ingeniero o arquitecto con licencia en Florida; nuestro servicio cubre la limpieza "
                       "exterior y la preparación de superficies."),
        "docs": "{docs} disponibles a pedido.",
        "qr": "Escaneá para agendar la inspección",
        "footer": "Informe preliminar", "page": "Página {n} de {total}",
        "review": "Reseña", "website": "Sitio web",
        "coastal": "<b>Exposición al salitre.</b> El edificio está {where}: el salitre y la humedad aceleran moho, "
                   "algas y corrosión en fachada, barandas y garajes.",
        "coast_near": "a ~{m} m de la costa", "coast_zone": "en zona costera",
        "zone": "<b>Antecedentes en la zona.</b> En edificios cercanos se reporta que {weak}. Nuestro enfoque: {arg}.",
        "surfaces": "<b>Superficies relevadas.</b> ",
        "none": "Sin hallazgos públicos registrados: el diagnóstico se completa en la inspección en sitio.",
        "rope": "Acceso por cuerdas / equipos certificados para {n} pisos.",
        "freq": "Frecuencia sugerida por salitre: fachada cada 6 a 8 meses, áreas de tránsito trimestral.",
        "elasto": "Preparación de superficies antes de pintura elastomérica, coordinada con las reparaciones de recertificación.",
    },
}

VERTICAL = {
    "en": {"community": "Condominium / HOA", "plaza": "Shopping center / retail plaza", "commercial": "Commercial building",
           "clinic": "Clinic / hospital"},
    "es": {"community": "Condominio / comunidad", "plaza": "Centro comercial", "commercial": "Edificio comercial",
           "clinic": "Clínica / hospital"},
}
COMPONENT = {
    "en": {"facade": "Facade (softwash)", "walkways": "Walkways & sidewalks", "garage": "Parking garage",
           "pool_deck": "Pool deck", "courts": "Courts", "gatehouse": "Gatehouse",
           "lot_hardscape": "Parking lot & exterior hardscape"},
    "es": {"facade": "Fachada (softwash)", "walkways": "Caminos y aceras", "garage": "Garaje / parking",
           "pool_deck": "Deck de piscina", "courts": "Canchas", "gatehouse": "Garita de acceso",
           "lot_hardscape": "Estacionamiento y áreas exteriores del lote"},
}
AMENITY = {
    "en": {"parking_garage": "parking garage", "pool_deck": "pool deck", "tennis_court": "courts",
           "sidewalks": "walkways", "gatehouse": "gatehouse"},
    "es": {"parking_garage": "garaje", "pool_deck": "deck de piscina", "tennis_court": "canchas",
           "sidewalks": "caminos/aceras", "gatehouse": "garita"},
}
SCOPE = {
    "en": {
        "community": ["Softwash biological treatment of facade and balconies (mold, algae, black streaks) at low pressure.",
                      "Garage decks and ramps: oil, rust and grime removal.",
                      "Common areas: walkways, pool deck, entrances and trash room."],
        "plaza": ["Sidewalks and storefronts: gum, grease and foot-traffic staining.",
                  "Parking lot oil and grease stains; dumpster pads cleaned, sanitized and deodorized.",
                  "Facade and canopies after hours, with no store closures."],
        "commercial": ["Softwash of facade, canopies and ground-floor glazing.",
                       "Main entrance, plaza and perimeter sidewalks.",
                       "Parking garage ramps, columns and oil stains, after hours."],
        "clinic": ["Patient entrances, ramps and sidewalks: cleaning and anti-slip biofilm treatment.",
                   "Parking and ambulance bays; facade cleaned with healthcare-safe products.",
                   "Scheduled outside patient hours."],
    },
    "es": {
        "community": ["Softwash de fachada y balcones con tratamiento biológico contra moho, algas y manchas negras (baja presión).",
                      "Limpieza de garaje y rampas: manchas de aceite, óxido y suciedad acumulada.",
                      "Áreas comunes exteriores: caminos, deck de piscina, entradas y cuarto de basura."],
        "plaza": ["Aceras y frentes de locales: chicle, grasa y manchas de tráfico peatonal.",
                  "Estacionamiento: manchas de aceite y grasa; dumpster pads con desinfección y control de olores.",
                  "Fachada y marquesinas en horario nocturno, sin cerrar locales."],
        "commercial": ["Softwash de fachada, marquesinas y vidrios de planta baja.",
                       "Entrada principal, plaza de acceso y aceras perimetrales.",
                       "Garaje: rampas, columnas y manchas de aceite; trabajo fuera de horario."],
        "clinic": ["Entradas de pacientes, rampas y aceras: limpieza y tratamiento antideslizante del biofilm.",
                   "Estacionamiento y dársenas de ambulancias; fachada con productos aptos para áreas de salud.",
                   "Programación fuera del horario de atención."],
    },
}
PAIN_EN = {
    "mold": "Mold / mildew", "algae": "Algae / green staining", "black_streaks": "Black streaks / roof staining",
    "violation": "HOA / code violation", "garage_stains": "Garage oil stains", "slippery": "Slippery walkways",
    "restoration": "Painting / restoration / recertification", "facade": "Dirty facade / windows",
    "hallways": "Dirty common areas", "trash": "Trash area odor", "rust": "Rust staining", "birds": "Bird droppings",
    "pressure_wash": "Overdue pressure cleaning", "dirty": "General soiling", "salt": "Salt exposure",
}
WEAKNESS_EN = {
    "recurrence": ("mold and staining return within weeks of high-pressure washing",
                   "softwash biological treatment kills the growth at low pressure instead of only blasting it off, so results last longer"),
    "pressure_damage": ("high pressure damaged paint, sealant or stucco",
                        "low-pressure cleaning that does not damage paint, sealant or stucco"),
    "streaks": ("crews leave streaks and marks on facades and floors", "even application and controlled rinsing, no streaks"),
    "unreliable": ("vendors miss dates or leave jobs half done", "written schedule and work completed section by section"),
    "mess": ("plants were damaged or water and chemicals got inside", "landscaping protection and water/product control"),
    "cost": ("owners complain about overcharges", "fixed quote after a free inspection"),
}
_REL_ES = re.compile(r"hace (\d+) (día|días|semana|semanas|mes|meses|año|años)")
_UNIT_EN = {"día": "day", "días": "days", "semana": "week", "semanas": "weeks", "mes": "month", "meses": "months",
            "año": "year", "años": "years"}


def when_en(s: str | None) -> str | None:
    if not s:
        return s
    m = _REL_ES.fullmatch(s.strip())
    return f"{m.group(1)} {_UNIT_EN[m.group(2)]} ago" if m else s


SIGNAL_LABEL_EN = {
    "milestone": "Milestone Inspection / Recertification window",
    "municipal_recert": "40-Year Recertification pending",
    "municipal_unsafe": "Open Unsafe Structure case",
    "municipal_violation": "Open building violation",
    "municipal_code": "Open code-compliance case",
    "municipal_permit": "Recent exterior work permit",
}
_MILESTONE_ES = re.compile(r"Construido en (\d{4}) \((\d+) años(, a <3 millas de la costa)?.*?(?:cumple|cumplió) (\d+) años "
                           r"(?:en (\d{4})|este año)")
_ES_WORDS = [("Permiso", "Permit"), ("recertificación", "recertification"), ("pendiente", "pending"),
             ("abierto", "open"), ("caso", "case"), ("desde", "since")]


def _data_from_quote(sig: dict[str, Any]) -> dict[str, Any] | None:
    """Structured data for signals stored before `data` existed (older leads.jsonl batches)."""
    if sig.get("category") != "milestone":
        return None
    m = _MILESTONE_ES.search(sig.get("quote") or "")
    if not m:
        return None
    built, age, ms = int(m.group(1)), int(m.group(2)), int(m.group(4))
    return {"year_built": built, "age": age, "milestone": ms, "milestone_year": built + ms,
            "coastal": bool(m.group(3)), "condo": "estructural del condado" not in (sig.get("quote") or "")}


def municipal_line(sig: dict[str, Any], lang: str) -> tuple[str, str]:
    """(label, text) for an official signal in the PDF language."""
    if lang == "es":
        return sig["label"], sig["quote"]
    d, c = sig.get("data") or _data_from_quote(sig), sig.get("category")
    if not d:
        text = sig.get("quote") or ""
        for es, en in _ES_WORDS:
            text = re.sub(rf"\b{es}\b", en, text)
        return SIGNAL_LABEL_EN.get(c, sig.get("label", "")), text
    if c == "milestone":
        coast = ", within 3 miles of the coast" if d.get("coastal") else ""
        verb = "Reaches" if d["milestone_year"] >= d["year_built"] + d["age"] else "Reached"   # this year or later
        if d.get("condo", True):
            return ("Milestone Inspection / Recertification window",
                    f"Built in {d['year_built']} ({d['age']} yrs{coast}). {verb} {d['milestone']} years in "
                    f"{d['milestone_year']}: SB 4-D Milestone Structural Inspection window (F.S. 553.899) and the county "
                    f"40-Year Recertification cycle.")
        return ("County building recertification window",
                f"Built in {d['year_built']} ({d['age']} yrs). {verb} {d['milestone']} years in {d['milestone_year']}: "
                f"county structural recertification cycle.")
    if c == "municipal_recert":
        plan = f", plan {d['plan']}" if d.get("plan") else ""
        return ("City of Miami 40-Year Recertification pending",
                f"{d.get('address')}: recertification {d.get('year')} pending ({d.get('detail')}{plan}).")
    if c == "municipal_unsafe":
        return ("Open Unsafe Structure case", f"Miami-Dade case {d.get('case')} at {d.get('address')}, opened {d.get('opened')}.")
    if c == "municipal_violation":
        return ("Open building violation", f"Miami-Dade case {d.get('case')} at {d.get('address')}, opened {d.get('opened')}.")
    if c == "municipal_code":
        return ("Open code-compliance case", f"{d.get('problem')} (case {d.get('case')}, opened {d.get('opened')}).")
    if c == "municipal_permit":
        return ("Recent exterior work permit", f"Permit {d.get('permit')} issued {d.get('issued')}: {d.get('work')}.")
    return sig["label"], sig["quote"]


def pain_label(p: dict[str, Any], lang: str) -> str:
    return PAIN_EN.get(p.get("category"), p.get("label", "")) if lang == "en" else p.get("label", "")


def weakness(comp: dict[str, Any], lang: str) -> tuple[str, str] | None:
    if not comp.get("top_weakness"):
        return None
    if lang == "en":
        return WEAKNESS_EN.get(comp["top_weakness"], (comp.get("top_weakness_es"), comp.get("argument")))
    return comp.get("top_weakness_es"), comp.get("argument")
