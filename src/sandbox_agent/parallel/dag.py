"""In-process async DAG executor using graphlib.TopologicalSorter.

Runs independent tasks in parallel with asyncio.gather, no external
infrastructure needed. A single-task plan degrades to a simple
sequential call — there's no special case, just a DAG with one node.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from graphlib import TopologicalSorter

from ..temporal.models import ExecutionPlan, SubTaskResult

logger = logging.getLogger(__name__)


async def execute_dag(
    plan: ExecutionPlan,
    run_task: Callable[[str, str, list[str], dict[str, str]], Awaitable[SubTaskResult]],
    max_retries: int = 2,
) -> dict[str, SubTaskResult]:
    """Execute a plan as a parallel DAG, returning all results.

    Args:
        plan: The execution plan (list of subtasks with dependencies).
        run_task: Async callable(task_id, description, datasets, predecessor_summaries)
                  that executes a single subtask and returns a SubTaskResult.
        max_retries: Number of retries per task on failure.

    Returns:
        Dict mapping task_id to SubTaskResult (including errors for failed tasks).
    """
    if not plan.tasks:
        return {}

    # Build the graphlib dependency graph: node → set of predecessors
    graph: dict[str, set[str]] = {}
    task_lookup = {}
    for task in plan.tasks:
        graph[task.task_id] = set(task.depends_on)
        task_lookup[task.task_id] = task

    sorter = TopologicalSorter(graph)
    sorter.prepare()

    results: dict[str, SubTaskResult] = {}

    while sorter.is_active():
        ready = sorter.get_ready()
        if not ready:
            break

        logger.info("DAG batch: running %d tasks in parallel: %s", len(ready), ready)

        async def _run_one(task_id: str) -> SubTaskResult:
            task = task_lookup[task_id]
            predecessor_summaries = {
                dep_id: results[dep_id].summary for dep_id in task.depends_on if dep_id in results
            }

            for attempt in range(max_retries + 1):
                try:
                    return await run_task(
                        task.task_id,
                        task.description,
                        task.datasets,
                        predecessor_summaries,
                    )
                except Exception as e:
                    if attempt < max_retries:
                        logger.warning(
                            "Task %s failed (attempt %d/%d): %s",
                            task_id,
                            attempt + 1,
                            max_retries + 1,
                            e,
                        )
                        continue
                    logger.error(
                        "Task %s failed after %d attempts: %s", task_id, max_retries + 1, e
                    )
                    return SubTaskResult(
                        task_id=task_id,
                        artifact_uid="",
                        summary=f"Error: {e}",
                        result_type="error",
                        error=str(e),
                    )
            # Unreachable, but satisfies type checker
            raise AssertionError("unreachable")

        batch_results = await asyncio.gather(*[_run_one(tid) for tid in ready])

        for tid, result in zip(ready, batch_results, strict=True):
            results[tid] = result
            sorter.done(tid)

    return results
