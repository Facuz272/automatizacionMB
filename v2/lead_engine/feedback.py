"""CRM feedback loop: what happened to the leads we sent (`python -m lead_engine feedback`, and step 0 of `daily`).

GET {CRM_BASE_URL}/api/public/lead-outcomes?since=<iso>&after_id=<uuid>&limit=500 (header x-webhook-secret;
contract in the CRM's docs/LEAD_OUTCOMES.md): {ok, count, leads: [...], next_since, next_after_id}, oldest change
first; the (next_since, next_after_id) pair is stored and the next run continues from it (first run: all).
Tolerant on purpose: a 404, a timeout or a bad body only log a warning, the cursor does not move and the run
goes on as before. Also accepted: a bare list, or `outcomes` / `items` / `data` with `next_cursor`.
Per lead (extra keys ignored, missing ones tolerated): id, batch_id | import_batch_id, source, segment,
phone, status, call_attempts, no_answer_streak, discard_reason (wrong_number | not_management |
external_management | out_of_area; the CRM's Spanish labels are accepted too), objection,
inspection_scheduled, closed_won, changed_at [, address, zip, county]; post-visit (CRM 0055, latest quote):
visit_completed, quote_amount_usd, quote_status (visit_completed | quote_sent | board_review |
changes_requested | won | lost), quote_loss_reason (price | competitor | postponed | no_budget | scope_mismatch).

What the engine does with it (state.db):
  · wrong_number, or no_answer_streak >= 6 (and no inspection / sale on it) → the phone goes to `bad_phones`
    for that building (matched by the phone it was sent with) and the building is reopened ("rephone"): the
    next run works it again, drops that number everywhere (Places, OSM, site, lodging, firm line) and
    looks for another one.
  · external_management → the building is flagged `prefer_firm` and reopened: the next run dials the
    management firm's published office line first (the building line stays as a backup in the Ficha).
  · out_of_area → its ZIP is reported (candidates for the tenant's exclude_zips), never applied silently.
  · not_management → counted in the report.
  · every outcome is stored once per CRM lead in `crm_outcomes`: conversion by seed / segment / county,
    and once a mix slice has >= 30 worked samples its share is scaled by its inspection rate against the
    overall rate (clipped to x0.5 – x2.0).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

import requests
from loguru import logger

from .config import settings
from .normalize import phone_key
from .state import Ledger

PATH = "/api/public/lead-outcomes"
NO_ANSWER_LIMIT = 6
MIN_SAMPLES = 30
WEIGHT_RANGE = (0.5, 2.0)
MAX_PAGES = 50

REASONS = {
    "wrong_number": ("wrong_number", "numero_equivocado", "wrong", "numero_incorrecto", "bad_number"),
    "not_management": ("not_management", "no_es_administrador", "no_es_administracion", "not_the_manager"),
    "external_management": ("external_management", "administracion_remota_externa", "administracion_externa",
                            "administracion_remota", "remote_management", "external"),
    "out_of_area": ("out_of_area", "fuera_de_zona", "fuera_de_area", "outside_area"),
}
_REASON_OF = {alias: canon for canon, aliases in REASONS.items() for alias in aliases}


# ── parsing ─────────────────────────────────────────────────────────────────

def _slug(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")


def discard_reason(raw: Any) -> str | None:
    s = _slug(raw)
    return _REASON_OF.get(s) or (s or None)


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "si", "sí")
    return bool(v)


def _amount(v: Any) -> float | None:
    """USD amount from a number or a numeric string ('12,500.00'); None when missing or not a number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", "").replace("$", "").strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return round(x, 2) if x >= 0 else None


def _zip(*texts: Any) -> str | None:
    for t in texts:
        m = re.search(r"\b(\d{5})(?:-\d{4})?\b(?!.*\b\d{5}\b)", str(t or ""))
        if m:
            return m.group(1)
    return None


