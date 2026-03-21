"""Tests for the in-process async DAG executor."""

import asyncio
import time

from sandbox_agent.parallel.dag import execute_dag
from sandbox_agent.temporal.models import ExecutionPlan, SubTask, SubTaskResult


async def _make_runner(delay: float = 0, fail_on: set[str] | None = None):
    """Create a run_task function that tracks call order and timing."""
    call_log = []

    async def run_task(
        task_id: str,
        description: str,
        datasets: list[str],
        predecessor_summaries: dict[str, str],
    ) -> SubTaskResult:
        if fail_on and task_id in fail_on:
            raise RuntimeError(f"Simulated failure: {task_id}")
        t0 = time.monotonic()
        if delay:
            await asyncio.sleep(delay)
        call_log.append(
            {"task_id": task_id, "time": time.monotonic() - t0, "preds": predecessor_summaries}
        )
        return SubTaskResult(
            task_id=task_id,
            artifact_uid=f"uid-{task_id}",
            summary=f"Result: {task_id}",
            result_type="scalar",
        )

    return run_task, call_log


class TestExecuteDag:
    async def test_empty_plan(self):
        plan = ExecutionPlan(tasks=[])
        run_task, _ = await _make_runner()
        results = await execute_dag(plan, run_task)
        assert results == {}

    async def test_single_task(self):
        plan = ExecutionPlan(tasks=[SubTask(task_id="a", description="do A")])
        run_task, log = await _make_runner()
        results = await execute_dag(plan, run_task)
        assert "a" in results
        assert results["a"].result_type == "scalar"
        assert len(log) == 1

    async def test_independent_tasks_run_in_parallel(self):
        """Three independent tasks should all start ~simultaneously."""
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A"),
                SubTask(task_id="b", description="B"),
                SubTask(task_id="c", description="C"),
            ]
        )
        run_task, log = await _make_runner(delay=0.1)
        t0 = time.monotonic()
        results = await execute_dag(plan, run_task)
        elapsed = time.monotonic() - t0

        assert len(results) == 3
        # If they ran in parallel, total time should be ~0.1s, not ~0.3s
        assert elapsed < 0.25, f"Tasks should run in parallel, took {elapsed:.2f}s"

    async def test_linear_chain_runs_sequentially(self):
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A"),
                SubTask(task_id="b", description="B", depends_on=["a"]),
                SubTask(task_id="c", description="C", depends_on=["b"]),
            ]
        )
        run_task, log = await _make_runner()
        results = await execute_dag(plan, run_task)

        assert len(results) == 3
        # Verify order: a before b before c
        order = [entry["task_id"] for entry in log]
        assert order == ["a", "b", "c"]

    async def test_diamond_dag(self):
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="root", description="root"),
                SubTask(task_id="left", description="left", depends_on=["root"]),
                SubTask(task_id="right", description="right", depends_on=["root"]),
                SubTask(task_id="join", description="join", depends_on=["left", "right"]),
            ]
        )
        run_task, log = await _make_runner()
        results = await execute_dag(plan, run_task)

        assert len(results) == 4
        order = [entry["task_id"] for entry in log]
        # root must come first
        assert order[0] == "root"
        # left and right must come before join
        assert order.index("left") < order.index("join")
        assert order.index("right") < order.index("join")

    async def test_predecessor_summaries_passed(self):
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A"),
                SubTask(task_id="b", description="B", depends_on=["a"]),
            ]
        )
        run_task, log = await _make_runner()
        await execute_dag(plan, run_task)

        # Task b should have received a's summary
        b_entry = [e for e in log if e["task_id"] == "b"][0]
        assert "a" in b_entry["preds"]
        assert b_entry["preds"]["a"] == "Result: a"

    async def test_partial_failure(self):
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A"),
                SubTask(task_id="b", description="B"),
            ]
        )
        run_task, _ = await _make_runner(fail_on={"b"})
        results = await execute_dag(plan, run_task, max_retries=0)

        assert results["a"].error is None
        assert results["b"].error is not None
        assert "Simulated failure" in results["b"].error

    async def test_retry_on_failure(self):
        """Tasks should retry up to max_retries times."""
        attempt_count = {"count": 0}
        original_run, _ = await _make_runner()

        async def flaky_run(task_id, desc, datasets, preds):
            attempt_count["count"] += 1
            if attempt_count["count"] <= 2:
                raise RuntimeError("transient error")
            return await original_run(task_id, desc, datasets, preds)

        plan = ExecutionPlan(tasks=[SubTask(task_id="a", description="A")])
        results = await execute_dag(plan, flaky_run, max_retries=2)

        # Should succeed on third attempt
        assert results["a"].error is None
        assert attempt_count["count"] == 3
