"""Outreach Pack per batch: ready-to-send messages built only from the building's real data.

For every accepted lead, in out/<batch>/outreach_pack.md and outreach_pack.json:
  * email_en       first-contact email (US business English) for the property manager / board,
                   referencing the attached 2-page PDF;
  * sms_en         short WhatsApp/SMS (≤ 320 characters);
  * script_es      phone script in Spanish for the setter (opening, reason, offer, PDF, sister
                   buildings, two common objections, close).

The hook is the strongest *verifiable* fact, in this order: milestone/recertification window
(year built), pending recertification or open case, recent exterior permit, a review quoting the
problem, then size (ft² / units). Nothing is invented: no contact name → "Hi there,"; company
phone/email/booking link only when the tenant has them; documents only when declared.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import settings
from .pdf_common import safe_filename
from .pdf_generator import documents_line


def _first_name(name: str | None) -> str | None:
    return name.split()[0] if name and name.strip() else None


def _signal(lead: dict[str, Any], *cats: str) -> dict[str, Any] | None:
    return next((s for s in lead.get("municipal") or [] if s.get("category") in cats), None)


def hook(lead: dict[str, Any]) -> dict[str, str] | None:
    """{"en": ..., "es": ..., "kind": ...} for the strongest verifiable fact, or None."""
    ms = _signal(lead, "milestone")
    if ms and ms.get("data"):
        d = ms["data"]
        when_en = f"in {d['milestone_year']}"
        return {"kind": "milestone", "short": f"reaches its {d['milestone']}-year milestone in {d['milestone_year']} (SB 4-D window)",
                "en": f"the building was completed in {d['year_built']} and reaches its {d['milestone']}-year mark "
                      f"{when_en}, which puts it in the SB 4-D Milestone Inspection / 40-Year Recertification window",
                "es": f"el edificio es de {d['year_built']} y cumple {d['milestone']} años en {d['milestone_year']}: "
                      f"está en la ventana de Milestone / recertificación"}
    rc = _signal(lead, "municipal_recert", "municipal_unsafe", "municipal_violation", "municipal_code")
    if rc:
        d = rc.get("data") or {}
        if rc["category"] == "municipal_recert" and d.get("program") == "bsip":
            return {"kind": "recert", "short": "has a 40/50-year Building Safety Inspection application open",
                    "en": f"the City of {d.get('city', 'Fort Lauderdale')} shows an open Building Safety Inspection "
                          f"Program (40/50-year) application for the building",
                    "es": f"tiene abierta la inspección de seguridad 40/50 años (BSIP) en "
                          f"{d.get('city', 'Fort Lauderdale')}"}
        if rc["category"] == "municipal_recert":
            return {"kind": "recert", "short": "has its 40-Year Recertification still pending", "en": f"the city lists its {d.get('year') or ''} 40-Year Recertification as "
                                             f"still pending".replace("its  ", "its "),
                    "es": f"la ciudad tiene la recertificación {d.get('year') or ''} pendiente".replace("  ", " ")}
        case = f" (case {d['case']})" if d.get("case") else ""
        return {"kind": "case", "short": "has an open building case", "en": f"there is an open county building case on the property{case}",
                "es": f"tiene un caso municipal abierto{case.replace('case', 'caso')}"}
    pm = _signal(lead, "municipal_permit")
    if pm and pm.get("data"):
        d = pm["data"]
        return {"kind": "permit", "short": f"the recent {str(d.get('work') or 'exterior').lower()} permit",
                "en": f"a permit was issued on {d.get('issued')} for {str(d.get('work') or 'exterior work').lower()}",
                "es": f"sacaron un permiso el {d.get('issued')} para {str(d.get('work') or 'obra exterior').lower()}"}
    review = next((p for p in lead.get("pain") or [] if p.get("source") == "review" and p.get("quote")), None)
    if review:
        q = review["quote"].strip().strip('"“”')
        q = q if len(q) <= 90 else q[:87].rsplit(" ", 1)[0] + "…"
        return {"kind": "review", "en": f"a recent public review mentions “{q}”",
                "es": f"una reseña pública reciente dice «{q}»"}
    return None


def _size(lead: dict[str, Any]) -> tuple[str | None, str | None]:
    cad, b = lead.get("cadastre") or {}, lead.get("building") or {}
    sqft, units, floors = cad.get("building_sqft"), b.get("units") or cad.get("units"), b.get("levels")
    en = ", ".join(x for x in (f"{sqft:,} ft² of building area" if sqft else None,
                               f"{units} units" if units else None, f"{floors} stories" if floors else None) if x)
    es = ", ".join(x for x in (f"{sqft:,} ft²".replace(",", ".") if sqft else None,
                               f"{units} unidades" if units else None, f"{floors} pisos" if floors else None) if x)
    return en or None, es or None


def _signature() -> str:
    bits = [settings.company_name] + [x for x in (settings.company_phone, settings.company_email,
                                                   settings.company_website) if x]
    return "\n".join(bits)


def messages(lead: dict[str, Any], pdf_name: str | None = None) -> dict[str, Any]:
    name = lead.get("company_name") or "your building"
    first = _first_name(lead.get("contact_name"))
    h = hook(lead)
    size_en, size_es = _size(lead)
    sib = lead.get("portfolio_siblings") or []
    firm = lead.get("management_company")
    booking = settings.company_booking_url
    company = settings.company_name

    # ── email (EN) ──
    subject = f"{name}: exterior maintenance & milestone-ready facade brief" if h and h["kind"] in (
        "milestone", "recert") else f"{name}: preliminary exterior maintenance brief"
    lines = [f"Hi {first}," if first else "Hi there,", ""]
    reason = f"While reviewing public records for {name}, we noticed that {h['en']}." if h else \
        f"We prepared a short exterior maintenance brief for {name}."
    lines.append(reason)
    if size_en:
        lines.append(f"Based on the official records ({size_en}), we put together a 2-page preliminary brief with the "
                     f"proposed softwash scope and an annual range" + (f" — it is attached ({pdf_name})." if pdf_name
                                                                         else "."))
    elif pdf_name:
        lines.append(f"The 2-page preliminary brief is attached ({pdf_name}).")
    lines.append("")
    lines.append("Our Softwash Biological Treatment removes mold, algae and black streaks at low pressure, and we "
                 "coordinate surface preparation with the engineer's timeline before any elastomeric coating.")
    if sib:
        names = ", ".join(p["name"] for p in sib[:4])
        lines.append(f"Since {firm or 'your firm'} also manages {names}, we can quote those buildings in the same "
                     f"visit.")
    docs = documents_line("en")
    if docs:
        lines.append(docs)
    lines += ["", "Would a free 60-minute on-site inspection next week work for you? " +
              (f"You can pick a time here: {booking}" if booking else "Just reply with a day and time that suits you."),
              "", "Best regards,", _signature()]
    email = {"subject": subject, "body": "\n".join(lines)}

    # ── SMS / WhatsApp (EN) ──
    sms = f"Hi{(' ' + first) if first else ''}, this is {company}. "
    if h and h["kind"] == "permit":
        sms += f"We saw {h['short']} at {name}. "
    elif h and h.get("short"):
        sms += f"{name} {h['short']}. "
    else:
        sms += f"About {name}'s exterior: "
    sms += "We prepared a free 2-page softwash brief — can I email it and book a free inspection?"
    if booking and len(sms) + len(booking) + 1 <= 320:
        sms += f" {booking}"
    sms = sms[:320]

    # ── setter script (ES) ──
    who = lead.get("contact_name") or "the property manager"
    s = [f"APERTURA: «Hi, good morning, this is ___ from {company}. May I speak with {who}?» "
         f"(si atiende recepción/front desk: pedir al Property Manager o a la oficina de administración).",
         f"MOTIVO (dato real): {h['es'] if h else 'preparamos un informe preliminar de mantenimiento exterior'}"
         + (f"; según registros oficiales: {size_es}." if size_es else "."),
         "OFERTA: inspección gratuita de 60 minutos para medir superficies y dejar una cotización cerrada.",
         f"PDF: «I'd like to email you a 2-page brief we prepared for {name}» — confirmar el email y enviarlo"
         + (f" ({pdf_name})." if pdf_name else ".")]
    if sib:
        s.append("EDIFICIOS HERMANOS (misma administradora" + (f", {firm}" if firm else "") + "): ofrecer cotizar en "
                 "la misma visita: " + "; ".join(f"{p['name']} ({p.get('units') or '?'} un.)" for p in sib[:5]) + ".")
    s += ["OBJECIÓN «ya tenemos proveedor»: «Perfecto, la inspección es gratuita y les sirve para comparar alcance y "
          "frecuencia; el softwash trata el moho a baja presión y dura más que el lavado a presión.»",
          "OBJECIÓN «mándenlo por email»: confirmar email, enviar el PDF hoy y agendar seguimiento en 48 h.",
          "CIERRE: proponer dos horarios concretos para la inspección y confirmar quién la recibe en el edificio."]
    if lead.get("evidence", {}).get("phone_from") == "firm_central":
        s.insert(0, "OJO: el teléfono es la central de la administradora; pedir por el manager del edificio "
                    f"{name}.")
    return {"company_name": name, "phone": lead.get("phone"), "email_to": lead.get("email"), "pdf": pdf_name,
            "hook_kind": h["kind"] if h else None, "email_en": email, "sms_en": sms, "script_es": s}


def build(records: list[dict[str, Any]], out_dir: str | Path, pdf_files: list[Path] | None = None) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    by_name = {p.stem: p.name for p in pdf_files or []}
    packs = []
    for r in records:
        pdf = by_name.get(safe_filename(r.get("company_name") or "lead"))
        packs.append(messages(r, pdf))
    json_path, md_path = out_dir / "outreach_pack.json", out_dir / "outreach_pack.md"
    json_path.write_text(json.dumps(packs, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# Outreach Pack · {out_dir.name}", ""]
    for i, p in enumerate(packs, 1):
        md += [f"## {i}. {p['company_name']}", "",
               f"Teléfono: {p['phone'] or '—'} · Email: {p['email_to'] or '—'} · PDF: {p['pdf'] or '—'}", "",
               "### Email (EN)", "", f"**Subject:** {p['email_en']['subject']}", "", "```text",
               p["email_en"]["body"], "```", "", "### WhatsApp / SMS (EN)", "", "```text", p["sms_en"], "```", "",
               "### Guion telefónico (ES)", ""] + [f"- {x}" for x in p["script_es"]] + [""]
    md_path.write_text("\n".join(md), encoding="utf-8")
    return md_path, json_path