def normalize_outcome(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One CRM row → canonical outcome (None when it has neither id nor phone)."""
    if not isinstance(raw, dict):
        return None
    phone = raw.get("phone") or raw.get("phone_e164")
    pkey = raw.get("phone_key") or (phone_key(phone) if phone else None)
    crm_id = raw.get("id") or raw.get("lead_id")
    if not crm_id and not pkey:
        return None
    source = raw.get("source") or ""
    zip5 = raw.get("zip") and str(raw["zip"])[:5] or _zip(raw.get("address"))
    return {
        "crm_id": str(crm_id or f"phone:{pkey}"),
        "batch_id": raw.get("batch_id") or raw.get("import_batch_id"),
        "source": source or None,
        "seed": source.rsplit(":", 1)[-1] if ":" in source else None,   # lead-engine-v2:dbpr → dbpr
        "segment": raw.get("segment"),
        "address": raw.get("address"),
        "zip": zip5,
        "county": raw.get("county"),
        "phone_key": pkey,
        "status": raw.get("status"),
        "call_attempts": _int(raw.get("call_attempts")),
        "no_answer_streak": _int(raw.get("no_answer_streak")),
        "discard_reason": discard_reason(raw.get("discard_reason")),
        "objection": raw.get("objection") or raw.get("objection_reason"),
        "inspection_scheduled": _bool(raw.get("inspection_scheduled")),
        "closed_won": _bool(raw.get("closed_won")),
        # Post-visit (CRM 0055, latest quote of the lead); None when the CRM does not send them.
        "visit_completed": _bool(raw["visit_completed"]) if raw.get("visit_completed") is not None else None,
        "quote_amount_usd": _amount(raw.get("quote_amount_usd")),
        "quote_status": _slug(raw.get("quote_status")) or None,
        "quote_loss_reason": _slug(raw.get("quote_loss_reason")) or None,
        "updated_at": raw.get("changed_at") or raw.get("updated_at"),
    }


def _items(body: Any) -> tuple[list[dict], dict[str, str] | None]:
    """(rows, cursor of the next page or None). CRM contract: {ok, count, leads, next_since, next_after_id}."""
    if isinstance(body, list):
        return body, None
    if isinstance(body, dict):
        for k in ("leads", "outcomes", "items", "data"):
            if isinstance(body.get(k), list):
                if body.get("next_since"):
                    return body[k], {"since": body["next_since"],
                                     **({"after_id": body["next_after_id"]} if body.get("next_after_id") else {})}
                nxt = body.get("next_cursor") or body.get("next")
                return body[k], ({"cursor": nxt} if nxt else None)
    raise ValueError("respuesta sin lista de resultados")


# ── fetch ───────────────────────────────────────────────────────────────────

PAGE = 500


def fetch_page(cursor: dict[str, str], timeout: float = 20) -> tuple[list[dict[str, Any]], dict[str, str] | None, int]:
    """(normalised outcomes, next cursor, raw rows) for one page. Raises on HTTP / body errors."""
    r = requests.get(settings.crm_base_url.rstrip("/") + PATH, params={**cursor, "limit": PAGE}, timeout=timeout,
                     headers={"x-webhook-secret": settings.crm_webhook_secret})
    if r.status_code == 404:
        raise EndpointMissing(PATH)
    r.raise_for_status()
    items, nxt = _items(r.json())
    return [o for o in (normalize_outcome(i) for i in items) if o], nxt, len(items)


class EndpointMissing(Exception):
    pass


def fetch_outcomes(cursor: dict[str, str] | None,
                   timeout: float = 20) -> tuple[list[dict[str, Any]], dict[str, str] | None] | None:
    """(every outcome after the cursor, cursor to store for the next run), or None when the endpoint is
    missing / failing (never raises). Pages until a short page (keyset cursor since + after_id)."""
    if not settings.crm_webhook_secret:
        return None
    cursor = dict(cursor or {})
    out: list[dict[str, Any]] = []
    try:
        for _ in range(MAX_PAGES):
            rows, nxt, n = fetch_page(cursor, timeout)
            out += rows
            if nxt:
                cursor = nxt
            if not nxt or n < PAGE:
                break
    except EndpointMissing:
        logger.warning("CRM {}: 404 (el endpoint todavía no existe); la corrida sigue sin feedback", PATH)
        return None
    except (requests.RequestException, ValueError) as exc:
        logger.warning("CRM {} no respondió bien ({}); la corrida sigue sin feedback", PATH, exc)
        return None
    return out, cursor


# ── ingest ──────────────────────────────────────────────────────────────────

def _county(o: dict[str, Any], buildings: list[dict]) -> str | None:
    from .municipal_signals import county_of

    if o.get("county"):
        return _slug(o["county"]).replace("_", "-")
    z = o.get("zip") or next((b["address_key"].rsplit("|", 1)[-1] for b in buildings
                              if b.get("address_key") and "|" in b["address_key"]), None)
    return county_of(f"FL {z}") if z else None


def _building_keys(o: dict[str, Any], buildings: list[dict]) -> list[tuple[list[str], dict]]:
    """[(keys, seen row)] for every building this outcome's phone was sent for (or its own address)."""
    from .dbpr import address_key

    out = []
    for b in buildings:
        keys = [k for k in (b.get("address_key") and "addr:" + b["address_key"],
                            b.get("source_id") and "src:" + b["source_id"]) if k]
        if keys:
            out.append((keys, b))
    ak = address_key(o.get("address"))
    if ak and not any("addr:" + ak in keys for keys, _ in out):
        out.append((["addr:" + ak], {"address_key": ak, "source_id": None}))
    return out


def worked(o: dict[str, Any]) -> bool:
    """A conversion sample: somebody called it, or it ended (discarded / inspection / sale)."""
    return bool(o["call_attempts"] or o["discard_reason"] or o["inspection_scheduled"] or o["closed_won"]
                or o.get("visit_completed"))


def ingest(outcomes: Iterable[dict[str, Any]], ledger: Ledger | None = None) -> dict[str, Any]:
    ledger = ledger or Ledger()
    now = datetime.now(timezone.utc).isoformat()
    rep: dict[str, Any] = {"stored": 0, "bad_phones": [], "reopened": 0, "prefer_firm": [], "out_of_area_zips": {},
                           "not_management": 0, "unmatched": 0}
    for o in outcomes:
        buildings = ledger.buildings_for_phone(o["phone_key"]) if o.get("phone_key") else []
        targets = _building_keys(o, buildings)
        vc = o.get("visit_completed")
        row = {"crm_id": o["crm_id"], "batch_id": o["batch_id"], "source": o["source"],
               "seed": o["seed"] or next((b["source"] for b in buildings if b.get("source")), None),
               "segment": o["segment"], "county": _county(o, buildings), "zip": o["zip"], "phone_key": o["phone_key"],
               "building_key": targets[0][0][0] if targets else None, "status": o["status"],
               "call_attempts": o["call_attempts"], "no_answer_streak": o["no_answer_streak"],
               "discard_reason": o["discard_reason"], "objection": o["objection"],
               "inspection_scheduled": int(o["inspection_scheduled"]), "closed_won": int(o["closed_won"]),
               "updated_at": o["updated_at"], "synced_at": now,
               "visit_completed": None if vc is None else int(vc), "quote_amount_usd": o.get("quote_amount_usd"),
               "quote_status": o.get("quote_status"), "quote_loss_reason": o.get("quote_loss_reason")}
        ledger.conn.execute(f"INSERT OR REPLACE INTO crm_outcomes ({', '.join(row)}) VALUES "
                            f"({', '.join('?' * len(row))})", tuple(row.values()))
        rep["stored"] += 1
        reason = o["discard_reason"]
        converted = o["inspection_scheduled"] or o["closed_won"]
        bad = None if converted else ("wrong_number" if reason == "wrong_number" else
                                      f"no contesta ({o['no_answer_streak']} seguidas)"
                                      if o["no_answer_streak"] >= NO_ANSWER_LIMIT else None)
        if (bad or reason == "external_management") and not targets:
            rep["unmatched"] += 1
        if bad and o.get("phone_key"):
            for keys, b in targets:
                new = [ledger.add_bad_phone(k, o["phone_key"], bad, o["crm_id"]) for k in keys]
                if any(new):
                    rep["bad_phones"].append({"building": b.get("company_name") or keys[0], "phone_key": o["phone_key"],
                                              "reason": bad})
                    rep["reopened"] += bool(ledger.reopen_building(b.get("address_key"), b.get("source_id")))
        if reason == "external_management" and not converted:
            for keys, b in targets:
                if any(ledger.conn.execute("SELECT 1 FROM building_flags WHERE building_key = ? AND flag = 'prefer_firm'",
                                           (k,)).fetchone() for k in keys):
                    continue
                for k in keys:
                    ledger.set_flag(k, "prefer_firm", "external_management", o["crm_id"])
                rep["prefer_firm"].append(b.get("company_name") or keys[0])
                rep["reopened"] += bool(ledger.reopen_building(b.get("address_key"), b.get("source_id")))
        if reason == "out_of_area" and o.get("zip"):
            rep["out_of_area_zips"][o["zip"]] = rep["out_of_area_zips"].get(o["zip"], 0) + 1
        if reason == "not_management":
            rep["not_management"] += 1
    ledger.conn.commit()
    return rep


def sync(ledger: Ledger | None = None, since: str | None = None) -> dict[str, Any]:
    """Fetch + ingest from the stored cursor (first time: everything); remembers the CRM's cursor.
    Never raises: a failure is a warning in the report and the cursor does not move."""
    if not settings.crm_webhook_secret:
        return {"ok": False, "skipped": True, "why": "sin CRM_WEBHOOK_SECRET"}
    try:
        ledger = ledger or Ledger()
        if since:
            cursor = {"since": since}
        else:
            cursor = {k: v for k, v in (("since", ledger.get_state("outcomes_since")),
                                        ("after_id", ledger.get_state("outcomes_after_id"))) if v}
        got = fetch_outcomes(cursor)
        if got is None:
            return {"ok": False, "why": f"{PATH} no disponible (404 o error); sin feedback en esta corrida"}
        outcomes, nxt = got
        rep = ingest(outcomes, ledger)
        if nxt and nxt.get("since"):
            ledger.set_state("outcomes_since", nxt["since"])
            ledger.set_state("outcomes_after_id", nxt.get("after_id") or "")
        return {"ok": True, "fetched": len(outcomes), "since": cursor.get("since"), **rep}
    except Exception as exc:                 # noqa: BLE001 — the feedback never stops a production run
        logger.warning("feedback del CRM falló ({}); la corrida sigue", exc)
        return {"ok": False, "why": f"error: {exc.__class__.__name__}: {exc}"[:200]}


# ── conversion metrics ──────────────────────────────────────────────────────

GROUPS = {"seed": "seed", "segment": "segment", "county": "county", "source": "source"}


def conversion(ledger: Ledger | None = None, by: tuple[str, ...] = ("seed", "segment", "county")) -> list[dict[str, Any]]:
    """Worked samples, inspections and sales per group (only leads somebody actually worked)."""
    ledger = ledger or Ledger()
    cols = [GROUPS[b] for b in by]
    sel = ", ".join(f"COALESCE({c}, '?')" for c in cols)
    rows = ledger.conn.execute(
        f"SELECT {sel}, COUNT(*), SUM(inspection_scheduled OR closed_won), SUM(closed_won), "
        "SUM(COALESCE(visit_completed, 0)), SUM(quote_amount_usd IS NOT NULL), "
        "SUM(CASE WHEN quote_status = 'won' THEN 1 ELSE 0 END), SUM(CASE WHEN quote_status = 'lost' THEN 1 ELSE 0 END), "
        "SUM(quote_amount_usd), SUM(CASE WHEN quote_status = 'won' THEN quote_amount_usd END) FROM crm_outcomes "
        "WHERE call_attempts > 0 OR discard_reason IS NOT NULL OR inspection_scheduled = 1 OR closed_won = 1 "
        "OR visit_completed = 1 OR quote_status IS NOT NULL "
        f"GROUP BY {sel} ORDER BY COUNT(*) DESC").fetchall()
    out = []
    k = len(cols)
    for r in rows:
        n, insp, won = r[k], r[k + 1] or 0, r[k + 2] or 0
        visits, quoted, q_won, q_lost = (r[k + 3] or 0), (r[k + 4] or 0), (r[k + 5] or 0), (r[k + 6] or 0)
        out.append({**dict(zip(by, r[:k])), "n": n, "inspections": insp, "closed": won,
                    "inspection_rate": round(insp / n, 3) if n else 0.0, "close_rate": round(won / n, 3) if n else 0.0,
                    "visits": visits, "quotes": quoted, "quotes_won": q_won, "quotes_lost": q_lost,
                    "quoted_usd": round(r[k + 7] or 0, 2), "won_usd": round(r[k + 8] or 0, 2),
                    "quote_win_rate": round(q_won / (q_won + q_lost), 3) if q_won + q_lost else None})
    return out


def loss_reasons(ledger: Ledger | None = None) -> dict[str, int]:
    """Why quotes were lost (price, competitor, postponed, no_budget, scope_mismatch, sin motivo)."""
    ledger = ledger or Ledger()
    rows = ledger.conn.execute("SELECT COALESCE(quote_loss_reason, 'sin motivo'), COUNT(*) FROM crm_outcomes "
                               "WHERE quote_status = 'lost' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    return dict(rows)


def _slice_rows(ledger: Ledger, sl: dict[str, Any]) -> tuple[int, int]:
    from .verticals import label as vertical_label

    where, args = "seed = ?", [sl["seed"]]
    if sl.get("vertical") and settings.crm_segment_mode == "vertical":
        where += " AND segment = ?"
        args.append(vertical_label(sl["vertical"]))
    n, insp = ledger.conn.execute(
        f"SELECT COUNT(*), SUM(inspection_scheduled OR closed_won) FROM crm_outcomes WHERE {where} AND "
        "(call_attempts > 0 OR discard_reason IS NOT NULL OR inspection_scheduled = 1 OR closed_won = 1)",
        args).fetchone()
    return n or 0, insp or 0


def weighted_mix(slices: list[dict[str, Any]], ledger: Ledger | None = None,
                 min_samples: int = MIN_SAMPLES) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(slices with adjusted shares, explanation). A slice moves only with >= min_samples worked leads and
    an overall rate above zero (overall = Lead Engine leads only, never the CRM's other leads); its share is multiplied by its rate / overall rate within WEIGHT_RANGE."""
    try:
        ledger = ledger or Ledger()
        tot_n, tot_i = ledger.conn.execute(
            "SELECT COUNT(*), SUM(inspection_scheduled OR closed_won) FROM crm_outcomes WHERE seed IS NOT NULL AND "
            "(call_attempts > 0 OR discard_reason IS NOT NULL OR inspection_scheduled = 1 OR closed_won = 1)").fetchone()
    except Exception as exc:                 # noqa: BLE001
        logger.warning("métricas de conversión no disponibles ({})", exc)
        return [dict(s) for s in slices], []
    base = (tot_i or 0) / tot_n if tot_n else 0.0
    out, notes = [], []
    for sl in slices:
        sl = dict(sl)
        n, insp = _slice_rows(ledger, sl) if "seed" in sl else (0, 0)
        mult = 1.0
        if n >= min_samples and base > 0:
            lo, hi = WEIGHT_RANGE
            mult = round(min(hi, max(lo, (insp / n) / base)), 2)
        notes.append({"slice": sl.get("name") or sl.get("seed"), "samples": n, "inspections": insp,
                      "multiplier": mult, "share_before": sl.get("share"),
                      "share_after": round(float(sl.get("share") or 0) * mult, 4)})
        sl["share"] = float(sl.get("share") or 0) * mult
        out.append(sl)
    return out, notes


def report_text(rep: dict[str, Any]) -> str:
    if not rep.get("ok"):
        return f"feedback CRM: {rep.get('why')}"
    parts = [f"{rep['fetched']} resultados"]
    if rep["bad_phones"]:
        parts.append(f"{len(rep['bad_phones'])} teléfonos descartados")
    if rep["prefer_firm"]:
        parts.append(f"{len(rep['prefer_firm'])} edificios pasan a la administradora")
    if rep["reopened"]:
        parts.append(f"{rep['reopened']} edificios reabiertos")
    if rep["out_of_area_zips"]:
        parts.append("fuera de zona: " + ", ".join(f"{z} ({n})" for z, n in sorted(rep["out_of_area_zips"].items())))
    if rep["not_management"]:
        parts.append(f"{rep['not_management']} 'no es administrador'")
    if rep.get("unmatched"):
        parts.append(f"{rep['unmatched']} descarte(s) de leads que no cargó el motor (sin edificio que reabrir)")
    return "feedback CRM: " + " · ".join(parts)
