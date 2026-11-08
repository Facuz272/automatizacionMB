"""Pain Signal Detector: weighted exterior-cleaning pains in reviews and site text.

Each category has EN/ES patterns and a weight (how strongly it predicts a
softwash/pressure-cleaning sale). A hit keeps the exact sentence and the review
date so the setter hook can quote evidence ("reseña de mar 2026: «black mold in
the hallways»"). Weights are discounted by age and by negation ("no mold").

pain_score = sum over categories of the best (weight × recency × source factor),
capped at 10. ``VERIFIED_PAIN`` is the threshold the scoring uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

VERIFIED_PAIN = 3.0
MAX_SCORE = 10.0


@dataclass(frozen=True)
class PainCategory:
    key: str
    label: str          # Spanish label for the setter
    weight: float
    patterns: tuple[str, ...]


CATEGORIES: tuple[PainCategory, ...] = (
    PainCategory("mold", "moho/hongos", 3.0, (
        r"black mold", r"mou?ld(?:y)?", r"mildew", r"moho", r"hongos?")),
    PainCategory("algae", "algas/manchas verdes", 3.0, (
        r"algae", r"green (?:stains?|film|slime|spots?)", r"turning green", r"algas?", r"manchas? verdes?", r"verd[ií]n")),
    PainCategory("black_streaks", "manchas negras/techo", 3.0, (
        r"black (?:streaks?|stains?|marks?)", r"roof (?:stains?|is (?:dirty|black))", r"dirty roof",
        r"manchas? negras?", r"techo (?:sucio|manchado)")),
    PainCategory("violation", "violación HOA/código", 3.0, (
        r"code enforcement", r"(?:hoa|city|county) (?:violation|fine|citation)", r"violation notice",
        r"got (?:a )?(?:fine|citation)", r"\bfined\b", r"multa", r"infracci[oó]n", r"violaci[oó]n")),
    PainCategory("garage_stains", "garaje/manchas de aceite", 2.5, (
        r"oil stains?", r"(?:parking )?garage (?:is |was )?(?:dirty|filthy|disgusting|stained)",
        r"dirty (?:parking )?garage", r"garaje sucio", r"manchas? de aceite", r"estacionamiento sucio")),
    PainCategory("slippery", "pisos/caminos resbalosos", 2.5, (
        r"slippery", r"slick (?:walkway|sidewalk|pavers?)", r"resbal(?:oso|adizo)s?")),
    PainCategory("restoration", "pintura/restauración/recertificación", 2.5, (
        r"repaint(?:ing|ed)?", r"(?:concrete|building|facade) restoration", r"recertification",
        r"40[- ]year", r"special assessment", r"pintar el edificio", r"restauraci[oó]n", r"recertificaci[oó]n")),
    PainCategory("facade", "fachada/ventanas sucias", 2.5, (
        r"dirty (?:windows|facade|fa[cç]ade|walls|exterior|balcon(?:y|ies))", r"stained (?:walls|facade|exterior)",
        r"fachada sucia", r"ventanas sucias", r"paredes sucias", r"balcones sucios")),
    PainCategory("hallways", "pasillos/áreas comunes sucias", 2.0, (
        r"dirty (?:hallways?|corridors?|lobby|common areas?|stairs|stairwells?|elevators?)", r"hallways? (?:are|is) (?:dirty|filthy)",
        r"pasillos? sucios?", r"[aá]reas? comunes? sucias?", r"escaleras sucias")),
    PainCategory("trash", "basura/olor en área de residuos", 2.0, (
        r"trash chute", r"dumpster(?: pad| area)?", r"garbage (?:smell|area|room)", r"smells? (?:like garbage|bad|awful)",
        r"\bstinks?\b", r"olor a basura", r"cuarto de basura", r"mal olor")),
    PainCategory("rust", "óxido", 2.0, (r"\brust(?:y|ed)?\b", r"rust stains?", r"[oó]xido", r"oxidad[oa]s?")),
    PainCategory("birds", "excremento de aves", 2.0, (
        r"bird (?:droppings|poop|mess)", r"pigeons?", r"excremento de (?:aves|p[aá]jaros|palomas)", r"palomas")),
    PainCategory("pressure_wash", "falta lavado a presión", 2.0, (
        r"(?:needs?|need to be|could use|overdue for) (?:a )?(?:pressure|power)[- ]wash(?:ing|ed)?",
        r"never (?:pressure|power)[- ]wash", r"hidrolavad[oa]", r"lavado a presi[oó]n")),
    PainCategory("dirty", "suciedad general", 1.0, (
        r"\bfilthy\b", r"\bdirty\b", r"\bgrime\b", r"\bdisgusting\b", r"\bsucio\b", r"\bsucia\b", r"\bmugre\b")),
    PainCategory("salt", "salitre", 1.5, (r"salt (?:air|spray|damage)", r"salitre")),
)

_NEGATION = re.compile(r"(?i)\b(?:no|not|never|without|free of|zero|sin|nada de|nunca)\b[\w\s,'’-]{0,15}$")
_COMPILED = [(c, re.compile(r"(?i)\b(?:" + "|".join(c.patterns) + r")")) for c in CATEGORIES]
_SENTENCE_END = re.compile(r"(?<=[.!?¡¿\n])\s+")


def _sentence_around(text: str, start: int, end: int, max_len: int = 170) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start))
    rights = [i for i in (text.find(".", end), text.find("!", end), text.find("?", end), text.find("\n", end)) if i != -1]
    right = min(rights) + 1 if rights else len(text)
    s = " ".join(text[left + 1 : right].split())
    if len(s) > max_len:
        mid = (start - left - 1)
        a = max(0, mid - max_len // 2)
        s = ("…" if a else "") + s[a : a + max_len].strip() + "…"
    return s


_REL = re.compile(r"(?i)\b(a|an|one|\d+)\s+(day|week|month|year)s?\s+ago\b|\bhace\s+(un|una|\d+)\s+(d[ií]a|semana|mes|año)")
_REL_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365, "dia": 1, "día": 1, "semana": 7, "mes": 30, "año": 365}


def relative_days(relative: str | None) -> int | None:
    """'3 months ago' / 'a year ago' / 'hace 2 semanas' -> approximate age in days."""
    m = _REL.search(relative or "")
    if not m:
        return None
    qty, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    n = 1 if qty.lower() in ("a", "an", "one", "un", "una") else int(qty)
    return n * _REL_DAYS[unit.lower()]


def age_days(published: str | None, relative: str | None = None, now: datetime | None = None) -> int | None:
    if published:
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
            return ((now or datetime.now(timezone.utc)) - when).days
        except ValueError:
            pass
    return relative_days(relative)


def recency_factor(published: str | None, now: datetime | None = None, relative: str | None = None) -> float:
    days = age_days(published, relative, now)
    if days is None:
        return 0.7
    if days <= 365:
        return 1.0
    if days <= 730:
        return 0.6
    return 0.3


_UNIT_ES = {1: "día", 7: "semana", 30: "mes", 365: "año"}
_UNIT_ES_PL = {"día": "días", "semana": "semanas", "mes": "meses", "año": "años"}


def relative_es(relative: str | None) -> str | None:
    """'2 months ago' -> 'hace 2 meses'; Spanish input is returned as is."""
    if not relative:
        return None
    if relative.lower().startswith("hace"):
        return relative
    m = _REL.search(relative)
    if not m or not m.group(1):
        return relative
    q = m.group(1).lower()
    n = 1 if q in ("a", "an", "one") else int(q)
    unit = _UNIT_ES[_REL_DAYS[m.group(2).lower()]]
    return f"hace {n} {unit if n == 1 else _UNIT_ES_PL[unit]}"


def _date_label(review: dict[str, Any]) -> str | None:
    pub = review.get("published")
    if pub:
        try:
            return datetime.fromisoformat(pub.replace("Z", "+00:00")).strftime("%Y-%m")
        except ValueError:
            pass
    return relative_es(review.get("relative"))


def scan_text(text: str, source: str, factor: float = 1.0, date: str | None = None) -> list[dict[str, Any]]:
    """All non-negated category hits in one text (best hit per category)."""
    hits: dict[str, dict[str, Any]] = {}
    for cat, rx in _COMPILED:
        for m in rx.finditer(text):
            if _NEGATION.search(text[max(0, m.start() - 25) : m.start()]):
                continue
            score = round(cat.weight * factor, 2)
            if cat.key not in hits or score > hits[cat.key]["weight"]:
                hits[cat.key] = {
                    "category": cat.key, "label": cat.label, "weight": score,
                    "quote": _sentence_around(text, m.start(), m.end()), "match": m.group(0),
                    "date": date, "source": source,
                }
            break
    # "dirty" is generic: drop it when a specific category already explains the same text.
    if "dirty" in hits and len(hits) > 1:
        hits.pop("dirty")
    return list(hits.values())


def detect(reviews: Iterable[dict[str, Any]], web_text: str = "", now: datetime | None = None) -> tuple[list[dict[str, Any]], float]:
    """(signals sorted by weight, pain_score 0–10)."""
    best: dict[str, dict[str, Any]] = {}
    for rv in reviews:
        text = rv.get("text") or ""
        if not text:
            continue
        factor = recency_factor(rv.get("published"), now, rv.get("relative"))
        rating = rv.get("rating")
        if isinstance(rating, (int, float)) and rating <= 3:
            factor *= 1.2
        for hit in scan_text(text, "review", factor, _date_label(rv)):
            if hit["category"] not in best or hit["weight"] > best[hit["category"]]["weight"]:
                best[hit["category"]] = hit
    # Site text (rules pages, notices about restoration/assessments) counts half.
    if web_text:
        for hit in scan_text(web_text, "website", 0.5):
            if hit["category"] in ("restoration", "violation", "pressure_wash") and hit["category"] not in best:
                best[hit["category"]] = hit
    signals = sorted(best.values(), key=lambda h: -h["weight"])
    score = min(MAX_SCORE, round(sum(h["weight"] for h in signals), 2))
    return signals, score
