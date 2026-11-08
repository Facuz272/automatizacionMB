"""Website extraction.

``parse_page`` (pure, tested) reads one HTML document:
  * emails from mailto:, visible text, JSON-LD and hydrated JSON state
  * phones with a contextual label (management / hoa / front_desk / leasing /
    maintenance / fax / unlabeled) taken from the link text, the surrounding
    text, JSON-LD contactPoint.contactType or the JSON key next to it
  * people with titles, "managed by X" mentions, JSON-LD postal addresses
  * hydrated state of JS frameworks (__NEXT_DATA__, application/json scripts,
    window.__NUXT__/__INITIAL_STATE__ = {...}) so SPA sites are not "empty"
``crawl`` visits the homepage plus a few same-site contact/about/management
pages, and re-renders with Playwright (render.py) when the static HTML looks
like an unrendered single-page app.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

from . import render
from .config import settings
from .normalize import EMAIL_RE, PHONE_TEXT_RE, clean_address, clean_email, extract_domain, to_e164

LINK_HINTS = re.compile(
    r"(?i)contact|about|team|staff|management|manager|board|directors|office|leadership|our-people|who-we-are|amenities"
)
MAX_PAGES = 5
TEXT_LIMIT = 8000

TITLE_WORDS = (
    r"Assistant Property Manager|Assistant Manager|Property Manager|Community Association Manager|Community Manager|"
    r"Association Manager|General Manager|Building Manager|Site Manager|Portfolio Manager|LCAM|CAM|"
    r"Board President|President|Vice President|Treasurer|Secretary|Director of Operations|"
    r"Chief Engineer|Director of Facilities|Facilities Director|Facilities Manager|Facility Manager|Building Engineer|"
    r"Maintenance Supervisor|Maintenance Director|Maintenance Manager|Director of Maintenance|Superintendent|"
    r"Office Manager|Practice Manager|Administrator|Owner|Principal|Managing Director"
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

# ── phone labels ────────────────────────────────────────────────────────────
PHONE_LABEL_RES: list[tuple[str, re.Pattern[str]]] = [
    ("fax", re.compile(r"(?i)\bfax\b|\bfacs[ií]mil")),
    ("management", re.compile(
        r"(?i)management office|manager'?s office|property manager|community manager|building manager|"
        r"association manager|general manager|\bl?cam\b|\bmanagement\b|\bmanager\b|administraci[oó]n|administrador|"
        r"oficina de (?:la )?administraci[oó]n|admin(?:istration)? office")),
    ("hoa", re.compile(r"(?i)\bhoa\b|\bassociation\b|\bboard\b|asociaci[oó]n|\bjunta\b")),
    ("maintenance", re.compile(r"(?i)maintenance|emergenc|after[- ]hours|mantenimiento")),
    ("front_desk", re.compile(r"(?i)front desk|concierge|lobby|security|valet|guard|reception|doorman|recepci[oó]n|seguridad|porter[ií]a")),
    ("leasing", re.compile(r"(?i)leasing|sales|rentals?|ventas|alquiler|renting")),
]
LABEL_PRIORITY = {"management": 0, "hoa": 1, "google_main": 2, "unlabeled": 3, "maintenance": 4,
                  "front_desk": 5, "leasing": 6, "firm_central": 7, "fax": 9}
GENERIC_LABELS = {"unlabeled", "google_main"}


def better_label(existing: str, new: str) -> bool:
    """Should `new` replace `existing` for the same number? Specific beats generic; among specific, priority."""
    if new in GENERIC_LABELS:
        return existing == "unlabeled" and new == "google_main"
    if existing in GENERIC_LABELS:
        return True
    return LABEL_PRIORITY[new] < LABEL_PRIORITY[existing]


_LEADING_NOISE = re.compile(r"^(?:Contact|Call|Email|Meet|Our|Your|The|Mr|Mrs|Ms|Dr|Please|Ask|For)\.?\s+")


def clean_person_name(name: str) -> str | None:
    """'Contact Maria Lopez' -> 'Maria Lopez'; None when fewer than two words remain."""
    n = name.strip()
    while _LEADING_NOISE.match(n):
        n = _LEADING_NOISE.sub("", n, count=1)
    return n if len(n.split()) >= 2 and n not in _FALSE_NAMES else None


def label_phone(context: str) -> str:
    """Closest label wins: the regex whose match ends nearest to the end of the context (the number)."""
    best, best_pos = "unlabeled", -1
    for label, rx in PHONE_LABEL_RES:
        for m in rx.finditer(context or ""):
            if m.end() > best_pos or (m.end() == best_pos and LABEL_PRIORITY[label] < LABEL_PRIORITY[best]):
                best, best_pos = label, m.end()
    return best


@dataclass
class SiteFacts:
    emails: dict[str, str] = field(default_factory=dict)        # email -> how found
    phones: list[dict[str, Any]] = field(default_factory=list)   # {number,label,context,source}
    addresses: list[str] = field(default_factory=list)           # from JSON-LD PostalAddress
    people: list[tuple[str, str]] = field(default_factory=list)  # (name, title)
    managed_by: list[str] = field(default_factory=list)
    text: str = ""
    pages: list[str] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)            # pages re-rendered with Playwright
    embedded_json: bool = False
    office_hours: str | None = None

    @property
    def phone_numbers(self) -> list[str]:
        return [p["number"] for p in self.phones]

    def add_phone(self, number: str | None, label: str, context: str, source: str) -> None:
        if not number:
            return
        for p in self.phones:
            if p["number"] == number:
                if better_label(p["label"], label):
                    p.update(label=label, context=" ".join(context.split())[-100:], source=source)
                return
        self.phones.append({"number": number, "label": label, "context": " ".join(context.split())[-100:], "source": source})

    def best_phone(self) -> dict[str, Any] | None:
        usable = [p for p in self.phones if p["label"] != "fax"]
        return sorted(usable, key=lambda p: LABEL_PRIORITY[p["label"]])[0] if usable else None

    def merge(self, other: "SiteFacts") -> None:
        for e, how in other.emails.items():
            self.emails.setdefault(e, how)
        for p in other.phones:
            self.add_phone(p["number"], p["label"], p["context"], p["source"])
        for lst, src in ((self.addresses, other.addresses), (self.people, other.people),
                         (self.managed_by, other.managed_by)):
            for x in src:
                if x not in lst:
                    lst.append(x)
        if other.text and len(self.text) < TEXT_LIMIT:
            self.text = (self.text + "\n" + other.text)[:TEXT_LIMIT]
        self.pages.extend(other.pages)
        self.rendered.extend(other.rendered)
        self.office_hours = self.office_hours or other.office_hours
        self.embedded_json = self.embedded_json or other.embedded_json


# ── JSON helpers ────────────────────────────────────────────────────────────

def _walk(node: Any) -> Iterator[dict]:
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            stack.extend(n)


def _strings(node: Any, limit: int = 4000) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack and len(out) < limit:
        n = stack.pop()
        if isinstance(n, str):
            s = n.strip()
            if 2 < len(s) < 600 and not s.startswith(("http", "/", "{", "data:")):
                out.append(s)
        elif isinstance(n, dict):
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)
    return out


def _jsonld_nodes(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        yield from _walk(data)


_STATE_ASSIGN = re.compile(r"window\.(?:__NUXT__|__INITIAL_STATE__|__APOLLO_STATE__|__PRELOADED_STATE__)\s*=\s*(\{.*\})\s*;?\s*$", re.S)


def embedded_states(soup: BeautifulSoup) -> list[Any]:
    """Parsed hydration payloads: __NEXT_DATA__, application/json scripts, window.__X__ = {...}."""
    out: list[Any] = []
    for tag in soup.find_all("script"):
        body = tag.string or ""
        if not body.strip():
            continue
        typ = (tag.get("type") or "").lower()
        if tag.get("id") == "__NEXT_DATA__" or typ == "application/json":
            try:
                out.append(json.loads(body))
            except ValueError:
                pass
        elif not typ or "javascript" in typ:
            m = _STATE_ASSIGN.search(body.strip())
            if m:
                try:
                    out.append(json.loads(m.group(1)))
                except ValueError:
                    pass
    return out


_PERSON_KEYS = ("name", "fullName", "full_name", "displayName")
_TITLE_KEYS = ("title", "jobTitle", "job_title", "position", "role")
_PHONE_KEYS = ("phone", "telephone", "phoneNumber", "phone_number", "tel", "mobile")
_PHONE_LABEL_KEYS = ("label", "type", "contactType", "department", "name", "title")


def _from_state(state: Any, facts: SiteFacts) -> None:
    for node in _walk(state):
        name = next((node[k] for k in _PERSON_KEYS if isinstance(node.get(k), str)), None)
        title = next((node[k] for k in _TITLE_KEYS if isinstance(node.get(k), str)), None)
        if name and title and re.fullmatch(_NAME, name.strip()) and re.search(rf"(?i){TITLE_WORDS}", title):
            pair = (name.strip(), title.strip())
            if pair not in facts.people:
                facts.people.append(pair)
        for k in _PHONE_KEYS:
            if isinstance(node.get(k), str):
                ctx = " ".join(str(node[x]) for x in _PHONE_LABEL_KEYS if isinstance(node.get(x), str) and x != k)
                facts.add_phone(to_e164(node[k]), label_phone(ctx), ctx, "embedded_json")
        em = node.get("email")
        if isinstance(em, str) and (e := clean_email(em)):
            facts.emails.setdefault(e, "embedded_json")


def _postal(addr: dict) -> str:
    parts = [addr.get("streetAddress"), addr.get("addressLocality")]
    tail = " ".join(x for x in (addr.get("addressRegion"), addr.get("postalCode")) if x)
    return clean_address(", ".join(x for x in (*parts, tail) if x))


def _link_context(a) -> str:
    """Text just before a tel: link (previous siblings + parent text up to the link) + the link text."""
    own = a.get_text(" ", strip=True)
    parent = a.parent
    before = ""
    if parent is not None:
        chunks = []
        for sib in a.previous_siblings:
            t = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
            chunks.insert(0, t)
            if sum(len(c) for c in chunks) > 120:
                break
        before = " ".join(chunks)
        if not before.strip() and parent.parent is not None:
            prev = parent.find_previous_sibling()
            before = prev.get_text(" ", strip=True) if prev is not None else ""
    return f"{before[-120:]} {own}".strip()


# ── page parsing (pure) ─────────────────────────────────────────────────────

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
            ctx = _link_context(a)
            facts.add_phone(to_e164(href[4:]), label_phone(ctx), ctx, "tel_link")

    jsonld_hours: list[str] = []
    for node in _jsonld_nodes(soup):
        oh = node.get("openingHours")
        if isinstance(oh, str):
            jsonld_hours.append(oh)
        elif isinstance(oh, list):
            jsonld_hours += [x for x in oh if isinstance(x, str)]
        tel = node.get("telephone")
        if isinstance(tel, str):
            ctx = " ".join(str(node.get(k, "")) for k in ("contactType", "name", "@type"))
            lbl = label_phone(ctx) if node.get("contactType") else "unlabeled"
            facts.add_phone(to_e164(tel), lbl, ctx, "jsonld")
        em = node.get("email")
        if isinstance(em, str) and (e := clean_email(em)):
            facts.emails.setdefault(e, "jsonld")
        addr = node.get("address")
        if isinstance(addr, dict) and (s := _postal(addr)) and s not in facts.addresses:
            facts.addresses.append(s)
        if node.get("@type") == "Person" and node.get("name") and node.get("jobTitle"):
            facts.people.append((str(node["name"]).strip(), str(node["jobTitle"]).strip()))

    states = embedded_states(soup)
    state_text = ""
    for st in states:
        facts.embedded_json = True
        _from_state(st, facts)
        state_text += "\n".join(_strings(st)) + "\n"

    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [" ".join(l.split()) for l in text.splitlines()]
    text = "\n".join(l for l in lines if l)
    if state_text and len(text) < 600:
        text = (text + "\n" + state_text).strip()
    facts.text = text[:TEXT_LIMIT]

    for m in EMAIL_RE.findall(text):
        e = clean_email(m)
        if e:
            facts.emails.setdefault(e, "text")
    for m in PHONE_TEXT_RE.finditer(text):
        line_start = text.rfind("\n", 0, max(0, text.rfind("\n", 0, m.start())))
        ctx = text[max(line_start, m.start() - 90) : m.start()]
        facts.add_phone(to_e164(m.group(0)), label_phone(ctx), ctx, "text")
    for rx in PERSON_TITLE_RES:
        for m in rx.finditer(text):
            name, title = clean_person_name(m.group("name")), m.group("title").strip()
            if name and (name, title) not in facts.people:
                facts.people.append((name, title))
    for m in MANAGED_BY_RE.finditer(text):
        co = m.group("co").strip(" ,.")
        if co and co not in facts.managed_by:
            facts.managed_by.append(co)
    from .roles import office_hours

    facts.office_hours = office_hours(text.replace("\n", " "), jsonld_hours)
    return facts


def looks_like_spa(html: str, facts: SiteFacts) -> bool:
    """Static HTML that is mostly a JS shell: little text, a mount point, several scripts."""
    if len(facts.text) >= 400:
        return False
    mount = re.search(r'id=["\'](?:root|app|__next|__nuxt|svelte|main-app)["\']', html)
    scripts = len(re.findall(r"<script\b", html, re.I))
    return bool(mount) or scripts >= 4


# ── crawling ────────────────────────────────────────────────────────────────

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


def fetch_and_parse(session: requests.Session, url: str) -> tuple[str | None, SiteFacts | None]:
    html = _get(session, url)
    if html is None:
        return None, None
    facts = parse_page(html, url)
    if render.enabled() and looks_like_spa(html, facts):
        rendered = render.render(url)
        if rendered:
            rfacts = parse_page(rendered, url)
            rfacts.rendered.append(url)
            rfacts.merge(facts)
            return rendered, rfacts
    return html, facts


def crawl(website: str, max_pages: int = MAX_PAGES) -> SiteFacts:
    if not website.startswith("http"):
        website = "https://" + website
    session = requests.Session()
    session.headers["User-Agent"] = settings.user_agent
    facts = SiteFacts()
    home, home_facts = fetch_and_parse(session, website)
    if home is None or home_facts is None:
        return facts
    facts.merge(home_facts)
    queue = _candidate_links(home, website)
    # Common paths even when the menu is rendered by JS.
    for path in ("/contact", "/contact-us", "/about", "/management", "/board", "/amenities"):
        u = urljoin(website, path)
        if u not in queue:
            queue.append(u)
    for url in queue[: max_pages - 1]:
        time.sleep(0.5)
        _, page_facts = fetch_and_parse(session, url)
        if page_facts:
            facts.merge(page_facts)
    return facts
