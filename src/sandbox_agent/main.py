import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .agent.client import AgentClient
from .api.routes import router
from .config import DATA_DIR, ROOT_PATH, SQLITE_PATH, STATIC_DIR
from .data.duckdb_store import DuckDBStore
from .data.sqlite_store import SQLiteStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Initializing DuckDB store...")
    duckdb_store = DuckDBStore()
    await duckdb_store.load_datasets()

    logger.info("Initializing SQLite store...")
    sqlite_store = SQLiteStore(str(SQLITE_PATH))
    await sqlite_store.initialize()

    logger.info("Initializing agent client...")
    agent_client = AgentClient(duckdb_store, sqlite_store)
    schema_context = duckdb_store.get_schema_context()
    agent_client.set_schema_context(schema_context)

    app.state.duckdb_store = duckdb_store
    app.state.sqlite_store = sqlite_store
    app.state.agent_client = agent_client

    logger.info("Startup complete — ready to serve")
    yield

    # Shutdown
    logger.info("Shutting down...")
    await agent_client.close()
    await sqlite_store.close()
    duckdb_store.close()


app = FastAPI(title="Data Analysis Agent", root_path=ROOT_PATH, lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
