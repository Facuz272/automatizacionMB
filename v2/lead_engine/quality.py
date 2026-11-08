"""Per-batch quality audit: out/<lote>/quality_report.json + console summary."""

from __future__ import annotations

from collections import Counter
from statistics import mean, median
from typing import Any

from . import costs
from .models import Lead


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 1) if d else 0.0


def build(batch_id: str, strategy: str, leads: list[Lead], rejected: list[dict]) -> dict[str, Any]:
    n = len(leads)
    scores = [l.lead_score for l in leads]
    cost = costs.tracker.report()
    report: dict[str, Any] = {
        "import_batch_id": batch_id,
        "strategy": strategy,
        "accepted": n,
        "rejected": len(rejected),
        "rejected_reasons": dict(Counter(r["reason"] for r in rejected).most_common()),
        "pct": {
            "decision_maker": _pct(sum(bool(l.contact_name) for l in leads), n),
            "decision_maker_with_title": _pct(sum(bool(l.contact_name and l.contact_title) for l in leads), n),
            "management_company_identified": _pct(sum(bool(l.management_company) or l.management_type == "onsite"
                                                      for l in leads), n),
            "email_any": _pct(sum(bool(l.email) for l in leads), n),
            "email_verified_mx": _pct(sum(l.email_verified for l in leads), n),
            "pain_signals": _pct(sum(bool(l.pain) for l in leads), n),
            "pain_verified": _pct(sum(l.pain_score >= 3 for l in leads), n),
            "management_office_phone": _pct(sum(any(p["number"] == l.phone and p["label"] == "management"
                                                    for p in l.phones) for l in leads), n),
            "building_profile": _pct(sum(bool((l.building or {}).get("levels") or (l.building or {}).get("footprint_m2"))
                                         for l in leads), n),
            "coastal_or_large": _pct(sum(bool((l.building or {}).get("coastal") or (l.building or {}).get("large"))
                                         for l in leads), n),
            "dbpr_match": _pct(sum(bool(l.public_records.get("dbpr")) for l in leads), n),
            "ai_qualified": _pct(sum(l.ai_used for l in leads), n),
        },
        "lead_score": {
            "avg": round(mean(scores), 1) if scores else 0,
            "median": median(scores) if scores else 0,
            "min": min(scores) if scores else 0,
            "max": max(scores) if scores else 0,
            "bands": {"80-100": sum(s >= 80 for s in scores), "60-79": sum(60 <= s < 80 for s in scores),
                      "40-59": sum(40 <= s < 60 for s in scores), "0-39": sum(s < 40 for s in scores)},
            "avg_breakdown": {k: round(mean(l.score_breakdown.get(k, 0) for l in leads), 1)
                              for k in ("decision_maker", "onsite_or_pain", "email_mx", "exposure_size", "mgmt_phone")}
            if leads else {},
        },
        "segments": dict(Counter(l.segment for l in leads)),
        "strategy_kinds": dict(Counter(l.strategy_kind for l in leads)),
        "top_pain_categories": dict(Counter(p["category"] for l in leads for p in l.pain).most_common(8)),
        "email_status": dict(Counter(l.email_status for l in leads if l.email)),
        "cost_estimate_usd": cost,
        "cost_per_accepted_lead_usd": round(cost["total_usd"] / n, 4) if n else None,
    }
    return report


def print_report(r: dict[str, Any], out_dir) -> None:
    p = r["pct"]
    s = r["lead_score"]
    print("\n=== Lead Engine v2 · auditoría de calidad ===")
    print(f"Lote: {r['import_batch_id']}  ({r['strategy']})  ·  aceptados {r['accepted']}  ·  rechazados {r['rejected']}")
    rows = [
        ("Con decisor", p["decision_maker"]), ("  …con cargo", p["decision_maker_with_title"]),
        ("Administradora identificada", p["management_company_identified"]),
        ("Email verificado (MX)", p["email_verified_mx"]), ("Señales de dolor", p["pain_signals"]),
        ("Dolor verificado (≥3)", p["pain_verified"]), ("Tel. de administración", p["management_office_phone"]),
        ("Perfil físico (OSM)", p["building_profile"]), ("Costero o grande", p["coastal_or_large"]),
        ("Cruce DBPR", p["dbpr_match"]), ("Precalificado con IA", p["ai_qualified"]),
    ]
    for label, v in rows:
        print(f"  {label:<30} {v:5.1f}%")
    print(f"  lead_score promedio {s['avg']} (mediana {s['median']}, rango {s['min']}–{s['max']})  bandas {s['bands']}")
    if r["rejected_reasons"]:
        print("  Rechazos:", r["rejected_reasons"])
    c = r["cost_estimate_usd"]
    print(f"  Costo estimado APIs: US${c['total_usd']:.2f}"
          + (f"  (US${r['cost_per_accepted_lead_usd']:.3f}/lead)" if r["cost_per_accepted_lead_usd"] is not None else ""))
    print(f"Archivos: {out_dir}/  (quality_report.json, crm_payload.json ordenado por lead_score)")
