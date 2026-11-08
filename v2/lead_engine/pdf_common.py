"""Shared PDF building blocks: tenant colours, styles, tables, number formats, vector QR and footer."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, Table, TableStyle

from .config import settings

INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#61707E")
RULE = colors.HexColor("#D5DDE5")
FILL = colors.HexColor("#F2F6F9")


def brand() -> colors.Color:
    try:
        return colors.HexColor(settings.brand_color)
    except (ValueError, TypeError, AttributeError):
        return colors.HexColor("#0E5A8A")


def cp1252_ok(ch: str) -> bool:
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def txt(s: Any) -> str:
    """Escape for Paragraph markup and keep it cp1252-safe for the standard fonts."""
    t = str(s if s is not None else "")
    t = t.replace("≈", "~").replace("→", "->").replace("⚠️", "").replace("✓", "").replace("²", "²")
    t = "".join(ch if cp1252_ok(ch) else unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode() or "?"
                for ch in t)
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def num(n: Any, lang: str, suffix: str = "") -> str:
    try:
        s = f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"
    return (s.replace(",", ".") if lang == "es" else s) + suffix


def usd(n: Any, lang: str) -> str:
    v = num(n, lang)
    return "—" if v == "—" else f"US$ {v}" if lang == "es" else f"${v}"


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def safe_filename(name: str, max_len: int = 80) -> str:
    base = unicodedata.normalize("NFKD", name or "lead").encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")
    return (base or "lead")[:max_len]


def styles() -> dict[str, ParagraphStyle]:
    ss = getSampleStyleSheet()
    b = brand()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK,
                          alignment=TA_LEFT)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.8, leading=10, textColor=MUTED),
        "brand": ParagraphStyle("brand", parent=base, fontName="Helvetica-Bold", fontSize=11, textColor=b),
        "kicker": ParagraphStyle("kicker", parent=base, fontSize=9, textColor=MUTED, spaceAfter=4),
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=24, leading=29, spaceAfter=6),
        "sub": ParagraphStyle("sub", parent=base, fontSize=13, leading=17, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=b,
                             spaceBefore=10, spaceAfter=4),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=2, spaceAfter=2),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=11.5),
        "cellb": ParagraphStyle("cellb", parent=base, fontName="Helvetica-Bold", fontSize=9, leading=11.5),
    }


def table(rows: list[list[Any]], widths: list[float], st: dict[str, ParagraphStyle], header: bool = False,
          bold_first_col: bool = True) -> Table:
    hdr = ParagraphStyle("hdr", parent=st["cellb"], textColor=colors.white)
    data = [[Paragraph(txt(c), hdr if (header and i == 0) else st["cellb"] if (j == 0 and bold_first_col) else st["cell"])
             for j, c in enumerate(r)] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.5, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if bold_first_col:
        style.append(("BACKGROUND", (0, 0), (0, -1), FILL))
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), brand())]
    t.setStyle(TableStyle(style))
    return t


# ── QR ──────────────────────────────────────────────────────────────────────

def qr_target() -> str | None:
    """Booking URL, else website, else tel: link — None when the tenant configured none of them."""
    if settings.company_booking_url:
        return settings.company_booking_url
    if settings.company_website:
        return settings.company_website
    if settings.company_phone:
        return "tel:" + re.sub(r"[^\d+]", "", settings.company_phone)
    return None


def qr_drawing(value: str, size: float) -> Drawing:
    """Vector QR (reportlab's own QrCodeWidget, no external dependency) scaled to `size` points."""
    w = QrCodeWidget(value)
    x1, y1, x2, y2 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(w)
    d._qr_value = value         # for tests / inspection (underscore: reportlab validates public attributes)
    return d


def draw_footer(canvas, doc, text: str, page_label: str, qr: tuple[str, str] | None = None) -> None:
    """Footer line + page label; on the page that asks for it, a vector QR with a caption at bottom right."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(doc.leftMargin, 0.42 * inch, text)
    right = doc.leftMargin + doc.width
    if qr:
        value, caption = qr
        size = 0.9 * inch
        renderPDF.draw(qr_drawing(value, size), canvas, right - size, 0.32 * inch)
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.drawRightString(right - size - 6, 0.32 * inch + size - 10, caption)
        canvas.setFont("Helvetica", 7)
        shown = value if len(value) <= 48 else value[:45] + "..."
        canvas.drawRightString(right - size - 6, 0.32 * inch + size - 21, shown)
        canvas.drawRightString(right - size - 6, 0.42 * inch, page_label)
    else:
        canvas.drawRightString(right, 0.42 * inch, page_label)
    canvas.restoreState()
