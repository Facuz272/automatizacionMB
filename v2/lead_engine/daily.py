"""Daily routine in one command (`python -m lead_engine daily`).

  1. CRM pool: GET {CRM_BASE_URL}/api/public/health (x-webhook-secret = CRM_WEBHOOK_SECRET). When the
     answer carries an available-leads count (`pool_available`, or `facts.pool_available` /
     `pool_remaining` / `pool_restante`) the batch is `--target-pool` minus that count. The current CRM
     only returns `pool_low` (true/false): then the batch is the whole target when the pool is low and
     `--fallback-limit` when it is not. No secret → `--fallback-limit`.
  2. Run with the tenant profile (default mb_softwash): skips buildings seen in earlier runs, English
     PDFs with QR and the outreach pack.
  3. `--commit` imports the accepted leads; otherwise a CRM dry run (when the secret exists).
  4. SLACK_WEBHOOK_URL → executive summary (leads injected, annual value of the batch, top 3 buildings
     with their pain signal, folder with the PDFs). Also written to out/<batch>/daily_summary.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from .config import settings

COUNT_KEYS = ("pool_available", "pool_remaining", "pool_restante", "available")


def pool_count(health: dict[str, Any]) -> int | None:
    for scope in (health, health.get("facts") or {}, health.get("pool") or {}):
        for k in COUNT_KEYS:
            v = scope.get(k) if isinstance(scope, dict) else None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
    return None


def pool_low(health: dict[str, Any]) -> bool:
    facts = health.get("facts") or {}
    return bool(facts.get("pool_low")) or any(p.get("code") == "pool_low" for p in health.get("problems") or [])


def fetch_health(timeout: float = 20) -> dict[str, Any] | None:
    if not settings.crm_webhook_secret:
        return None
    try:
        r = requests.get(settings.crm_base_url.rstrip("/") + "/api/public/health",
                         headers={"x-webhook-secret": settings.crm_webhook_secret}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("No se pudo leer /api/public/health ({}); se usa --fallback-limit", exc)
        return None


DEFAULT_TARGET = 40


def batch_size(health: dict[str, Any] | None, target: int | None, fallback: int) -> tuple[int, str]:
    """(leads to produce, why). Without an explicit target the CRM's own `pool_target` is used (else 40)."""
    if target is None:
        crm_target = (health or {}).get("pool_target")
        target = int(crm_target) if isinstance(crm_target, (int, float)) and crm_target > 0 else DEFAULT_TARGET
    if health is None:
        return fallback, "sin lectura del CRM"
    n = pool_count(health)
    if n is not None:
        return max(0, target - n), f"Pool con {n} disponibles (objetivo {target})"
    if pool_low(health):
        return target, "el CRM marca el Pool bajo"
    return fallback, "el CRM no informa cantidad y el Pool no está bajo"


def _signal_es(rec: dict[str, Any]) -> str:
    for s in (rec.get("municipal") or []) + (rec.get("pain") or []):
        if s.get("label"):
            return s["label"]
    return "sin señal urgente"


def summary(out: Path, committed: bool) -> dict[str, Any]:
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    qr = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    crm = qr.get("crm") or {}
    injected = (crm.get("inserted") or 0) if committed else 0
    low = sum((l.get("estimated_deal") or {}).get("low") or 0 for l in leads)
    high = sum((l.get("estimated_deal") or {}).get("high") or 0 for l in leads)
    top = sorted(leads, key=lambda l: -(l.get("lead_score") or 0))[:3]
    return {
        "batch": out.name, "accepted": len(leads), "committed": committed,
        "injected": injected,
        "would_insert": crm.get("would_insert"), "annual_usd_low": low, "annual_usd_high": high,
        "needs_phone": len((out / "needs_phone.csv").read_text(encoding="utf-8").splitlines()) - 1
        if (out / "needs_phone.csv").exists() else 0,
        "consolidated_siblings": qr.get("consolidated_siblings", 0),
        "top": [{"name": l.get("company_name"), "address": l.get("address"), "score": l.get("lead_score"),
                 "signal": _signal_es(l), "est_high": (l.get("estimated_deal") or {}).get("high")} for l in top],
        "pdf_dir": str(out / "pdf"), "outreach_pack": str(out / "outreach_pack.md"),
        "field_sheets": str(out / "campo"), "mix": qr.get("mix"),
    }


