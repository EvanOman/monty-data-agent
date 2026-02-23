"""Tests for the Code Mode MCP server tools."""

import json

import pytest

from sandbox_agent.codemode.registry import FUNCTION_REGISTRY, build_datasets_metadata
from sandbox_agent.codemode.server import ServerContext, search


class FakeContext:
    """Minimal stand-in for FastMCP's Context."""

    def __init__(self, server_ctx):
        self.request_context = type("RC", (), {"lifespan_context": server_ctx})()


@pytest.fixture
def server_ctx(duckdb_store, ext_functions):
    datasets = build_datasets_metadata(duckdb_store)
    return ServerContext(
        duckdb_store=duckdb_store,
        ext_functions=ext_functions,
        search_metadata={"functions": FUNCTION_REGISTRY, "datasets": datasets},
    )


@pytest.fixture
def ctx(server_ctx):
    return FakeContext(server_ctx)


# ── Registry tests ──


def test_registry_has_all_functions():
    assert set(FUNCTION_REGISTRY.keys()) == {"fetch", "count", "describe", "tables"}


def test_registry_entries_have_required_keys():
    for name, entry in FUNCTION_REGISTRY.items():
        assert "signature" in entry, f"{name} missing signature"
        assert "description" in entry, f"{name} missing description"
        assert "parameters" in entry, f"{name} missing parameters"
        assert "examples" in entry, f"{name} missing examples"


def test_build_datasets_metadata(duckdb_store):
    datasets = build_datasets_metadata(duckdb_store)
    assert "test_table" in datasets
    assert "id" in datasets["test_table"]["columns"]
    assert "name" in datasets["test_table"]["columns"]


# ── Search tool tests ──


def test_search_list_functions(ctx):
    result = search("list(functions.keys())", ctx)
    parsed = json.loads(result)
    assert "fetch" in parsed
    assert "count" in parsed


def test_search_function_details(ctx):
    result = search('functions["fetch"]', ctx)
    parsed = json.loads(result)
    assert "signature" in parsed
    assert "fetch" in parsed["signature"]


def test_search_list_datasets(ctx):
    result = search("list(datasets.keys())", ctx)
    parsed = json.loads(result)
    assert "test_table" in parsed


def test_search_dataset_schema(ctx):
    result = search('datasets["test_table"]', ctx)
    parsed = json.loads(result)
    assert "columns" in parsed
    assert "id" in parsed["columns"]


def test_search_error_handling(ctx):
    result = search("nonexistent_var", ctx)
    assert "Error" in result


# ── Execute tool tests ──


def test_execute_simple_expression(server_ctx):
    from sandbox_agent.codemode.server import execute

    ctx = FakeContext(server_ctx)
    result = execute("1 + 2", ctx)
    parsed = json.loads(result)
    assert parsed["type"] == "scalar"
    assert parsed["error"] is None
    assert json.loads(parsed["data"]) == 3


def test_execute_fetch(server_ctx):
    from sandbox_agent.codemode.server import execute

    ctx = FakeContext(server_ctx)
    result = execute('fetch("test_table")', ctx)
    parsed = json.loads(result)
    assert parsed["type"] == "table"
    assert parsed["error"] is None
    data = json.loads(parsed["data"])
    assert len(data) == 3


def test_execute_error(server_ctx):
    from sandbox_agent.codemode.server import execute

    ctx = FakeContext(server_ctx)
    result = execute("def foo(:", ctx)
    parsed = json.loads(result)
    assert parsed["error"] is not None


def test_execute_count(server_ctx):
    from sandbox_agent.codemode.server import execute

    ctx = FakeContext(server_ctx)
    result = execute('count("test_table")', ctx)
    parsed = json.loads(result)
    assert parsed["type"] == "scalar"
    assert json.loads(parsed["data"]) == 3


# ── SQLite mode support ──


@pytest.mark.asyncio
async def test_sqlite_conversation_with_mode(sqlite_store):
    conv = await sqlite_store.create_conversation(mode="codemode")
    assert conv["mode"] == "codemode"

    fetched = await sqlite_store.get_conversation(conv["id"])
    assert fetched["mode"] == "codemode"


@pytest.mark.asyncio
async def test_sqlite_conversation_default_mode(sqlite_store):
    conv = await sqlite_store.create_conversation()
    assert conv["mode"] == "standard"
