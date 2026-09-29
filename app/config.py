import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AEMET_API_KEY = os.getenv("AEMET_API_KEY", "").strip()
METEOCAT_API_KEY = os.getenv("METEOCAT_API_KEY", "").strip()
SIAR_TOKEN = os.getenv("SIAR_TOKEN", "").strip()
SAIH_API_KEY = os.getenv("SAIH_API_KEY", "").strip()

CACHE_DIR = Path(os.getenv("CACHE_DIR", ROOT / "cache"))
USER_AGENT = "RadarSetas/0.1 (seguimiento de lluvia para micologia; uso no comercial)"
