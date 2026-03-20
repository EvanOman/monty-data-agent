"""Data models for the Temporal Plan-Execute-Synthesize pipeline."""

from dataclasses import dataclass, field


@dataclass
class SubTask:
    """A single sub-task in an execution plan."""

    task_id: str
    description: str
    datasets: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    """A DAG of sub-tasks produced by the planning phase."""

    tasks: list[SubTask] = field(default_factory=list)

    def batches(self) -> list[list[SubTask]]:
        """Topologically sort tasks into batches that can run in parallel.

        Each batch contains tasks whose dependencies are all satisfied by
        earlier batches. Tasks within a batch are independent and can run
        concurrently.
        """
        completed: set[str] = set()
        remaining = list(self.tasks)
        batches: list[list[SubTask]] = []

        while remaining:
            # Find tasks whose dependencies are all completed
            ready = [t for t in remaining if all(d in completed for d in t.depends_on)]
            if not ready:
                # Circular dependency or missing dep — just run everything remaining
                batches.append(remaining)
                break
            batches.append(ready)
            completed.update(t.task_id for t in ready)
            remaining = [t for t in remaining if t.task_id not in completed]

        return batches


@dataclass
class SubTaskResult:
    """The result of executing a single sub-task."""

    task_id: str
    artifact_uid: str
    summary: str
    result_type: str
    error: str | None = None


# --- Activity input dataclasses (single-dataclass-per-activity pattern) ---


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
