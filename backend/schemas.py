from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ToolVersion = Literal[
    "computer_use_20241022",
    "computer_use_20250124",
    "computer_use_20250429",
    "computer_use_20251124",
]


class CreateSessionRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=512, description="Anthropic API key")
    model: str = Field(
        "claude-sonnet-4-5-20250929",
        pattern=r"^claude-[a-z0-9.-]{1,80}$",
        description="Claude model id",
    )
    # The session agent only drives the Anthropic API; Bedrock and Vertex are not wired up.
    provider: Literal["anthropic"] = Field("anthropic", description="API provider")
    tool_version: ToolVersion = Field("computer_use_20250124", description="Computer-use tool version")


class CompleteRunRequest(BaseModel):
    """Sent by the session agent when a task finishes: that task's messages only."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000, description="Task text to send to the agent")


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


class CreatedSessionResponse(SessionResponse):
    access_token: str = Field(..., description="Bearer token for this session; shown only once")


class HistoryResponse(BaseModel):
    session_id: UUID
    messages: list[MessageResponse]
