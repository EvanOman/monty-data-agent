import pytest
import pytest_asyncio

from sandbox_agent.data.duckdb_store import DuckDBStore
from sandbox_agent.data.sqlite_store import SQLiteStore
from sandbox_agent.sandbox.functions import ExternalFunctions


@pytest.fixture
def duckdb_store():
    store = DuckDBStore()
    # Load a small test table directly instead of remote URLs
    store._conn.execute(
        "CREATE TABLE test_table AS SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) AS t(id, name)"
    )
    yield store
    store.close()


@pytest_asyncio.fixture
async def sqlite_store(tmp_path):
    store = SQLiteStore(str(tmp_path / "test.db"))
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def ext_functions(duckdb_store):
    return ExternalFunctions(duckdb_store)
