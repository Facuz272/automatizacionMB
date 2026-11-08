"""lead_score 0–100: objective priority used to order each batch before import.

The CRM appends imported leads in payload order and the 6 AM distribution hands
them out by position, so sorting by score means setters get the best leads of
the batch first.

  decision_maker  25  full name + LCAM/CAM/Property/Community/General/Building Manager
                      (15 with another title such as Board President, 8 name only)
  onsite_or_pain  25  on-site management confirmed or verified pain (pain_score ≥ 3)
                      (10 when strategy is onsite but not confirmed)
  email_mx        15  email whose domain has MX records
  exposure_size   20  coastal (salt) or large building (levels/footprint/units)
                      (10 when mid-size: ≥4 floors, ≥800 m² or ≥40 units)
  mgmt_phone      15  primary phone labelled Management Office / Property Manager
                      (8 when labelled HOA/association)
"""

from __future__ import annotations

import re

from .models import Lead
from .pain import VERIFIED_PAIN

WEIGHTS = {"decision_maker": 25, "onsite_or_pain": 25, "email_mx": 15, "exposure_size": 20, "mgmt_phone": 15}
PM_TITLE = re.compile(
    r"(?i)\b(l?cam|community association manager|property manager|community manager|association manager|"
    r"general manager|building manager|site manager|portfolio manager|administrador(?:a)?)\b")


def _decision_maker(lead: Lead) -> int:
    name = (lead.contact_name or "").strip()
    if len(name.split()) < 2:
        return 0
    if lead.contact_title and PM_TITLE.search(lead.contact_title):
        return 25
    if lead.contact_title:
        return 15
    return 8


def _onsite_or_pain(lead: Lead) -> int:
    if lead.management_type == "onsite" or lead.pain_score >= VERIFIED_PAIN:
        return 25
    if lead.strategy_kind == "onsite":
        return 10
    return 0


def _exposure_size(lead: Lead) -> int:
    b = lead.building or {}
    if b.get("coastal") or b.get("large"):
        return 20
    footprint = b.get("complex_footprint_m2") or b.get("footprint_m2") or 0
    if (b.get("levels") or 0) >= 4 or footprint >= 800 or (b.get("units") or 0) >= 40:
        return 10
    return 0


def _mgmt_phone(lead: Lead) -> int:
    primary = next((p for p in lead.phones if p["number"] == lead.phone), None)
    if not primary:
        return 0
    return {"management": 15, "hoa": 8}.get(primary["label"], 0)


def score(lead: Lead) -> tuple[int, dict[str, int]]:
    parts = {
        "decision_maker": _decision_maker(lead),
        "onsite_or_pain": _onsite_or_pain(lead),
        "email_mx": 15 if lead.email_verified else 0,
        "exposure_size": _exposure_size(lead),
        "mgmt_phone": _mgmt_phone(lead),
    }
    return min(100, sum(parts.values())), parts


def apply(lead: Lead) -> None:
    lead.lead_score, lead.score_breakdown = score(lead)


def rank(leads: list[Lead]) -> list[Lead]:
    """Highest score first; ties by pain, then reviews (stable)."""
    return sorted(leads, key=lambda l: (-l.lead_score, -l.pain_score, -(l.total_reviews or 0)))
