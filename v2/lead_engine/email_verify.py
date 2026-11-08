"""Email domain validation through DNS (MX, falling back to A/AAAA).

Statuses:
  mx        domain has MX records                      -> verified
  a_only    no MX but the host resolves (RFC 5321 implicit MX) -> kept, not "verified"
  no_mail   domain exists but has neither MX nor A       -> dropped
  nxdomain  domain does not exist (typo / dead)         -> dropped
  unchecked DNS unavailable / timeout                   -> kept (never drop on our own failure)
Results are cached per domain for the run.

Resolver sanity probe: some networks (sandboxes, captive DNS) answer A queries but
return empty MX answers for everything. Before trusting "no MX", we check that a
domain known to have MX (MX_PROBE_DOMAIN, default gmail.com) resolves; if it does
not, MX verification is reported as unavailable and statuses become "unchecked"
(nxdomain is still detected and dropped).
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from loguru import logger

try:  # dnspython is a declared dependency; the fallback keeps tests/dev working without it.
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover
    dns = None  # type: ignore[assignment]

DROP = {"no_mail", "nxdomain"}


@lru_cache(maxsize=1)
def mx_supported() -> bool:
    if dns is None:
        return False
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4.0
    try:
        return bool(resolver.resolve(os.getenv("MX_PROBE_DOMAIN", "gmail.com"), "MX"))
    except dns.exception.DNSException:
        logger.warning("El DNS de este entorno no responde MX: los emails quedan como 'unchecked' (solo se descartan dominios inexistentes)")
        return False


@lru_cache(maxsize=2048)
def domain_status(domain: str, timeout: float = 4.0) -> str:
    domain = domain.strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return "nxdomain"
    if dns is None:
        try:
            socket.getaddrinfo(domain, None)
            return "a_only"
        except socket.gaierror:
            return "unchecked"
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    try:
        answers = resolver.resolve(domain, "MX")
        if any(str(r.exchange).rstrip(".") for r in answers):
            return "mx"
    except dns.resolver.NXDOMAIN:
        return "nxdomain"
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        if not mx_supported():
            # Can't trust "no MX" here; an A lookup still tells a dead domain apart.
            try:
                resolver.resolve(domain, "A")
            except dns.resolver.NXDOMAIN:
                return "nxdomain"
            except dns.exception.DNSException:
                pass
            return "unchecked"
    except dns.exception.DNSException as exc:
        logger.debug("DNS MX {}: {}", domain, exc)
        return "unchecked"
    for rtype in ("A", "AAAA"):
        try:
            if resolver.resolve(domain, rtype):
                return "a_only"
        except dns.resolver.NXDOMAIN:
            return "nxdomain"
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            continue
        except dns.exception.DNSException:
            return "unchecked"
    return "no_mail"


def email_status(email: str | None) -> str:
    if not email or "@" not in email:
        return "unchecked"
    return domain_status(email.rsplit("@", 1)[1])


def first_deliverable(emails: list[str]) -> tuple[str | None, str]:
    """First email (in preference order) whose domain is not dead; (None, last status) if all are."""
    last = "unchecked"
    for e in emails:
        st = email_status(e)
        if st not in DROP:
            return e, st
        last = st
    return None, last
