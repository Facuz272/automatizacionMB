"""Pre-flight check before production runs (`python -m lead_engine preflight --tenant mb_softwash`).

Configuration: API keys, CRM, Slack, the tenant's booking link / documents / contact, local registries
(data/dbpr.db: condos, CAM licences, lodging). Connectivity, with short timeouts and in parallel:
public endpoints (Miami-Dade cadastre, Broward tax roll, City of Miami recertification, Miami-Dade
permits, Census geocoder, NPI Registry, Overpass + mirrors) and, only when their keys exist, a light
ping to Google Places (Text Search, IDs-only field mask), Anthropic (model list, no tokens) and the
CRM /api/public/health. Status per line: OK / PENDIENTE (not configured yet) / ERROR. Nothing is
written anywhere; exit code 1 only when something configured fails.
"""

from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import requests

from .config import settings

OK, PENDING, ERROR = "OK", "PENDIENTE", "ERROR"
TIMEOUT = 8.0


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str = ""
    ms: int | None = None


# ── configuration (pure, no network) ────────────────────────────────────────

def config_checks(env: dict[str, str] | None = None) -> list[Check]:
    env = os.environ if env is None else env
    out = []

    def key(name: str, value: str | None, why: str) -> None:
        out.append(Check("config", name, OK if value else PENDING, "cargada" if value else why))

    key("GOOGLE_PLACES_API_KEY", settings.google_places_api_key,
        "sin clave: teléfonos solo por OSM/Lodging/web, plazas y comerciales encuentran pocos")
    key("ANTHROPIC_API_KEY", settings.anthropic_api_key, "sin clave: calificación solo por reglas")
    out.append(Check("config", "CRM_BASE_URL", OK, settings.crm_base_url))
    key("CRM_WEBHOOK_SECRET", settings.crm_webhook_secret, "sin secreto: no hay dry-run/commit ni lectura del Pool")
    key("SLACK_WEBHOOK_URL", env.get("SLACK_WEBHOOK_URL"), "opcional: sin resumen diario en Slack")
    from . import search
    prov = search.provider()
    out.append(Check("config", "búsqueda externa", OK if prov else PENDING,
                     prov or "opcional: SERPAPI_KEY / GOOGLE_CSE_KEY+GOOGLE_CSE_CX / BRAVE_SEARCH_KEY"))
    t = settings.tenant
    out.append(Check("tenant", "perfil", OK if t else PENDING, t or "sin --tenant ni TENANT"))
    out.append(Check("tenant", "booking_url", OK if settings.company_booking_url else PENDING,
                     settings.company_booking_url or "el QR apunta al sitio web hasta cargar la agenda"))
    docs = settings.documents_available or []
    out.append(Check("tenant", "documents_available", OK if docs else PENDING,
                     ", ".join(docs) if docs else "COI / W-9 no se mencionan en los PDF hasta declararlos"))
    for field_name, label in (("company_phone", "teléfono"), ("company_email", "email"),
                              ("company_website", "sitio web")):
        v = getattr(settings, field_name)
        out.append(Check("tenant", label, OK if v else PENDING, v or "vacío en el perfil"))
    out += registry_checks()
    out += run_state_checks()
    return out


def run_state_checks() -> list[Check]:
    """Run lock (another run in progress?) and committed batches waiting for the CRM."""
    from .runlock import holder, lock_path
    from .state import Ledger

    who = holder()
    out = [Check("datos", "lock de corrida", ERROR if who else OK,
                 f"hay una corrida en curso: {who}" if who else f"libre ({lock_path()})")]
    pend = Ledger().pending()
    out.append(Check("datos", "lotes pendientes de CRM", PENDING if pend else OK,
                     "; ".join(f"{p['batch_id']} ({p['attempts']} intento/s: {(p['last_error'] or '')[:60]})"
                               for p in pend[:3]) + (" · se reenvían al arrancar daily" if pend else "")
                     if pend else "ninguno"))
    return out


