"""
Apollo.io decision-maker resolver.

Given a company domain, finds the highest-seniority decision-maker (Owner,
Founder, C-suite, VP, Director, Manager — in that order) and returns their
verified work email. This is the strategic fix for the "info@ black hole":
instead of scraping whatever generic address sits in a website footer, we go
straight to the person who can approve a $400 service.

Flow:
  1. People Search (mixed_people/search) — filter by domain + seniority,
     get back ranked candidates.
  2. People Enrichment (people/match) — Apollo masks emails in search results
     ("email_not_unlocked@domain.com"); match() spends one credit to reveal
     the real address.

Returns None on any failure (no key, network error, no decision-maker found,
masked email that won't unlock) so the enricher can fall back to scraping.
"""

import os

import requests
from loguru import logger

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
APOLLO_MATCH_URL  = "https://api.apollo.io/api/v1/people/match"

# Seniority levels Apollo recognises, ordered best → worst. We request all of
# them and then pick the most senior person available at the company.
SENIORITY_RANK: tuple[str, ...] = (
    "owner", "founder", "c_suite", "partner",
    "vp", "head", "director", "manager",
)
_RANK_INDEX = {level: i for i, level in enumerate(SENIORITY_RANK)}

# Apollo returns this sentinel when an email exists but the plan hasn't
# unlocked it. Never send to it.
_MASKED_EMAIL_MARKER = "email_not_unlocked"

REQUEST_TIMEOUT_S = 15


def _api_key() -> str | None:
    return os.getenv("APOLLO_API_KEY")


def _headers(key: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": key,
    }


def _seniority_sort_key(person: dict) -> int:
    """Lower is more senior. Unknown seniority sorts last."""
    return _RANK_INDEX.get((person.get("seniority") or "").lower(), len(SENIORITY_RANK))


def _search_candidates(domain: str, key: str) -> list[dict]:
    """Return decision-maker candidates at `domain`, most senior first."""
    payload = {
        "q_organization_domains_list": [domain],
        "person_seniorities": list(SENIORITY_RANK),
        "page": 1,
        "per_page": 10,
    }
    resp = requests.post(
        APOLLO_SEARCH_URL, json=payload, headers=_headers(key), timeout=REQUEST_TIMEOUT_S
    )
    resp.raise_for_status()
    people = resp.json().get("people", []) or []
    return sorted(people, key=_seniority_sort_key)


def _reveal_email(person_id: str, key: str) -> str | None:
    """Spend one enrichment credit to unlock a candidate's real email."""
    payload = {"id": person_id, "reveal_personal_emails": False}
    resp = requests.post(
        APOLLO_MATCH_URL, json=payload, headers=_headers(key), timeout=REQUEST_TIMEOUT_S
    )
    resp.raise_for_status()
    email = (resp.json().get("person") or {}).get("email")
    if not email or _MASKED_EMAIL_MARKER in email.lower():
        return None
    return email


def find_decision_maker(domain: str) -> tuple[str, str, str] | None:
    """
    Resolve the best decision-maker at `domain`.

    Returns (email, full_name, title) or None.
    """
    key = _api_key()
    if not key:
        return None

    try:
        candidates = _search_candidates(domain, key)
    except requests.RequestException as exc:
        logger.warning("Apollo search failed for {}: {}", domain, exc)
        return None

    if not candidates:
        logger.debug("Apollo: no decision-maker listed for {}", domain)
        return None

    for person in candidates:
        full_name = person.get("name") or " ".join(
            filter(None, [person.get("first_name"), person.get("last_name")])
        )
        title = person.get("title") or ""

        # Use the email already in the search result if it's real.
        email = person.get("email") or ""
        if email and _MASKED_EMAIL_MARKER not in email.lower():
            return email, full_name, title

        person_id = person.get("id")
        if not person_id:
            continue
        try:
            revealed = _reveal_email(person_id, key)
        except requests.RequestException as exc:
            logger.warning("Apollo match failed for {} at {}: {}", full_name, domain, exc)
            continue
        if revealed:
            return revealed, full_name, title

    logger.debug("Apollo: candidates found for {} but none had an unlockable email", domain)
    return None
