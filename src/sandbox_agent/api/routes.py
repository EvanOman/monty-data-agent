import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..sandbox.executor import execute_code
from ..sandbox.functions import ExternalFunctions
from .models import ChatRequest
from .sse import (
    sse_artifact,
    sse_code,
    sse_done,
    sse_error,
    sse_init,
    sse_result,
    sse_status,
    sse_text,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    agent_client = request.app.state.agent_client
    sqlite = request.app.state.sqlite_store

    conversation_id = req.conversation_id
    if not conversation_id:
        conv = await sqlite.create_conversation()
        conversation_id = conv["id"]

    await sqlite.add_message(conversation_id, "user", req.message)

    async def event_generator():
        yield sse_init({"conversation_id": conversation_id})

        full_text_parts = []
        try:
            async for event in agent_client.chat(conversation_id, req.message):
                if event.type == "text":
                    yield sse_text(event.data)
                    full_text_parts.append(event.data)
                elif event.type == "code":
                    yield sse_code(event.data)
                elif event.type == "result":
                    yield sse_result(event.data)
                elif event.type == "artifact":
                    yield sse_artifact(event.data)
                elif event.type == "status":
                    yield sse_status(event.data)
                elif event.type == "error":
                    yield sse_error(event.data)
                elif event.type == "done":
                    full_text = "".join(full_text_parts)
                    if full_text.strip():
                        await sqlite.add_message(conversation_id, "assistant", full_text)
                    conv = await sqlite.get_conversation(conversation_id)
                    if conv and conv["title"] == "New conversation":
                        title = req.message.strip()[:80]
                        if len(title) >= 80:
                            title = title[:77] + "..."
                        await sqlite.update_conversation_title(conversation_id, title)
                    yield sse_done(json.loads(event.data))
        except Exception as e:
            logger.exception("Error in chat stream")
            yield sse_error(str(e))
            yield sse_done({"error": str(e)})

    return EventSourceResponse(event_generator(), ping=15)


@router.get("/api/conversations")
async def list_conversations(request: Request):
    sqlite = request.app.state.sqlite_store
    return await sqlite.list_conversations()


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    sqlite = request.app.state.sqlite_store
    conv = await sqlite.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await sqlite.get_messages(conversation_id)
    artifacts = await sqlite.get_artifacts_for_conversation(conversation_id)
    return {"conversation": conv, "messages": messages, "artifacts": artifacts}


@router.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request):
    sqlite = request.app.state.sqlite_store
    artifact = await sqlite.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # Remove monty_state blob from response
    result = dict(artifact)
    result.pop("monty_state", None)
    return result


@router.post("/api/artifacts/{artifact_id}/replay")
async def replay_artifact(artifact_id: str, request: Request):
    sqlite = request.app.state.sqlite_store
    duckdb_store = request.app.state.duckdb_store

    artifact = await sqlite.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    ext_functions = ExternalFunctions(duckdb_store)

    result = await asyncio.to_thread(execute_code, artifact["code"], ext_functions)

    return {
        "artifact_id": artifact_id,
        "code": artifact["code"],
        "result_json": result.output_json,
        "result_type": result.output_type,
        "error": result.error,
    }
