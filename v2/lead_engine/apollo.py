"""Apollo.io people lookup by domain (port of v1 modules/apollo.py).

Differences with v1: we keep the person's name and title even when the email is
masked (v1 discarded the whole match), titles target property management roles
instead of generic seniority, and revealing an email (1 credit) is opt-in via
APOLLO_REVEAL_EMAILS. Transient errors return None so one Apollo hiccup never
blocks a lead from reaching the CRM.
"""

from __future__ import annotations

import time

import requests
from loguru import logger

from . import costs
from .config import settings
from .normalize import clean_email

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
MASKED = "email_not_unlocked"

TITLES = [
    "property manager", "community association manager", "community manager", "association manager",
    "general manager", "building manager", "portfolio manager", "director of operations",
    "president", "owner", "principal", "vice president",
]
_TITLE_RANK = {t: i for i, t in enumerate(TITLES)}


def _post(url: str, payload: dict) -> dict | None:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": settings.apollo_api_key or ""}
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After", "")
            time.sleep(int(ra) if ra.isdigit() else 2 ** attempt)
            continue
        if r.status_code >= 400:
            logger.debug("Apollo HTTP {}: {}", r.status_code, r.text[:200])
            return None
        return r.json()
    logger.warning("Apollo no disponible; se sigue sin decisor de Apollo")
    return None


def _rank(person: dict) -> int:
    title = (person.get("title") or "").lower()
    return min((i for t, i in _TITLE_RANK.items() if t in title), default=len(TITLES))


def find_contact(domain: str) -> dict | None:
    """{'name','title','email'|None,'linkedin'} for the best PM-type person at `domain`."""
    if not settings.apollo_api_key or not domain:
        return None
    costs.tracker.add("apollo_search")
    data = _post(SEARCH_URL, {
        "q_organization_domains_list": [domain],
        "person_titles": TITLES,
        "page": 1,
        "per_page": 10,
    })
    people = sorted((data or {}).get("people") or [], key=_rank)
    for p in people:
        name = p.get("name") or " ".join(filter(None, [p.get("first_name"), p.get("last_name")]))
        if not name:
            continue
        email = p.get("email") or ""
        if MASKED in email.lower():
            email = ""
        if not email and settings.apollo_reveal_emails and p.get("id"):
            costs.tracker.add("apollo_reveal")
            m = _post(MATCH_URL, {"id": p["id"], "reveal_personal_emails": False})
            email = ((m or {}).get("person") or {}).get("email") or ""
            if MASKED in email.lower():
                email = ""
        return {"name": name.strip(), "title": (p.get("title") or "").strip() or None,
                "email": clean_email(email), "linkedin": p.get("linkedin_url")}
    return None
