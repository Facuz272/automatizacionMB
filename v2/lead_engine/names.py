"""Name normalisation shared by the DBPR condo registry and the CAM licence registry (pure).

firm_key("Castle Management, LLC")      == firm_key("CASTLE MGMT LLC")         == "CASTLE MANAGEMENT"
firm_key("Premier Assn. Services Inc")  == "PREMIER ASSOCIATION SERVICES"
person_key("Orly Iglesias")             == person_key("IGLESIAS, ORLY M")      == "IGLESIAS|ORLY"
"""

from __future__ import annotations

import difflib
import re

ABBREV = {
    "MGMT": "MANAGEMENT", "MGT": "MANAGEMENT", "MNGMT": "MANAGEMENT", "MANAGMENT": "MANAGEMENT", "MANAGEMEMT": "MANAGEMENT",
    "PROP": "PROPERTY", "PROPS": "PROPERTIES", "PROPERTIE": "PROPERTIES", "PROEPRTY": "PROPERTY",
    "ASSN": "ASSOCIATION", "ASSOC": "ASSOCIATION", "ASSOCS": "ASSOCIATES",
    "SVC": "SERVICES", "SVCS": "SERVICES", "SERV": "SERVICES", "SERVICE": "SERVICES",
    "GRP": "GROUP", "CTR": "CENTER", "INTL": "INTERNATIONAL", "FLA": "FLORIDA", "FL": "FLORIDA",
    "RES": "RESIDENTIAL", "RESIDENTAL": "RESIDENTIAL", "COMM": "COMMUNITY", "CMNTY": "COMMUNITY",
}
FIRM_STOP = {"INC", "LLC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PA", "PL", "PLLC", "LLP", "LP",
             "THE", "OF", "AND", "A", "DBA", "INCORPORATED", "LIMITED", "USA"}
NAME_SUFFIX = {"JR", "SR", "II", "III", "IV", "MR", "MRS", "MS", "DR", "ESQ", "CAM", "LCAM", "CMCA", "AMS", "PCAM"}


# Words every management firm uses: they never identify a firm on their own.
GENERIC = {
    "PROPERTY", "PROPERTIES", "MANAGEMENT", "SERVICES", "GROUP", "COMMUNITY", "ASSOCIATION", "ASSOCIATES", "ASSOCIATE",
    "SOLUTIONS", "REALTY", "CONSULTANTS", "CONSULTANT", "CONSULTING", "FLORIDA", "SOUTH", "MAINTENANCE", "MANAGERS",
    "PROFESSIONAL", "PROFESSIONALS", "ENTERPRISES", "HOLDINGS", "CONDO", "CONDOMINIUM", "RESIDENTIAL", "REAL", "ESTATE",
    "INTERNATIONAL", "AMERICA", "AMERICAN", "MIAMI", "PARTNERS", "ADVISORS", "SYSTEMS", "COMMUNITIES",
}
_GENERIC_LONG = sorted(g for g in GENERIC if len(g) >= 6)


def _tokens(s: str | None) -> list[str]:
    s = (s or "").upper().replace("&", " AND ").replace("'", "")
    s = re.sub(r"\bL\.?\s?L\.?\s?C\.?", " LLC ", s)
    s = re.sub(r"\b([A-Z])\.(?=[A-Z]\.)", r"\1", s)          # V.T.J. -> VTJ
    return [t for t in re.split(r"[^A-Z0-9]+", s) if t]


def _fix_generic_typo(t: str) -> str:
    """'MANGEMENT' -> 'MANAGEMENT', 'SERVUCES' -> 'SERVICES' (only for long words close to a generic one)."""
    if t in GENERIC or len(t) < 6:
        return t
    best = difflib.get_close_matches(t, _GENERIC_LONG, n=1, cutoff=0.82)
    return best[0] if best else t


def firm_key(name: str | None) -> str:
    toks = [_fix_generic_typo(ABBREV.get(t, t)) for t in _tokens(name)]
    return " ".join(t for t in toks if t not in FIRM_STOP)


def compact_key(name: str | None) -> str:
    """Spacing-insensitive key on the raw words: 'First Service Residential' == 'FIRSTSERVICE RESIDENTIAL'."""
    return "".join(t for t in _tokens(name) if t not in FIRM_STOP)


def distinctive(key: str) -> str:
    return " ".join(t for t in key.split() if t not in GENERIC)


def firm_similarity(a: str | None, b: str | None) -> float:
    ta, tb = set(firm_key(a).split()), set(firm_key(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def firm_match_score(key_a: str, key_b: str) -> tuple[float, str] | None:
    """How well two firm keys refer to the same firm, judged on their distinctive words.

    - same distinctive words and at least one shared generic word    -> (1.0, "same_name")
    - distinctive words spelled almost the same (≥0.9, ≥5 letters,
      same first letter) and shared generic words                     -> (ratio, "spelling")
    Firms with no distinctive word only match exactly (handled by the caller).
    """
    da, db = distinctive(key_a), distinctive(key_b)
    if not da or not db:
        return None
    ga, gb = set(key_a.split()) - set(da.split()), set(key_b.split()) - set(db.split())
    shares_generic = bool(ga & gb) or (not ga and not gb)
    if not shares_generic:
        return None
    if da == db:
        # One common word ("ROYAL", "PRIME") is weaker evidence than a distinctive multi-word name.
        return (1.0, "same_name") if len(da.split()) > 1 or len(da) >= 8 else (0.85, "same_word")
    a, b = da.replace(" ", ""), db.replace(" ", "")
    if min(len(a), len(b)) < 5 or a[0] != b[0]:
        return None
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return (ratio, "spelling") if ratio >= 0.9 else None


def split_person(name: str | None) -> tuple[str, str] | None:
    """('LAST', 'FIRST') from 'First [Middle] Last' or 'LAST, FIRST MIDDLE'; None if fewer than two names."""
    raw = (name or "").strip()
    if not raw:
        return None
    if "," in raw:
        last, rest = raw.split(",", 1)
        lt = [t for t in _tokens(last) if t not in NAME_SUFFIX]
        ft = [t for t in _tokens(rest) if t not in NAME_SUFFIX and len(t) > 1]
        if not lt or not ft:
            return None
        return lt[-1], ft[0]
    toks = [t for t in _tokens(raw) if t not in NAME_SUFFIX]
    toks = [t for i, t in enumerate(toks) if i in (0, len(toks) - 1) or len(t) > 1]  # drop middle initials
    if len(toks) < 2:
        return None
    return toks[-1], toks[0]


def person_key(name: str | None) -> str | None:
    p = split_person(name)
    return f"{p[0]}|{p[1]}" if p else None
