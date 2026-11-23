"""Keep the CRM Pool stocked by itself (`python -m lead_engine auto-replenish [--commit]`, systemd 03:00 ET).

1. Demand from the CRM (`/api/public/health`): the Pool and the daily need (`pool_target` is 2 days of new
   leads for the active setters at their quota). Below the floor (REPLENISH_FLOOR_DAYS, default 2) it refills
   to the target (REPLENISH_TARGET_DAYS, default 3); otherwise nothing runs. Never more than REPLENISH_MAX_LEADS.
2. Territory queue in state.db (`territories`): every kind × area (condos onsite / portfolio per city of the
   tenant, CAB management firms per county, commercial buildings) with last_run_at and yield. Each run splits
   the deficit by REPLENISH_MIX and takes, per kind, the area worked longest ago that is not exhausted, moving
   to the next one when an area gives nothing new. The report warns when few areas are left.
3. Guardrails: a list-price ceiling (REPLENISH_MAX_COST_USD, default 10) that stops examining candidates once
   reached, the run lock, the CRM resend queue, retries in every client, and a row per run in `replenish_runs`.
   The month's API calls are kept in `api_usage` so the report shows the cost after Google's free monthly usage.
4. Without --commit everything goes to the CRM as a dry run (nothing is written); --plan only prints the plan.
5. SLACK_WEBHOOK_URL: Pool before / after, leads added, % with email, areas used, cost, alerts.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from . import costs
from .config import settings
from .state import Ledger

DEFAULT_MIX = {"condo_onsite": 0.40, "condo_portfolio": 0.25, "cab_firm": 0.35}
KINDS = {
    # kind: (pipeline source, strategy, vertical)
    "condo_onsite": ("dbpr", "onsite", None),
    "condo_portfolio": ("dbpr", "portfolio", None),
    "cab_firm": ("cab", "portfolio", None),
    "commercial": ("pa", "onsite", "commercial"),
    "plaza": ("pa", "onsite", "plaza"),
}
CAB_AREAS = {"Miami-Dade": "23", "Broward": "16"}
COMMERCIAL_AREAS = ["Miami", "Miami Beach", "Doral", "Coral Gables", "Aventura", "Hialeah"]
LOW_AREAS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS territories (
  kind TEXT, area TEXT, last_run_at TEXT, runs INTEGER DEFAULT 0, accepted INTEGER DEFAULT 0,
  exhausted INTEGER DEFAULT 0, rank INTEGER DEFAULT 0, PRIMARY KEY (kind, area)
);
CREATE TABLE IF NOT EXISTS replenish_runs (
  run_at TEXT PRIMARY KEY, committed INTEGER, pool_before INTEGER, pool_after INTEGER, target INTEGER,
  deficit INTEGER, inserted INTEGER, accepted INTEGER, with_email INTEGER, cost_list_usd REAL,
  cost_net_usd REAL, summary TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (month TEXT, key TEXT, count INTEGER, PRIMARY KEY (month, key));
"""


# ── demand (pure) ───────────────────────────────────────────────────────────

def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def demand(health: dict[str, Any] | None, *, floor_days: float | None = None, target_days: float | None = None,
           max_leads: int | None = None) -> dict[str, Any]:
    """{run, pool, daily, floor, target, deficit, why}. The CRM's pool_target is 2 days of need."""
    from .daily import pool_count

    floor_days = env_float("REPLENISH_FLOOR_DAYS", 2) if floor_days is None else floor_days
    target_days = env_float("REPLENISH_TARGET_DAYS", 3) if target_days is None else target_days
    max_leads = int(env_float("REPLENISH_MAX_LEADS", 400)) if max_leads is None else max_leads
    if not health:
        return {"run": False, "why": "sin lectura del CRM (/api/public/health)", "pool": None, "deficit": 0}
    pool = pool_count(health)
    crm_target = health.get("pool_target")
    if pool is None or not isinstance(crm_target, (int, float)) or crm_target <= 0:
        return {"run": False, "why": "el CRM no informa Pool ni objetivo", "pool": pool, "deficit": 0}
    daily = crm_target / 2
    floor, target = math.ceil(daily * floor_days), math.ceil(daily * target_days)
    out = {"pool": pool, "daily": round(daily, 1), "floor": floor, "target": target}
    if pool >= floor:
        return {**out, "run": False, "deficit": 0,
                "why": f"Pool {pool} ≥ piso de {floor_days:g} días ({floor}); no hace falta cargar"}
    deficit = min(target - pool, max_leads)
    return {**out, "run": deficit > 0, "deficit": deficit,
            "why": f"Pool {pool} < piso {floor}: se completan {deficit} para llegar a {target_days:g} días ({target})"
                   + (f" (tope {max_leads})" if target - pool > max_leads else "")}


