from pydantic import BaseModel


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str


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
