"""Internal field & costing sheet for the inspectors (Spanish, 1 page per building).

The English brief is for the property manager; this sheet is what Tomás / Mateo carry to the visit:

  * surfaces to treat (ft²), from the same geometry valuation.py uses: facade (perimeter × height),
    walkways/entrances, parking garage, pool deck, dumpster pads, courts, commercial lot;
  * operation: crew-hours per surface (production rates), work days for the crew, gallons of mix
    and of concentrate (sodium hypochlorite 12.5%) and surfactant;
  * Good / Better / Best matrix to close on site:
      1. Entrada / Horizontal — walkways, entrances, pool deck, dumpster pads (floor price applies)
      2. Completa / Milestone — full facade + garage + common areas
      3. Contrato Anual Preferente — option 2 as initial deep clean + semi-annual maintenance of the
         high-traffic areas, with the tenant's annual discount.

Every rate is a reference assumption kept in FIELD_DEFAULTS and overridable from the tenant profile
(`field_rates`); the sheet says so, and the measured ft² on site always wins.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.platypus import KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from . import valuation
from .config import settings
from .pdf_common import draw_footer, num, safe_filename, styles, table, txt, usd

FIELD_DEFAULTS: dict[str, Any] = {
    # production, ft² per crew-hour
    "facade_ft2_h": 1200, "facade_rope_ft2_h": 700, "flat_ft2_h": 1000, "garage_ft2_h": 1500,
    "lot_ft2_h": 4000,                       # open parking lots: surface cleaner / truck unit
    # gallons of mixed solution per 100 ft² and target SH % in the mix
    "facade_mix_gal_100": 1.0, "flat_mix_gal_100": 0.5, "facade_sh_pct": 1.5, "flat_sh_pct": 0.75,
    "sh_concentrate_pct": 12.5, "surfactant_oz_per_gal": 1.0,
    "crew_size": 3, "hours_per_day": 8, "setup_hours_per_day": 1.0,
    "dumpster_pads": 2, "dumpster_pad_ft2": 200,
    "min_job_usd": 1500, "annual_discount_pct": 10, "maintenance_visits_per_year": 2,
}


def rates() -> dict[str, Any]:
    return {**FIELD_DEFAULTS, **(settings.field_rates or {})}


# ── surfaces ────────────────────────────────────────────────────────────────

def surfaces(rec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{key: {"label", "ft2", "rate": (lo, hi) $/ft², "kind": vertical|flat|garage, "group"}} (pure)."""
    b = dict(rec.get("building") or {})
    cad = rec.get("cadastre") or {}
    b.setdefault("units", cad.get("units"))
    b.setdefault("levels", cad.get("floors"))
    levels, footprint = valuation._geometry(b)
    units = b.get("units") or 0
    amen = b.get("amenities") or {}
    # Surfaces OSM/site did not confirm: inferred from the cadastre / DBPR scale (stored by the pipeline when
    # OSM was down, or computed here for records without them) so the quote is never missing a row.
    inferred = dict(b.get("inferred") or {})
    for k, v in valuation.infer_surfaces(b, cad, rec.get("vertical") or b.get("vertical"),
                                         hotel=bool(rec.get("lodging"))).items():
        if k not in amen:
            inferred.setdefault(k, v)
    r = rates()
    out: dict[str, dict[str, Any]] = {}
    R = valuation.RATES
    if footprint and levels:
        perim = 4 * math.sqrt(footprint)
        out["facade"] = {"label": "Fachada y balcones (softwash)", "ft2": perim * levels * valuation.FLOOR_HEIGHT_M *
                         valuation.M2_TO_FT2, "rate": R["facade"][:2], "kind": "vertical", "group": "full",
                         "rope": levels >= valuation.ROPE_ACCESS_LEVELS}
    vertical = rec.get("vertical") or b.get("vertical") or "community"
    if vertical == "community" or units:
        out["walkways"] = {"label": "Aceras, pasillos y entradas", "ft2": 2000 + 20 * units,
                           "rate": R["walkways"][:2], "kind": "flat", "group": "entry"}
    if "pool_deck" in amen:
        out["pool_deck"] = {"label": "Pool deck", "ft2": 6000 if units >= 150 or (levels or 0) >= 15 else 3000,
                            "rate": R["pool_deck"][:2], "kind": "flat", "group": "entry"}
    elif "pool_deck" in inferred:
        out["pool_deck"] = {"label": f"Pool deck / áreas recreativas ({inferred['pool_deck']['source']})",
                            "ft2": inferred["pool_deck"]["ft2"], "rate": R["pool_deck"][:2], "kind": "flat",
                            "group": "entry", "inferred": True}
    out["dumpster"] = {"label": f"Dumpster pads ({r['dumpster_pads']})", "ft2": r["dumpster_pads"] * r["dumpster_pad_ft2"],
                       "rate": R["walkways"][:2], "kind": "flat", "group": "entry"}
    if "parking_garage" in amen:
        g = 250 * (units or max(20, (footprint or 0) * valuation.M2_TO_FT2 / 400))
        out["garage"] = {"label": "Garaje (losas y rampas)", "ft2": g, "rate": R["garage"][:2], "kind": "garage",
                         "group": "full"}
    elif "parking_garage" in inferred:
        out["garage"] = {"label": f"Garaje / estacionamiento ({inferred['parking_garage']['source']})",
                         "ft2": inferred["parking_garage"]["ft2"], "rate": R["garage"][:2], "kind": "garage",
                         "group": "full", "inferred": True}
    if "tennis_court" in amen:
        out["courts"] = {"label": "Canchas", "ft2": 7200, "rate": R["courts"][:2], "kind": "flat", "group": "full"}
    if "parking_lot" in inferred:
        out["lot"] = {"label": f"Estacionamiento y hardscape del lote ({inferred['parking_lot']['source']})",
                      "ft2": inferred["parking_lot"]["ft2"], "rate": R["lot_hardscape"][:2], "kind": "lot",
                      "group": "entry", "inferred": True}
    for v in out.values():
        v["ft2"] = int(round(v["ft2"], -1))
    return out


