"""Two-page pre-sale technical report (PDF) per enriched lead.

  page 1  cover: company, report title, property, address, date, prepared-for, official record
          table (Miami-Dade Property Appraiser / DBPR), report reference
  page 2  1. technical diagnosis (age & milestone/recertification, municipal permits and cases,
             review evidence, coastal exposure, zone competitor findings)
          2. proposed softwash scope (by vertical and washable surfaces)
          3. estimated annual contract range (valuation components)
          4. next step + disclaimer
Each page is wrapped in a KeepInFrame that shrinks its content, so the report is always exactly
two pages whatever the lead carries. Input is the record written to out/<lote>/leads.jsonl.
Only Helvetica (WinAnsi) is used; characters outside cp1252 are replaced.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

from .config import settings

BRAND = colors.HexColor("#0E5A8A")
INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#61707E")
RULE = colors.HexColor("#D5DDE5")
FILL = colors.HexColor("#F2F6F9")
VERTICAL_ES = {"community": "Condominio / comunidad", "plaza": "Centro comercial", "commercial": "Edificio comercial",
               "clinic": "Clínica / hospital"}
COMPONENT_ES = {"facade": "Fachada (softwash)", "walkways": "Caminos y aceras", "garage": "Garaje / parking",
                "pool_deck": "Deck de piscina", "courts": "Canchas", "gatehouse": "Garita de acceso",
                "lot_hardscape": "Estacionamiento y áreas exteriores del lote"}
SCOPE = {
    "community": ["Softwash de fachada y balcones con tratamiento contra moho, algas y manchas negras (baja presión).",
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
}


def _txt(s: Any) -> str:
    """Escape for Paragraph markup and keep it cp1252-safe for the standard fonts."""
    t = str(s if s is not None else "")
    t = t.replace("≈", "~").replace("→", "->").replace("⚠️", "").replace("✓", "")
    t = "".join(ch if _cp1252(ch) else unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode() or "?"
                for ch in t)
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cp1252(ch: str) -> bool:
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _usd(n: Any) -> str:
    try:
        return f"US$ {int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _num(n: Any, suffix: str = "") -> str:
    try:
        return f"{int(n):,}".replace(",", ".") + suffix
    except (TypeError, ValueError):
        return "—"


def _cap(s: str) -> str:
    """Upper-case the first letter only ('ventana legal de Milestone' keeps 'Milestone')."""
    return s[:1].upper() + s[1:] if s else s


def safe_filename(name: str, max_len: int = 80) -> str:
    """'Oceanside Plaza Condo, The' -> 'Oceanside_Plaza_Condo_The' (ASCII, no path characters)."""
    base = unicodedata.normalize("NFKD", name or "lead").encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")
    return (base or "lead")[:max_len]


def _styles() -> dict[str, ParagraphStyle]:
    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK,
                          alignment=TA_LEFT)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.8, leading=10, textColor=MUTED),
        "brand": ParagraphStyle("brand", parent=base, fontName="Helvetica-Bold", fontSize=11, textColor=BRAND),
        "kicker": ParagraphStyle("kicker", parent=base, fontSize=9, textColor=MUTED, spaceAfter=4),
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=24, leading=29, spaceAfter=6),
        "sub": ParagraphStyle("sub", parent=base, fontSize=13, leading=17, textColor=INK, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=BRAND,
                             spaceBefore=10, spaceAfter=4),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=2, spaceAfter=2),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=11.5),
        "cellb": ParagraphStyle("cellb", parent=base, fontName="Helvetica-Bold", fontSize=9, leading=11.5),
    }


def _table(rows: list[list[Any]], widths: list[float], st: dict[str, ParagraphStyle], header: bool = False) -> Table:
    hdr = ParagraphStyle("hdr", parent=st["cellb"], textColor=colors.white)
    data = [[Paragraph(_txt(c), hdr if (header and i == 0) else st["cellb"] if j == 0 else st["cell"])
             for j, c in enumerate(r)] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, hAlign="LEFT")
    style = [("GRID", (0, 0), (-1, -1), 0.5, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
             ("BACKGROUND", (0, 0), (0, -1), FILL)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), BRAND), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    t.setStyle(TableStyle(style))
    return t


# ── content (pure) ──────────────────────────────────────────────────────────

def record_rows(lead: dict[str, Any]) -> list[list[str]]:
    c = lead.get("cadastre") or {}
    b = lead.get("building") or {}
    d = (lead.get("public_records") or {}).get("dbpr") or {}
    year = c.get("year_built") or b.get("year_built")
    age = f" ({datetime.now(ZoneInfo('America/New_York')).year - int(year)} años)" if year else ""
    rows = [
        ["Propiedad", lead.get("company_name")],
        ["Dirección", lead.get("address")],
        ["Tipo", VERTICAL_ES.get(lead.get("vertical") or "community", "Condominio")],
        ["Año de construcción", f"{year}{age}" if year else "—"],
        ["Pisos", _num(b.get("levels") or c.get("floors"))],
        ["Unidades", _num(b.get("units") or c.get("units") or d.get("units"))],
        ["Superficie construida", _num(c.get("building_sqft") or b.get("building_sqft"), " ft²")],
    ]
    if c.get("lot_sqft"):
        rows.append(["Lote", _num(c["lot_sqft"], " ft²")])
    if c.get("land_use"):
        rows.append(["Uso de suelo", c["land_use"].title()])
    if c.get("assessed_value"):
        rows.append(["Valor tasado", _usd(c["assessed_value"])])
    if d.get("association"):
        rows.append(["Asociación", d["association"]])
    if lead.get("management_company"):
        rows.append(["Administración", lead["management_company"]])
    if c.get("folio"):
        rows.append(["Folio", c["folio"]])
    src = [s for s, ok in (("Miami-Dade Property Appraiser", c), ("DBPR Florida (padrón de condominios)", d)) if ok]
    rows.append(["Fuente", ", ".join(src) or "Registros públicos y relevamiento propio"])
    return rows


def diagnosis(lead: dict[str, Any], max_items: int = 6) -> list[str]:
    out: list[str] = []
    for m in lead.get("municipal") or []:
        out.append(f"<b>{_txt(_cap(m['label']))}.</b> {_txt(m['quote'])}")
    quoted: set[str] = set()
    for p in lead.get("pain") or []:
        if p.get("source") == "municipal" or p.get("quote") in quoted:     # one review can explain several pains
            continue
        quoted.add(p.get("quote"))
        when = f" ({p['date']})" if p.get("date") else ""
        src = "Reseña" if p.get("source") == "review" else "Sitio web"
        out.append(f"<b>{_txt(_cap(p['label']))}.</b> {src}{_txt(when)}: «{_txt(p['quote'])}»")
    b = lead.get("building") or {}
    if b.get("coastal"):
        dist = b.get("coast_distance_m")
        where = f"a ~{_num(dist)} m de la costa" if dist is not None else "en zona costera"
        out.append(f"<b>Exposición al salitre.</b> El edificio está {where}: el salitre y la humedad aceleran moho, "
                   "algas y corrosión en fachada, barandas y garajes.")
    comp = lead.get("competitor_intel") or {}
    if comp.get("top_weakness"):
        out.append(f"<b>Antecedentes en la zona.</b> En edificios cercanos se reporta que {_txt(comp['top_weakness_es'])}. "
                   f"Nuestro enfoque: {_txt(comp['argument'])}.")
    amen = list((b.get("amenities") or {}).keys())
    if amen:
        names = {"parking_garage": "garaje", "pool_deck": "deck de piscina", "tennis_court": "canchas",
                 "sidewalks": "caminos/aceras", "gatehouse": "garita"}
        out.append("<b>Superficies relevadas.</b> " + ", ".join(names.get(a, a) for a in amen) + ".")
    return out[:max_items] or ["Sin hallazgos públicos registrados: el diagnóstico se completa en la inspección en sitio."]


def scope(lead: dict[str, Any]) -> list[str]:
    items = list(SCOPE.get(lead.get("vertical") or "community", SCOPE["community"]))
    b = lead.get("building") or {}
    if (b.get("levels") or 0) >= 8:
        items.append(f"Acceso por cuerdas / equipos certificados para {b['levels']} pisos.")
    if b.get("coastal"):
        items.append("Frecuencia sugerida por salitre: fachada cada 6 a 8 meses, áreas de tránsito trimestral.")
    return items


def deal_rows(lead: dict[str, Any]) -> list[list[str]] | None:
    d = lead.get("estimated_deal") or {}
    if not d:
        return None
    rows = [["Componente", "Rango estimado / año"]]
    for k, (lo, hi) in (d.get("components") or {}).items():
        rows.append([COMPONENT_ES.get(k, k), f"{_usd(lo)} – {_usd(hi)}"])
    rows.append(["Total anual estimado", f"{_usd(d['low'])} – {_usd(d['high'])}"])
    return rows


# ── build ───────────────────────────────────────────────────────────────────

def build(lead: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    st = _styles()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.75 * inch, bottomMargin=0.75 * inch,
                            title=f"Informe preliminar · {lead.get('company_name', '')}",
                            author=settings.company_name)
    fw, fh = doc.width, doc.height
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%d/%m/%Y")
    who = lead.get("contact_name")
    prepared = f"{who} ({lead['contact_title']})" if who and lead.get("contact_title") else (who or "Administración del edificio")
    contact_bits = [x for x in (settings.company_phone, settings.company_email, settings.company_website) if x]

    cover = [
        Paragraph(_txt(settings.company_name), st["brand"]),
        Spacer(1, 0.9 * inch),
        Paragraph("INFORME TÉCNICO PRELIMINAR DE PREVENTA", st["kicker"]),
        Paragraph(_txt(lead.get("company_name")), st["title"]),
        Paragraph(_txt(lead.get("address")), st["sub"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Preparado para: {_txt(prepared)} · Fecha: {today}", st["base"]),
        Spacer(1, 0.35 * inch),
        Paragraph("Datos oficiales de la propiedad", st["h2"]),
        _table(record_rows(lead), [1.9 * inch, fw - 1.9 * inch], st),
        Spacer(1, 0.3 * inch),
        Paragraph("Ref. " + _txt(lead.get("import_batch_id") or "") + " · " + _txt(lead.get("source_id") or ""), st["small"]),
    ]
    if contact_bits:
        cover.append(Paragraph(_txt(" · ".join(contact_bits)), st["small"]))

    body: list[Any] = [Paragraph("1. Diagnóstico técnico preliminar", st["h2"])]
    body += [Paragraph(x, st["bullet"], bulletText="•") for x in diagnosis(lead)]
    body.append(Paragraph("2. Alcance propuesto del servicio Softwash", st["h2"]))
    body += [Paragraph(_txt(x), st["bullet"], bulletText="•") for x in scope(lead)]
    rows = deal_rows(lead)
    body.append(Paragraph("3. Rango estimado del contrato anual", st["h2"]))
    if rows:
        body.append(_table(rows, [fw * 0.58, fw * 0.42], st, header=True))
    else:
        body.append(Paragraph("El rango se define en la inspección en sitio (faltan datos de superficie).", st["base"]))
    body.append(Paragraph("4. Próximo paso", st["h2"]))
    body.append(Paragraph("Inspección gratuita en sitio (aprox. 60 minutos) para medir superficies, tomar muestras de "
                          "manchas y entregar una cotización cerrada.", st["base"]))
    body.append(Spacer(1, 8))
    body.append(Paragraph("Estimación preliminar basada en registros públicos (Miami-Dade Property Appraiser, DBPR, "
                          "registros municipales) y reseñas públicas. No constituye una inspección estructural ni una "
                          "cotización vinculante; los importes pueden variar tras la inspección.", st["small"]))

    story = [KeepInFrame(fw, fh, cover, mode="shrink"), PageBreak(), KeepInFrame(fw, fh, body, mode="shrink")]

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(doc.leftMargin, 0.45 * inch, _txt(f"{settings.company_name} · Informe preliminar · {today}")
                          .replace("&amp;", "&"))
        canvas.drawRightString(doc.leftMargin + fw, 0.45 * inch, f"Página {d.page} de 2")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


def generate_batch(records: Iterable[dict[str, Any]], out_dir: str | Path, *, top: int | None = None,
                   min_score: int = 0) -> list[Path]:
    """PDFs for the batch (best lead_score first) in out_dir/<company_name>.pdf; repeated names get a suffix."""
    recs = sorted((r for r in records if (r.get("lead_score") or 0) >= min_score),
                  key=lambda r: -(r.get("lead_score") or 0))
    if top:
        recs = recs[:top]
    out, used = [], set()
    for r in recs:
        name = safe_filename(r.get("company_name") or "lead")
        candidate, n = name, 2
        while candidate in used:
            candidate, n = f"{name}_{n}", n + 1
        used.add(candidate)
        out.append(build(r, Path(out_dir) / f"{candidate}.pdf"))
    return out


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