def registry_checks() -> list[Check]:
    path = settings.state_db.with_name("dbpr.db")
    if not path.exists():
        return [Check("datos", "data/dbpr.db", PENDING, "falta: python -m lead_engine dbpr-sync")]
    out = []
    conn = sqlite3.connect(path)
    for table, label, cmd in (("dbpr_condos", "padrón de condominios", "dbpr-sync"),
                              ("cam_licenses", "licencias CAM/CAB", "dbpr-sync"),
                              ("dbpr_lodging", "padrón de alojamiento", "dbpr-sync")):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            n = 0
        out.append(Check("datos", label, OK if n else PENDING, f"{n:,} filas" if n else f"vacío: {cmd}"))
    conn.close()
    from . import sunbiz
    n = sunbiz.count() if sunbiz._db_path().exists() else 0
    out.append(Check("datos", "Sunbiz (directivos)", OK if n else PENDING,
                     f"{n:,} asociaciones" if n else "vacío: archivo en data/sunbiz/ + sunbiz-sync (opcional)"))
    return out


# ── connectivity ────────────────────────────────────────────────────────────

def _arcgis(url: str, where: str) -> Callable[[], str]:
    def probe() -> str:
        r = requests.get(url, params={"where": where, "returnCountOnly": "true", "f": "json"}, timeout=TIMEOUT,
                         headers={"User-Agent": settings.user_agent})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message"))
        return f"responde ({data.get('count', '?')} filas de prueba)"
    return probe