# ── operation ───────────────────────────────────────────────────────────────

def operation(surf: dict[str, dict[str, Any]], keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Crew-hours, work days and chemicals for the chosen surfaces (pure)."""
    r = rates()
    keys = list(keys) if keys is not None else list(surf)
    rows, hours, mix_v, mix_f = [], 0.0, 0.0, 0.0
    for k in keys:
        s = surf.get(k)
        if not s:
            continue
        if s["kind"] == "vertical":
            prod = r["facade_rope_ft2_h"] if s.get("rope") else r["facade_ft2_h"]
            mix = s["ft2"] / 100 * r["facade_mix_gal_100"]
            mix_v += mix
        else:
            prod = {"garage": r["garage_ft2_h"], "lot": r["lot_ft2_h"]}.get(s["kind"], r["flat_ft2_h"])
            mix = s["ft2"] / 100 * r["flat_mix_gal_100"]
            mix_f += mix
        h = s["ft2"] / prod
        hours += h
        rows.append({"key": k, "label": s["label"], "ft2": s["ft2"], "crew_hours": round(h, 1), "mix_gal": round(mix)})
    per_day = max(1.0, r["hours_per_day"] - r["setup_hours_per_day"])
    sh = (mix_v * r["facade_sh_pct"] + mix_f * r["flat_sh_pct"]) / r["sh_concentrate_pct"]
    return {"rows": rows, "crew_hours": round(hours, 1), "days": max(1, math.ceil(hours / per_day)) if hours else 0,
            "crew_size": r["crew_size"], "mix_gal": round(mix_v + mix_f), "sh_gal": round(sh, 1),
            "surfactant_oz": round((mix_v + mix_f) * r["surfactant_oz_per_gal"]),
            "rope": any(surf[k].get("rope") for k in keys if k in surf)}


# ── Good / Better / Best ────────────────────────────────────────────────────

def _price(surf: dict[str, dict[str, Any]], keys: Iterable[str]) -> tuple[int, int]:
    lo = hi = 0.0
    for k in keys:
        s = surf.get(k)
        if not s:
            continue
        f = valuation.ROPE_ACCESS_FACTOR if s.get("rope") else 1.0
        lo += s["ft2"] * s["rate"][0] * f
        hi += s["ft2"] * s["rate"][1] * f
    return int(round(lo, -1)), int(round(hi, -1))


def options(surf: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    r = rates()
    floor = r["min_job_usd"]
    entry = [k for k, s in surf.items() if s["group"] == "entry"]
    full = list(surf)
    o1 = _price(surf, entry)
    o1 = (max(o1[0], floor), max(o1[1], floor))
    o2 = _price(surf, full)
    o2 = (max(o2[0], floor), max(o2[1], floor))
    visits = r["maintenance_visits_per_year"]
    disc = r["annual_discount_pct"] / 100
    # Deep clean at the option-2 price + the maintenance visits at the preferred-contract discount
    # (so the annual contract never costs less than the one-off full cleaning it includes).
    upkeep = (o1[0] * visits * (1 - disc), o1[1] * visits * (1 - disc))
    o3_gross = (o2[0] + o1[0] * visits, o2[1] + o1[1] * visits)
    o3 = (int(round(o2[0] + upkeep[0], -1)), int(round(o2[1] + upkeep[1], -1)))
    mid = lambda p: int(round((p[0] + p[1]) / 2, -1))  # noqa: E731
    short = {"walkways": "aceras y entradas", "pool_deck": "pool deck", "dumpster": "dumpster pads",
             "lot": "estacionamiento del lote", "facade": "fachada completa", "garage": "garaje",
             "courts": "canchas"}
    entry_txt = ", ".join(short[k] for k in entry)
    full_txt = " + ".join([short[k] for k in full if k in ("facade", "garage", "courts")] + ["áreas comunes"])
    return [
        {"n": 1, "name": "Entrada / Horizontal", "scope": entry_txt[:1].upper() + entry_txt[1:]
         + (" (aplica precio piso)" if _price(surf, entry)[1] < floor else ""), "keys": entry,
         "range": o1, "suggested": mid(o1), "ops": operation(surf, entry)},
        {"n": 2, "name": "Completa / Milestone", "scope": full_txt[:1].upper() + full_txt[1:],
         "keys": full, "range": o2, "suggested": mid(o2), "ops": operation(surf, full)},
        {"n": 3, "name": "Contrato Anual Preferente",
         "scope": f"Lavado profundo inicial (opción 2) + {visits} mantenimientos/año de áreas de alto tránsito "
                  f"con {r['annual_discount_pct']}% de descuento", "keys": full, "range": o3, "suggested": mid(o3),
         "monthly": int(round(mid(o3) / 12, -1)), "gross": o3_gross},
    ]


# ── PDF ─────────────────────────────────────────────────────────────────────

def _h(x: float) -> str:
    """127.8 -> '127,8'; 8.0 -> '8' (es)."""
    t = f"{x:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return t[:-2] if t.endswith(",0") else t


def _story(rec: dict[str, Any], st: dict, fw: float, inspector: str | None, when: str | None) -> list[Any]:
    surf = surfaces(rec)
    ops = operation(surf)
    opts = options(surf)
    cad, b = rec.get("cadastre") or {}, rec.get("building") or {}
    sig = next((m for m in rec.get("municipal") or []), None)
    head = " · ".join(x for x in (
        f"{b.get('levels') or cad.get('floors')} piso(s)" if (b.get("levels") or cad.get("floors")) else None,
        f"{b.get('units') or cad.get('units')} unidades" if (b.get("units") or cad.get("units")) else None,
        f"construido {cad.get('year_built')}" if cad.get("year_built") else None,
        "costero (salitre)" if b.get("coastal") else None) if x)
    contact = " · ".join(x for x in (rec.get("contact_name"), rec.get("contact_title"), rec.get("phone")) if x)
    out: list[Any] = [
        Paragraph("HOJA DE CAMPO Y COTIZACIÓN · USO INTERNO", st["kicker"]),
        Paragraph(txt(rec.get("company_name")), st["title"]),
        Paragraph(txt(rec.get("address")), st["base"]),
        Paragraph(txt(" · ".join(x for x in (f"Inspector: {inspector}" if inspector else None,
                                              f"Visita: {when}" if when else None, contact or None) if x)), st["base"]),
    ]
    if head:
        out.append(Paragraph(txt(head), st["base"]))
    if sig:
        out.append(Paragraph(txt(f"Señal: {sig.get('label')} — {sig.get('quote', '')[:160]}"), st["small"]))
    if rec.get("portfolio_siblings"):
        out.append(Paragraph(txt("Edificios hermanos para cotizar en la misma visita: " + "; ".join(
            f"{p['name']} ({p.get('units') or '?'} un.)" for p in rec["portfolio_siblings"][:5])), st["small"]))
    out.append(Paragraph("Superficies y operación", st["h2"]))
    rows = [["Superficie", "ft² (aprox.)", "Horas cuadrilla", "Mezcla (gal)"]]
    rows += [[x["label"], num(x["ft2"], "es"), _h(x["crew_hours"]), num(x["mix_gal"], "es")] for x in ops["rows"]]
    rows.append(["Total", num(sum(x["ft2"] for x in ops["rows"]), "es"), _h(ops["crew_hours"]),
                 num(ops["mix_gal"], "es")])
    out.append(table(rows, [fw * 0.43, fw * 0.19, fw * 0.19, fw * 0.19], st, header=True))
    r = rates()
    out.append(Paragraph(txt(
        f"Cuadrilla de {ops['crew_size']} · {ops['days']} jornada(s) de {_h(r['hours_per_day'])} h (incluye "
        f"{_h(r['setup_hours_per_day'])} h de armado/día) · Hipoclorito (SH {_h(r['sh_concentrate_pct'])}%): "
        f"~{_h(ops['sh_gal'])} gal · surfactante ~{_h(ops['surfactant_oz'] / 128)} gal" + (" · requiere acceso por cuerdas / equipo certificado" if ops["rope"] else "")),
        st["base"]))
    missing = [x for k, x in (("garage", "garaje"), ("pool_deck", "pool deck")) if k not in surf
               and (rec.get("vertical") or "community") == "community"]
    if missing:
        out.append(Paragraph(txt(f"No detectado en registros: {', '.join(missing)} — confirmar en sitio y sumar a "
                                 f"la opción que corresponda."), st["small"]))
    out.append(Paragraph("Opciones para cerrar en el lugar", st["h2"]))
    orows = [["Opción", "Alcance", "Rango", "Sugerido"]]
    for o in opts:
        scope = o["scope"] + (f" (≈ {usd(o['monthly'], 'es')}/mes)" if o.get("monthly") else "")
        orows.append([f"{o['n']}. {o['name']}", scope, f"{usd(o['range'][0], 'es')} – {usd(o['range'][1], 'es')}",
                      usd(o["suggested"], "es")])
    out.append(table(orows, [fw * 0.22, fw * 0.42, fw * 0.2, fw * 0.16], st, header=True))
    out.append(Spacer(1, 6))
    out.append(Paragraph(txt(
        "Medidas y rendimientos son estimaciones de referencia a partir de registros públicos (catastro, DBPR, OSM); "
        "medir en sitio y ajustar. Mezclas: fachada ~1,5% SH, superficies ~0,75% SH; seguir la etiqueta del producto, "
        "proteger plantas y metales y enjuagar. El precio de la opción 3 se cobra anual o en cuotas mensuales."), st["small"]))
    out.append(Paragraph("Notas de la visita: ______________________________________________________________", st["base"]))
    return out


def build(records: list[dict[str, Any]] | dict[str, Any], path: str | Path, *, inspector: str | None = None,
          whens: list[str | None] | None = None) -> Path:
    """One page per record, in order (a single building or a whole route)."""
    recs = [records] if isinstance(records, dict) else list(records)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    st = styles()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.8 * inch, title="Hoja de campo",
                            author=settings.company_name)
    fw, fh = doc.width, doc.height
    story: list[Any] = []
    for i, rec in enumerate(recs):
        if i:
            story.append(PageBreak())
        when = (whens or [None] * len(recs))[i]
        story.append(KeepInFrame(fw, fh, _story(rec, st, fw, inspector, when), mode="shrink"))
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%d/%m/%Y")
    footer = f"{settings.company_name} · Hoja de campo (interna) · {today}"
    doc.build(story, onFirstPage=lambda c, d: draw_footer(c, d, footer, f"{d.page}/{len(recs)}"),
              onLaterPages=lambda c, d: draw_footer(c, d, footer, f"{d.page}/{len(recs)}"))
    return path


def generate_batch(records: Iterable[dict[str, Any]], out_dir: str | Path) -> list[Path]:
    out, used = [], set()
    for r in sorted(records, key=lambda r: -(r.get("lead_score") or 0)):
        name = safe_filename(r.get("company_name") or "lead")
        cand, n = name, 2
        while cand in used:
            cand, n = f"{name}_{n}", n + 1
        used.add(cand)
        out.append(build(r, Path(out_dir) / f"{cand}_campo.pdf"))
    return out
