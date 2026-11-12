"""Email domain validation through DNS (MX, falling back to A/AAAA).

Statuses:
  mx        domain has MX records                                  -> verified
  a_only    no MX but the host resolves (RFC 5321 implicit MX)     -> dropped when EMAIL_REQUIRE_MX (default)
  no_mail   domain exists but has neither MX nor A                 -> dropped
  nxdomain  domain does not exist (typo / dead)                     -> dropped
  unchecked no working DNS at all / timeout                         -> kept (never drop on our own failure)
Results are cached per domain for the run.

Resolvers: the system resolver when it answers MX for a domain known to have it (MX_PROBE_DOMAIN,
default gmail.com). Some networks (sandboxes, captive DNS — this happened in production testing:
every MX query returned "no answer", gmail.com included) break MX lookups while A lookups work; then
the check goes through DNS-over-HTTPS (Google, then Cloudflare) instead of being switched off, so a
real "no MX" is still detected and a false one never drops a good email.
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache

import requests
from loguru import logger

try:  # dnspython is a declared dependency; the fallback keeps tests/dev working without it.
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover
    dns = None  # type: ignore[assignment]

DOH_URLS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")
RTYPE = {"MX": 15, "A": 1, "AAAA": 28}


def require_mx() -> bool:
    return os.getenv("EMAIL_REQUIRE_MX", "true").strip().lower() not in ("0", "false", "no")


def drop_statuses() -> set[str]:
    return {"no_mail", "nxdomain"} | ({"a_only"} if require_mx() else set())


DROP = {"no_mail", "nxdomain", "a_only"}       # with the default EMAIL_REQUIRE_MX=true (see drop_statuses)


class _NoDNS(Exception):
    pass


def _system(domain: str, rtype: str, timeout: float) -> tuple[str, list[str]]:
    """('ok'|'nxdomain'|'noanswer', records) via the system resolver; _NoDNS when it fails."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    try:
        ans = resolver.resolve(domain, rtype)
        return "ok", [str(getattr(r, "exchange", r)).rstrip(".") for r in ans]
    except dns.resolver.NXDOMAIN:
        return "nxdomain", []
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return "noanswer", []
    except dns.exception.DNSException as exc:
        raise _NoDNS(str(exc)) from exc


def _doh(domain: str, rtype: str, timeout: float) -> tuple[str, list[str]]:
    """Same answer through DNS-over-HTTPS (JSON API); _NoDNS when no provider answers."""
    last = ""
    for url in DOH_URLS:
        try:
            r = requests.get(url, params={"name": domain, "type": rtype}, timeout=timeout,
                             headers={"accept": "application/dns-json"})
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            last = str(exc)
            continue
        if data.get("Status") == 3:
            return "nxdomain", []
        if data.get("Status") != 0:
            last = f"status {data.get('Status')}"
            continue
        recs = [a.get("data", "").split()[-1].rstrip(".") for a in data.get("Answer") or []
                if a.get("type") == RTYPE[rtype]]
        return ("ok", recs) if recs else ("noanswer", [])
    raise _NoDNS(last or "DoH sin respuesta")


@lru_cache(maxsize=1)
def backend() -> str:
    """'system' | 'doh' | 'none': the first resolver that answers MX for a domain known to have MX."""
    probe = os.getenv("MX_PROBE_DOMAIN", "gmail.com")
    if dns is not None:
        try:
            if _system(probe, "MX", 4.0)[0] == "ok":
                return "system"
        except _NoDNS:
            pass
    try:
        if _doh(probe, "MX", 6.0)[0] == "ok":
            logger.info("DNS del sistema no responde MX; se verifica por DNS-over-HTTPS")
            return "doh"
    except _NoDNS:
        pass
    logger.warning("Sin DNS que responda MX: los emails quedan como 'unchecked' (no se descarta ninguno)")
    return "none"


def mx_supported() -> bool:
    return backend() != "none"


def _lookup(domain: str, rtype: str, timeout: float) -> tuple[str, list[str]]:
    b = backend()
    if b == "system":
        return _system(domain, rtype, timeout)
    if b == "doh":
        return _doh(domain, rtype, timeout + 2)
    raise _NoDNS("sin DNS")


@lru_cache(maxsize=2048)
def domain_status(domain: str, timeout: float = 4.0) -> str:
    domain = domain.strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return "nxdomain"
    if dns is None and backend() == "none":
        try:
            socket.getaddrinfo(domain, None)
            return "a_only"
        except socket.gaierror:
            return "unchecked"
    try:
        st, recs = _lookup(domain, "MX", timeout)
        if st == "nxdomain":
            return "nxdomain"
        if st == "ok" and any(recs):
            return "mx"
        for rtype in ("A", "AAAA"):
            st, recs = _lookup(domain, rtype, timeout)
            if st == "nxdomain":
                return "nxdomain"
            if st == "ok" and recs:
                return "a_only"
        return "no_mail"
    except _NoDNS as exc:
        logger.debug("DNS {}: {}", domain, exc)
    if dns is not None:                      # no MX-capable DNS: an A lookup still tells a dead domain apart
        try:
            if _system(domain, "A", timeout)[0] == "nxdomain":
                return "nxdomain"
        except _NoDNS:
            pass
    return "unchecked"


def email_status(email: str | None) -> str:
    if not email or "@" not in email:
        return "unchecked"
    return domain_status(email.rsplit("@", 1)[1])


def first_deliverable(emails: list[str]) -> tuple[str | None, str]:
    """First email (in preference order) whose domain is not dead; (None, last status) if all are."""
    last = "unchecked"
    for e in emails:
        st = email_status(e)
        if st not in drop_statuses():
            return e, st
        last = st
    return None, last
