"""Temporal activities for the Plan-Execute-Synthesize pipeline.

Each activity is a unit of work executed by a Temporal worker. Activities can
be retried independently on failure.
"""

import asyncio
import json
import logging

from temporalio import activity

from ..config import TEMPORAL_MODEL as MODEL
from ..engine.executor import execute_code
from ..engine.functions import ExternalFunctions
from .models import (
    ExecuteSubtaskInput,
    ExecutionPlan,
    PlanInput,
    SubTask,
    SubTaskResult,
    SynthesizeInput,
)

logger = logging.getLogger(__name__)


@activity.defn
async def plan_subtasks(input: PlanInput) -> ExecutionPlan:
    """Decompose a user question into a DAG of independent sub-tasks."""
    from .worker import get_shared_anthropic

    client = get_shared_anthropic()

    # Build user prompt with conversation history for context
    parts = []
    if input.conversation_history:
        parts.append("## Conversation History\n")
        for msg in input.conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts.append(f"**{role}**: {content}\n")
        parts.append("\n## Current Question\n")
    parts.append(input.question)

    activity.heartbeat("calling LLM for planning...")
    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=input.plan_system_prompt,
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fencing if the model wraps it
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()

    parsed = json.loads(raw)

    tasks = []
    for t in parsed["tasks"]:
        tasks.append(
            SubTask(
                task_id=t["task_id"],
                description=t["description"],
                datasets=t.get("datasets", []),
                depends_on=t.get("depends_on", []),
            )
        )

    plan = ExecutionPlan(tasks=tasks)
    logger.info("Plan created with %d tasks in %d batches", len(tasks), len(plan.batches()))
    return plan


@activity.defn
async def execute_subtask(input: ExecuteSubtaskInput) -> SubTaskResult:
    """Execute a single sub-task: LLM generates code, Monty runs it."""
    from .worker import get_shared_anthropic, get_shared_stores

    duckdb_store, sqlite_store = get_shared_stores()
    ext_functions = ExternalFunctions(duckdb_store)
    client = get_shared_anthropic()

    # Build the user prompt with context from predecessors
    parts = [f"## Task\n{input.description}"]

    if input.predecessor_summaries:
        parts.append("\n## Results from previous steps\n")
        for dep_id, summary in input.predecessor_summaries.items():
            parts.append(f"### {dep_id}\n{summary}\n")

    parts.append(
        "\nWrite Python code to accomplish this task. Return ONLY the code, no explanation."
    )

    user_prompt = "\n".join(parts)

    activity.heartbeat("calling LLM for code generation...")
    response = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=input.subtask_system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    code = response.content[0].text.strip()
    # Strip markdown fencing if present
    if code.startswith("```"):
        code = code.split("\n", 1)[1]
        if code.endswith("```"):
            code = code[: code.rfind("```")]
        code = code.strip()

    logger.info("Subtask %s: executing code (%d chars)", input.task_id, len(code))

    # Execute in Monty sandbox
    activity.heartbeat("executing code in sandbox...")
    result = await asyncio.to_thread(execute_code, code, ext_functions)

    # Save artifact with real conversation_id
    activity.heartbeat("saving artifact...")
    artifact = await sqlite_store.save_artifact(
        conversation_id=input.conversation_id or "temporal",
        message_id=None,
        code=code,
        monty_state=result.monty_state,
        result_json=result.output_json,
        result_type=result.output_type,
        error=result.error,
    )

    if result.error:
        logger.warning("Subtask %s failed: %s", input.task_id, result.error)
        return SubTaskResult(
            task_id=input.task_id,
            artifact_uid=artifact["id"],
            summary=f"Error: {result.error}",
            result_type="error",
            error=result.error,
        )

    # Build summary for downstream tasks
    summary = _format_summary(artifact["id"], result)
    logger.info("Subtask %s completed: %s", input.task_id, result.output_type)

    return SubTaskResult(
        task_id=input.task_id,
        artifact_uid=artifact["id"],
        summary=summary,
        result_type=result.output_type,
    )


@activity.defn
async def synthesize_results(input: SynthesizeInput) -> str:
    """Combine all sub-task results into a coherent response."""
    from .worker import get_shared_anthropic

    client = get_shared_anthropic()

    parts = [f"## Original Question\n{input.question}\n", "## Analysis Results\n"]
    for task_id, summary in input.task_summaries.items():
        parts.append(f"### {task_id}\n{summary}\n")

    parts.append(
        "\nSynthesize these results into a clear, coherent response "
        "that directly answers the user's question."
    )

    activity.heartbeat("calling LLM for synthesis...")
    response = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=input.synthesize_system_prompt,
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )

    return response.content[0].text


def _format_summary(uid: str, result) -> str:
    """Build a result summary string for use by downstream tasks."""
    if result.output_type == "table" and result.output_json:
        data = json.loads(result.output_json)
        row_count = len(data)
        cols = list(data[0].keys()) if data else []
        # Include first few rows for downstream context
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
