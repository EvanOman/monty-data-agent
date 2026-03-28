"""Temporal-specific data models.

Core models (SubTask, ExecutionPlan, SubTaskResult) are re-exported from
the shared planning module. Temporal-specific activity input dataclasses
are defined here.
"""

from dataclasses import dataclass, field

# Re-export shared models so existing imports continue to work
from ..planning.models import ExecutionPlan, SubTask, SubTaskResult

__all__ = [
    "ExecutionPlan",
    "ExecuteSubtaskInput",
    "PlanInput",
    "SubTask",
    "SubTaskResult",
    "SynthesizeInput",
]


# --- Temporal activity input dataclasses (single-dataclass-per-activity pattern) ---


@dataclass
class PlanInput:
    """Input for the plan_subtasks activity."""

    question: str
    schema_context: str
    plan_system_prompt: str
    conversation_history: list[dict] = field(default_factory=list)


@dataclass
class ExecuteSubtaskInput:
    """Input for the execute_subtask activity."""

    task_id: str
    description: str
    datasets: list[str]
    predecessor_summaries: dict[str, str]
    schema_context: str
    subtask_system_prompt: str
    conversation_id: str = ""


@dataclass
class SynthesizeInput:
    """Input for the synthesize_results activity."""

    question: str
    task_summaries: dict[str, str]
    synthesize_system_prompt: str
