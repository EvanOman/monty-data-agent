"""Shared components used across all agent modes."""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from .engine.executor import ExecutionResult, execute_code
from .engine.functions import ExternalFunctions

logger = logging.getLogger(__name__)

MAX_LOAD_ROWS = 100


@dataclass
class ChatEvent:
    """An event yielded during chat streaming."""

    type: str  # "text", "code", "result", "artifact", "status", "error", "done"
    data: str = ""


# Type alias for the optional async event callback
EventCallback = Callable[[str, str], Coroutine[Any, Any, None]] | None


class ToolExecutor:
    """Encapsulates reusable tool logic for execute_code and load_result.

    Used by both AgentClient (Claude SDK) and PydanticAIClient (Pydantic AI).
    """

    def __init__(self, ext_functions: ExternalFunctions, sqlite_store) -> None:
        self._ext = ext_functions
        self._sqlite = sqlite_store

    async def run_code(
        self,
        code: str,
        conversation_id: str,
        on_event: EventCallback = None,
    ) -> tuple[str, dict, dict]:
        """Execute code in Monty sandbox, save artifact, return (summary, artifact, timing).

        Args:
            code: Python code to execute.
            conversation_id: Conversation to associate artifact with.
            on_event: Optional async callback for status events, called as on_event(type, data).

        Returns:
            Tuple of (summary_text, artifact_dict, timing_dict).
        """
        if on_event:
            await on_event("status", "Running code in sandbox...")

        t0 = time.time()
        result: ExecutionResult = await asyncio.to_thread(execute_code, code, self._ext)
        exec_ms = round((time.time() - t0) * 1000)

        artifact = await self._sqlite.save_artifact(
            conversation_id=conversation_id,
            message_id=None,
            code=code,
            monty_state=result.monty_state,
            result_json=result.output_json,
            result_type=result.output_type,
            error=result.error,
        )

        timing = {
            "name": "execute_code",
            "duration_ms": exec_ms,
            "has_error": result.error is not None,
        }

        if result.error:
            if on_event:
                await on_event("status", "Code failed, agent may retry...")
            return f"Error: {result.error}", artifact, timing

        summary = self._format_summary(artifact["id"], result)
        return summary, artifact, timing

    def _format_summary(self, uid: str, result: ExecutionResult) -> str:
        """Build the result summary text returned to the LLM."""
        if result.output_type == "table" and result.output_json:
            data = json.loads(result.output_json)
            row_count = len(data)
            cols = list(data[0].keys()) if data else []
            return f"Result UID: {uid}\nType: table\nRows: {row_count}\nColumns: {', '.join(cols)}"
        elif result.output_type == "scalar" and result.output_json:
            return f"Result UID: {uid}\nType: scalar (displayed as a metric)\nValue: {result.output_json}"
        elif result.output_type == "dict" and result.output_json:
            data = json.loads(result.output_json)
            keys = ", ".join(data.keys()) if isinstance(data, dict) else ""
            return f"Result UID: {uid}\nType: dict (displayed as key-value pairs)\nKeys: {keys}"
        elif result.output_json:
            return (
                f"Result UID: {uid}\nType: {result.output_type}\nData: {result.output_json[:200]}"
            )
        else:
            return f"Result UID: {uid}\nType: none\nValue: None"

    async def load_result(self, uid: str) -> str:
        """Load result data by UID, format as markdown table or JSON."""
        artifact = await self._sqlite.get_artifact(uid)
        if not artifact:
            return f"Error: No result found for UID {uid}"

        if artifact.get("error"):
            return f"Error in result: {artifact['error']}"

        result_json = artifact.get("result_json")
        if not result_json:
            return "Result: None"

        data = json.loads(result_json)

        if isinstance(data, list) and data and isinstance(data[0], dict):
            cols = list(data[0].keys())
            truncated = data[:MAX_LOAD_ROWS]
            header = " | ".join(cols)
            sep = " | ".join(["---"] * len(cols))
            rows_text = "\n".join(" | ".join(str(r.get(c, "")) for c in cols) for r in truncated)
            trunc_note = (
                f"\n\n(Showing {len(truncated)} of {len(data)} rows)"
                if len(data) > MAX_LOAD_ROWS
                else ""
            )
            return f"{header}\n{sep}\n{rows_text}{trunc_note}"
        else:
            return json.dumps(data, indent=2, default=str)
