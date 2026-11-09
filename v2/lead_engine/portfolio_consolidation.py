"""One firm phone, several buildings: consolidate them on the lead that enters the CRM.

The CRM keeps one lead per phone. When ALLOW_MGMT_FIRM_PHONE=true several buildings of the
same management firm resolve to the firm's central line; only one can be imported. Instead of
losing the rest in needs_phone.csv:

  1. every building sharing that central phone gets a light official check (cadastre year/floors,
     milestone window, municipal records, annual estimate) — no Places/web cost;
  2. the strongest one (urgent signals, then the high end of the annual estimate, then units)
     becomes the primary lead that carries the phone and gets the full enrichment;
  3. the others are listed on the primary (`portfolio_siblings`): in the setter's sales_strategy
     ("al llamar a la central de X, cotizar también: …") and in the PDF; they stay in
     needs_phone.csv with `consolidated_into` so `enrich-phones` can still find their own line.
"""

from __future__ import annotations

from typing import Any

from . import appraiser, municipal_signals, valuation
from .models import Lead
from .normalize import phone_key

MAX_LISTED = 5          # siblings named in the CRM hook (all of them go to the PDF)
SIGNAL_SHORT = {
    "milestone": "Milestone/recertificación",
    "municipal_recert": "recertificación pendiente",
    "municipal_unsafe": "estructura insegura",
    "municipal_violation": "violación de edificio abierta",
    "municipal_code": "caso de código abierto",
    "municipal_permit": "permiso de obra exterior",
}


def _coastal(lead: Lead) -> bool:
    from .pipeline import COASTAL_3MI_ZIPS
    from .dbpr import zip5
    return (zip5(lead.address) or "") in COASTAL_3MI_ZIPS


def light_profile(lead: Lead) -> dict[str, Any]:
    """Official facts for a building without its own phone (cached on the lead)."""
    if lead.evidence.get("light_profile"):
        return lead.evidence["light_profile"]
    rec = lead.public_records.get("dbpr") or {}
    cad = (lead.cadastre if lead.source == "pa" and lead.cadastre else None) or appraiser.lookup(lead.address) or {}
    units = rec.get("units") or cad.get("units") or 0
    coastal = _coastal(lead)
    sigs = list(municipal_signals.lookup(lead.address, dbpr_county=rec.get("county")) or [])
    ms = appraiser.milestone_signal(cad.get("year_built"), cad.get("floors"), coastal, units=units,
                                    condo=lead.vertical == "community")
    if ms:
        sigs.insert(0, ms)
    if units:
        est = valuation.estimate({"units": units, "levels": cad.get("floors"), "coastal": coastal})
    elif cad.get("building_sqft"):                   # plazas / commercial: built area and lot
        est = valuation.estimate({"vertical": lead.vertical, "building_sqft": cad["building_sqft"],
                                  "lot_sqft": cad.get("lot_sqft"), "levels": cad.get("floors"), "coastal": coastal})
    else:
        est = None
    prof = {"name": lead.company_name, "address": lead.address, "units": units, "year_built": cad.get("year_built"),
            "floors": cad.get("floors"), "building_sqft": cad.get("building_sqft"),
            "signals": [{"category": s["category"], "label": s["label"], "weight": s.get("weight", 0),
                         **({"data": s["data"]} if s.get("data") else {}), "quote": s.get("quote", "")}
                        for s in sigs],
            "est_low": (est or {}).get("low", 0), "est_high": (est or {}).get("high", 0)}
    lead.evidence["light_profile"] = prof
    return prof


def strength(prof: dict[str, Any]) -> tuple:
    return (sum(s.get("weight", 0) for s in prof["signals"]), prof["est_high"], prof["units"] or 0)


def consolidate(gated: list[tuple[Lead, Any]], held: list[Lead]) -> tuple[list[tuple[Lead, Any]], list[Lead]]:
    """Pick the strongest building per shared central phone. Returns (gated, siblings left without phone)."""
    groups: dict[str, list[Lead]] = {}
    for lead in held:
        groups.setdefault(phone_key(lead.phone or ""), []).append(lead)
    if not groups:
        return gated, []
    out_gated, leftovers = [], []
    for lead, facts in gated:
        key = phone_key(lead.phone or "")
        if key not in groups or lead.evidence.get("phone_from") != "firm_central":
            out_gated.append((lead, facts))
            continue
        members = [lead] + groups.pop(key)
        best = max(members, key=lambda l: strength(light_profile(l)))
        siblings = [m for m in members if m is not best]
        best.portfolio_siblings = sorted((light_profile(m) for m in siblings), key=strength, reverse=True)
        best.evidence["consolidated_siblings"] = [m.source_id for m in siblings]
        for m in siblings:
            m.evidence["consolidated_into"] = best.company_name
        out_gated.append((best, facts if best is lead else None))
        leftovers += siblings
    for rest in groups.values():          # their primary was dropped before (e.g. already in the CRM)
        leftovers += rest
    return out_gated, leftovers


def sibling_text(prof: dict[str, Any]) -> str:
    bits = [f"{prof['units']} un." if prof.get("units") else None]
    bits += [SIGNAL_SHORT.get(s["category"], s["label"]) for s in prof["signals"][:2]]
    return f"[{prof['name']} · {prof['address'].split(',')[0]} – {', '.join(b for b in bits if b)}]"


def hook_line(lead: Lead) -> str | None:
    """Spanish line for the setter (CRM sales_strategy)."""
    sib = lead.portfolio_siblings
    if not sib:
        return None
    firm = lead.management_company or "la administradora"
    items = [sibling_text(p) for p in sib[:MAX_LISTED]]
    shown = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]
    more = f" (+{len(sib) - MAX_LISTED} más en el PDF)" if len(sib) > MAX_LISTED else ""
    total = sum(p.get("est_high") or 0 for p in sib)
    money = f" Potencial extra estimado: hasta US$ {total:,}".replace(",", ".") + "/año." if total else ""
    return (f"Teléfono = central de {firm}: al llamar, cotizar también en el mismo llamado {shown}{more}."
            f"{money}")


def apply(lead: Lead) -> None:
    line = hook_line(lead)
    if line and line not in (lead.sales_strategy or ""):
        lead.sales_strategy = f"{line} {lead.sales_strategy or ''}".strip()
