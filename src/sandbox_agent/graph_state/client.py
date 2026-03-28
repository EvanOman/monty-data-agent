"""GraphStateClient — Plan-Execute-Synthesize using pydantic-graph's BaseNode API.

Uses pydantic-graph's original state-machine API with typed node transitions.
The graph is defined once at module level; parallelism happens inside
ExecuteBatchNode via asyncio.gather.
"""

from __future__ import annotations

import json
import logging
import time

from anthropic import AsyncAnthropic
from pydantic_graph import End, Graph

from ..config import GRAPH_STATE_MODEL
from ..planning.helpers import chunk_text
from ..shared import ChatEvent
from .nodes import (
    ExecuteBatchNode,
    PipelineDeps,
    PipelineState,
    PlanNode,
    SynthesizeNode,
)

logger = logging.getLogger(__name__)

# Build the graph once at module level
_graph = Graph(nodes=[PlanNode, ExecuteBatchNode, SynthesizeNode])

MAX_HISTORY_MESSAGES = 10


class GraphStateClient:
    """Chat client using pydantic-graph state-machine for Plan-Execute-Synthesize."""

    def __init__(self, duckdb_store, sqlite_store) -> None:
        self._duckdb = duckdb_store
        self._sqlite = sqlite_store
        self._anthropic = AsyncAnthropic()
        self._schema_context: str = ""

    def set_schema_context(self, ctx: str) -> None:
        self._schema_context = ctx

    async def chat(self, conversation_id: str, user_message: str):
        """Yield ChatEvent objects as the graph executes the pipeline."""
        t_start = time.time()

        yield ChatEvent(type="status", data="Starting plan-execute-synthesize pipeline...")

        # Load conversation history
        history = await self._sqlite.get_messages(conversation_id)
        if history and history[-1]["content"] == user_message:
            history = history[:-1]
        history = history[-MAX_HISTORY_MESSAGES:]
        conversation_history = [{"role": m["role"], "content": m["content"]} for m in history]

        state = PipelineState(
            question=user_message,
            schema_context=self._schema_context,
            conversation_id=conversation_id,
            conversation_history=conversation_history,
        )

        deps = PipelineDeps(
            anthropic=self._anthropic,
            duckdb_store=self._duckdb,
            sqlite_store=self._sqlite,
            model=GRAPH_STATE_MODEL,
        )

        yield ChatEvent(type="status", data="Planning analysis...")

        try:
            async with _graph.iter(PlanNode(), state=state, deps=deps) as run:
                async for node in run:
                    if isinstance(node, ExecuteBatchNode):
                        if state.plan and node.batch_index == 0:
                            n = len(state.plan.tasks)
                            b = len(state.plan.batches())
                            yield ChatEvent(
                                type="status",
                                data=f"Executing {n} subtasks in {b} batches...",
                            )
                        batch_num = node.batch_index + 1
                        total = len(state.plan.batches()) if state.plan else "?"
                        yield ChatEvent(type="status", data=f"Running batch {batch_num}/{total}...")

                    elif isinstance(node, SynthesizeNode):
                        # Emit artifacts from completed subtasks
                        for _task_id, result in state.results.items():
                            if result.artifact_uid:
                                artifact = await self._sqlite.get_artifact(result.artifact_uid)
                                if artifact:
                                    yield ChatEvent(type="code", data=artifact.get("code", ""))
                                    yield ChatEvent(
                                        type="artifact",
                                        data=json.dumps(
                                            {
                                                "id": result.artifact_uid,
                                                "code": artifact.get("code", ""),
                                                "result_json": artifact.get("result_json"),
                                                "result_type": artifact.get("result_type"),
                                                "error": artifact.get("error"),
                                            }
                                        ),
                                    )
                        yield ChatEvent(type="status", data="Synthesizing results...")

                    elif isinstance(node, End):
                        pass

            # Get final synthesis text
            result = run.result
            if result is not None:
                for text_chunk in chunk_text(result.output):
                    yield ChatEvent(type="text", data=text_chunk)

        except Exception as e:
            logger.exception("Error in graph_state pipeline")
            yield ChatEvent(type="error", data=str(e))

        t_end = time.time()
        total_ms = round((t_end - t_start) * 1000)

        task_count = len(state.plan.tasks) if state.plan else 0
        artifact_ids = [r.artifact_uid for r in state.results.values() if r.artifact_uid]

        yield ChatEvent(
            type="done",
            data=json.dumps(
                {
                    "artifacts": artifact_ids,
                    "timing": {
                        "total_ms": total_ms,
                        "turns": task_count + 2,
                        "tool_calls": task_count,
                        "mode": "graph_state",
                        "spans": [],
                    },
                }
            ),
        )

    async def close(self) -> None:
        pass
