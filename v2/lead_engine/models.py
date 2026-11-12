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

import re
from dataclasses import asdict, dataclass, field, fields
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
    "firm_central": "central administradora",
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


def _deliverable() -> set[str]:
    """Email statuses sent to the CRM: verified MX, or unchecked (our DNS failed); a host without MX only
    when EMAIL_REQUIRE_MX=false."""
    from .email_verify import require_mx
    return {"mx", "unchecked"} | (set() if require_mx() else {"a_only"})


SALES_STRATEGY_MAX = 2000      # optText(2000) in the CRM import schema
MIN_HOOK = 600


def fit_hook(hook: str | None, limit: int) -> str:
    """One line within `limit`; a trailing "Zona: …" sentence is kept whole and the text before it is clipped."""
    text = " ".join((hook or "").split())
    if len(text) <= limit:
        return text
    at = text.rfind(" Zona: ")
    if at > 0 and len(text) - at < limit // 2:
        zona = text[at + 1:]
        return f"{clip(text[:at], limit - len(zona) - 1)} {zona}"
    return clip(text, limit) or ""


def fit_brief(brief: str, limit: int) -> str:
    """Drop whole facts (from the end, keeping the score) until the Ficha line fits."""
    if len(brief) <= limit:
        return brief
    head, _, body = brief.partition(": ")
    facts = body.split(" · ")
    score = facts.pop() if facts and facts[-1].startswith("score ") else None
    while facts and len(f"{head}: " + " · ".join(facts + ([score] if score else []))) > limit:
        facts.pop()
    return f"{head}: " + " · ".join(facts + ([score] if score else [])) if facts or score else ""


