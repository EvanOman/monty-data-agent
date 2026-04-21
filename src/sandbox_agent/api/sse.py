"""SSE helpers — thin adapters over the chatkit package.

Wraps chatkit's ChatEvent factories into the dict form that sse-starlette's
EventSourceResponse expects. Keeps route handlers terse and centralizes the
sandbox-agent-specific bits that aren't in the chatkit protocol (the
``result`` event and the ``conversation_id``→``thread_id`` rename in
``sse_init``).
"""

from chatkit import ChatEvent, SSEPayload


def _to_dict(event: ChatEvent) -> dict:
    return SSEPayload.from_chat_event(event).to_dict()


def sse_text(text: str) -> dict:
    return _to_dict(ChatEvent.text(text))


def sse_code(code: str) -> dict:
    return _to_dict(ChatEvent.code(code))


def sse_result(result_json: str) -> dict:
    """sandbox-agent specific event type (not in chatkit protocol)."""
    return {"event": "result", "data": result_json}


def sse_artifact(artifact_json: str) -> dict:
    # chatkit's artifact factory expects structured args; here we already have JSON
    return {"event": "artifact", "data": artifact_json}


def sse_status(message: str) -> dict:
    return _to_dict(ChatEvent.status(message))


def sse_error(error: str) -> dict:
    return _to_dict(ChatEvent.error(error))


def sse_init(data: dict) -> dict:
    # chatkit's init expects thread_id; sandbox-agent passes conversation_id
    conv_id = data.get("conversation_id", data.get("thread_id", ""))
    return _to_dict(ChatEvent.init(thread_id=conv_id))


def sse_done(data: dict) -> dict:
    return _to_dict(ChatEvent.done(**data))
