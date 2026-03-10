"""Code Mode client — bridges the MCP server with the Anthropic API for the web UI."""

import json
import logging
import sys
import time

import anthropic
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ..config import CODEMODE_MODEL
from ..shared import ChatEvent

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 25


class CodeModeClient:
    """Spawns the Code Mode MCP server, uses Anthropic API for LLM, yields ChatEvents."""

    def __init__(self, sqlite_store) -> None:
        self._sqlite = sqlite_store
        self._anthropic = anthropic.AsyncAnthropic()

    async def chat(self, conversation_id: str, user_message: str):
        """Async generator yielding ChatEvent objects."""
        t_start = time.time()
        timing_spans = []
        tool_timings = []
        pending_artifacts = []
        turn_count = 0
        tool_call_count = 0

        yield ChatEvent(type="status", data="Starting Code Mode...")

        # Load conversation history
        history = await self._sqlite.get_messages(conversation_id)
        if history and history[-1]["content"] == user_message:
            history = history[:-1]

        # Build messages for Anthropic API
        messages = []
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        # Spawn MCP server as subprocess and connect
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sandbox_agent.codemode"],
        )

        from .prompts import SYSTEM_PROMPT

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Get tool definitions from MCP server
                tools_result = await session.list_tools()
                tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema,
                    }
                    for t in tools_result.tools
                ]

                # Tool-calling loop
                for _ in range(MAX_TOOL_ROUNDS):
                    turn_count += 1
                    t_llm_start = time.time()

                    yield ChatEvent(type="status", data="Thinking...")

                    # Call Anthropic API with streaming
                    response_text_parts = []
                    tool_use_blocks = []

                    async with self._anthropic.messages.stream(
                        model=CODEMODE_MODEL,
                        max_tokens=4096,
                        system=SYSTEM_PROMPT,
                        messages=messages,
                        tools=tools,
                    ) as stream:
                        async for event in stream:
                            if event.type == "content_block_start":
                                if event.content_block.type == "text":
                                    pass  # Will accumulate via deltas
                                elif event.content_block.type == "tool_use":
                                    tool_use_blocks.append(
                                        {
                                            "id": event.content_block.id,
                                            "name": event.content_block.name,
                                            "input_json": "",
                                        }
                                    )
                            elif event.type == "content_block_delta":
                                if event.delta.type == "text_delta":
                                    yield ChatEvent(type="text", data=event.delta.text)
                                    response_text_parts.append(event.delta.text)
                                elif event.delta.type == "input_json_delta":
                                    if tool_use_blocks:
                                        tool_use_blocks[-1]["input_json"] += (
                                            event.delta.partial_json
                                        )

                        final_message = await stream.get_final_message()

                    t_llm_end = time.time()
                    timing_spans.append(
                        {
                            "name": f"LLM Turn {turn_count}",
                            "type": "llm",
                            "start_ms": round((t_llm_start - t_start) * 1000),
                            "duration_ms": round((t_llm_end - t_llm_start) * 1000),
                        }
                    )

                    # Check if we're done (no tool use)
                    if final_message.stop_reason == "end_turn":
                        break

                    # Process tool calls
                    if final_message.stop_reason == "tool_use":
                        # Build assistant message content for history
                        assistant_content = []
                        for block in final_message.content:
                            if block.type == "text":
                                assistant_content.append(
                                    {
                                        "type": "text",
                                        "text": block.text,
                                    }
                                )
                            elif block.type == "tool_use":
                                assistant_content.append(
                                    {
                                        "type": "tool_use",
                                        "id": block.id,
                                        "name": block.name,
                                        "input": block.input,
                                    }
                                )
                        messages.append({"role": "assistant", "content": assistant_content})

                        # Execute each tool call
                        tool_results = []
                        for block in final_message.content:
                            if block.type != "tool_use":
                                continue
                            tool_call_count += 1
                            tool_name = block.name
                            tool_input = block.input

                            if tool_name == "execute":
                                code = tool_input.get("code", "")
                                yield ChatEvent(type="code", data=code)
                                yield ChatEvent(type="status", data="Running code in sandbox...")

                            t_tool_start = time.time()
                            mcp_result = await session.call_tool(tool_name, tool_input)
                            t_tool_end = time.time()

                            tool_result_text = ""
                            for content in mcp_result.content:
                                if hasattr(content, "text"):
                                    tool_result_text += content.text

                            tool_timings.append(
                                {
                                    "name": tool_name,
                                    "duration_ms": round((t_tool_end - t_tool_start) * 1000),
                                    "has_error": mcp_result.isError or False,
                                }
                            )
                            timing_spans.append(
                                {
                                    "name": f"Tool: {tool_name}",
                                    "type": "tool",
                                    "start_ms": round((t_tool_start - t_start) * 1000),
                                    "duration_ms": round((t_tool_end - t_tool_start) * 1000),
                                }
                            )

                            # Save artifact for execute calls
                            if tool_name == "execute":
                                code = tool_input.get("code", "")
                                try:
                                    result_data = json.loads(tool_result_text)
                                    artifact = await self._sqlite.save_artifact(
                                        conversation_id=conversation_id,
                                        message_id=None,
                                        code=code,
                                        result_json=result_data.get("data"),
                                        result_type=result_data.get("type"),
                                        error=result_data.get("error"),
                                    )
                                    pending_artifacts.append(artifact)
                                except (json.JSONDecodeError, AttributeError):
                                    artifact = await self._sqlite.save_artifact(
                                        conversation_id=conversation_id,
                                        message_id=None,
                                        code=code,
                                        error=tool_result_text,
                                    )
                                    pending_artifacts.append(artifact)

                                yield ChatEvent(type="status", data="Analyzing results...")

                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": tool_result_text,
                                }
                            )

                        messages.append({"role": "user", "content": tool_results})
                    else:
                        # Unknown stop reason, break
                        break

        # Emit artifact events
        for artifact in pending_artifacts:
            artifact_data = {
                "id": artifact["id"],
                "code": artifact["code"],
                "result_json": artifact.get("result_json"),
                "result_type": artifact.get("result_type"),
                "error": artifact.get("error"),
            }
            yield ChatEvent(type="artifact", data=json.dumps(artifact_data))

        t_end = time.time()
        total_ms = round((t_end - t_start) * 1000)

        timing_data = {
            "total_ms": total_ms,
            "turns": turn_count,
            "tool_calls": tool_call_count,
            "spans": timing_spans,
            "tool_details": tool_timings,
        }

        yield ChatEvent(
            type="done",
            data=json.dumps(
                {
                    "artifacts": [a["id"] for a in pending_artifacts],
                    "timing": timing_data,
                }
            ),
        )

    async def close(self) -> None:
        pass
