"""Pure normalisation and validation helpers (no network). Tested in tests/test_normalize.py.

These fix the v1 bugs:
  * extract_domain used ``str.lstrip("www.")`` which strips any leading w/./ chars
    ("web.com" -> "eb.com", "www.wwwidgets.com" -> "idgets.com").
  * phones were stored in whatever format Google returned; the CRM matches on
    the last 10 digits, so everything is normalised to E.164 (+1XXXXXXXXXX).
  * addresses were never checked; a lead only passes with a street number.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ── Domains ─────────────────────────────────────────────────────────────────

# Hosts that are not the company's own site (social, link-in-bio, directories).
NON_COMPANY_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "yelp.com",
    "google.com", "goo.gl", "linktr.ee", "business.site", "sites.google.com", "wixsite.com",
    "nextdoor.com", "bbb.org", "zillow.com", "apartments.com", "realtor.com",
)


def extract_domain(url: str | None) -> str:
    """Registered host without a leading ``www.`` prefix ('' if unparsable)."""
    if not url:
        return ""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def is_company_site(url: str | None) -> bool:
    domain = extract_domain(url)
    return bool(domain) and not any(domain == h or domain.endswith("." + h) for h in NON_COMPANY_HOSTS)


# ── Phones ──────────────────────────────────────────────────────────────────

# NANP: area code and exchange cannot start with 0/1; 555-01XX is fictional.
_NANP = re.compile(r"^[2-9]\d{2}[2-9]\d{6}$")


def to_e164(raw: str | None) -> str | None:
    """US/CA number -> '+1XXXXXXXXXX', or None. Extensions ('x123', 'ext. 4') are dropped."""
    if not raw:
        return None
    main = re.split(r"(?i)\s*(?:ext\.?|x|#)\s*\d+\s*$", raw.strip())[0]
    digits = re.sub(r"\D", "", main)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or not _NANP.match(digits):
        return None
    if digits[3:6] == "555" and digits[6:8] == "01":
        return None
    return "+1" + digits


def phone_key(phone: str) -> str:
    """Same rule as the CRM (src/lib/integrations.ts phoneKey): last 10 digits."""
    return re.sub(r"\D", "", phone)[-10:]


PHONE_TEXT_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}\b")


# ── Addresses ───────────────────────────────────────────────────────────────

_STREET_NUMBER = re.compile(r"^\s*\d{1,6}[A-Za-z]?(?:-\d{1,5})?\s+\S+")
_PO_BOX = re.compile(r"(?i)\b(?:p\.?\s*o\.?\s*box|post office box)\b")
_STATE_ZIP = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")


def clean_address(raw: str | None) -> str:
    if not raw:
        return ""
    addr = " ".join(raw.replace("\n", ", ").split())
    addr = re.sub(r",\s*(USA|United States)$", "", addr)
    return addr.strip(" ,")


def address_problem(addr: str | None, state: str | None = None) -> str | None:
    """Why an address is not a real street address, or None when it is usable.

    ``state`` comes from settings.target_state (TARGET_STATE); None/'' skips the state check.
    """
    a = clean_address(addr)
    if not a:
        return "sin dirección"
    if "@" in a:
        return "dirección contiene @"
    if _PO_BOX.search(a):
        return "dirección es un P.O. Box"
    if not _STREET_NUMBER.match(a):
        return "dirección sin número de calle"
    if len(a) < 12 or "," not in a:
        return "dirección incompleta"
    if state:
        m = _STATE_ZIP.search(a)
        if m and m.group(1) != state:
            return f"dirección fuera de {state}"
    return None


# ── Emails ──────────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
_BAD_EMAIL_TLDS = ("png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js")
NEVER_SEND = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "mailer-daemon",
    "abuse", "hostmaster", "webmaster", "privacy", "unsubscribe",
})
# Role inboxes are fine for this CRM (setters email after a call), ranked best -> worst.
ROLE_RANK = ("manager", "management", "office", "admin", "info", "contact", "frontdesk", "board", "hello")
_PLACEHOLDER_DOMAINS = ("example.com", "domain.com", "email.com", "yourdomain.com", "sentry.io", "wixpress.com")


def clean_email(raw: str | None) -> str | None:
    if not raw:
        return None
    e = raw.strip().strip(".,;:<>()[]\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:].split("?")[0]
    if not EMAIL_RE.fullmatch(e) or len(e) > 200:
        return None
    local, domain = e.rsplit("@", 1)
    if domain.rsplit(".", 1)[-1] in _BAD_EMAIL_TLDS or domain in _PLACEHOLDER_DOMAINS:
        return None
    if local.split("+", 1)[0] in NEVER_SEND:
        return None
    return e


def rank_email(email: str, site_domain: str = "", person_name: str = "") -> tuple[int, int, str]:
    """Sort key (lower = better): personal on company domain > role on company domain > other."""
    local, domain = email.split("@", 1)
    base = local.split("+", 1)[0]
    on_site = 0 if site_domain and (domain == site_domain or domain.endswith("." + site_domain)) else 1
    tokens = re.split(r"[._-]", base)
    if person_name and any(t and t in person_name.lower() for t in tokens if len(t) > 2):
        kind = 0
    elif base in ROLE_RANK or tokens[0] in ROLE_RANK:
        kind = 2 + (ROLE_RANK.index(base) if base in ROLE_RANK else ROLE_RANK.index(tokens[0]))
    else:
        kind = 1  # looks personal (jdoe@)
    return (on_site, kind, email)


def best_email(emails: set[str] | list[str], site_domain: str = "", person_name: str = "") -> str | None:
    cleaned = {c for c in (clean_email(e) for e in emails) if c}
    if not cleaned:
        return None
    return sorted(cleaned, key=lambda e: rank_email(e, site_domain, person_name))[0]


def clip(text: str | None, n: int) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"
