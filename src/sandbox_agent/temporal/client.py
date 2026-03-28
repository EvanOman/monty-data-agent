"""Temporal client — integrates the Plan-Execute-Synthesize workflow with the chat interface.

Follows the same async generator pattern as AgentClient, CodeModeClient, and PydanticAIClient:
yields ChatEvent objects that the SSE endpoint streams to the frontend.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta

from temporalio.client import Client, WorkflowHandle

from ..config import TEMPORAL_ADDRESS
from ..shared import ChatEvent
from .prompts import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt
from .worker import TASK_QUEUE

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10


class TemporalClient:
    """Chat client that orchestrates analysis via Temporal workflows."""

    def __init__(self, sqlite_store) -> None:
        self._sqlite = sqlite_store
        self._schema_context: str = ""
        self._temporal_client: Client | None = None

    def set_schema_context(self, ctx: str) -> None:
        self._schema_context = ctx

    async def _ensure_connected(self) -> Client:
        if self._temporal_client is None:
            self._temporal_client = await Client.connect(TEMPORAL_ADDRESS)
        return self._temporal_client

    async def chat(self, conversation_id: str, user_message: str):
        """Yield ChatEvent objects as the workflow processes the message.

        Starts a Temporal workflow, polls for progress, and streams
        results back as ChatEvents matching the existing SSE protocol.
        """
        t_start = time.time()

        yield ChatEvent(type="status", data="Connecting to orchestrator...")

        try:
            client = await self._ensure_connected()
        except Exception as e:
            logger.error("Failed to connect to Temporal: %s", e)
            yield ChatEvent(type="error", data=f"Temporal connection failed: {e}")
            yield ChatEvent(
                type="done",
                data=json.dumps({"artifacts": [], "timing": {"total_ms": 0, "error": str(e)}}),
            )
            return

        # Load conversation history (same pattern as AgentClient/PydanticAIClient)
        history = await self._sqlite.get_messages(conversation_id)
        # Deduplicate — the route handler already persisted the current user message
        if history and history[-1]["content"] == user_message:
            history = history[:-1]
        # Keep bounded
        history = history[-MAX_HISTORY_MESSAGES:]
        # Serialize to simple dicts for Temporal
        conversation_history = [{"role": msg["role"], "content": msg["content"]} for msg in history]

        # Build prompts
        plan_prompt = build_plan_prompt(self._schema_context)
        subtask_prompt = build_subtask_prompt(self._schema_context)

        yield ChatEvent(type="status", data="Planning analysis...")

        # Start the workflow
        workflow_id = f"chat-{conversation_id}-{uuid.uuid4().hex[:8]}"

        try:
            handle: WorkflowHandle = await client.start_workflow(
                "PlanExecuteSynthesize",
                args=[
                    user_message,
                    self._schema_context,
                    plan_prompt,
                    subtask_prompt,
                    SYNTHESIZE_SYSTEM_PROMPT,
                    conversation_id,
                    conversation_history,
                ],
                id=workflow_id,
                task_queue=TASK_QUEUE,
                execution_timeout=timedelta(minutes=5),
            )
        except Exception as e:
            logger.exception("Failed to start Temporal workflow")
            yield ChatEvent(type="error", data=str(e))
            yield ChatEvent(
                type="done",
                data=json.dumps({"artifacts": [], "timing": {"total_ms": 0, "error": str(e)}}),
            )
            return

        # Poll workflow progress and emit SSE events as subtasks complete
        emitted_tasks = 0
        try:
            while True:
                try:
                    progress = await handle.query("get_progress")
                except Exception:
                    # Query may fail if workflow just started — retry
                    await asyncio.sleep(1)
                    continue

                status = progress.get("status", "")

                # Emit newly completed tasks
                completed = progress.get("completed_tasks", [])
                for task_info in completed[emitted_tasks:]:
                    task_id = task_info.get("task_id", "")
                    error = task_info.get("error")
                    if error:
                        yield ChatEvent(type="status", data=f"Task '{task_id}' failed: {error}")
                    else:
                        yield ChatEvent(type="status", data=f"Completed: {task_id}")
                emitted_tasks = len(completed)

                # Emit plan info once available
                plan = progress.get("plan", [])
                if plan and emitted_tasks == 0 and status == "executing":
                    task_count = len(plan)
                    yield ChatEvent(
                        type="status",
                        data=f"Executing {task_count} sub-tasks...",
                    )

                if status in ("synthesizing", "done"):
                    if status == "synthesizing":
                        yield ChatEvent(type="status", data="Synthesizing results...")
                    break

                await asyncio.sleep(1)

        except Exception as e:
            logger.warning("Progress polling failed, waiting for result: %s", e)

        # Wait for final result
        try:
            result = await handle.result()
        except Exception as e:
            logger.exception("Temporal workflow failed")
            yield ChatEvent(type="error", data=str(e))
            yield ChatEvent(
                type="done",
                data=json.dumps({"artifacts": [], "timing": {"total_ms": 0, "error": str(e)}}),
            )
            return

        # Extract results
        plan = result.get("plan", [])
        results = result.get("results", {})
        synthesis = result.get("synthesis", "")

        task_count = len(plan)
        parallel_batches = _count_batches(plan)
        yield ChatEvent(
            type="status",
            data=f"Executed {task_count} sub-tasks in {parallel_batches} parallel batches",
        )

        # Emit artifacts for each sub-task result
        artifact_ids = []
        for _task_id, task_result in results.items():
            uid = task_result.get("artifact_uid", "")
            if uid:
                artifact_ids.append(uid)
                artifact = await self._sqlite.get_artifact(uid)
                if artifact:
                    yield ChatEvent(type="code", data=artifact.get("code", ""))
                    artifact_data = {
                        "id": uid,
                        "code": artifact.get("code", ""),
                        "result_json": artifact.get("result_json"),
                        "result_type": artifact.get("result_type"),
                        "error": artifact.get("error"),
                    }
                    yield ChatEvent(type="artifact", data=json.dumps(artifact_data))

        # Stream the synthesis as text
        for chunk in _chunk_text(synthesis, chunk_size=40):
            yield ChatEvent(type="text", data=chunk)

        t_end = time.time()
        total_ms = round((t_end - t_start) * 1000)

        timing_data = {
            "total_ms": total_ms,
            "turns": task_count + 2,  # plan + N subtasks + synthesize
            "tool_calls": task_count,
            "mode": "temporal",
            "plan": plan,
            "spans": [],
        }

        yield ChatEvent(
            type="done",
            data=json.dumps({"artifacts": artifact_ids, "timing": timing_data}),
        )

    async def close(self) -> None:
        pass


def _count_batches(plan: list[dict]) -> int:
    """Count how many parallel batches the plan requires."""
    completed: set[str] = set()
    remaining = list(plan)
    batch_count = 0

    while remaining:
        ready = [t for t in remaining if all(d in completed for d in t.get("depends_on", []))]
        if not ready:
            batch_count += 1
            break
        batch_count += 1
        completed.update(t["task_id"] for t in ready)
        remaining = [t for t in remaining if t["task_id"] not in completed]

    return batch_count


def _chunk_text(text: str, chunk_size: int = 40) -> list[str]:
    """Split text into chunks for simulated streaming."""
    words = text.split(" ")
    chunks = []
    current = []
    current_len = 0

    for word in words:
        current.append(word)
        current_len += len(word) + 1
        if current_len >= chunk_size:
            chunks.append(" ".join(current) + " ")
            current = []
            current_len = 0

    if current:
        chunks.append(" ".join(current))

    return chunks
