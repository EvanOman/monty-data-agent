"""Temporal workflow: Plan-Execute-Synthesize pipeline.

The workflow decomposes a question into sub-tasks, executes them as a parallel
DAG, and synthesizes the results into a coherent response.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# Use workflow-safe imports for activity stubs
with workflow.unsafe.imports_passed_through():
    from .models import (
        ExecuteSubtaskInput,
        ExecutionPlan,
        PlanInput,
        SubTaskResult,
        SynthesizeInput,
    )


@workflow.defn
class PlanExecuteSynthesize:
    """Orchestrates the three-phase agent pipeline."""

    def __init__(self) -> None:
        self._status: str = "planning"
        self._plan: list[dict] = []
        self._completed_tasks: list[dict] = []

    @workflow.query
    def get_progress(self) -> dict:
        """Query handler for streaming progress to the client."""
        return {
            "status": self._status,
            "plan": self._plan,
            "completed_tasks": self._completed_tasks,
        }

    @workflow.run
    async def run(
        self,
        question: str,
        schema_context: str,
        plan_system_prompt: str,
        subtask_system_prompt: str,
        synthesize_system_prompt: str,
        conversation_id: str = "",
        conversation_history: list[dict] | None = None,
    ) -> dict:
        """Execute the full Plan-Execute-Synthesize pipeline.

        Returns a dict with:
            - plan: the execution plan (list of task dicts)
            - results: per-task results (task_id -> SubTaskResult dict)
            - synthesis: the final synthesized text
        """
        retry = RetryPolicy(
            maximum_attempts=3,
            backoff_coefficient=2.0,
        )

        # Phase 1: Plan
        self._status = "planning"
        plan_input = PlanInput(
            question=question,
            schema_context=schema_context,
            plan_system_prompt=plan_system_prompt,
            conversation_history=conversation_history or [],
        )
        plan: ExecutionPlan = await workflow.execute_activity(
            "plan_subtasks",
            args=[plan_input],
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

        self._plan = [
            {
                "task_id": t.task_id,
                "description": t.description,
                "datasets": t.datasets,
                "depends_on": t.depends_on,
            }
            for t in plan.tasks
        ]

        # Phase 2: Execute (parallel batches)
        self._status = "executing"
        all_results: dict[str, SubTaskResult] = {}

        for batch in plan.batches():
            batch_coros = []
            for task in batch:
                predecessor_summaries = {
                    dep_id: all_results[dep_id].summary
                    for dep_id in task.depends_on
                    if dep_id in all_results
                }

                subtask_input = ExecuteSubtaskInput(
                    task_id=task.task_id,
                    description=task.description,
                    datasets=task.datasets,
                    predecessor_summaries=predecessor_summaries,
                    schema_context=schema_context,
                    subtask_system_prompt=subtask_system_prompt,
                    conversation_id=conversation_id,
                )

                coro = workflow.execute_activity(
                    "execute_subtask",
                    args=[subtask_input],
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                batch_coros.append((task.task_id, coro))

            # Run all tasks in this batch concurrently, tolerating individual failures
            if len(batch_coros) == 1:
                task_id, coro = batch_coros[0]
                try:
                    result = await coro
                except Exception as e:
                    result = SubTaskResult(
                        task_id=task_id,
                        artifact_uid="",
                        summary=f"Error: {e}",
                        result_type="error",
                        error=str(e),
                    )
                all_results[task_id] = result
                self._completed_tasks.append(
                    {"task_id": task_id, "result_type": result.result_type, "error": result.error}
                )
            else:
                raw_results = await asyncio.gather(
                    *[coro for _, coro in batch_coros],
                    return_exceptions=True,
                )
                for (task_id, _), raw in zip(batch_coros, raw_results, strict=True):
                    if isinstance(raw, BaseException):
                        result = SubTaskResult(
                            task_id=task_id,
                            artifact_uid="",
                            summary=f"Error: {raw}",
                            result_type="error",
                            error=str(raw),
                        )
                    else:
                        result = raw
                    all_results[task_id] = result
                    self._completed_tasks.append(
                        {
                            "task_id": task_id,
                            "result_type": result.result_type,
                            "error": result.error,
                        }
                    )

        # Phase 3: Synthesize
        self._status = "synthesizing"
        task_summaries = {tid: r.summary for tid, r in all_results.items()}

        synth_input = SynthesizeInput(
            question=question,
            task_summaries=task_summaries,
            synthesize_system_prompt=synthesize_system_prompt,
        )
        synthesis: str = await workflow.execute_activity(
            "synthesize_results",
            args=[synth_input],
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

        self._status = "done"

        return {
            "plan": self._plan,
            "results": {
                tid: {
                    "task_id": r.task_id,
                    "artifact_uid": r.artifact_uid,
                    "summary": r.summary,
                    "result_type": r.result_type,
                    "error": r.error,
                }
                for tid, r in all_results.items()
            },
            "synthesis": synthesis,
        }
