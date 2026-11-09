"""Operators of shopping centers / commercial buildings (the retail side of mgmt_domains.py).

Plazas are owned by single-purpose LLCs ("SFA ATLANTIS ASSOC LP", "CCB ASSOCIATES 39 LLC"), so the
operator is recognised from the cadastre itself: owner name, care-of ("% KIMCO REALTY") and, above
all, the owner's mailing address — every property of an operator gets its tax bill at the same office
(500 N Broadway, Jericho → Kimco; 17100 Collins Ave → RK Centers; 7978 Cooper Creek Blvd → Benderson).

The catalog (data/commercial_operators.json) was built and verified live: operators found on the
1,753 plazas / commercial buildings of the service area, domain and phone read from each operator's
own website. Only `phone_scope: "florida"` numbers are dialled (labelled "central administradora",
like the condo firms, and only with ALLOW_MGMT_FIRM_PHONE=true); out-of-state headquarters stay as
reference. Several plazas of one operator in a batch share that phone, so portfolio_consolidation
puts them on one lead exactly as it does for condo management firms.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).parent / "data" / "commercial_operators.json"
COMMERCIAL_VERTICALS = {"plaza", "commercial", "clinic"}


@lru_cache(maxsize=1)
def catalog() -> list[dict[str, Any]]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    for op in data["operators"]:
        op["_owner"] = re.compile(op["owner_regex"])
        op["_mail"] = re.compile(op["mailing_regex"])
    return data["operators"]


def match_record(owner: str | None, care_of: str | None, mailing: str | None) -> dict[str, Any] | None:
    """Operator for a cadastre record (owner / care-of name first, then the mailing address)."""
    names = f"{owner or ''} | {care_of or ''}".upper()
    mail = re.sub(r"\s+", " ", (mailing or "").upper())
    for op in catalog():
        if op["_owner"].search(names):
            return op
    for op in catalog():
        if mail and op["_mail"].search(mail):
            return op
    return None


def match(lead) -> dict[str, Any] | None:
    if lead.vertical not in COMMERCIAL_VERTICALS:
        return None
    c = lead.cadastre or {}
    return match_record(c.get("owner_name"), c.get("care_of"), c.get("owner_mailing"))


def dial_phone(op: dict[str, Any] | None) -> str | None:
    """The operator's Florida office number, the only one the setters call."""
    return op["phone"] if op and op.get("phone") and op.get("phone_scope") == "florida" else None


def public(op: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in op.items() if not k.startswith("_")}
