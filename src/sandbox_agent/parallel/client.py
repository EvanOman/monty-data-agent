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
from ..planning import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt
from ..planning.helpers import (
    chunk_text,
    format_history_prompt,
    format_result_summary,
    get_text,
    parse_plan_json,
    strip_code_fences,
)
from ..planning.models import SubTaskResult
from ..shared import ChatEvent
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

        # Emit artifacts
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

        for text_chunk in chunk_text(synthesis):
            yield ChatEvent(type="text", data=text_chunk)

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

    async def _plan(self, question: str, history: list[dict]):
        """Call the LLM to decompose the question into subtasks."""
        plan_prompt = build_plan_prompt(self._schema_context)
        history_prefix = format_history_prompt(history)

        response = await self._anthropic.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=plan_prompt,
            messages=[{"role": "user", "content": f"{history_prefix}{question}"}],
        )

        return parse_plan_json(get_text(response))

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

        code = strip_code_fences(get_text(response))
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

        summary = format_result_summary(artifact["id"], result)
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
        return get_text(response)

    async def close(self) -> None:
        pass
