"""Native API boundaries. Import only when the optional chat stack is enabled."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ChatError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=128)


class RegisterRequest(LoginRequest):
    display_name: str = Field(min_length=1, max_length=64)
    invite_token: str = Field(min_length=1, max_length=256)


class TextData(StrictModel):
    text: str = Field(min_length=1, max_length=4000)


class TextSegment(StrictModel):
    type: Literal["text"] = "text"
    data: TextData


class MentionData(StrictModel):
    user_id: str = Field(pattern=r"^(all|[1-9][0-9]{0,9})$")


class MentionSegment(StrictModel):
    type: Literal["mention"] = "mention"
    data: MentionData


class ReplyData(StrictModel):
    message_id: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


class ReplySegment(StrictModel):
    type: Literal["reply"] = "reply"
    data: ReplyData


class ImageData(StrictModel):
    media_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ImageSegment(StrictModel):
    type: Literal["image"] = "image"
    data: ImageData


MessageSegment = Annotated[TextSegment | MentionSegment | ReplySegment | ImageSegment, Field(discriminator="type")]


class SendMessageRequest(StrictModel):
    client_message_id: UUID
    segments: list[MessageSegment] = Field(min_length=1, max_length=16)


class GroupRequest(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    member_ids: list[str] = Field(default_factory=list, max_length=100)


class DirectRequest(StrictModel):
    target_user_id: str


class ProfilePatch(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    bio: str | None = Field(default=None, max_length=280)
