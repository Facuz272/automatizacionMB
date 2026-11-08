"""Decision-maker roles and office hours (pure).

Roles:
  pm          Property / Community / Association / General / Building Manager, LCAM/CAM, Administrator
  assistant   Assistant (Property) Manager, Office Manager, Administrative Assistant
  facilities  Chief Engineer, Director of Facilities, Facilities / Maintenance Manager or Supervisor,
              Building Engineer, Superintendent — the ones who usually request pressure-washing quotes
  board       Board President / VP / Treasurer / Secretary / Director
  exec        President, CEO, Owner, Principal, COO (management firms, clinics)
  other
"""

from __future__ import annotations

import re

ROLE_RX = [
    ("assistant", re.compile(r"(?i)assistant (?:property |community |building )?manager|office manager|admin(?:istrative)? assistant|"
                             r"asistente")),
    ("facilities", re.compile(r"(?i)chief engineer|building engineer|facilit|maintenance|superintendent|plant operations|"
                              r"director of operations|operations manager|ingenier|mantenimiento")),
    ("pm", re.compile(r"(?i)\bl?cam\b|property manager|community (?:association )?manager|association manager|"
                      r"general manager|building manager|site manager|portfolio manager|administrator|practice manager|"
                      r"administrador")),
    ("board", re.compile(r"(?i)board|treasurer|secretary|vice president|\bvp\b|\bdirector\b(?! of)")),
    ("exec", re.compile(r"(?i)president|\bceo\b|chief executive|\bcoo\b|chief operating|owner|principal|managing partner")),
]
# Who the setter tries when the primary contact doesn't pick up.
PLAN_B_ORDER = ("facilities", "assistant", "pm", "board", "exec")
# Who becomes the primary contact.
PRIMARY_ORDER = ("pm", "facilities", "assistant", "board", "exec", "other")

FACILITIES_TITLES = (
    r"Chief Engineer|Director of Facilities|Facilities Director|Facilities Manager|Facility Manager|Building Engineer|"
    r"Maintenance Supervisor|Maintenance Director|Maintenance Manager|Director of Maintenance|Superintendent|"
    r"Assistant Property Manager|Assistant Manager|Office Manager|Practice Manager|Administrator"
)


def role_of(title: str | None) -> str:
    t = title or ""
    for role, rx in ROLE_RX:
        if rx.search(t):
            return role
    return "other"


def pick_primary(contacts: list[dict]) -> dict | None:
    for role in PRIMARY_ORDER:
        for c in contacts:
            if c.get("role") == role:
                return c
    return None


# ── office hours ────────────────────────────────────────────────────────────

_DAY = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?"
_TIME = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)"
HOURS_RX = re.compile(
    rf"(?i)(?:office hours|business hours|hours of operation|horario(?: de oficina)?|management office hours)?\s*[:\-]?\s*"
    rf"(?P<d1>{_DAY})\s*(?:-|–|—|to|through|thru|a)\s*(?P<d2>{_DAY})\s*[:,]?\s*(?P<t1>{_TIME})\s*(?:-|–|—|to|a)\s*(?P<t2>{_TIME})")
_ES = {"mon": "L", "tue": "M", "wed": "Mi", "thu": "J", "fri": "V", "sat": "S", "sun": "D"}


def _day(d: str) -> str:
    return _ES.get(d.lower()[:3], d)


def _hour(t: str) -> str:
    m = re.match(r"(?i)(\d{1,2})(?::(\d{2}))?\s*([ap])", t.replace(".", ""))
    if not m:
        return t
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return f"{h}:{m.group(2)}" if m.group(2) and m.group(2) != "00" else str(h)


def office_hours(text: str, jsonld_hours: list[str] | None = None) -> str | None:
    """'Office hours: Monday - Friday 9:00 AM - 5:00 PM' -> 'L–V 9–17'."""
    m = HOURS_RX.search(text or "")
    if m:
        return f"{_day(m.group('d1'))}–{_day(m.group('d2'))} {_hour(m.group('t1'))}–{_hour(m.group('t2'))}"
    for h in jsonld_hours or []:          # schema.org openingHours "Mo-Fr 09:00-17:00"
        mm = re.match(r"(?i)(Mo|Tu|We|Th|Fr|Sa|Su)-(Mo|Tu|We|Th|Fr|Sa|Su)\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})", h.strip())
        if mm:
            es = {"Mo": "L", "Tu": "M", "We": "Mi", "Th": "J", "Fr": "V", "Sa": "S", "Su": "D"}
            t1 = str(int(mm.group(3))) + (f":{mm.group(4)}" if mm.group(4) != "00" else "")
            t2 = str(int(mm.group(5))) + (f":{mm.group(6)}" if mm.group(6) != "00" else "")
            return f"{es[mm.group(1).title()]}–{es[mm.group(2).title()]} {t1}–{t2}"
    return None
