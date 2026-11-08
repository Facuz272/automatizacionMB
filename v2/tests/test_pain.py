from datetime import datetime, timezone

from lead_engine.pain import VERIFIED_PAIN, detect, recency_factor, relative_days, scan_text

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def test_weighted_categories_quotes_and_dates():
    reviews = [
        {"text": "Beautiful lobby. However the hallways are dirty and there is black mold near the pool deck.",
         "rating": 2, "published": "2026-09-14T10:00:00Z"},
        {"text": "The parking garage has oil stains everywhere.", "rating": 3, "relative": "3 months ago"},
        {"text": "Great location, friendly staff.", "rating": 5},
    ]
    signals, score = detect(reviews, now=NOW)
    cats = [s["category"] for s in signals]
    assert cats[0] == "mold" and "garage_stains" in cats and "hallways" in cats
    mold = signals[0]
    assert mold["date"] == "2026-09" and "black mold near the pool deck" in mold["quote"]
    assert next(s for s in signals if s["category"] == "garage_stains")["date"] == "hace 3 meses"
    assert score >= VERIFIED_PAIN and score <= 10


def test_spanish_terms_and_violation():
    signals, _ = detect([{"text": "Nos llegó una multa del condado y los pasillos sucios dan pena. Hay moho en el techo."}], now=NOW)
    assert {"violation", "hallways", "mold"} <= {s["category"] for s in signals}


def test_negation_and_generic_dirty():
    assert scan_text("Very clean building, no mold at all.", "review") == []
    assert scan_text("Without any mildew issues.", "review") == []
    hits = scan_text("The facade is dirty and covered in algae.", "review")
    assert {h["category"] for h in hits} == {"algae"}   # generic 'dirty' dropped when a specific pain explains it


def test_recency():
    assert recency_factor("2026-05-01T00:00:00Z", NOW) == 1.0
    assert recency_factor("2025-01-01T00:00:00Z", NOW) == 0.6
    assert recency_factor("2020-01-01T00:00:00Z", NOW) == 0.3
    assert recency_factor(None, NOW, "2 years ago") == 0.6
    assert recency_factor(None, NOW) == 0.7
    from lead_engine.pain import relative_es
    assert relative_es("a month ago") == "hace 1 mes" and relative_es("2 years ago") == "hace 2 años"
    assert relative_days("a month ago") == 30 and relative_days("hace 2 semanas") == 14 and relative_days("") is None


def test_old_reviews_weigh_less_and_web_only_counts_specific_categories():
    fresh, s1 = detect([{"text": "Mold in the hallway.", "published": "2026-10-01T00:00:00Z"}], now=NOW)
    old, s2 = detect([{"text": "Mold in the hallway.", "published": "2019-10-01T00:00:00Z"}], now=NOW)
    assert s1 > s2
    sig, _ = detect([], web_text="Notice: building repainting and concrete restoration starts in January. Dirty shoes off.")
    assert [s["category"] for s in sig] == ["restoration"] and sig[0]["source"] == "website"
