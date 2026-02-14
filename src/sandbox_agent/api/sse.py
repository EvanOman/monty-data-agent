import json


def format_sse_event(event_type: str, data: str) -> dict:
    """Format an SSE event for sse-starlette's EventSourceResponse."""
    return {"event": event_type, "data": data}


def sse_text(text: str) -> dict:
    return format_sse_event("text", text)


def sse_code(code: str) -> dict:
    return format_sse_event("code", code)


def sse_result(result_json: str) -> dict:
    return format_sse_event("result", result_json)


def sse_artifact(artifact_json: str) -> dict:
    return format_sse_event("artifact", artifact_json)


def sse_status(message: str) -> dict:
    return format_sse_event("status", message)


def sse_error(error: str) -> dict:
    return format_sse_event("error", error)


def sse_init(data: dict) -> dict:
    return format_sse_event("init", json.dumps(data))


def sse_done(data: dict) -> dict:
    return format_sse_event("done", json.dumps(data))
