"""Lead record shared by every stage, and its mapping to the CRM import contract.

CRM contract (mbguiaclientes: src/lib/lead-import.ts + SQL public.import_leads,
migration 0045): company_name, phone, segment, contact_name, contact_title, email,
address, website_url, maps_url, google_rating, total_reviews, strategy_kind,
sales_strategy, properties_count, management_company, source, import_batch_id.
Unknown keys are silently dropped by the endpoint, so a new field here needs a
CRM migration first.

``sales_strategy`` is what the setter reads on the card: the 1–2 line hook and,
unless CRM_INCLUDE_BRIEF=false, one "Ficha" line with the physical/contact facts
the CRM has no column for (floors, footprint, units, washable surfaces, coastal
exposure, labelled phones, lead_score). The full record stays in leads.jsonl.
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
AMENITY_LABEL = {
    "parking_garage": "garaje/parking multinivel",
    "pool_deck": "deck de piscina",
    "tennis_court": "canchas de tenis",
    "sidewalks": "caminos/aceras",
    "gatehouse": "garita de acceso",
}
PHONE_LABEL = {
    "management": "administración",
    "hoa": "asociación",
    "front_desk": "front desk",
    "leasing": "leasing/ventas",
    "maintenance": "mantenimiento",
    "google_main": "Google",
    "unlabeled": "otro",
}


ROOF_ES = {
    "concrete": "hormigón", "flat": "plano", "metal": "metálico", "tile": "tejas", "roof_tiles": "tejas",
    "asphalt": "asfáltico", "asphalt_shingle": "tejas asfálticas", "gravel": "grava", "gabled": "a dos aguas",
    "hipped": "a cuatro aguas", "glass": "vidrio", "tar_paper": "membrana asfáltica", "white": "blanco",
    "grey": "gris", "gray": "gris", "red": "rojo", "black": "negro",
}


def roof_es(roof: str) -> str:
    return " ".join(ROOF_ES.get(w.lower(), w) for w in roof.split())


def fmt_int(n: float | int) -> str:
    """12345 -> '12.345' (es-AR style thousands)."""
    return f"{int(round(n)):,}".replace(",", ".")


@dataclass
class Lead:
    # identity / source
    source: str                     # google_places | osm | seed_csv
    source_id: str                  # place_id, osm node id, csv row
    strategy_kind: StrategyKind
    company_name: str
    # contactability (gates)
    phone: str | None = None        # E.164 once normalised (primary, see phones)
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    # web
    website_url: str | None = None
    domain: str = ""
    maps_url: str | None = None
    google_rating: float | None = None
    total_reviews: int | None = None
    place_types: list[str] = field(default_factory=list)
    # {"text", "rating", "published" (ISO|None), "relative"}
    reviews: list[dict[str, Any]] = field(default_factory=list)
    # {"number", "label", "source", "context"}; label ∈ PHONE_LABEL
    phones: list[dict[str, Any]] = field(default_factory=list)
    # enrichment
    contact_name: str | None = None
    contact_title: str | None = None
    email: str | None = None
    email_source: str | None = None       # mailto | text | jsonld | apollo | search | seed
    email_status: str = "unchecked"       # mx | a_only | no_mail | nxdomain | unchecked
    management_company: str | None = None
    management_type: str = "unknown"       # onsite | third_party | self_managed | unknown
    association_name: str | None = None
    properties_count: int | None = None
    segment: Segment | None = None
    sales_strategy: str | None = None
    # physical profile: levels, height_m, footprint_m2, roof, amenities{name: source},
    # coastal, coast_distance_m, units, large
    building: dict[str, Any] = field(default_factory=dict)
    # pain detector: {"category","label","weight","quote","date","source"}
    pain: list[dict[str, Any]] = field(default_factory=list)
    pain_score: float = 0.0
    public_records: dict[str, Any] = field(default_factory=dict)   # dbpr match etc.
    # municipal open data hits (also merged into `pain`): recert pending, unsafe structure, code case, permits
    municipal: list[dict[str, Any]] = field(default_factory=list)
    # DBPR CAM licences: {"contact": {...CAM...}, "firm": {...CAB...}}
    licenses: dict[str, Any] = field(default_factory=dict)
    # other condos in the area under the same management firm / person (dbpr.cluster)
    portfolio_cluster: dict[str, Any] = field(default_factory=dict)
    # valuation.estimate: {"low", "high", "components", "basis"} USD/year
    estimated_deal: dict[str, Any] = field(default_factory=dict)
    search_hits: list[dict[str, Any]] = field(default_factory=list)
    lead_score: int = 0
    score_breakdown: dict[str, int] = field(default_factory=dict)
    # bookkeeping
    web_text: str = ""                     # trimmed site text for the AI step (not exported)
    evidence: dict[str, Any] = field(default_factory=dict)
    ai_used: bool = False
    rejected_reason: str | None = None

    # ── derived ───────────────────────────────────────────────────────────
    @property
    def email_verified(self) -> bool:
        return bool(self.email) and self.email_status == "mx"

    @property
    def pain_signals(self) -> list[str]:
        """Short human labels ('moho: "…"') kept for backwards compatibility."""
        return [f'{p["label"]}: "{p["quote"]}"' for p in self.pain]

    def secondary_phones(self) -> list[dict[str, Any]]:
        return [p for p in self.phones if p["number"] != self.phone and p["label"] != "fax"]

    def brief(self) -> str:
        """One-line 'Ficha' for the setter: physical facts + labelled phones + score."""
        b = self.building or {}
        parts: list[str] = []
        if b.get("levels"):
            parts.append(f"{b['levels']} pisos")
        if b.get("footprint_m2"):
            parts.append(f"~{fmt_int(b['footprint_m2'])} m² de huella (~{fmt_int(b['footprint_m2'] * 10.7639)} ft²)")
        units = b.get("units")
        if units:
            parts.append(f"{units} unidades")
        if b.get("roof"):
            parts.append(f"techo {roof_es(b['roof'])}")
        amen = [AMENITY_LABEL[a] for a in AMENITY_LABEL if a in (b.get("amenities") or {})]
        if amen:
            parts.append(", ".join(amen))
        if b.get("coastal"):
            dist = b.get("coast_distance_m")
            parts.append("frente costero (salitre)" if dist is not None and dist <= 300 else "zona costera (salitre)")
        if self.estimated_deal:
            from .valuation import label as deal_label
            parts.append(deal_label(self.estimated_deal))
        cl = self.portfolio_cluster
        if cl and cl.get("count", 0) > 1:
            who = self.management_company if cl.get("by") == "company" and self.management_company else (
                self.contact_name if cl.get("by") == "person" and self.contact_name else "la misma administración")
            names = ", ".join(x["name"] for x in cl.get("siblings", [])[:2])
            parts.append(f"{who} administra {cl['count']} condominios en la zona" + (f" (incl. {names})" if names else ""))
        lic = self.licenses or {}
        if lic.get("contact", {}).get("active"):
            parts.append(f"LCAM activa {lic['contact']['license_id']}")
        if lic.get("firm", {}).get("active"):
            approx = "" if lic["firm"].get("matched_by") == "exact" else "≈"
            parts.append(f"firma CAM licenciada {approx}{lic['firm']['license_id']}")
        primary = next((p for p in self.phones if p["number"] == self.phone), None)
        if primary and primary["label"] in ("management", "hoa"):
            parts.append(f"tel. principal = {PHONE_LABEL[primary['label']]}")
        others = [f"{PHONE_LABEL.get(p['label'], p['label'])} {p['number']}" for p in self.secondary_phones()[:2]]
        if others:
            parts.append("otros tel.: " + "; ".join(others))
        if self.lead_score:
            parts.append(f"score {self.lead_score}/100")
        return "Ficha: " + " · ".join(parts) if parts else ""

    def internal_record(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("web_text", None)
        d["email_verified"] = self.email_verified
        return d

    def crm_payload(self, batch_id: str, source: str = "lead-engine-v2", include_brief: bool = True) -> dict[str, Any]:
        """Exactly what POST /api/public/leads-import accepts (no None values)."""
        management = self.management_company
        if not management and self.management_type in ("onsite", "self_managed"):
            management = MANAGEMENT_LABEL[self.management_type].capitalize()
        hook = (self.sales_strategy or "").strip()
        brief = self.brief() if include_brief else ""
        text = "\n".join(p for p in (hook, brief) if p)
        payload: dict[str, Any] = {
            "company_name": clip(self.company_name, 200),
            "phone": self.phone,
            "segment": self.segment,
            "strategy_kind": self.strategy_kind,
            "contact_name": clip(self.contact_name, 120),
            "contact_title": clip(self.contact_title, 120),
            "management_company": clip(management, 200),
            # Dead or misspelled domains (nxdomain / no MX nor A) never reach the CRM.
            "email": self.email if self.email_status in ("mx", "a_only", "unchecked") else None,
            "address": clip(self.address, 300),
            "website_url": clip(self.website_url, 500),
            "maps_url": clip(self.maps_url, 500),
            "google_rating": self.google_rating,
            "total_reviews": self.total_reviews,
            "sales_strategy": clip(text, 2000),
            "properties_count": self.properties_count if self.properties_count and self.properties_count > 0 else None,
            "source": clip(f"{source}:{self.source}", 60),
            "import_batch_id": batch_id,
        }
        return {k: v for k, v in payload.items() if v is not None and v != ""}
