"""Tests for the graph_state Plan-Execute-Synthesize pipeline.

Tests the node structure, state management, and shared model compatibility.
"""

from sandbox_agent.graph_state.nodes import (
    ExecuteBatchNode,
    PipelineDeps,
    PipelineState,
    PlanNode,
    SynthesizeNode,
)
from sandbox_agent.planning.helpers import parse_plan_json
from sandbox_agent.planning.models import ExecutionPlan, SubTask, SubTaskResult


class TestPipelineState:
    def test_initial_state(self):
        state = PipelineState(
            question="What is the average age?",
            schema_context="titanic(age, survived)",
            conversation_id="conv-123",
            conversation_history=[],
        )
        assert state.plan is None
        assert state.results == {}

    def test_state_with_plan(self):
        state = PipelineState(
            question="Q",
            schema_context="S",
            conversation_id="C",
            conversation_history=[],
        )
        state.plan = ExecutionPlan(
            tasks=[
                SubTask(task_id="a", description="A"),
                SubTask(task_id="b", description="B", depends_on=["a"]),
            ]
        )
        batches = state.plan.batches()
        assert len(batches) == 2
        assert batches[0][0].task_id == "a"

    def test_state_accumulates_results(self):
        state = PipelineState(
            question="Q", schema_context="S", conversation_id="C", conversation_history=[]
        )
        state.results["a"] = SubTaskResult(
            task_id="a", artifact_uid="uid-a", summary="ok", result_type="scalar"
        )
        state.results["b"] = SubTaskResult(
            task_id="b", artifact_uid="", summary="Error: boom", result_type="error", error="boom"
        )
        assert len(state.results) == 2
        assert state.results["a"].error is None
        assert state.results["b"].error == "boom"


class TestNodeTypes:
    """Verify the node type annotations match pydantic-graph expectations."""

    def test_plan_node_is_dataclass(self):
        node = PlanNode()
        assert hasattr(node, "run")

    def test_execute_batch_node_has_batch_index(self):
        node = ExecuteBatchNode(batch_index=2)
        assert node.batch_index == 2

    def test_execute_batch_node_default_index(self):
        node = ExecuteBatchNode()
        assert node.batch_index == 0

    def test_synthesize_node_is_dataclass(self):
        node = SynthesizeNode()
        assert hasattr(node, "run")


class TestParsePlanForGraphState:
    """Test that parse_plan_json produces plans compatible with the batch loop."""

    def test_single_task_one_batch(self):
        raw = '{"tasks": [{"task_id": "avg_age", "description": "Compute average age"}]}'
        plan = parse_plan_json(raw)
        batches = plan.batches()
        assert len(batches) == 1
        assert len(batches[0]) == 1

    def test_diamond_dag_three_batches(self):
        raw = """{"tasks": [
            {"task_id": "a", "description": "A"},
            {"task_id": "b", "description": "B", "depends_on": ["a"]},
            {"task_id": "c", "description": "C", "depends_on": ["a"]},
            {"task_id": "d", "description": "D", "depends_on": ["b", "c"]}
        ]}"""
        plan = parse_plan_json(raw)
        batches = plan.batches()
        assert len(batches) == 3
        assert batches[0][0].task_id == "a"
        batch2_ids = {t.task_id for t in batches[1]}
        assert batch2_ids == {"b", "c"}
        assert batches[2][0].task_id == "d"

    def test_code_fenced_plan(self):
        raw = '```json\n{"tasks": [{"task_id": "x", "description": "X"}]}\n```'
        plan = parse_plan_json(raw)
        assert plan.tasks[0].task_id == "x"


class TestPipelineDeps:
    def test_deps_defaults(self):
        deps = PipelineDeps(
            anthropic=None,  # type: ignore
            duckdb_store=None,
            sqlite_store=None,
        )
        assert deps.model == "claude-sonnet-4-5-20250929"

    def test_deps_custom_model(self):
        deps = PipelineDeps(
            anthropic=None,  # type: ignore
            duckdb_store=None,
            sqlite_store=None,
            model="custom-model",
        )
        assert deps.model == "custom-model"
