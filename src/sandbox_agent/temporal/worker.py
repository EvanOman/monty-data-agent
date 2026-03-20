"""Temporal worker process.

Run this alongside the FastAPI app to process workflow tasks:

    python -m sandbox_agent.temporal

Or via the Justfile:

    just worker
"""

import asyncio
import logging

from anthropic import AsyncAnthropic
from temporalio.client import Client
from temporalio.worker import Worker

from ..config import TEMPORAL_ADDRESS
from .activities import execute_subtask, plan_subtasks, synthesize_results
from .workflows import PlanExecuteSynthesize

logger = logging.getLogger(__name__)

TASK_QUEUE = "sandbox-agent"

# Shared stores and clients, set during worker startup, accessed by activities
_duckdb_store = None
_sqlite_store = None
_anthropic_client: AsyncAnthropic | None = None


def get_shared_stores():
    """Get the shared DuckDB and SQLite stores (set by the worker on startup)."""
    if _duckdb_store is None or _sqlite_store is None:
        raise RuntimeError("Worker stores not initialized — is the worker running?")
    return _duckdb_store, _sqlite_store


def get_shared_anthropic() -> AsyncAnthropic:
    """Get the shared Anthropic client (set by the worker on startup)."""
    if _anthropic_client is None:
        raise RuntimeError("Anthropic client not initialized — is the worker running?")
    return _anthropic_client


async def run_worker():
    """Start the Temporal worker with all activities and workflows registered."""
    global _duckdb_store, _sqlite_store, _anthropic_client

    from ..config import DATA_DIR, SQLITE_PATH
    from ..engine.duckdb_store import DuckDBStore
    from ..engine.sqlite_store import SQLiteStore

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Initialize shared stores
    logger.info("Initializing DuckDB store for worker...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _duckdb_store = DuckDBStore()
    await _duckdb_store.load_datasets()

    logger.info("Initializing SQLite store for worker...")
    _sqlite_store = SQLiteStore(str(SQLITE_PATH))
    await _sqlite_store.initialize()

    # Initialize shared Anthropic client
    logger.info("Initializing Anthropic client for worker...")
    _anthropic_client = AsyncAnthropic()

    # Connect to Temporal
    logger.info("Connecting to Temporal at %s...", TEMPORAL_ADDRESS)
    client = await Client.connect(TEMPORAL_ADDRESS)

    # Create and run worker
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PlanExecuteSynthesize],
        activities=[plan_subtasks, execute_subtask, synthesize_results],
        max_concurrent_activities=5,
    )

    logger.info("Temporal worker started on queue '%s'", TASK_QUEUE)
    await worker.run()


def main():
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
