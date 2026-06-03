import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "data" / "leads.db")

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
TARGET_CITIES = ["Doral"]
TARGET_VERTICALS = ["property management"]
