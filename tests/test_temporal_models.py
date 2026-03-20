"""Tests for Temporal data models — batching, serialization, edge cases."""

import json

from sandbox_agent.temporal.models import (
    ExecuteSubtaskInput,
    ExecutionPlan,
    PlanInput,
    SubTask,
    SubTaskResult,
    SynthesizeInput,
)


class TestExecutionPlanBatches:
    def test_empty_plan(self):
        plan = ExecutionPlan(tasks=[])
        assert plan.batches() == []

    def test_single_task(self):
        plan = ExecutionPlan(tasks=[SubTask(task_id="a", description="do A")])
        batches = plan.batches()
        assert len(batches) == 1
        assert len(batches[0]) == 1
        assert batches[0][0].task_id == "a"

    def test_all_independent(self):
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="do A"),
                SubTask(task_id="b", description="do B"),
                SubTask(task_id="c", description="do C"),
            ]
        )
        batches = plan.batches()
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_linear_chain(self):
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="do A"),
                SubTask(task_id="b", description="do B", depends_on=["a"]),
                SubTask(task_id="c", description="do C", depends_on=["b"]),
            ]
        )
        batches = plan.batches()
        assert len(batches) == 3
        assert [b[0].task_id for b in batches] == ["a", "b", "c"]

    def test_diamond_dag(self):
        """A depends on nothing, B and C depend on A, D depends on B and C."""
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="root"),
                SubTask(task_id="b", description="left", depends_on=["a"]),
                SubTask(task_id="c", description="right", depends_on=["a"]),
                SubTask(task_id="d", description="join", depends_on=["b", "c"]),
            ]
        )
        batches = plan.batches()
        assert len(batches) == 3
        assert batches[0][0].task_id == "a"
        batch2_ids = {t.task_id for t in batches[1]}
        assert batch2_ids == {"b", "c"}
        assert batches[2][0].task_id == "d"

    def test_circular_dependency_fallback(self):
        """Circular deps should fall through to the fallback batch."""
        plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A", depends_on=["b"]),
                SubTask(task_id="b", description="B", depends_on=["a"]),
            ]
        )
        batches = plan.batches()
        # Should have one fallback batch with both tasks
        assert len(batches) == 1
        assert len(batches[0]) == 2


class TestDataclassSerialization:
    """Verify that all dataclasses round-trip through JSON (simulating Temporal's converter)."""

    def _round_trip(self, obj, cls):
        """Simulate Temporal's JSON serialization."""
        from dataclasses import asdict

        data = json.loads(json.dumps(asdict(obj)))
        return cls(**data)

    def test_subtask_round_trip(self):
        original = SubTask(
            task_id="test",
            description="test task",
            datasets=["table1"],
            depends_on=["other"],
        )
        restored = self._round_trip(original, SubTask)
        assert restored.task_id == original.task_id
        assert restored.datasets == original.datasets
        assert restored.depends_on == original.depends_on

    def test_plan_input_round_trip(self):
        original = PlanInput(
            question="What is X?",
            schema_context="schema here",
            plan_system_prompt="system prompt",
            conversation_history=[{"role": "user", "content": "hello"}],
        )
        restored = self._round_trip(original, PlanInput)
        assert restored.question == original.question
        assert restored.conversation_history == original.conversation_history

    def test_plan_input_empty_history(self):
        original = PlanInput(
            question="Q",
            schema_context="S",
            plan_system_prompt="P",
        )
        restored = self._round_trip(original, PlanInput)
        assert restored.conversation_history == []

    def test_execute_subtask_input_round_trip(self):
        original = ExecuteSubtaskInput(
            task_id="t1",
            description="desc",
            datasets=["d1"],
            predecessor_summaries={"prev": "summary"},
            schema_context="schema",
            subtask_system_prompt="prompt",
            conversation_id="conv-123",
        )
        restored = self._round_trip(original, ExecuteSubtaskInput)
        assert restored.conversation_id == "conv-123"
        assert restored.predecessor_summaries == {"prev": "summary"}

    def test_synthesize_input_round_trip(self):
        original = SynthesizeInput(
            question="Q",
            task_summaries={"t1": "result 1", "t2": "result 2"},
            synthesize_system_prompt="synth prompt",
        )
        restored = self._round_trip(original, SynthesizeInput)
        assert restored.task_summaries == original.task_summaries

    def test_subtask_result_round_trip(self):
        original = SubTaskResult(
            task_id="t1",
            artifact_uid="uid-123",
            summary="some summary",
            result_type="table",
            error=None,
        )
        restored = self._round_trip(original, SubTaskResult)
        assert restored.artifact_uid == "uid-123"
        assert restored.error is None

    def test_subtask_result_with_error(self):
        original = SubTaskResult(
            task_id="t1",
            artifact_uid="",
            summary="Error: boom",
            result_type="error",
            error="boom",
        )
        restored = self._round_trip(original, SubTaskResult)
        assert restored.error == "boom"

    def test_execution_plan_with_nested_subtasks(self):
        original = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A", datasets=["t1"]),
                SubTask(task_id="b", description="B", depends_on=["a"]),
            ]
        )
        from dataclasses import asdict

        data = json.loads(json.dumps(asdict(original)))
        # Reconstruct manually (as Temporal would)
        restored = ExecutionPlan(tasks=[SubTask(**t) for t in data["tasks"]])
        assert len(restored.tasks) == 2
        assert restored.tasks[1].depends_on == ["a"]
