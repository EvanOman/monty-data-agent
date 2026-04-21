from typing import Any

from pydantic import BaseModel, Field, model_validator


class ChatRequest(BaseModel):
    """Chat request — accepts both legacy and chatkit protocol formats."""

    conversation_id: str | None = None
    message: str
    mode: str = "standard"
    # Chatkit protocol fields
    thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_fields(self) -> "ChatRequest":
        # chatkit sends thread_id instead of conversation_id
        if self.thread_id and not self.conversation_id:
            self.conversation_id = self.thread_id
        # chatkit sends mode inside metadata
        if "mode" in self.metadata and self.mode == "standard":
            self.mode = self.metadata["mode"]
        return self


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: str


class ArtifactOut(BaseModel):
    id: str
    conversation_id: str
    message_id: str | None
    code: str
    result_json: str | None
    result_type: str | None
    error: str | None
    created_at: str