def _census() -> str:
    from .geocode import URL
    r = requests.get(URL, params={"address": "111 NW 1st St, Miami, FL 33128", "benchmark": "Public_AR_Current",
                                  "format": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    n = len(r.json().get("result", {}).get("addressMatches", []))
    return f"{n} coincidencia(s)"


def _npi() -> str:
    from .npi import API
    r = requests.get(API, params={"version": "2.1", "state": "FL", "city": "MIAMI", "enumeration_type": "NPI-2",
                                  "limit": 1}, timeout=TIMEOUT)
    r.raise_for_status()
    return f"{r.json().get('result_count', 0)} resultado(s)"


def _overpass() -> str:
    from .overpass import endpoints
    ok, bad = [], []
    for u in endpoints():
        try:
            r = requests.post(u, data={"data": "[out:json][timeout:5];node(1);out ids;"}, timeout=(4, 6),
                              headers={"User-Agent": settings.user_agent})
            r.raise_for_status()
            ok.append(u.split("/")[2])
        except requests.RequestException:
            bad.append(u.split("/")[2])
    if not ok:
        raise RuntimeError("ningún endpoint responde: " + ", ".join(bad) + " (se usa la inferencia catastral)")
    return "responden: " + ", ".join(ok) + (f" · caídos: {', '.join(bad)}" if bad else "")


def _dns_mx() -> str:
    from . import email_verify
    email_verify.backend.cache_clear()
    b = email_verify.backend()
    if b == "none":
        raise RuntimeError("ningún DNS responde MX (ni el del sistema ni DNS-over-HTTPS): los emails no se verifican")
    st = email_verify.domain_status("gmail.com")
    return {"system": "DNS del sistema", "doh": "DNS-over-HTTPS (el DNS del sistema no responde MX)"}[b] + f" · gmail.com={st}"


def _places() -> str:
    from .collectors.google_places import SEARCH_URL
    r = requests.post(SEARCH_URL, json={"textQuery": "condominium Miami Beach FL", "pageSize": 1}, timeout=TIMEOUT,
                      headers={"X-Goog-Api-Key": settings.google_places_api_key, "X-Goog-FieldMask": "places.id"})
    if r.status_code in (401, 403):
        raise RuntimeError(f"clave rechazada (HTTP {r.status_code})")
    r.raise_for_status()
    return f"clave válida ({len(r.json().get('places', []))} resultado, campo id solamente)"


def _anthropic() -> str:
    r = requests.get("https://api.anthropic.com/v1/models", timeout=TIMEOUT,
                     headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"})
    if r.status_code in (401, 403):
        raise RuntimeError(f"clave rechazada (HTTP {r.status_code})")
    r.raise_for_status()
    ids = [m.get("id") for m in r.json().get("data", [])]
    model = settings.anthropic_model
    return f"clave válida · modelo {model} " + ("disponible" if model in ids else "no figura en la lista")


def _crm() -> str:
    from .daily import fetch_health
    h = fetch_health(timeout=TIMEOUT)
    if h is None:
        raise RuntimeError("sin respuesta o secreto rechazado")
    pool = f" · Pool {h['pool_available']}/{h.get('pool_target', '?')}" if "pool_available" in h else ""
    probs = ", ".join(p.get("code", "?") for p in h.get("problems") or [])
    return ("ok" if h.get("ok") else f"problemas: {probs}") + pool


def network_probes() -> list[tuple[str, str, Callable[[], str] | None, str]]:
    """(group, name, probe or None when its key is missing, why pending)."""
    from . import appraiser, broward, municipal_signals

    layer = {l.key: l.url for l in municipal_signals.LAYERS}
    return [
        ("red", "Catastro Miami-Dade", _arcgis(appraiser.URL, "FOLIO = '0000000000000'"), ""),
        ("red", "Catastro Broward (tax roll)", _arcgis(broward.URL, "FOLIO_NUMBER = '000000000000'"), ""),
        ("red", "Municipio Miami (recertificación)", _arcgis(layer["miami_recert"], "1=0"), ""),
        ("red", "Miami-Dade permisos", _arcgis(layer["mdc_permits"], "1=0"), ""),
        ("red", "Census geocoder", _census, ""),
        ("red", "NPI Registry", _npi, ""),
        ("red", "Overpass (OSM) + mirrors", _overpass, ""),
        ("red", "DNS / MX de emails", _dns_mx, ""),
        ("claves", "Google Places", _places if settings.google_places_api_key else None, "sin GOOGLE_PLACES_API_KEY"),
        ("claves", "Anthropic", _anthropic if settings.anthropic_api_key else None, "sin ANTHROPIC_API_KEY"),
        ("claves", "CRM /api/public/health", _crm if settings.crm_webhook_secret else None, "sin CRM_WEBHOOK_SECRET"),
    ]


def run_probe(group: str, name: str, probe: Callable[[], str] | None, why: str) -> Check:
    if probe is None:
        return Check(group, name, PENDING, why)
    t = time.monotonic()
    try:
        detail = probe()
        return Check(group, name, OK, detail, int((time.monotonic() - t) * 1000))
    except Exception as exc:                       # noqa: BLE001 — every failure is a diagnostic line
        return Check(group, name, ERROR, f"{exc.__class__.__name__}: {exc}"[:160], int((time.monotonic() - t) * 1000))


def run(network: bool = True) -> list[Check]:
    checks = config_checks()
    if network:
        probes = network_probes()
        with ThreadPoolExecutor(max_workers=len(probes)) as pool:
            checks += list(pool.map(lambda p: run_probe(*p), probes))
    return checks


def table(checks: list[Check]) -> str:
    w = max(len(c.name) for c in checks)
    lines, group = [], None
    for c in checks:
        if c.group != group:
            group = c.group
            lines.append(f"\n[{group}]")
        ms = f" ({c.ms} ms)" if c.ms is not None else ""
        lines.append(f"  {c.status:<9} {c.name:<{w}}  {c.detail}{ms}")
    n = {s: sum(c.status == s for c in checks) for s in (OK, PENDING, ERROR)}
    lines.append(f"\nOK {n[OK]} · PENDIENTE {n[PENDING]} · ERROR {n[ERROR]}")
    return "\n".join(lines)
