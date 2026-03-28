"""Tests for the pydantic-graph beta API mode.

Tests the shared planning helpers and model compatibility with the beta API client.
"""

from sandbox_agent.planning.helpers import (
    format_history_prompt,
    parse_plan_json,
    strip_code_fences,
)
from sandbox_agent.planning.models import ExecutionPlan, SubTask, SubTaskResult


class TestStripCodeFences:
    def test_no_fences(self):
        assert strip_code_fences("hello world") == "hello world"

    def test_json_fences(self):
        text = '```json\n{"tasks": []}\n```'
        assert strip_code_fences(text) == '{"tasks": []}'

    def test_plain_fences(self):
        text = "```\ncode here\n```"
        assert strip_code_fences(text) == "code here"

    def test_python_fences(self):
        text = "```python\nprint('hi')\n```"
        assert strip_code_fences(text) == "print('hi')"


class TestParsePlanJson:
    def test_simple_plan(self):
        raw = '{"tasks": [{"task_id": "a", "description": "do A"}]}'
        plan = parse_plan_json(raw)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].task_id == "a"

    def test_plan_with_fences(self):
        raw = '```json\n{"tasks": [{"task_id": "a", "description": "do A"}]}\n```'
        plan = parse_plan_json(raw)
        assert len(plan.tasks) == 1

    def test_plan_with_deps(self):
        raw = """{"tasks": [
            {"task_id": "a", "description": "A"},
            {"task_id": "b", "description": "B", "depends_on": ["a"]}
        ]}"""
        plan = parse_plan_json(raw)
        assert plan.tasks[1].depends_on == ["a"]

    def test_plan_with_datasets(self):
        raw = '{"tasks": [{"task_id": "a", "description": "A", "datasets": ["titanic"]}]}'
        plan = parse_plan_json(raw)
        assert plan.tasks[0].datasets == ["titanic"]


class TestFormatHistoryPrompt:
    def test_empty_history(self):
        assert format_history_prompt([]) == ""

    def test_with_history(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = format_history_prompt(history)
        assert "**user**: hello" in result
        assert "## Current Question" in result


class TestSharedModelsForBetaApi:
    """Verify shared models work for the pydantic-graph beta API use case."""

    def test_subtask_fields(self):
        task = SubTask(task_id="avg_salary", description="Compute avg salary")
        assert task.task_id == "avg_salary"
        assert task.datasets == []
        assert task.depends_on == []

    def test_subtask_result_as_list(self):
        """Join collects list[SubTaskResult] — verify it works as list elements."""
        results = [
            SubTaskResult(task_id="a", artifact_uid="uid-a", summary="ok", result_type="scalar"),
            SubTaskResult(
                task_id="b",
                artifact_uid="uid-b",
                summary="error",
                result_type="error",
                error="boom",
            ),
        ]
        assert len(results) == 2
        assert results[0].error is None
        assert results[1].error == "boom"

    def test_plan_returns_task_list_for_map(self):
        """The plan step returns list[SubTask] which .map() fans out."""
        plan = ExecutionPlan(
            tasks=[SubTask(task_id="a", description="A"), SubTask(task_id="b", description="B")]
        )
        assert len(plan.tasks) == 2
        assert all(isinstance(t, SubTask) for t in plan.tasks)
