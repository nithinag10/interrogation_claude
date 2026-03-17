from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SessionState(StrEnum):
    NEW = "NEW"
    INTAKE = "INTAKE"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    READY_FOR_RESEARCH = "READY_FOR_RESEARCH"
    RESEARCH_IN_PROGRESS = "RESEARCH_IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Message(BaseModel):
    id: str
    role: MessageRole
    content: str
    phase: str
    created_at: str


class SessionRecord(BaseModel):
    id: str
    user_id: str
    title: str
    state: SessionState
    context: dict[str, Any]
    messages: list[Message]
    created_at: str
    updated_at: str
    last_research_summary: str | None = None


class CreateSessionRequest(BaseModel):
    title: str = Field(default="Business Research Session", max_length=200)


class CreateSessionResponse(BaseModel):
    session_id: str
    state: SessionState
    created_at: str


class ChatSendRequest(BaseModel):
    session_id: str
    message: str = Field(..., min_length=1, max_length=6000)
    stream: bool = False


class ChatSendResponse(BaseModel):
    session_id: str
    state: SessionState
    assistant_message: str
    clarification_questions: list[str] = []


class InterruptRequest(BaseModel):
    session_id: str


class InterruptResponse(BaseModel):
    session_id: str
    state: SessionState
    message: str


class FeedbackSubmitRequest(BaseModel):
    session_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


class FeedbackSubmitResponse(BaseModel):
    session_id: str
    state: SessionState
    rating: int
    comment: str
    message: str


class SessionResponse(BaseModel):
    session_id: str
    user_id: str
    title: str
    state: SessionState
    context: dict[str, Any]
    messages: list[Message]
    created_at: str
    updated_at: str
