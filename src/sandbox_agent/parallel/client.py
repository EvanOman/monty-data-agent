"""Parallel Plan-Execute-Synthesize client using in-process DAG execution.

Same architecture as the Temporal mode (plan → parallel execute → synthesize)
but runs entirely in-process using graphlib + asyncio.gather. No external
server, no separate worker, no Docker containers.
"""

import asyncio
import json
import logging
import time

from anthropic import AsyncAnthropic

from ..config import PARALLEL_MODEL as MODEL
from ..engine.executor import execute_code
from ..engine.functions import ExternalFunctions
from ..shared import ChatEvent
from ..temporal.models import ExecutionPlan, SubTask, SubTaskResult
from ..temporal.prompts import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt
from .dag import execute_dag

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10


class ParallelClient:
    """Chat client that runs Plan-Execute-Synthesize in-process."""

    def __init__(self, duckdb_store, sqlite_store) -> None:
        self._duckdb = duckdb_store
        self._sqlite = sqlite_store
        self._anthropic = AsyncAnthropic()
        self._schema_context: str = ""

    def set_schema_context(self, ctx: str) -> None:
        self._schema_context = ctx

    async def chat(self, conversation_id: str, user_message: str):
        """Yield ChatEvent objects as the pipeline processes the message."""
        t_start = time.time()

        yield ChatEvent(type="status", data="Planning analysis...")

        # Load conversation history
        history = await self._sqlite.get_messages(conversation_id)
        if history and history[-1]["content"] == user_message:
            history = history[:-1]
        history = history[-MAX_HISTORY_MESSAGES:]

        # Phase 1: Plan
        try:
            plan = await self._plan(user_message, history)
        except Exception as e:
            logger.exception("Planning failed")
            yield ChatEvent(type="error", data=f"Planning failed: {e}")
            yield ChatEvent(
                type="done",
                data=json.dumps({"artifacts": [], "timing": {"total_ms": 0, "error": str(e)}}),
            )
            return

        task_count = len(plan.tasks)
        batches = plan.batches()
        yield ChatEvent(
            type="status",
            data=f"Executing {task_count} sub-tasks in {len(batches)} batches...",
        )

        # Phase 2: Execute (parallel DAG)
        subtask_prompt = build_subtask_prompt(self._schema_context)
        artifact_ids: list[str] = []

        async def run_task(
            task_id: str,
            description: str,
            datasets: list[str],
            predecessor_summaries: dict[str, str],
        ) -> SubTaskResult:
            return await self._execute_subtask(
                task_id,
                description,
                datasets,
                predecessor_summaries,
                subtask_prompt,
                conversation_id,
            )

        all_results = await execute_dag(plan, run_task)

        # Emit artifacts as they come back
        for task_id, result in all_results.items():
            if result.error:
                yield ChatEvent(type="status", data=f"Task '{task_id}' failed: {result.error}")
            else:
                yield ChatEvent(type="status", data=f"Completed: {task_id}")

            if result.artifact_uid:
                artifact_ids.append(result.artifact_uid)
                artifact = await self._sqlite.get_artifact(result.artifact_uid)
                if artifact:
                    yield ChatEvent(type="code", data=artifact.get("code", ""))
                    yield ChatEvent(
                        type="artifact",
                        data=json.dumps(
                            {
                                "id": result.artifact_uid,
                                "code": artifact.get("code", ""),
                                "result_json": artifact.get("result_json"),
                                "result_type": artifact.get("result_type"),
                                "error": artifact.get("error"),
                            }
                        ),
                    )

        # Phase 3: Synthesize
        yield ChatEvent(type="status", data="Synthesizing results...")

        try:
            synthesis = await self._synthesize(user_message, all_results)
        except Exception as e:
            logger.exception("Synthesis failed")
            synthesis = f"Error during synthesis: {e}"

        # Stream synthesis text
        words = synthesis.split(" ")
        chunk = []
        for word in words:
            chunk.append(word)
            if len(" ".join(chunk)) >= 40:
                yield ChatEvent(type="text", data=" ".join(chunk) + " ")
                chunk = []
        if chunk:
            yield ChatEvent(type="text", data=" ".join(chunk))

        t_end = time.time()
        total_ms = round((t_end - t_start) * 1000)

        yield ChatEvent(
            type="done",
            data=json.dumps(
                {
                    "artifacts": artifact_ids,
                    "timing": {
                        "total_ms": total_ms,
                        "turns": task_count + 2,
                        "tool_calls": task_count,
                        "mode": "parallel",
                        "plan": [
                            {
                                "task_id": t.task_id,
                                "description": t.description,
                                "datasets": t.datasets,
                                "depends_on": t.depends_on,
                            }
                            for t in plan.tasks
                        ],
                        "spans": [],
                    },
                }
            ),
        )

    async def _plan(self, question: str, history: list[dict]) -> ExecutionPlan:
        """Call the LLM to decompose the question into subtasks."""
        plan_prompt = build_plan_prompt(self._schema_context)

        parts = []
        if history:
            parts.append("## Conversation History\n")
            for msg in history:
                parts.append(f"**{msg.get('role', 'user')}**: {msg.get('content', '')}\n")
            parts.append("\n## Current Question\n")
        parts.append(question)

        response = await self._anthropic.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=plan_prompt,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            if raw.endswith("```"):
                raw = raw[: raw.rfind("```")]
            raw = raw.strip()

        parsed = json.loads(raw)
        tasks = [
            SubTask(
                task_id=t["task_id"],
                description=t["description"],
                datasets=t.get("datasets", []),
                depends_on=t.get("depends_on", []),
            )
            for t in parsed["tasks"]
        ]

        plan = ExecutionPlan(tasks=tasks)
        logger.info("Plan: %d tasks in %d batches", len(tasks), len(plan.batches()))
        return plan

    async def _execute_subtask(
        self,
        task_id: str,
        description: str,
        datasets: list[str],
        predecessor_summaries: dict[str, str],
        subtask_prompt: str,
        conversation_id: str,
    ) -> SubTaskResult:
        """Execute a single subtask: LLM generates code, sandbox runs it."""
        ext_functions = ExternalFunctions(self._duckdb)

        parts = [f"## Task\n{description}"]
        if predecessor_summaries:
            parts.append("\n## Results from previous steps\n")
            for dep_id, summary in predecessor_summaries.items():
                parts.append(f"### {dep_id}\n{summary}\n")
        parts.append(
            "\nWrite Python code to accomplish this task. Return ONLY the code, no explanation."
        )

        response = await self._anthropic.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=subtask_prompt,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )

        code = response.content[0].text.strip()
        if code.startswith("```"):
            code = code.split("\n", 1)[1]
            if code.endswith("```"):
                code = code[: code.rfind("```")]
            code = code.strip()

        logger.info("Subtask %s: executing code (%d chars)", task_id, len(code))
        result = await asyncio.to_thread(execute_code, code, ext_functions)

        artifact = await self._sqlite.save_artifact(
            conversation_id=conversation_id,
            message_id=None,
            code=code,
            monty_state=result.monty_state,
            result_json=result.output_json,
            result_type=result.output_type,
            error=result.error,
        )

        if result.error:
            return SubTaskResult(
                task_id=task_id,
                artifact_uid=artifact["id"],
                summary=f"Error: {result.error}",
                result_type="error",
                error=result.error,
            )

        summary = _format_summary(artifact["id"], result)
        return SubTaskResult(
            task_id=task_id,
            artifact_uid=artifact["id"],
            summary=summary,
            result_type=result.output_type,
        )

    async def _synthesize(self, question: str, results: dict[str, SubTaskResult]) -> str:
        """Combine all subtask results into a coherent response."""
        parts = [f"## Original Question\n{question}\n", "## Analysis Results\n"]
        for task_id, result in results.items():
            parts.append(f"### {task_id}\n{result.summary}\n")
        parts.append(
            "\nSynthesize these results into a clear, coherent response "
            "that directly answers the user's question."
        )

        response = await self._anthropic.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYNTHESIZE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
        return response.content[0].text

    async def close(self) -> None:
        pass


def _format_summary(uid: str, result) -> str:
    """Build a result summary string for use by downstream tasks."""
    if result.output_type == "table" and result.output_json:
        data = json.loads(result.output_json)
        row_count = len(data)
        cols = list(data[0].keys()) if data else []
        preview = json.dumps(data[:5], indent=2) if data else "[]"
        return (
            f"Result UID: {uid}\nType: table\nRows: {row_count}\n"
            f"Columns: {', '.join(cols)}\nPreview:\n{preview}"
        )
    elif result.output_type == "scalar" and result.output_json:
        return f"Result UID: {uid}\nType: scalar\nValue: {result.output_json}"
    elif result.output_type == "dict" and result.output_json:
        return f"Result UID: {uid}\nType: dict\nData: {result.output_json}"
    elif result.output_json:
        return f"Result UID: {uid}\nType: {result.output_type}\nData: {result.output_json[:500]}"
    else:
        return f"Result UID: {uid}\nType: none"
