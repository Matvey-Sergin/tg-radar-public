import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

class Config:
    API_ID = int(os.getenv("API_ID", 0))
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("OVERSEER_BOT_TOKEN", "")
    SPAIN_BOT_TOKEN = os.getenv("SPAIN_BOT_TOKEN", "")
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

    # Keep the old marketplace database intact. IT Radar starts with a clean,
    # domain-specific database unless DB_PATH is explicitly provided.
    DB_PATH = os.getenv("DB_PATH") or str(PROJECT_ROOT / "data" / "it_radar.db")

    raw_keys = os.getenv("GROQ_API_KEYS", "")
    GROQ_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]

    # Keep account_1/account_2 stable regardless of environment variable order.
    session_items = []
    for key, value in os.environ.items():
        if key.startswith("SESSION_STRING") and value.strip():
            suffix = key.removeprefix("SESSION_STRING")
            order = int(suffix) if suffix.isdigit() else 10_000
            session_items.append((order, key, value.strip()))
    SESSIONS = [value for _, _, value in sorted(session_items)]
