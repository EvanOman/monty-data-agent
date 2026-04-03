import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .agent.client import AgentClient
from .api.routes import router
from .codemode.client import CodeModeClient
from .config import CHATKIT_DIST_DIR, CHATKIT_SRC_DIR, DATA_DIR, ROOT_PATH, SQLITE_PATH, STATIC_DIR
from .engine.duckdb_store import DuckDBStore
from .engine.sqlite_store import SQLiteStore
from .graph_state.client import GraphStateClient
from .parallel.client import ParallelClient
from .pydantic_agent.client import PydanticAIClient
from .pydantic_graph_mode.client import PydanticGraphClient
from .temporal.client import TemporalClient

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

    logger.info("Initializing Code Mode client...")
    codemode_client = CodeModeClient(sqlite_store)

    logger.info("Initializing Pydantic AI client...")
    pydantic_ai_client = PydanticAIClient(duckdb_store, sqlite_store)
    pydantic_ai_client.set_schema_context(schema_context)

    logger.info("Initializing Temporal client...")
    temporal_client = TemporalClient(sqlite_store)
    temporal_client.set_schema_context(schema_context)

    logger.info("Initializing Parallel client...")
    parallel_client = ParallelClient(duckdb_store, sqlite_store)
    parallel_client.set_schema_context(schema_context)

    logger.info("Initializing Pydantic Graph client...")
    pydantic_graph_client = PydanticGraphClient(duckdb_store, sqlite_store)
    pydantic_graph_client.set_schema_context(schema_context)

    logger.info("Initializing Graph State client...")
    graph_state_client = GraphStateClient(duckdb_store, sqlite_store)
    graph_state_client.set_schema_context(schema_context)

    app.state.duckdb_store = duckdb_store
    app.state.sqlite_store = sqlite_store
    app.state.agent_client = agent_client
    app.state.codemode_client = codemode_client
    app.state.pydantic_ai_client = pydantic_ai_client
    app.state.temporal_client = temporal_client
    app.state.parallel_client = parallel_client
    app.state.pydantic_graph_client = pydantic_graph_client
    app.state.graph_state_client = graph_state_client

    logger.info("Startup complete — ready to serve")
    yield

    # Shutdown
    logger.info("Shutting down...")
    await agent_client.close()
    await codemode_client.close()
    await pydantic_ai_client.close()
    await temporal_client.close()
    await parallel_client.close()
    await pydantic_graph_client.close()
    await graph_state_client.close()
    await sqlite_store.close()
    duckdb_store.close()


app = FastAPI(title="Data Analysis Agent", root_path=ROOT_PATH, lifespan=lifespan)
app.include_router(router)

# Mount chatkit assets from the sibling repo so the frontend can import them.
# In production these would come from a proper package/CDN instead.
app.mount("/chatkit/dist", StaticFiles(directory=str(CHATKIT_DIST_DIR)), name="chatkit-dist")
app.mount("/chatkit/src", StaticFiles(directory=str(CHATKIT_SRC_DIR)), name="chatkit-src")

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
