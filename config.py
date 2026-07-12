import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "data" / "leads.db")

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
# Scraper search space: every (vertical × city) pair becomes one Google Places
# text query. Kept deliberately moderate — each query is billable and the
# scraper runs Mon–Fri, so 5×3 = 15 queries/run balances a full lead queue
# against Places API cost. Widen further only if the queue starts running dry.
# Cities: Doral + nearby Miami-Dade municipalities with dense managed property.
TARGET_CITIES = ["Doral", "Miami", "Coral Gables", "Hialeah", "Aventura"]
# Verticals: the softwash/exterior-cleaning ICP — firms that pay to clean
# building facades and common areas (property mgrs, HOA & condo associations).
TARGET_VERTICALS = [
    "property management",
    "HOA management",
    "condominium association management",
]

# When True, and a domain yields neither a decision-maker (Apollo) nor a
# personal email, the enricher falls back to a SINGLE human-read generic inbox
# (info@, contact@, office@…). Deliberately the right call for micro-business
# outreach, where info@ is often the owner's real inbox — and the wrong call for
# enterprise targeting, where it's a black hole. Bounce/trap addresses
# (noreply@, postmaster@, abuse@…) are NEVER used, regardless of this flag.
# Default ON so the pipeline keeps a live queue; flip to false in .env to revert
# to strict decision-maker-only mode.
ALLOW_GENERIC_FALLBACK = os.getenv("ALLOW_GENERIC_FALLBACK", "true").lower() in (
    "1", "true", "yes", "on",
)
