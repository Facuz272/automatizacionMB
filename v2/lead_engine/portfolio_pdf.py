"""Two-page executive Portfolio Dossier per management firm (English by default, Spanish optional).

  page 1  firm, CAB licence, portfolio KPIs (buildings in the area, units, combined annual estimate,
          buildings analysed, in the milestone/recertification window, with open municipal records),
          most urgent properties table
  page 2  Preferred Vendor Program proposal: single vendor, volume tiers (tenant pvp_tiers), priority
          scheduling for milestone/recertification buildings, consolidated reporting, documents the
          tenant has (on request), suggested pilot, next steps, disclaimer; QR to book in the footer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.platypus import KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from .config import settings
from .pdf_common import draw_footer, num, qr_target, styles, table, txt, usd
from .pdf_generator import documents_line

T = {
    "en": {
        "kicker": "PORTFOLIO DOSSIER · PREFERRED VENDOR PROGRAM", "license": "Florida CAM firm license",
        "kpi": "Portfolio in the service area", "buildings": "Condominium buildings (Florida DBPR registry)",
        "units": "Total units", "est": "Combined annual exterior-maintenance estimate",
        "analyzed": "Largest buildings checked against the county Property Appraiser (Miami-Dade, east Broward)",
        "milestone": "Of those, in the SB 4-D Milestone / 40-Year Recertification window",
        "records": "Of those, with open municipal records (recertification, violations or recent exterior permits)",
        "cities": "Main cities", "urgent": "Most urgent properties", "col": ["Property", "City", "Units", "Built", "Signal"],
        "none_signal": "—", "pvp": "Preferred Vendor Program proposal",
        "pvp_items": [
            "One softwash vendor for every association you manage, with a single point of contact and consistent scope.",
            "Volume pricing: {tiers}.",
            "Priority scheduling for buildings in the Milestone Inspection / 40-Year Recertification window, coordinated "
            "with the engineer's timeline and the surface preparation before elastomeric coating.",
            "Consolidated quarterly report per building (before/after photos, areas treated, next service date) for "
            "board meetings.",
        ],
        "tier": "{pct}% off for {n}+ buildings", "pilot": "Suggested pilot",
        "pilot_text": "{name} ({city}): {why}. A free on-site inspection there gives the board a fixed quote and a "
                      "reference for the rest of the portfolio.",
        "why_default": "largest building in the portfolio", "next": "Next steps",
        "next_items": ["30-minute call to confirm the buildings and the service calendar.",
                       "Free inspection of the pilot building and fixed quote.",
                       "Program agreement with pricing by tier and quarterly reporting."],
        "disclaimer": ("Estimates based on public records (Florida DBPR condominium and CAM registries, Miami-Dade "
                       "Property Appraiser, municipal open data). Building counts include the associations whose registry "
                       "care-of line names your firm; amounts are preliminary and not a binding quote. Milestone "
                       "inspections are performed by Florida-licensed engineers or architects; our service covers "
                       "exterior cleaning and surface preparation."),
        "qr": "Scan to book a call", "footer": "Portfolio dossier", "page": "Page {n} of {total}",
    },
    "es": {
        "kicker": "DOSSIER DE PORTAFOLIO · PROGRAMA DE PROVEEDOR PREFERENTE", "license": "Licencia CAM de la firma (Florida)",
        "kpi": "Portafolio en la zona de servicio", "buildings": "Edificios de condominio (padrón DBPR)",
        "units": "Unidades totales", "est": "Estimación anual combinada de mantenimiento exterior",
        "analyzed": "Edificios más grandes cruzados con el catastro del condado (Miami-Dade, este de Broward)",
        "milestone": "De ellos, en ventana Milestone (SB 4-D) / recertificación",
        "records": "De ellos, con registros municipales abiertos (recertificación, violaciones o permisos de obra exterior)",
        "cities": "Ciudades principales", "urgent": "Propiedades más urgentes",
        "col": ["Propiedad", "Ciudad", "Unidades", "Año", "Señal"], "none_signal": "—",
        "pvp": "Propuesta de Programa de Proveedor Preferente",
        "pvp_items": [
            "Un único proveedor de softwash para todas las asociaciones que administran, con un contacto y un alcance uniforme.",
            "Precio por volumen: {tiers}.",
            "Prioridad de agenda para edificios en ventana de Milestone / recertificación, coordinada con el ingeniero y "
            "con la preparación de superficies antes de la pintura elastomérica.",
            "Informe trimestral consolidado por edificio (fotos antes/después, áreas tratadas, próxima fecha) para las "
            "reuniones de junta.",
        ],
        "tier": "{pct}% de descuento desde {n} edificios", "pilot": "Piloto sugerido",
        "pilot_text": "{name} ({city}): {why}. Una inspección gratuita ahí le da a la junta una cotización cerrada y una "
                      "referencia para el resto del portafolio.",
        "why_default": "el edificio más grande del portafolio", "next": "Próximos pasos",
        "next_items": ["Llamada de 30 minutos para confirmar los edificios y el calendario de servicio.",
                       "Inspección gratuita del edificio piloto y cotización cerrada.",
                       "Acuerdo del programa con precios por escala e informe trimestral."],
        "disclaimer": ("Estimaciones basadas en registros públicos (padrones DBPR de condominios y de CAM, Miami-Dade "
                       "Property Appraiser, datos abiertos municipales). La cantidad de edificios incluye las asociaciones "
                       "cuyo «C/O» en el padrón nombra a la firma; los importes son preliminares y no constituyen una "
                       "cotización. Las inspecciones Milestone las hacen ingenieros o arquitectos con licencia en Florida; "
                       "nuestro servicio cubre limpieza exterior y preparación de superficies."),
        "qr": "Escaneá para agendar una llamada", "footer": "Dossier de portafolio", "page": "Página {n} de {total}",
    },
}


def _signal_text(b: dict[str, Any], lang: str) -> str:
    from .pdf_i18n import municipal_line

    if not b.get("signals"):
        return T[lang]["none_signal"]
    label, _ = municipal_line(b["signals"][0], lang)
    return label


def tiers_text(lang: str) -> str:
    tiers = sorted(settings.pvp_tiers or [], key=lambda t: t[0])
    return "; ".join(T[lang]["tier"].format(n=n, pct=pct) for n, pct in tiers) or "—"


def build(firm: dict[str, Any], path: str | Path, lang: str | None = None) -> Path:
    lang = (lang or settings.pdf_lang or "en").lower()
    lang = lang if lang in T else "en"
    t = T[lang]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    st = styles()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.75 * inch, bottomMargin=1.35 * inch,
                            title=f"Portfolio Dossier · {firm['firm']}", author=settings.company_name)
    fw, fh = doc.width, doc.height
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d/%Y" if lang == "en" else "%d/%m/%Y")

    kpis = [[t["buildings"], num(firm["buildings"], lang)], [t["units"], num(firm["units"], lang)],
            [t["est"], f"{usd(firm['total_estimated_low'], lang)} – {usd(firm['total_estimated_usd'], lang)}"]]
    if firm.get("live_checked"):
        kpis += [[t["analyzed"], num(firm.get("analyzed"), lang)],
                 [t["milestone"], num(firm.get("milestone_window"), lang)],
                 [t["records"], num(firm.get("municipal_records"), lang)]]
    kpis.append([t["cities"], ", ".join(firm.get("cities") or []) or "—"])

    urgent = firm.get("urgent") or firm["building_list"][:10]
    rows = [t["col"]] + [[b["name"], b.get("city") or "", num(b.get("units"), lang), str(b.get("year_built") or "—"),
                          _signal_text(b, lang)] for b in urgent[:10]]

    page1 = [
        Paragraph(txt(settings.company_name), st["brand"]),
        Spacer(1, 0.45 * inch),
        Paragraph(t["kicker"], st["kicker"]),
        Paragraph(txt(firm["firm"]), st["title"]),
        Paragraph(f"{t['license']}: {txt(firm.get('license') or '—')} · {today}", st["base"]),
        Paragraph(t["kpi"], st["h2"]),
        table(kpis, [fw * 0.62, fw * 0.38], st),
        Paragraph(t["urgent"], st["h2"]),
        table(rows, [fw * 0.30, fw * 0.17, fw * 0.11, fw * 0.09, fw * 0.33], st, header=True, bold_first_col=False),
    ]

    pilot = urgent[0] if urgent else None
    why = t["why_default"]
    if pilot and pilot.get("signals"):
        from .pdf_i18n import municipal_line
        why = municipal_line(pilot["signals"][0], lang)[0]
    page2: list[Any] = [Paragraph(t["pvp"], st["h2"])]
    for item in t["pvp_items"]:
        page2.append(Paragraph(txt(item.format(tiers=tiers_text(lang))), st["bullet"], bulletText="•"))
    docs = documents_line(lang)
    if docs:
        page2.append(Paragraph(txt(docs), st["bullet"], bulletText="•"))
    if pilot:
        page2.append(Paragraph(t["pilot"], st["h2"]))
        page2.append(Paragraph(txt(t["pilot_text"].format(name=pilot["name"], city=pilot.get("city") or "", why=why)),
                               st["base"]))
    page2.append(Paragraph(t["next"], st["h2"]))
    page2 += [Paragraph(txt(x), st["bullet"], bulletText="•") for x in t["next_items"]]
    page2.append(Spacer(1, 8))
    page2.append(Paragraph(t["disclaimer"], st["small"]))

    story = [KeepInFrame(fw, fh, page1, mode="shrink"), PageBreak(), KeepInFrame(fw, fh, page2, mode="shrink")]
    footer_text = f"{settings.company_name} · {t['footer']} · {today}"
    target = qr_target()
    doc.build(story,
              onFirstPage=lambda c, d: draw_footer(c, d, footer_text, t["page"].format(n=1, total=2)),
              onLaterPages=lambda c, d: draw_footer(c, d, footer_text, t["page"].format(n=d.page, total=2),
                                                    qr=(target, t["qr"]) if target else None))
    return path
