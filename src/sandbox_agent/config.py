import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_DIR / "data"
STATIC_DIR = PROJECT_DIR / "static"
SQLITE_PATH = DATA_DIR / "store.db"

PORT = int(os.environ.get("PORT", "19876"))
MODEL = os.environ.get("MODEL", "claude-sonnet-4-5-20250929")
ROOT_PATH = os.environ.get("ROOT_PATH", "")
MAX_MONTY_DURATION_SECS = 30
MAX_AGENT_TURNS = 25
SESSION_TTL_MINUTES = 30
