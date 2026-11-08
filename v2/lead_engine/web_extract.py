"""Website extraction: emails (mailto + text), phones (tel:), JSON-LD, people with
titles and "managed by" mentions. ``parse_page`` is pure (tested); ``crawl``
does the HTTP part, visiting the homepage plus a few same-site pages whose link
text/URL looks like contact / about / team / management / board.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

from .config import settings
from .normalize import EMAIL_RE, PHONE_TEXT_RE, clean_address, clean_email, extract_domain, to_e164

LINK_HINTS = re.compile(
    r"(?i)contact|about|team|staff|management|manager|board|directors|office|leadership|our-people|who-we-are"
)
MAX_PAGES = 5
TEXT_LIMIT = 6000

TITLE_WORDS = (
    r"Property Manager|Community Association Manager|Community Manager|Association Manager|"
    r"General Manager|Building Manager|Site Manager|Portfolio Manager|LCAM|CAM|"
    r"Board President|President|Vice President|Treasurer|Secretary|Director of Operations|"
    r"Facilities Manager|Maintenance Manager|Chief Engineer|Owner|Principal|Managing Director"
)
_NAME = r"[A-Z][a-z'’\-]+(?: [A-Z]\.)?(?: [A-Z][a-z'’\-]+){1,2}"  # spaces only: never spans lines
PERSON_TITLE_RES = [
    re.compile(rf"(?P<name>{_NAME})\s*(?:,|–|—|-|\||\n)\s*(?P<title>{TITLE_WORDS})\b"),
    re.compile(rf"(?P<title>{TITLE_WORDS})[ \t]*(?::|–|—|-)[ \t]*(?P<name>{_NAME})"),
]
MANAGED_BY_RE = re.compile(
    r"(?i)(?:professionally\s+)?managed\s+by\s*:?\s+(?P<co>[A-Z][\w&.,'’\- ]{2,60}?)(?=[.,;\n]|\s{2}|$)"
)
_FALSE_NAMES = {"Contact Us", "Read More", "Learn More", "Privacy Policy", "Board Of", "Our Team"}


@dataclass
class SiteFacts:
    emails: dict[str, str] = field(default_factory=dict)      # email -> how found (mailto|text|jsonld)
    phones: list[str] = field(default_factory=list)            # E.164, tel: links first
    addresses: list[str] = field(default_factory=list)         # from JSON-LD PostalAddress
    people: list[tuple[str, str]] = field(default_factory=list)  # (name, title)
    managed_by: list[str] = field(default_factory=list)
    text: str = ""
    pages: list[str] = field(default_factory=list)

    def merge(self, other: "SiteFacts") -> None:
        for e, how in other.emails.items():
            self.emails.setdefault(e, how)
        for lst, src in ((self.phones, other.phones), (self.addresses, other.addresses),
                         (self.people, other.people), (self.managed_by, other.managed_by)):
            for x in src:
                if x not in lst:
                    lst.append(x)
        if other.text and len(self.text) < TEXT_LIMIT:
            self.text = (self.text + "\n" + other.text)[:TEXT_LIMIT]
        self.pages.extend(other.pages)


def _jsonld_nodes(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)


def _postal(addr: dict) -> str:
    parts = [addr.get("streetAddress"), addr.get("addressLocality")]
    tail = " ".join(x for x in (addr.get("addressRegion"), addr.get("postalCode")) if x)
    return clean_address(", ".join(x for x in (*parts, tail) if x))


def parse_page(html: str, url: str = "") -> SiteFacts:
    facts = SiteFacts(pages=[url] if url else [])
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            e = clean_email(href)
            if e:
                facts.emails.setdefault(e, "mailto")
        elif href.lower().startswith("tel:"):
            p = to_e164(href[4:])
            if p and p not in facts.phones:
                facts.phones.append(p)

    for node in _jsonld_nodes(soup):
        tel = node.get("telephone")
        if isinstance(tel, str) and (p := to_e164(tel)) and p not in facts.phones:
            facts.phones.append(p)
        em = node.get("email")
        if isinstance(em, str) and (e := clean_email(em)):
            facts.emails.setdefault(e, "jsonld")
        addr = node.get("address")
        if isinstance(addr, dict) and (s := _postal(addr)) and s not in facts.addresses:
            facts.addresses.append(s)
        if node.get("@type") == "Person" and node.get("name") and node.get("jobTitle"):
            facts.people.append((str(node["name"]).strip(), str(node["jobTitle"]).strip()))

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [" ".join(l.split()) for l in text.splitlines()]
    text = "\n".join(l for l in lines if l)
    facts.text = text[:TEXT_LIMIT]

    for m in EMAIL_RE.findall(text):
        e = clean_email(m)
        if e:
            facts.emails.setdefault(e, "text")
    for m in PHONE_TEXT_RE.findall(text):
        p = to_e164(m)
        if p and p not in facts.phones:
            facts.phones.append(p)
    for rx in PERSON_TITLE_RES:
        for m in rx.finditer(text):
            name, title = m.group("name").strip(), m.group("title").strip()
            if name in _FALSE_NAMES or name.split()[0] in ("The", "Our", "Your", "Contact"):
                continue
            if (name, title) not in facts.people:
                facts.people.append((name, title))
    for m in MANAGED_BY_RE.finditer(text):
        co = m.group("co").strip(" ,.")
        if co and co not in facts.managed_by:
            facts.managed_by.append(co)
    return facts


def _candidate_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    host = extract_domain(base)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"].split("#")[0])
        if not href.startswith("http") or extract_domain(href) != host:
            continue
        label = f"{a.get_text(' ', strip=True)} {urlparse(href).path}"
        if LINK_HINTS.search(label) and href not in out and href.rstrip("/") != base.rstrip("/"):
            out.append(href)
    return out


def _get(session: requests.Session, url: str) -> str | None:
    try:
        r = session.get(url, timeout=settings.http_timeout_s, allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and "html" in ctype:
            return r.text[:800_000]
    except requests.RequestException as exc:
        logger.debug("web {}: {}", url, exc)
    return None


def crawl(website: str, max_pages: int = MAX_PAGES) -> SiteFacts:
    if not website.startswith("http"):
        website = "https://" + website
    session = requests.Session()
    session.headers["User-Agent"] = settings.user_agent
    facts = SiteFacts()
    home = _get(session, website)
    if home is None:
        return facts
    facts.merge(parse_page(home, website))
    queue = _candidate_links(home, website)
    # Common paths even when the menu is rendered by JS.
    for path in ("/contact", "/contact-us", "/about", "/management", "/board"):
        u = urljoin(website, path)
        if u not in queue:
            queue.append(u)
    for url in queue[: max_pages - 1]:
        time.sleep(0.5)
        html = _get(session, url)
        if html:
            facts.merge(parse_page(html, url))
    return facts
