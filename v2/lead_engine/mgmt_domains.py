"""Corporate domains of Florida community-association management firms.

When the building has no website/email but we know its management firm (DBPR care-of line,
"managed by" on a site, search), the setter still gets a verified corporate channel:
  1. curated catalog below (firms we can name with certainty; all resolved in DNS on 2026-11-08),
     extendable with MGMT_DOMAINS_FILE (JSON {"Firm name": "domain.com"}) without code changes;
  2. otherwise, with a search API key, the first official result for the firm;
then the domain's mail records are checked (email_verify) and, optionally, the firm's contact
page is read for a corporate inbox on that same domain. Personal addresses are never guessed.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from loguru import logger

from . import email_verify
from .names import GENERIC, compact_key, firm_key, firm_match_score

_GENERIC_TAIL = sorted(GENERIC | {"CONSULTING", "SERVICE", "SERVICES", "GROUP", "FLORIDA", "FL", "SOUTH", "OF"}, key=len, reverse=True)


def _only_generic(rest: str) -> bool:
    """True when `rest` is a concatenation of generic words ('FLORIDA', 'CONSULTING', 'GROUPOFFLORIDA')."""
    if not rest:
        return True
    return any(rest.startswith(w) and _only_generic(rest[len(w):]) for w in _GENERIC_TAIL)
from .normalize import extract_domain, is_company_site

CATALOG = {
    "FirstService Residential": "fsresidential.com",
    "Castle Group": "castlegroup.com",
    "Castle Management": "castlegroup.com",
    "KW Property Management & Consulting": "kwpmc.com",
    "AKAM": "akam.com",
    "Campbell Property Management": "campbellproperty.com",
    "RealManage": "realmanage.com",
    "Associa": "associaonline.com",
    "The Continental Group": "thecontinentalgroup.com",
    "Miami Management": "miamimanagement.com",
    "Seacrest Services": "seacrestservices.com",
    "Leland Management": "lelandmanagement.com",
    "Sentry Management": "sentrymgt.com",
    "Vesta Property Services": "vestapropertyservices.com",
}


@lru_cache(maxsize=1)
def catalog() -> dict[str, str]:
    out = dict(CATALOG)
    path = os.getenv("MGMT_DOMAINS_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                out.update({k: v.strip().lower() for k, v in json.load(fh).items() if isinstance(v, str)})
        except (OSError, ValueError) as exc:
            logger.warning("MGMT_DOMAINS_FILE ilegible: {}", exc)
    return out


def from_catalog(firm: str | None) -> tuple[str, str] | None:
    """(catalog firm name, domain) for a firm name in any spelling."""
    if not firm:
        return None
    key, ck = firm_key(firm), compact_key(firm)
    best: tuple[float, str, str] | None = None
    kc = key.replace(" ", "")                      # abbreviations expanded: KW PROPERTY MGMT -> KWPROPERTYMANAGEMENT
    for name, domain in catalog().items():
        cn, nk = compact_key(name), firm_key(name).replace(" ", "")
        if cn == ck or nk == kc:
            return name, domain
        # "FIRSTSERVICERESIDENTIAL" ⊂ "FIRSTSERVICERESIDENTIALFLORIDA"; "KWPROPERTYMANAGEMENT" ⊂ "…CONSULTING"
        for a, b in ((cn, ck), (nk, kc)):
            short, long_ = sorted((a, b), key=len)
            if len(short) >= 10 and long_.startswith(short) and _only_generic(long_[len(short):]):
                return name, domain
        m = firm_match_score(key, firm_key(name))
        if m and m[1] != "same_word" and (best is None or m[0] > best[0]):
            best = (m[0], name, domain)
    if best:
        return best[1], best[2]
    # Short catalog names ("AKAM", "Associa") must appear as a whole word.
    words = set(key.split())
    for name, domain in catalog().items():
        nk = firm_key(name).split()
        if len(nk) == 1 and nk[0] in words and len(nk[0]) >= 4:
            return name, domain
    return None


def from_search(firm: str) -> str | None:
    from . import search

    if not search.provider():
        return None
    distinct = [t for t in firm_key(firm).split() if len(t) > 2][:2]
    for r in search.web_search(f'"{firm}" community association management Florida', n=6):
        url = r.get("url", "")
        title = (r.get("title") or "").upper()
        if is_company_site(url) and distinct and all(t in title.replace(" ", "") or t in title for t in distinct):
            return extract_domain(url)
    return None


def corporate_email(domain: str) -> str | None:
    """Best inbox on the firm's own domain from its homepage/contact page (never guessed)."""
    from .normalize import best_email
    from .web_extract import crawl

    facts = crawl(f"https://{domain}", max_pages=2)
    own = [e for e in facts.emails if e.endswith("@" + domain) or e.endswith("." + domain)]
    return best_email(own, domain) if own else None


def resolve(firm: str | None, *, use_search: bool = True, fetch_email: bool = True,
            check_mx: bool = True) -> dict[str, Any] | None:
    if not firm:
        return None
    hit = from_catalog(firm)
    source = "catalog"
    domain = hit[1] if hit else None
    if not domain and use_search:
        domain, source = from_search(firm), "search"
    if not domain:
        return None
    status = email_verify.domain_status(domain) if check_mx else "unchecked"
    if status in email_verify.DROP:
        return None
    out: dict[str, Any] = {"domain": domain, "status": status, "source": source, "firm": hit[0] if hit else firm}
    if fetch_email:
        e = corporate_email(domain)
        if e:
            out["email"] = e
    return out