def mix() -> dict[str, float]:
    raw = os.getenv("REPLENISH_MIX")
    if raw:
        try:
            m = {k: float(v) for k, v in json.loads(raw).items() if k in KINDS and float(v) > 0}
            if m:
                return m
        except (ValueError, AttributeError):
            logger.warning("REPLENISH_MIX inválido; se usa el mix por defecto")
    return dict(DEFAULT_MIX)


def split(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Largest-remainder split of `total` by share (sums exactly to total)."""
    weight = sum(shares.values()) or 1
    raw = {k: total * v / weight for k, v in shares.items()}
    q = {k: int(x) for k, x in raw.items()}
    for k in sorted(raw, key=lambda k: -(raw[k] - q[k]))[: total - sum(q.values())]:
        q[k] += 1
    return q


# ── territory queue ─────────────────────────────────────────────────────────

def _conn(ledger: Ledger):
    ledger.conn.executescript(SCHEMA)
    if "rank" not in {r[1] for r in ledger.conn.execute("PRAGMA table_info(territories)")}:
        ledger.conn.execute("ALTER TABLE territories ADD COLUMN rank INTEGER DEFAULT 0")
    return ledger.conn


def areas_for(kind: str) -> list[str]:
    from .config import cities_for

    if kind == "cab_firm":
        return list(CAB_AREAS)
    if kind in ("commercial", "plaza"):
        return list(COMMERCIAL_AREAS)
    strategy = KINDS[kind][1]
    return list(dict.fromkeys(cities_for(strategy)))


def sync_territories(ledger: Ledger) -> None:
    conn = _conn(ledger)
    for kind in KINDS:
        for rank, area in enumerate(areas_for(kind)):       # the tenant's own priority order
            conn.execute("INSERT INTO territories (kind, area, rank) VALUES (?, ?, ?) ON CONFLICT(kind, area) "
                         "DO UPDATE SET rank = excluded.rank", (kind, area, rank))
    conn.commit()


def next_areas(ledger: Ledger, kind: str) -> list[str]:
    """Areas of a kind not exhausted: never-run ones first in the tenant's order, then worked longest ago."""
    rows = _conn(ledger).execute(
        "SELECT area FROM territories WHERE kind = ? AND exhausted = 0 ORDER BY last_run_at IS NOT NULL, "
        "last_run_at, rank, area", (kind,)).fetchall()
    return [r[0] for r in rows]


def record_area(ledger: Ledger, kind: str, area: str, accepted: int, exhausted: bool) -> None:
    conn = _conn(ledger)
    conn.execute("UPDATE territories SET last_run_at = ?, runs = runs + 1, accepted = accepted + ?, "
                 "exhausted = ? WHERE kind = ? AND area = ?",
                 (datetime.now(timezone.utc).isoformat(), accepted, int(exhausted), kind, area))
    conn.commit()


def inventory_alerts(ledger: Ledger, kinds: list[str]) -> list[str]:
    alerts = []
    for kind in kinds:
        left = len(next_areas(ledger, kind))
        if left <= LOW_AREAS:
            alerts.append(f"quedan {left} zona(s) sin agotar para {kind}: sumar ciudades / bajar min_units")
    return alerts


# ── monthly API usage (free tier) ───────────────────────────────────────────

def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def usage_this_month(ledger: Ledger, month: str | None = None) -> dict[str, int]:
    rows = _conn(ledger).execute("SELECT key, count FROM api_usage WHERE month = ?", (month or month_key(),)).fetchall()
    return dict(rows)


def add_usage(ledger: Ledger, counts: dict[str, int], month: str | None = None) -> None:
    conn = _conn(ledger)
    for k, n in counts.items():
        conn.execute("INSERT INTO api_usage VALUES (?,?,?) ON CONFLICT(month, key) DO UPDATE SET "
                     "count = api_usage.count + excluded.count", (month or month_key(), k, int(n)))
    conn.commit()


# ── one slice ───────────────────────────────────────────────────────────────

@contextmanager
def _area(kind: str, area: str):
    """Restrict the seed of one slice to one area (tenant city list / CAB county / cadastre city)."""
    before_cities, before_vertical = settings.target_cities, settings.seed_vertical
    before_cab = os.environ.get("CAB_COUNTIES")
    source, strategy, vertical = KINDS[kind]
    try:
        if kind == "cab_firm":
            os.environ["CAB_COUNTIES"] = CAB_AREAS[area]
        else:
            object.__setattr__(settings, "target_cities", {**(before_cities or {}), strategy: [area]})
        if vertical:
            object.__setattr__(settings, "seed_vertical", vertical)
        yield
    finally:
        object.__setattr__(settings, "target_cities", before_cities)
        object.__setattr__(settings, "seed_vertical", before_vertical)
        if before_cab is None:
            os.environ.pop("CAB_COUNTIES", None)
        else:
            os.environ["CAB_COUNTIES"] = before_cab


def _batch_stats(out) -> dict[str, Any]:
    qr = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    crm = qr.get("crm") or {}
    by_api = ((qr.get("cost_estimate_usd") or {}).get("by_api") or {})
    return {"batch": out.name, "accepted": len(leads),
            "with_email": sum(1 for l in leads if l.get("email") and l.get("email_status") in ("mx", "a_only")),
            "inserted": crm.get("inserted") or 0, "would_insert": crm.get("would_insert") or 0,
            "crm_error": crm.get("error"), "counts": {k: v.get("count", 0) for k, v in by_api.items()},
            "cost_usd": (qr.get("cost_estimate_usd") or {}).get("total_usd") or 0.0}


def run_slice(kind: str, area: str, n: int, *, commit: bool, budget_left: float, use_ai: bool = True) -> dict:
    from . import pipeline

    source, strategy, _ = KINDS[kind]
    costs.tracker.budget_usd = max(0.0, budget_left)
    with _area(kind, area):
        out = pipeline.run(strategy=strategy, limit=n, sources=[source], use_ai=use_ai, send=True, commit=commit,
                           remember=commit, make_pdfs=False)
    return {"kind": kind, "area": area, "quota": n, **_batch_stats(out)}


# ── the run ─────────────────────────────────────────────────────────────────

def summary_text(s: dict[str, Any]) -> str:
    if s.get("skipped"):
        return f"*Lead Engine · auto-replenish*: {s['why']}."
    mode = "cargados al Pool" if s["committed"] else "listos (dry run, sin escribir)"
    pct = f"{round(100 * s['with_email'] / s['accepted'])}%" if s["accepted"] else "—"
    lines = [f"*Lead Engine · auto-replenish* · {s['why']}",
             f"Pool: {s['pool_before']} → {s.get('pool_after', '?')} (objetivo {s['target']}) · "
             f"{s['inserted'] if s['committed'] else s['accepted']} leads {mode} · con email {pct}",
             "Zonas: " + ", ".join(f"{x['kind']}/{x['area']} {x['accepted']}" for x in s["slices"]),
             f"Costo: US$ {s['cost_list_usd']:.2f} lista · US$ {s['cost_net_usd']:.2f} después del cupo gratis"]
    if s.get("alerts"):
        lines.append("⚠️ " + " · ".join(s["alerts"]))
    return "\n".join(lines)


def run(*, commit: bool = False, plan_only: bool = False, use_ai: bool = True, slack: bool = True,
        max_cost: float | None = None) -> dict[str, Any]:
    from . import daily, pipeline, runlock

    with runlock.hold("auto-replenish"):
        ledger = Ledger()
        sync_territories(ledger)
        started = datetime.now(timezone.utc)
        # The CRM resend queue and the outcomes feedback go first, exactly as in `daily`.
        if not plan_only and settings.crm_webhook_secret:
            resent = pipeline.resend_pending()
            if resent["still_pending"]:
                s = {"skipped": True, "why": f"el CRM sigue sin confirmar {len(resent['still_pending'])} lote(s); "
                                            "no se arma uno nuevo"}
                if slack:
                    daily.post_slack(summary_text(s))
                return s
            from . import feedback
            feedback.sync(ledger)
        health = daily.fetch_health()
        d = demand(health)
        shares = mix()
        plan = {k: q for k, q in split(d.get("deficit") or 0, shares).items() if q > 0}
        if plan_only or not d["run"]:
            s = {"skipped": not d["run"], "plan_only": plan_only, "why": d["why"], "demand": d, "plan": plan,
                 "areas": {k: next_areas(ledger, k)[:3] for k in plan or shares}}
            if slack and not plan_only:
                daily.post_slack(summary_text(s))
            return s

        budget = env_float("REPLENISH_MAX_COST_USD", 10.0) if max_cost is None else max_cost
        spent, slices, alerts = 0.0, [], []
        month_used = usage_this_month(ledger)
        counts_total: dict[str, int] = {}
        for kind, quota in plan.items():
            left = quota
            for area in next_areas(ledger, kind):
                if left <= 0 or spent >= budget:
                    break
                try:
                    r = run_slice(kind, area, left, commit=commit, budget_left=budget - spent, use_ai=use_ai)
                except Exception as exc:                    # noqa: BLE001 — one slice never stops the run
                    logger.exception("auto-replenish {} / {}: {}", kind, area, exc)
                    alerts.append(f"{kind}/{area} falló: {exc.__class__.__name__}")
                    continue
                spent += r["cost_usd"]
                for k, v in r["counts"].items():
                    counts_total[k] = counts_total.get(k, 0) + v
                got = r["inserted"] if commit else r["accepted"]
                record_area(ledger, kind, area, r["accepted"], exhausted=r["accepted"] == 0)
                slices.append(r)
                left -= got
                if r.get("crm_error"):
                    alerts.append(f"CRM: {r['crm_error']}")
            if left > 0:
                alerts.append(f"{kind}: faltaron {left} (zonas agotadas o tope de costo)")
        costs.tracker.budget_usd = None
        if spent >= budget:
            alerts.append(f"se alcanzó el tope de costo US$ {budget:g}")
        alerts += inventory_alerts(ledger, list(plan))
        net = costs.net_of_free_tier(counts_total, month_used)
        add_usage(ledger, counts_total)
        after = daily.pool_count(daily.fetch_health() or {}) if commit else None
        s = {"skipped": False, "committed": commit, "why": d["why"], "pool_before": d["pool"], "pool_after": after,
             "target": d["target"], "deficit": d["deficit"], "slices": slices,
             "accepted": sum(x["accepted"] for x in slices), "inserted": sum(x["inserted"] for x in slices),
             "with_email": sum(x["with_email"] for x in slices), "cost_list_usd": round(spent, 4),
             "cost_net_usd": net, "alerts": alerts}
        _conn(ledger).execute("INSERT OR REPLACE INTO replenish_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (started.isoformat(), int(commit), s["pool_before"], after, s["target"], s["deficit"],
                               s["inserted"], s["accepted"], s["with_email"], s["cost_list_usd"], net,
                               json.dumps({k: v for k, v in s.items() if k != "slices"}, ensure_ascii=False)))
        ledger.conn.commit()
        if slack:
            s["slack_sent"] = daily.post_slack(summary_text(s))
        return s