def slack_text(s: dict[str, Any], why: str) -> str:
    money = lambda n: f"US$ {n:,}".replace(",", ".")  # noqa: E731
    head = (f"*Lead Engine · {s['batch']}*\n"
            f"{'Inyectados al CRM' if s['committed'] else 'Listos (dry-run, sin importar)'}: *{s['injected'] if s['committed'] else s['accepted']}*"
            f" · valor anual estimado del lote: *{money(s['annual_usd_low'])} – {money(s['annual_usd_high'])}*\n"
            f"Motivo del tamaño: {why}. Sin teléfono: {s['needs_phone']}"
            + (f" · edificios consolidados en llamadas de administradora: {s['consolidated_siblings']}"
               if s["consolidated_siblings"] else ""))
    if s.get("mix"):
        head += "\nMix: " + " · ".join(f"{k} {v['accepted']}/{v['quota']}" for k, v in s["mix"].items())
    tops = "\n".join(f"{i}. *{t['name']}* ({t['address']}) — {t['signal']}"
                     + (f" · hasta {money(t['est_high'])}/año" if t.get("est_high") else "")
                     for i, t in enumerate(s["top"], 1))
    return f"{head}\n{tops}\nPDFs: `{s['pdf_dir']}` · Outreach: `{s['outreach_pack']}`"


def post_slack(text: str, url: str | None = None) -> bool:
    url = url or os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        r = requests.post(url, json={"text": text}, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Slack falló: {}", exc)
        return False


# ── multi-vertical mix ──────────────────────────────────────────────────────

SEEDS = {"dbpr", "pa", "npi", "places", "osm"}


def validate_mix(slices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, sl in enumerate(slices):
        if sl.get("seed") not in SEEDS or not (sl.get("share") or 0) > 0:
            raise ValueError(f"daily_mix[{i}]: hace falta seed ({'/'.join(sorted(SEEDS))}) y share > 0")
        if sl["seed"] == "pa" and sl.get("vertical") not in ("plaza", "commercial", "clinic"):
            raise ValueError(f"daily_mix[{i}]: seed pa requiere vertical plaza|commercial|clinic")
        out.append({"name": sl.get("name") or f"{sl['seed']}-{sl.get('vertical') or sl.get('strategy', 'onsite')}",
                    "share": float(sl["share"]), "seed": sl["seed"], "strategy": sl.get("strategy") or "onsite",
                    "vertical": sl.get("vertical")})
    return out


def quotas(total: int, slices: list[dict[str, Any]]) -> list[int]:
    """Largest-remainder split of `total` by share (sums exactly to total)."""
    weight = sum(s["share"] for s in slices) or 1
    raw = [total * s["share"] / weight for s in slices]
    q = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - q[i]))[: total - sum(q)]:
        q[i] += 1
    return q


def _with_vertical(vertical: str | None):
    from contextlib import contextmanager

    from . import pipeline

    @contextmanager
    def cm():
        target = pipeline.settings
        before = target.seed_vertical
        if vertical:
            object.__setattr__(target, "seed_vertical", vertical)
        try:
            yield
        finally:
            object.__setattr__(target, "seed_vertical", before)
    return cm()


