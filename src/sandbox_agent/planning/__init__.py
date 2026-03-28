"""Shared planning infrastructure for Plan-Execute-Synthesize modes.

This module provides the common models, prompts, and orchestration protocol
used by all parallel execution backends (graphlib, pydantic-graph beta,
pydantic-graph state machine, Temporal).
"""

from .models import ExecutionPlan, SubTask, SubTaskResult
from .prompts import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt

__all__ = [
    "ExecutionPlan",
    "SubTask",
    "SubTaskResult",
    "SYNTHESIZE_SYSTEM_PROMPT",
    "build_plan_prompt",
    "build_subtask_prompt",
]
