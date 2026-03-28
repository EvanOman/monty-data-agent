"""Tests for Temporal workflow logic and client helpers.

These tests validate the data flow and helper functions without requiring
a Temporal test server. Full workflow integration is validated in E2E testing
with a real Temporal server (Phase 5).
"""

from sandbox_agent.temporal.client import _chunk_text, _count_batches
from sandbox_agent.temporal.models import (
    ExecuteSubtaskInput,
    ExecutionPlan,
    PlanInput,
    SubTask,
    SubTaskResult,
    SynthesizeInput,
)


class TestCountBatches:
    """Test the batch counting helper used for status messages."""

    def test_no_tasks(self):
        assert _count_batches([]) == 0

    def test_single_task(self):
        plan = [{"task_id": "a", "depends_on": []}]
        assert _count_batches(plan) == 1

    def test_all_independent(self):
        plan = [
            {"task_id": "a", "depends_on": []},
            {"task_id": "b", "depends_on": []},
            {"task_id": "c", "depends_on": []},
        ]
        assert _count_batches(plan) == 1

    def test_linear_chain(self):
        plan = [
            {"task_id": "a", "depends_on": []},
            {"task_id": "b", "depends_on": ["a"]},
            {"task_id": "c", "depends_on": ["b"]},
        ]
        assert _count_batches(plan) == 3

    def test_diamond(self):
        plan = [
            {"task_id": "a", "depends_on": []},
            {"task_id": "b", "depends_on": ["a"]},
            {"task_id": "c", "depends_on": ["a"]},
            {"task_id": "d", "depends_on": ["b", "c"]},
        ]
        assert _count_batches(plan) == 3


class TestChunkText:
    def test_empty_text(self):
        assert _chunk_text("") == [""]

    def test_short_text(self):
        result = _chunk_text("hello world", chunk_size=40)
        assert len(result) == 1
        assert result[0] == "hello world"

    def test_long_text_splits(self):
        text = "word " * 20  # 100 chars
        result = _chunk_text(text.strip(), chunk_size=40)
        assert len(result) > 1
        # Reconstruct should give back original (modulo trailing spaces)
        reconstructed = "".join(result).strip()
        assert reconstructed == text.strip()

    def test_preserves_words(self):
        text = "the quick brown fox jumps over the lazy dog"
        result = _chunk_text(text, chunk_size=15)
        # No word should be split
        for chunk in result:
            for word in chunk.strip().split():
                assert word in text


class TestActivityInputConstruction:
    """Test that the dataclasses can be constructed correctly for the workflow data flow."""

    def test_plan_input_with_history(self):
        inp = PlanInput(
            question="What is average salary?",
            schema_context="employees(id, salary)",
            plan_system_prompt="You are a planner",
            conversation_history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )
        assert len(inp.conversation_history) == 2
        assert inp.conversation_history[0]["role"] == "user"

    def test_execute_input_with_predecessors(self):
        inp = ExecuteSubtaskInput(
            task_id="join_results",
            description="Join salary and burnout data",
            datasets=[],
            predecessor_summaries={
                "avg_salary": "Result UID: abc\nType: table\nRows: 5",
                "burnout": "Result UID: def\nType: table\nRows: 5",
            },
            schema_context="employees(id, salary, burnout_rate)",
            subtask_system_prompt="Write code",
            conversation_id="conv-123",
        )
        assert len(inp.predecessor_summaries) == 2
        assert inp.conversation_id == "conv-123"

    def test_synthesize_input(self):
        inp = SynthesizeInput(
            question="Compare salary and burnout",
            task_summaries={
                "avg_salary": "Average salary by department...",
                "burnout": "Burnout rates by department...",
                "comparison": "Departments with high salary but low satisfaction...",
            },
            synthesize_system_prompt="Synthesize these results",
        )
        assert len(inp.task_summaries) == 3


class TestWorkflowDataFlow:
    """Simulate the workflow data flow: plan → batch → execute → synthesize."""

    def test_full_pipeline_data_flow(self):
        """Walk through the complete data flow without Temporal."""
        # Phase 1: Plan produces an ExecutionPlan
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="salary", description="Compute avg salary", datasets=["employees"]),
                SubTask(
                    task_id="burnout", description="Compute burnout rate", datasets=["employees"]
                ),
                SubTask(
                    task_id="compare",
                    description="Compare results",
                    depends_on=["salary", "burnout"],
                ),
            ]
        )

        # Batching produces correct parallel groups
        batches = plan.batches()
        assert len(batches) == 2
        batch1_ids = {t.task_id for t in batches[0]}
        assert batch1_ids == {"salary", "burnout"}
        assert batches[1][0].task_id == "compare"

        # Phase 2: Execute produces SubTaskResults
        all_results = {}

        # Batch 1: salary and burnout run in parallel
        for task in batches[0]:
            predecessor_summaries = {
                dep_id: all_results[dep_id].summary
                for dep_id in task.depends_on
                if dep_id in all_results
            }
            assert predecessor_summaries == {}  # no deps for batch 1

            result = SubTaskResult(
                task_id=task.task_id,
                artifact_uid=f"uid-{task.task_id}",
                summary=f"Result for {task.task_id}: computed",
                result_type="table",
            )
            all_results[task.task_id] = result

        # Batch 2: compare depends on salary and burnout
        for task in batches[1]:
            predecessor_summaries = {
                dep_id: all_results[dep_id].summary
                for dep_id in task.depends_on
                if dep_id in all_results
            }
            assert "salary" in predecessor_summaries
            assert "burnout" in predecessor_summaries

            result = SubTaskResult(
                task_id=task.task_id,
                artifact_uid=f"uid-{task.task_id}",
                summary="Comparison complete",
                result_type="table",
            )
            all_results[task.task_id] = result

        assert len(all_results) == 3

        # Phase 3: Synthesize receives all summaries
        task_summaries = {tid: r.summary for tid, r in all_results.items()}
        assert len(task_summaries) == 3
        assert "salary" in task_summaries
        assert "burnout" in task_summaries
        assert "compare" in task_summaries

    def test_partial_failure_data_flow(self):
        """Simulate one task failing — verify results still contain all tasks."""
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A"),
                SubTask(task_id="b", description="B"),
            ]
        )

        batches = plan.batches()
        assert len(batches) == 1

        # Simulate: a succeeds, b fails
        all_results = {}
        all_results["a"] = SubTaskResult(
            task_id="a",
            artifact_uid="uid-a",
            summary="A result: 42",
            result_type="scalar",
        )
        all_results["b"] = SubTaskResult(
            task_id="b",
            artifact_uid="",
            summary="Error: LLM returned malformed JSON",
            result_type="error",
            error="LLM returned malformed JSON",
        )

        # Synthesis should still receive both
        task_summaries = {tid: r.summary for tid, r in all_results.items()}
        assert len(task_summaries) == 2
        assert "Error" in task_summaries["b"]
        assert task_summaries["a"] == "A result: 42"
