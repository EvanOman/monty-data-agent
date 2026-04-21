import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_DIR / "data"
STATIC_DIR = PROJECT_DIR / "static"

# Chatkit sibling repo paths (for serving web component assets during development)
CHATKIT_DIR = PROJECT_DIR.parent / "chatkit"
CHATKIT_DIST_DIR = CHATKIT_DIR / "dist"
CHATKIT_SRC_DIR = CHATKIT_DIR / "src"
SQLITE_PATH = DATA_DIR / "store.db"

PORT = int(os.environ.get("PORT", "19876"))
MODEL = os.environ.get("MODEL", "claude-sonnet-4-5-20250929")
CODEMODE_MODEL = os.environ.get("CODEMODE_MODEL", "claude-sonnet-4-5-20250929")
ROOT_PATH = os.environ.get("ROOT_PATH", "")
MAX_MONTY_DURATION_SECS = 30
PYDANTIC_AI_MODEL = os.environ.get("PYDANTIC_AI_MODEL", "openai:gpt-5.4")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "moonshotai:kimi-k2.6")
MAX_AGENT_TURNS = 25
SESSION_TTL_MINUTES = 30
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_MODEL = os.environ.get("TEMPORAL_MODEL", MODEL)
PARALLEL_MODEL = os.environ.get("PARALLEL_MODEL", MODEL)
PYDANTIC_GRAPH_MODEL = os.environ.get("PYDANTIC_GRAPH_MODEL", MODEL)
GRAPH_STATE_MODEL = os.environ.get("GRAPH_STATE_MODEL", MODEL)
