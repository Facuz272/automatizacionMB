"""External web search to discover the management company / CAM when the
building's own site doesn't say (or there is no site).

Only official search APIs are used (no scraping of search-engine result pages):
  serpapi     SERPAPI_KEY                       https://serpapi.com/search.json
  google_cse  GOOGLE_CSE_KEY + GOOGLE_CSE_CX    Google Custom Search JSON API
  brave       BRAVE_SEARCH_KEY                  Brave Search API
SEARCH_PROVIDER=auto picks the first one configured; none configured = step skipped.

``analyze`` is pure: it keeps only results that mention the building, then
extracts management companies (known South-Florida CAM firms, "managed by X"),
people with titles and emails from the snippets/pages. Attribution is
conservative: facts must appear in a result that also names the building.
"""

from __future__ import annotations

import re
from typing import Any

import requests
from loguru import logger

from . import costs
from .config import settings
from .dbpr import name_tokens
from .normalize import EMAIL_RE, clean_email, is_company_site
from .web_extract import MANAGED_BY_RE, PERSON_TITLE_RES, clean_person_name, crawl

# Large/common community-association management firms in South Florida (lowercase match → display name).
KNOWN_CAM_FIRMS = {
    "firstservice residential": "FirstService Residential", "first service residential": "FirstService Residential",
    "castle group": "Castle Group", "castle management": "Castle Group",
    "kw property management": "KW Property Management & Consulting", "associa": "Associa",
    "campbell property management": "Campbell Property Management", "allied property group": "Allied Property Group",
    "miami management": "Miami Management", "lang management": "Lang Management",
    "vesta property services": "Vesta Property Services", "sunrise property management": "Sunrise Property Management",
    "atlas property management": "Atlas Property Management", "exclusive property management": "Exclusive Property Management",
    "seacrest services": "Seacrest Services", "gables management": "Gables Management",
    "ambassador property management": "Ambassador Property Management", "grs management": "GRS Management",
    "sobe management": "SoBe Management", "keystone property management": "Keystone Property Management",
    "brickell property management": "Brickell Property Management", "premier association services": "Premier Association Services",
    "management & associates": "Management & Associates", "affinity management": "Affinity Management Services",
    "cardinal management": "Cardinal Management Group", "sentry management": "Sentry Management",
    "leland management": "Leland Management", "rizzetta": "Rizzetta & Company", "inframark": "Inframark",
    "triton property management": "Triton Property Management",
}
_KNOWN_RX = re.compile(r"(?i)\b(" + "|".join(re.escape(k) for k in sorted(KNOWN_CAM_FIRMS, key=len, reverse=True)) + r")\b")


def build_query(name: str, city: str | None) -> str:
    where = f' "{city}"' if city else ""
    return f'"{name}"{where} ("property manager" OR "LCAM" OR "managed by" OR "condominium association")'


def provider() -> str | None:
    want = settings.search_provider
    avail = {
        "serpapi": bool(settings.serpapi_key),
        "google_cse": bool(settings.google_cse_key and settings.google_cse_cx),
        "brave": bool(settings.brave_search_key),
    }
    if want in avail:
        return want if avail[want] else None
    if want == "off":
        return None
    return next((k for k, ok in avail.items() if ok), None)


def _get(url: str, **kw) -> dict | None:
    try:
        r = requests.get(url, timeout=20, **kw)
        if r.status_code == 200:
            return r.json()
        logger.warning("Búsqueda HTTP {}: {}", r.status_code, r.text[:200])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Búsqueda falló: {}", exc)
    return None


def web_search(query: str, n: int = 8) -> list[dict[str, str]]:
    prov = provider()
    if not prov:
        return []
    costs.tracker.add(prov)
    if prov == "serpapi":
        data = _get("https://serpapi.com/search.json",
                    params={"engine": "google", "q": query, "num": n, "api_key": settings.serpapi_key, "gl": "us", "hl": "en"})
        items = (data or {}).get("organic_results") or []
        return [{"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", "")} for i in items[:n]]
    if prov == "google_cse":
        data = _get("https://www.googleapis.com/customsearch/v1",
                    params={"key": settings.google_cse_key, "cx": settings.google_cse_cx, "q": query, "num": min(n, 10)})
        items = (data or {}).get("items") or []
        return [{"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", "")} for i in items[:n]]
    data = _get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": n},
                headers={"X-Subscription-Token": settings.brave_search_key or "", "Accept": "application/json"})
    items = ((data or {}).get("web") or {}).get("results") or []
    return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("description", "")} for i in items[:n]]


def mentions_building(text: str, name: str) -> bool:
    """All distinctive tokens of the building name appear in the text."""
    toks = name_tokens(name)
    up = re.findall(r"[A-Z0-9]+", text.upper())
    return bool(toks) and toks <= set(up)


def analyze(results: list[dict[str, str]], name: str, pages: dict[str, str] | None = None) -> dict[str, Any]:
    """Extract management companies / people / emails from results that name the building."""
    companies: dict[str, int] = {}
    people: list[dict[str, str]] = []
    emails: list[str] = []
    hits: list[dict[str, str]] = []
    for r in results:
        blob = f"{r.get('title', '')}\n{r.get('snippet', '')}\n{(pages or {}).get(r.get('url', ''), '')}"
        if not mentions_building(blob, name):
            continue
        hits.append({"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")})
        for m in _KNOWN_RX.finditer(blob):
            co = KNOWN_CAM_FIRMS[m.group(1).lower()]
            companies[co] = companies.get(co, 0) + 1
        for m in MANAGED_BY_RE.finditer(blob):
            co = m.group("co").strip(" ,.")
            if co and len(co) > 3:
                companies[co] = companies.get(co, 0) + 1
        for rx in PERSON_TITLE_RES:
            for m in rx.finditer(blob):
                nm = clean_person_name(m.group("name"))
                if not nm:
                    continue
                p = {"name": nm, "title": m.group("title").strip(), "url": r.get("url", "")}
                if p["name"] not in {x["name"] for x in people}:
                    people.append(p)
        for e in EMAIL_RE.findall(blob):
            ce = clean_email(e)
            if ce and ce not in emails:
                emails.append(ce)
    ranked = sorted(companies.items(), key=lambda kv: -kv[1])
    return {"hits": hits, "companies": ranked, "people": people, "emails": emails}


def discover(name: str, city: str | None, crawl_pages: int = 2) -> dict[str, Any]:
    """Search + crawl the top relevant non-social results; empty dict when no provider."""
    if not provider():
        return {}
    query = build_query(name, city)
    results = web_search(query)
    pages: dict[str, str] = {}
    for r in results:
        if len(pages) >= crawl_pages:
            break
        url = r.get("url", "")
        if is_company_site(url) and mentions_building(f"{r.get('title', '')} {r.get('snippet', '')}", name):
            facts = crawl(url, max_pages=2)
            pages[url] = facts.text[:4000]
    out = analyze(results, name, pages)
    out["query"] = query
    out["provider"] = provider()
    return out


def city_of(address: str | None) -> str | None:
    parts = [p.strip() for p in (address or "").split(",")]
    return parts[1] if len(parts) >= 3 else None
