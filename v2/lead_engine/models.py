"""Lead record shared by every stage, and its mapping to the CRM import contract.

CRM contract (mbguiaclientes: src/lib/lead-import.ts + SQL public.import_leads,
migration 0045): company_name, phone, segment, contact_name, contact_title, email,
address, website_url, maps_url, google_rating, total_reviews, strategy_kind,
sales_strategy, properties_count, management_company, source, import_batch_id.
``sales_strategy`` carries only the 1–2 line hook for the setter; everything else
goes in its own column. Unknown keys are silently dropped by the endpoint, so a
new field here needs a CRM migration first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .normalize import clip

Segment = Literal["Dolor Urgente", "Gran Comunidad"]
StrategyKind = Literal["onsite", "pain", "portfolio"]
MANAGEMENT_LABEL = {
    "onsite": "administración en el edificio",
    "third_party": "administradora externa",
    "self_managed": "autoadministrado (junta)",
    "unknown": "administración sin confirmar",
}


@dataclass
class Lead:
    # identity / source
    source: str                     # google_places | osm | seed_csv
    source_id: str                  # place_id, osm node id, csv row
    strategy_kind: StrategyKind
    company_name: str
    # contactability (gates)
    phone: str | None = None        # E.164 once normalised
    address: str | None = None
    # web
    website_url: str | None = None
    domain: str = ""
    maps_url: str | None = None
    google_rating: float | None = None
    total_reviews: int | None = None
    place_types: list[str] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)
    # enrichment
    contact_name: str | None = None
    contact_title: str | None = None
    email: str | None = None
    email_source: str | None = None       # mailto | page_text | apollo | jsonld
    management_company: str | None = None
    management_type: str = "unknown"       # onsite | third_party | self_managed | unknown
    properties_count: int | None = None
    segment: Segment | None = None
    sales_strategy: str | None = None
    pain_signals: list[str] = field(default_factory=list)
    # bookkeeping
    web_text: str = ""                     # trimmed site text for the AI step (not exported)
    evidence: dict[str, Any] = field(default_factory=dict)
    ai_used: bool = False
    rejected_reason: str | None = None

    def internal_record(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("web_text", None)
        return d

    def crm_payload(self, batch_id: str, source: str = "lead-engine-v2") -> dict[str, Any]:
        """Exactly what POST /api/public/leads-import accepts (no None values)."""
        management = self.management_company
        if not management and self.management_type in ("onsite", "self_managed"):
            management = MANAGEMENT_LABEL[self.management_type].capitalize()
        payload: dict[str, Any] = {
            "company_name": clip(self.company_name, 200),
            "phone": self.phone,
            "segment": self.segment,
            "strategy_kind": self.strategy_kind,
            "contact_name": clip(self.contact_name, 120),
            "contact_title": clip(self.contact_title, 120),
            "management_company": clip(management, 200),
            "email": self.email,
            "address": clip(self.address, 300),
            "website_url": clip(self.website_url, 500),
            "maps_url": clip(self.maps_url, 500),
            "google_rating": self.google_rating,
            "total_reviews": self.total_reviews,
            "sales_strategy": clip(self.sales_strategy, 2000),
            "properties_count": self.properties_count if self.properties_count and self.properties_count > 0 else None,
            "source": clip(f"{source}:{self.source}", 60),
            "import_batch_id": batch_id,
        }
        return {k: v for k, v in payload.items() if v is not None and v != ""}