def clean_person_name(name: str | None) -> str | None:
    """'Guillermo Cancio-' → 'Guillermo Cancio': loose dashes, commas, colons or bullets left by the page layout."""
    if not name:
        return name
    n = re.sub(r"\s+", " ", name).strip(" \t-–—,;:|/•·*")
    return n or None


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
    # buildings of the same firm in this batch that share its central phone (portfolio_consolidation):
    # {"name","address","units","signals":[{"category","label"}],"est_high"}
    portfolio_siblings: list[dict[str, Any]] = field(default_factory=list)
    # valuation.estimate: {"low", "high", "components", "basis"} USD/year
    estimated_deal: dict[str, Any] = field(default_factory=dict)
    # business vertical: community | plaza | commercial | clinic (verticals.LABEL has the CRM-facing names)
    vertical: str = "community"
    # every decision maker found: {"name","title","role" (pm|facilities|board|exec|other),"source","phone"?}
    contacts: list[dict[str, Any]] = field(default_factory=list)
    # tactical Plan B when no second named contact exists: {"text", "phone"?, "source"} (pipeline.apply_plan_b_fallback)
    plan_b_fallback: dict[str, Any] = field(default_factory=dict)
    office_hours: str | None = None
    # official cadastre (appraiser.lookup), lodging licence (lodging.match)
    cadastre: dict[str, Any] = field(default_factory=dict)
    lodging: dict[str, Any] = field(default_factory=dict)
    # management firm's corporate domain (mgmt_domains.resolve): {"domain","status","source","email"?}
    management_domain: dict[str, Any] = field(default_factory=dict)
    # competitor_intel.analyze: zone weaknesses of cleaning vendors from neighbours' reviews
    competitor_intel: dict[str, Any] = field(default_factory=dict)
    search_hits: list[dict[str, Any]] = field(default_factory=list)
    lead_score: int = 0
    score_breakdown: dict[str, int] = field(default_factory=dict)
    # bookkeeping
    web_text: str = ""                     # trimmed site text for the AI step (not exported)
    evidence: dict[str, Any] = field(default_factory=dict)
    ai_used: bool = False
    rejected_reason: str | None = None

    # ── contacts ──────────────────────────────────────────────────────────
    def add_contact(self, name: str | None, title: str | None, source: str, phone: str | None = None) -> None:
        from .roles import role_of

        name = clean_person_name(name)
        if not name or len(name.split()) < 2:
            return
        for c in self.contacts:
            if c["name"].lower() == name.lower():
                if title and not c.get("title"):
                    c["title"], c["role"] = title, role_of(title)
                return
        c = {"name": name, "title": title, "role": role_of(title), "source": source}
        if phone:
            c["phone"] = phone
        self.contacts.append(c)

    def plan_b(self) -> dict[str, Any] | None:
        """Second door for the setter: facilities/engineering first, then an assistant PM, never the primary."""
        from .roles import PLAN_B_ORDER

        others = [c for c in self.contacts if (c["name"] or "").lower() != (self.contact_name or "").lower()]
        for role in PLAN_B_ORDER:
            for c in others:
                if c["role"] == role:
                    return c
        return None

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
        from .verticals import label as vertical_label

        b = self.building or {}
        prop = self.cadastre or {}
        # A CRM lead that is a management firm itself (backfill of older leads) is not a building.
        parts: list[str] = ["Administradora de propiedades" if self.evidence.get("lead_kind") == "management_firm"
                            else vertical_label(self.vertical)]
        if prop.get("year_built"):
            parts.append(f"construido {prop['year_built']}")
        if b.get("levels"):
            parts.append(f"{b['levels']} pisos")
        if b.get("footprint_m2"):
            parts.append(f"~{fmt_int(b['footprint_m2'])} m² de huella (~{fmt_int(b['footprint_m2'] * 10.7639)} ft²)")
        units = b.get("units")
        if units:
            parts.append(f"{units} unidades")
        if prop.get("building_sqft") and prop.get("kind") == "property":
            parts.append(f"{fmt_int(prop['building_sqft'])} ft² construidos")
        if prop.get("owner_name") and self.vertical != "community":
            from .dbpr import _title_case
            parts.append(f"propietario {_title_case(prop['owner_name'])}" + (f" (c/o {_title_case(prop['care_of'])})" if prop.get("care_of") else ""))
        if self.lodging.get("rooms"):
            parts.append(f"{self.lodging['kind']} con {self.lodging['rooms']} habitaciones")
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
        if cl and cl.get("count") and self.evidence.get("lead_kind") == "management_firm":
            names = ", ".join(x["name"] for x in cl.get("siblings", [])[:2])
            n = cl["count"]
            parts.append(f"administra {n} condominio{'s' if n != 1 else ''} registrado{'s' if n != 1 else ''} en DBPR "
                         f"en la zona" + (f" (incl. {names})" if names else ""))
        elif cl and cl.get("count", 0) > 1:
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
        pb = self.plan_b()
        if pb:
            parts.append(f"Plan B: {pb['title'] or 'contacto'} {pb['name']}" + (f" {pb['phone']}" if pb.get("phone") else ""))
        elif self.plan_b_fallback.get("text"):
            fb = self.plan_b_fallback
            parts.append(f"Plan B: {fb['text']}" + (f" {fb['phone']}" if fb.get("phone") else ""))
        if self.office_hours:
            parts.append(f"oficina {self.office_hours}")
        md = self.management_domain
        if md.get("domain") and not self.email:
            status = {"mx": "MX ok", "a_only": "sin MX, web activa", "unchecked": "sin verificar"}.get(md.get("status"), md.get("status"))
            parts.append(f"administradora: {md['domain']} ({status})" + (f" · {md['email']}" if md.get("email") else ""))
        primary = next((p for p in self.phones if p["number"] == self.phone), None)
        if primary and primary["label"] in ("management", "hoa", "firm_central"):
            parts.append(f"tel. principal = {PHONE_LABEL[primary['label']]}")
        shown_b = (self.plan_b_fallback or {}).get("phone") if not pb else None     # already on the Plan B
        others = [f"{PHONE_LABEL.get(p['label'], p['label'])} {p['number']}"
                  for p in self.secondary_phones() if p["number"] != shown_b][:2]
        if others:
            parts.append("otros tel.: " + "; ".join(others))
        if self.lead_score:
            parts.append(f"score {self.lead_score}/100")
        return "Ficha: " + " · ".join(parts) if parts else ""

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> "Lead":
        """Inverse of internal_record() (leads.jsonl row → Lead); unknown keys are ignored."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

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
        # Contract with the CRM card (mbguiaclientes src/lib/lead-brief.ts): the hook (with an inline
        # "Zona: …") on the first line, "Ficha: …" alone on the second. The Ficha is never cut mid-fact:
        # it gets its room first and the hook is clipped to what is left of the 2000 characters.
        brief = fit_brief(self.brief() if include_brief else "", SALES_STRATEGY_MAX - MIN_HOOK - 1)
        hook = fit_hook(self.sales_strategy, SALES_STRATEGY_MAX - (len(brief) + 1 if brief else 0))
        text = "\n".join(p for p in (hook, brief) if p)
        from .config import settings as _settings
        from .verticals import label as vertical_label

        segment = vertical_label(self.vertical) if _settings.crm_segment_mode == "vertical" else self.segment
        payload: dict[str, Any] = {
            "company_name": clip(self.company_name, 200),
            "phone": self.phone,
            "segment": segment,
            "strategy_kind": self.strategy_kind,
            "contact_name": clip(self.contact_name, 120),
            "contact_title": clip(self.contact_title, 120),
            "management_company": clip(management, 200),
            # Dead or misspelled domains (nxdomain / no MX nor A) never reach the CRM.
            "email": self.email if self.email_status in _deliverable() else None,
            "address": clip(self.address, 300),
            "website_url": clip(self.website_url, 500),
            "maps_url": clip(self.maps_url, 500),
            "google_rating": self.google_rating,
            "total_reviews": self.total_reviews,
            "sales_strategy": text or None,
            "properties_count": self.properties_count if self.properties_count and self.properties_count > 0 else None,
            "source": clip(f"{source}:{self.source}", 60),
            "import_batch_id": batch_id,
        }
        return {k: v for k, v in payload.items() if v is not None and v != ""}