def run_mix(total: int, slices: list[dict[str, Any]], *, send: bool, commit: bool, use_ai: bool = True,
            out_dir: Path | None = None) -> Path:
    """Each slice runs as its own collection (no CRM send, no PDFs) in out/<mix>/slices/, then the accepted
    leads are merged, de-duplicated by phone, ranked by lead_score and ticket, and finalized as ONE batch:
    PDFs, field sheets, outreach pack, CRM payload/import, quality report with the per-slice breakdown."""
    import csv

    from . import pipeline, scoring
    from .models import Lead
    from .normalize import phone_key

    slices = validate_mix(slices)
    batch_id = pipeline.new_batch_id("mix")
    out = Path(out_dir or settings.out_dir) / batch_id
    out.mkdir(parents=True, exist_ok=True)
    leads: list[Lead] = []
    rejected: list[dict] = []
    np_rows: list[dict] = []
    breakdown: dict[str, dict[str, Any]] = {}

    def one(sl: dict[str, Any], n: int) -> None:
        with _with_vertical(sl["vertical"]):
            o = pipeline.run(strategy=sl["strategy"], limit=n, sources=[sl["seed"]], use_ai=use_ai, send=False,
                             out_dir=out / "slices", make_pdfs=False)
        got = [Lead.from_record(json.loads(l)) for l in (o / "leads.jsonl").read_text(encoding="utf-8").splitlines()
               if l.strip()]
        leads.extend(got)
        rejected.extend(json.loads(l) for l in (o / "rejected.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())
        if (o / "needs_phone.csv").exists():
            np_rows.extend(csv.DictReader(open(o / "needs_phone.csv", encoding="utf-8")))
        b = breakdown.setdefault(sl["name"], {"quota": 0, "accepted": 0, "batches": []})
        b["quota"] += n
        b["accepted"] += len(got)
        b["batches"].append(o.name)

    for sl, q in zip(slices, quotas(total, slices)):
        if q > 0:
            one(sl, q)
    deficit = total - len(leads)
    if deficit > 0:                      # slices that came short (e.g. no phones) → top up from the main slice
        logger.info("Mix: faltan {} leads; se completan con '{}'", deficit, slices[0]["name"])
        one(slices[0], deficit)

    best: dict[str, Lead] = {}
    for l in scoring.rank(leads):
        best.setdefault(phone_key(l.phone or "") or l.source_id, l)
    accepted = scoring.rank(list(best.values()))[:total]
    if np_rows:
        with open(out / "needs_phone.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(np_rows[0].keys()))
            w.writeheader()
            w.writerows(np_rows)
    return pipeline.finalize(out, batch_id, "mix", accepted, rejected, send=send, commit=commit, make_pdfs=True,
                             pdf_lang="en", extra={"mix": breakdown})


def run(*, target_pool: int | None = None, fallback_limit: int = 10, limit: int | None = None, strategy: str = "onsite",
        sources: list[str] | None = None, commit: bool = False, use_ai: bool = True, out_dir: Path | None = None,
        slack: bool = True, mix: bool | None = None) -> dict[str, Any]:
    from . import pipeline

    health = None if limit is not None else fetch_health()
    n, why = (limit, "--limit explícito") if limit is not None else batch_size(health, target_pool, fallback_limit)
    logger.info("daily: {} leads ({})", n, why)
    if n <= 0:
        s = {"batch": None, "skipped": True, "why": why}
        if slack:
            post_slack(f"*Lead Engine · daily*: no hace falta cargar leads hoy ({why}).")
        return s
    use_mix = (mix if mix is not None else not sources) and bool(settings.daily_mix)
    if use_mix:
        out = run_mix(n, settings.daily_mix, send=bool(settings.crm_webhook_secret), commit=commit, use_ai=use_ai,
                      out_dir=out_dir)
    else:
        out = pipeline.run(strategy=strategy, limit=n, sources=sources, use_ai=use_ai,
                           send=bool(settings.crm_webhook_secret), commit=commit, out_dir=out_dir,
                           make_pdfs=True, pdf_lang="en")
    s = summary(out, committed=commit)
    s["why"] = why
    (out / "daily_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    s["slack_sent"] = post_slack(slack_text(s, why)) if slack else False
    return s
