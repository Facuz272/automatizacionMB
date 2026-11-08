"""Two-page pre-sale brief (PDF) per enriched lead — US commercial English by default (`lang="es"` optional).

  page 1  cover: company, "Preliminary Exterior Maintenance & Milestone Compliance Brief", property,
          address, date, prepared-for, official record table (Miami-Dade Property Appraiser / DBPR)
  page 2  1. technical assessment (milestone / 40-year recertification window, municipal permits and
             cases, review evidence, salt exposure, area history, surfaces)
          2. proposed softwash scope (by vertical, rope access, frequency, coating preparation)
          3. estimated annual contract range (valuation components)
          4. next step, documents available on request (only those the tenant declares), disclaimer
          footer: vector QR to COMPANY_BOOKING_URL (or website / tel:) so a board member can book from print
Each page is wrapped in a KeepInFrame that shrinks its content: the brief is always exactly two pages.
Input is the record written to out/<lote>/leads.jsonl. Branding and language come from the tenant.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.platypus import KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from . import pdf_i18n as i18n
from .config import settings
from .pdf_common import cap, draw_footer, num, qr_target, safe_filename, styles, table, txt, usd

__all__ = ["build", "generate_batch", "load_jsonl", "safe_filename", "record_rows", "diagnosis", "scope", "deal_rows"]


def _lang(lang: str | None) -> str:
    lang = (lang or settings.pdf_lang or "en").lower()
    return lang if lang in i18n.T else "en"


def _year_now() -> int:
    return datetime.now(ZoneInfo("America/New_York")).year


# ── content (pure) ──────────────────────────────────────────────────────────

def record_rows(lead: dict[str, Any], lang: str = "en") -> list[list[str]]:
    t = i18n.T[lang]
    c = lead.get("cadastre") or {}
    b = lead.get("building") or {}
    d = (lead.get("public_records") or {}).get("dbpr") or {}
    year = c.get("year_built") or b.get("year_built")
    rows = [
        [t["property"], lead.get("company_name")],
        [t["address"], lead.get("address")],
        [t["type"], i18n.VERTICAL[lang].get(lead.get("vertical") or "community")],
        [t["year_built"], f"{year} ({_year_now() - int(year)} {t['years']})" if year else "—"],
        [t["stories"], num(b.get("levels") or c.get("floors"), lang)],
        [t["units"], num(b.get("units") or c.get("units") or d.get("units"), lang)],
        [t["area"], num(c.get("building_sqft") or b.get("building_sqft"), lang, " ft²")],
    ]
    if c.get("lot_sqft"):
        rows.append([t["lot"], num(c["lot_sqft"], lang, " ft²")])
    if c.get("land_use"):
        rows.append([t["land_use"], c["land_use"].title()])
    if c.get("assessed_value"):
        rows.append([t["assessed"], usd(c["assessed_value"], lang)])
    if d.get("association"):
        rows.append([t["association"], d["association"]])
    if lead.get("management_company"):
        rows.append([t["management"], lead["management_company"]])
    if c.get("folio"):
        rows.append([t["folio"], c["folio"]])
    src = [t[k] for k, ok in (("src_pa", c), ("src_dbpr", d)) if ok]
    rows.append([t["sources"], ", ".join(src) or t["src_none"]])
    return rows


def diagnosis(lead: dict[str, Any], lang: str = "en", max_items: int = 6) -> list[str]:
    t = i18n.T[lang]
    out: list[str] = []
    for m in lead.get("municipal") or []:
        label, text = i18n.municipal_line(m, lang)
        out.append(f"<b>{txt(cap(label))}.</b> {txt(text)}")
    quoted: set[str] = set()
    for p in lead.get("pain") or []:
        if p.get("source") == "municipal" or p.get("quote") in quoted:
            continue
        quoted.add(p.get("quote"))
        when = p.get("date")
        when = i18n.when_en(when) if lang == "en" else when
        src = t["review"] if p.get("source") == "review" else t["website"]
        when_s = f" ({when})" if when else ""
        out.append(f"<b>{txt(cap(i18n.pain_label(p, lang)))}.</b> {src}{txt(when_s)}: “{txt(p['quote'])}”")
    b = lead.get("building") or {}
    if b.get("coastal"):
        dist = b.get("coast_distance_m")
        where = t["coast_near"].format(m=num(dist, lang)) if dist is not None else t["coast_zone"]
        out.append(t["coastal"].format(where=txt(where)))
    w = i18n.weakness(lead.get("competitor_intel") or {}, lang)
    if w:
        out.append(t["zone"].format(weak=txt(w[0]), arg=txt(w[1])))
    amen = list((b.get("amenities") or {}).keys())
    if amen:
        out.append(t["surfaces"] + txt(", ".join(i18n.AMENITY[lang].get(a, a) for a in amen)) + ".")
    return out[:max_items] or [t["none"]]


def scope(lead: dict[str, Any], lang: str = "en") -> list[str]:
    t = i18n.T[lang]
    items = list(i18n.SCOPE[lang].get(lead.get("vertical") or "community", i18n.SCOPE[lang]["community"]))
    b = lead.get("building") or {}
    if (b.get("levels") or 0) >= 8:
        items.append(t["rope"].format(n=b["levels"]))
    if b.get("coastal"):
        items.append(t["freq"])
    if any(m.get("category") in ("milestone", "municipal_recert") for m in lead.get("municipal") or []) or \
            any(p.get("category") == "restoration" for p in lead.get("pain") or []):
        items.append(t["elasto"])
    return items


def deal_rows(lead: dict[str, Any], lang: str = "en") -> list[list[str]] | None:
    t = i18n.T[lang]
    d = lead.get("estimated_deal") or {}
    if not d:
        return None
    rows = [[t["component"], t["range"]]]
    for k, (lo, hi) in (d.get("components") or {}).items():
        rows.append([i18n.COMPONENT[lang].get(k, k), f"{usd(lo, lang)} – {usd(hi, lang)}"])
    rows.append([t["total"], f"{usd(d['low'], lang)} – {usd(d['high'], lang)}"])
    return rows


def documents_line(lang: str) -> str | None:
    docs = [d for d in (settings.documents_available or []) if d]
    if not docs:
        return None
    names = {"COI": "Certificate of Insurance (COI)" if lang == "en" else "Certificado de seguro (COI)", "W-9": "W-9"}
    shown = [names.get(d, d) for d in docs]
    joined = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + (" and " if lang == "en" else " y ") + shown[-1]
    return i18n.T[lang]["docs"].format(docs=joined)


def _sibling_signal(p: dict[str, Any], lang: str) -> str:
    if not p.get("signals"):
        return "—"
    return i18n.municipal_line(p["signals"][0], lang)[0]


# ── build ───────────────────────────────────────────────────────────────────

def build(lead: dict[str, Any], path: str | Path, lang: str | None = None) -> Path:
    lang = _lang(lang)
    t = i18n.T[lang]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    st = styles()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                            topMargin=0.75 * inch, bottomMargin=1.35 * inch,
                            title=f"{t['kicker'].title()} · {lead.get('company_name', '')}", author=settings.company_name)
    fw, fh = doc.width, doc.height
    now = datetime.now(ZoneInfo("America/New_York"))
    today = now.strftime("%m/%d/%Y" if lang == "en" else "%d/%m/%Y")
    who = lead.get("contact_name")
    prepared = f"{who} ({lead['contact_title']})" if who and lead.get("contact_title") else (who or t["admin_default"])
    contact_bits = [x for x in (settings.company_phone, settings.company_email, settings.company_website) if x]

    cover = [
        Paragraph(txt(settings.company_name), st["brand"]),
        Spacer(1, 0.9 * inch),
        Paragraph(t["kicker"], st["kicker"]),
        Paragraph(txt(lead.get("company_name")), st["title"]),
        Paragraph(txt(lead.get("address")), st["sub"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"{t['prepared_for']}: {txt(prepared)} · {t['date']}: {today}", st["base"]),
        Spacer(1, 0.35 * inch),
        Paragraph(t["record_title"], st["h2"]),
        table(record_rows(lead, lang), [1.9 * inch, fw - 1.9 * inch], st),
        Spacer(1, 0.3 * inch),
        Paragraph(f"{t['ref']} " + txt(lead.get("import_batch_id") or "") + " · " + txt(lead.get("source_id") or ""),
                  st["small"]),
    ]
    if contact_bits:
        cover.append(Paragraph(txt(" · ".join(contact_bits)), st["small"]))

    body: list[Any] = [Paragraph(t["s1"], st["h2"])]
    body += [Paragraph(x, st["bullet"], bulletText="•") for x in diagnosis(lead, lang)]
    body.append(Paragraph(t["s2"], st["h2"]))
    body += [Paragraph(txt(x), st["bullet"], bulletText="•") for x in scope(lead, lang)]
    body.append(Paragraph(t["s3"], st["h2"]))
    rows = deal_rows(lead, lang)
    body.append(table(rows, [fw * 0.58, fw * 0.42], st, header=True) if rows else Paragraph(t["no_range"], st["base"]))
    sib = lead.get("portfolio_siblings") or []
    if sib:
        firm = lead.get("management_company") or ""
        body.append(Paragraph(txt(t["siblings"].format(firm=firm)), st["h2"]))
        srows = [t["sib_cols"]] + [[p["name"], (p.get("address") or "").split(",")[0], num(p.get("units"), lang),
                                    _sibling_signal(p, lang), usd(p.get("est_high"), lang) if p.get("est_high") else "—"]
                                   for p in sib[:8]]
        body.append(table(srows, [fw * 0.26, fw * 0.24, fw * 0.1, fw * 0.24, fw * 0.16], st, header=True,
                          bold_first_col=False))
    body.append(Paragraph(t["s4"], st["h2"]))
    body.append(Paragraph(t["next"], st["base"]))
    docs = documents_line(lang)
    if docs:
        body.append(Paragraph(txt(docs), st["base"]))
    body.append(Spacer(1, 8))
    body.append(Paragraph(t["disclaimer"], st["small"]))

    story = [KeepInFrame(fw, fh, cover, mode="shrink"), PageBreak(), KeepInFrame(fw, fh, body, mode="shrink")]
    footer_text = f"{settings.company_name} · {t['footer']} · {today}"
    target = qr_target()

    def first(canvas, d):
        draw_footer(canvas, d, footer_text, t["page"].format(n=1, total=2))

    def later(canvas, d):
        draw_footer(canvas, d, footer_text, t["page"].format(n=d.page, total=2),
                    qr=(target, t["qr"]) if target else None)

    doc.build(story, onFirstPage=first, onLaterPages=later)
    return path


def generate_batch(records: Iterable[dict[str, Any]], out_dir: str | Path, *, top: int | None = None,
                   min_score: int = 0, lang: str | None = None) -> list[Path]:
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
        out.append(build(r, Path(out_dir) / f"{candidate}.pdf", lang=lang))
    return out


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
