"""Tests for shared ToolExecutor and PydanticAIClient message history conversion."""

import pytest

from sandbox_agent.pydantic_agent.client import _build_message_history
from sandbox_agent.shared import ToolExecutor


@pytest.fixture
def tool_executor(duckdb_store, sqlite_store):
    from sandbox_agent.engine.functions import ExternalFunctions

    ext = ExternalFunctions(duckdb_store)
    return ToolExecutor(ext, sqlite_store)


class TestToolExecutor:
    async def test_run_code_success(self, tool_executor, sqlite_store):
        conv = await sqlite_store.create_conversation(mode="test")
        summary, artifact, timing = await tool_executor.run_code('count("test_table")', conv["id"])
        assert "Result UID:" in summary
        assert artifact["id"]
        assert artifact["result_type"] == "scalar"
        assert timing["name"] == "execute_code"
        assert timing["has_error"] is False

    async def test_run_code_error(self, tool_executor, sqlite_store):
        conv = await sqlite_store.create_conversation(mode="test")
        summary, artifact, timing = await tool_executor.run_code("undefined_var", conv["id"])
        assert summary.startswith("Error:")
        assert timing["has_error"] is True

    async def test_run_code_with_event_callback(self, tool_executor, sqlite_store):
        conv = await sqlite_store.create_conversation(mode="test")
        events = []

        async def on_event(etype, data):
            events.append((etype, data))

        await tool_executor.run_code('count("test_table")', conv["id"], on_event=on_event)
        assert any(e[0] == "status" for e in events)

    async def test_load_result_existing(self, tool_executor, sqlite_store):
        conv = await sqlite_store.create_conversation(mode="test")
        _, artifact, _ = await tool_executor.run_code('fetch("test_table")', conv["id"])
        text = await tool_executor.load_result(artifact["id"])
        assert "id" in text  # column name from test_table
        assert "name" in text

    async def test_load_result_missing(self, tool_executor):
        text = await tool_executor.load_result("nonexistent-uid")
        assert "Error" in text


class TestBuildMessageHistory:
    def test_empty_history(self):
        messages = _build_message_history([])
        assert messages == []

    def test_user_and_assistant(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]
        messages = _build_message_history(history)
        assert len(messages) == 3

        # Check types
        from pydantic_ai.messages import ModelRequest, ModelResponse

        assert isinstance(messages[0], ModelRequest)
        assert isinstance(messages[1], ModelResponse)
        assert isinstance(messages[2], ModelRequest)

        # Check content
        from pydantic_ai.messages import TextPart, UserPromptPart

        assert isinstance(messages[0].parts[0], UserPromptPart)
        assert messages[0].parts[0].content == "Hello"
        assert isinstance(messages[1].parts[0], TextPart)
        assert messages[1].parts[0].content == "Hi there"
        assert isinstance(messages[2].parts[0], UserPromptPart)
        assert messages[2].parts[0].content == "How are you?"
