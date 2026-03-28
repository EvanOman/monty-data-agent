"""Shared helper functions for Plan-Execute-Synthesize backends."""

import json
import logging
from typing import Any

from .models import ExecutionPlan, SubTask

logger = logging.getLogger(__name__)


def get_text(response: Any) -> str:
    """Extract text from an Anthropic API response, handling the content block union type."""
    block = response.content[0]
    return block.text


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1]
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")]
        stripped = stripped.strip()
    return stripped


def parse_plan_json(raw: str) -> ExecutionPlan:
    """Parse an LLM response into an ExecutionPlan.

    Handles markdown fencing, and constructs SubTask objects from the JSON.
    """
    cleaned = strip_code_fences(raw)
    parsed = json.loads(cleaned)

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


def format_result_summary(uid: str, result) -> str:
    """Build a result summary string for use by downstream tasks and synthesis."""
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


def format_history_prompt(history: list[dict]) -> str:
    """Format conversation history for inclusion in planner prompts."""
    if not history:
        return ""
    parts = ["## Conversation History\n"]
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        parts.append(f"**{role}**: {content}\n")
    parts.append("\n## Current Question\n")
    return "\n".join(parts)


def chunk_text(text: str, chunk_size: int = 40) -> list[str]:
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
