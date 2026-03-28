"""Pydantic-graph nodes for the Plan-Execute-Synthesize pipeline.

Uses pydantic-graph's original BaseNode API to model the pipeline as a
state machine: PlanNode -> ExecuteBatchNode -> (loop) -> SynthesizeNode -> End.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic
from pydantic_graph import BaseNode, End, GraphRunContext

from ..engine.executor import execute_code
from ..engine.functions import ExternalFunctions
from ..planning import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt
from ..planning.helpers import format_result_summary, parse_plan_json, strip_code_fences
from ..planning.models import ExecutionPlan, SubTask, SubTaskResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    """Mutable state threaded through the graph execution."""

    question: str
    schema_context: str
    conversation_id: str
    conversation_history: list[dict]
    plan: ExecutionPlan | None = None
    results: dict[str, SubTaskResult] = field(default_factory=dict)


@dataclass
class PipelineDeps:
    """Immutable dependencies injected into every node."""

    anthropic: AsyncAnthropic
    duckdb_store: Any
    sqlite_store: Any
    model: str = "claude-sonnet-4-5-20250929"


@dataclass
class PlanNode(BaseNode[PipelineState, PipelineDeps, str]):
    """Calls LLM to decompose the user question into an ExecutionPlan."""

    async def run(
        self, ctx: GraphRunContext[PipelineState, PipelineDeps]
    ) -> ExecuteBatchNode | End[str]:
        state = ctx.state
        deps = ctx.deps
        plan_prompt = build_plan_prompt(state.schema_context)

        history_prefix = ""
        if state.conversation_history:
            parts = [f"**{m['role']}**: {m['content']}" for m in state.conversation_history]
            history_prefix = "\n".join(parts) + "\n\n"

        logger.info("PlanNode: calling LLM to create execution plan")
        response = await deps.anthropic.messages.create(
            model=deps.model,
            max_tokens=4096,
            system=plan_prompt,
            messages=[{"role": "user", "content": f"{history_prefix}{state.question}"}],
        )

        try:
            state.plan = parse_plan_json(response.content[0].text)
        except Exception as e:
            logger.error("PlanNode: failed to parse plan: %s", e)
            return End(f"Sorry, I couldn't create an analysis plan. Error: {e}")

        logger.info(
            "PlanNode: %d subtasks in %d batches",
            len(state.plan.tasks),
            len(state.plan.batches()),
        )
        return ExecuteBatchNode(batch_index=0)


@dataclass
class ExecuteBatchNode(BaseNode[PipelineState, PipelineDeps, str]):
    """Executes all subtasks in the current batch using asyncio.gather."""

    batch_index: int = 0

    async def run(
        self, ctx: GraphRunContext[PipelineState, PipelineDeps]
    ) -> ExecuteBatchNode | SynthesizeNode | End[str]:
        state = ctx.state
        deps = ctx.deps
        plan = state.plan

        if plan is None:
            return End("Error: no execution plan available")

        batches = plan.batches()
        if self.batch_index >= len(batches):
            return SynthesizeNode()

        batch = batches[self.batch_index]
        subtask_prompt = build_subtask_prompt(state.schema_context)

        logger.info(
            "ExecuteBatchNode: batch %d/%d with %d tasks",
            self.batch_index + 1,
            len(batches),
            len(batch),
        )

        async def run_one(task: SubTask) -> SubTaskResult:
            try:
                parts = [f"## Task\n{task.description}"]
                preds = {d: state.results[d].summary for d in task.depends_on if d in state.results}
                if preds:
                    parts.append("\n## Results from previous steps\n")
                    for dep_id, summary in preds.items():
                        parts.append(f"### {dep_id}\n{summary}\n")
                parts.append("\nWrite Python code. Return ONLY code, no explanation.")

                response = await deps.anthropic.messages.create(
                    model=deps.model,
                    max_tokens=4096,
                    system=subtask_prompt,
                    messages=[{"role": "user", "content": "\n".join(parts)}],
                )

                code = strip_code_fences(response.content[0].text)
                ext_functions = ExternalFunctions(deps.duckdb_store)
                result = await asyncio.to_thread(execute_code, code, ext_functions)

                artifact = await deps.sqlite_store.save_artifact(
                    conversation_id=state.conversation_id,
                    message_id=None,
                    code=code,
                    monty_state=result.monty_state,
                    result_json=result.output_json,
                    result_type=result.output_type,
                    error=result.error,
                )

                if result.error:
                    return SubTaskResult(
                        task_id=task.task_id,
                        artifact_uid=artifact["id"],
                        summary=f"Error: {result.error}",
                        result_type="error",
                        error=result.error,
                    )

                return SubTaskResult(
                    task_id=task.task_id,
                    artifact_uid=artifact["id"],
                    summary=format_result_summary(artifact["id"], result),
                    result_type=result.output_type,
                )
            except Exception as e:
                logger.exception("Error executing subtask %s", task.task_id)
                return SubTaskResult(
                    task_id=task.task_id,
                    artifact_uid="",
                    summary=f"Error: {e}",
                    result_type="error",
                    error=str(e),
                )

        results = await asyncio.gather(*(run_one(t) for t in batch))
        for r in results:
            state.results[r.task_id] = r

        next_index = self.batch_index + 1
        if next_index < len(batches):
            return ExecuteBatchNode(batch_index=next_index)
        return SynthesizeNode()


@dataclass
class SynthesizeNode(BaseNode[PipelineState, PipelineDeps, str]):
    """Calls LLM to synthesize all subtask results into a final answer."""

    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> End[str]:
        state = ctx.state
        deps = ctx.deps

        parts = [f"## Original Question\n{state.question}\n", "## Analysis Results\n"]
        for task_id, result in state.results.items():
            parts.append(f"### {task_id}\n{result.summary}\n")
        parts.append("\nSynthesize into a clear, coherent response.")

        logger.info("SynthesizeNode: synthesizing %d results", len(state.results))
        response = await deps.anthropic.messages.create(
            model=deps.model,
            max_tokens=4096,
            system=SYNTHESIZE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
        return End(response.content[0].text)
