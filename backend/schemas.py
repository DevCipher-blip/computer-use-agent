from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    api_key: str = Field(..., description="Anthropic API key")
    model: str = Field("claude-sonnet-4-5-20250929", description="Claude model")
    provider: str = Field("anthropic", description="API provider (anthropic | bedrock | vertex)")
    tool_version: str = Field("computer_use_20251124", description="Tool version")


class SendMessageRequest(BaseModel):
    text: str = Field(..., description="Task text to send to the agent")


class VNCInfo(BaseModel):
    vnc_host: str
    vnc_port: int
    novnc_url: str


class SessionResponse(BaseModel):
    id: UUID
    status: str
    model: str
    provider: str
    tool_version: str
    vnc_info: VNCInfo | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: UUID
    session_id: UUID
    role: str
    content: list[Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int


class HistoryResponse(BaseModel):
    session_id: UUID
    messages: list[MessageResponse]
