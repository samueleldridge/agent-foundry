"""Provider-agnostic message types.

``FoundryMessage`` is the canonical shape every agent / tool / orchestration
layer works with. Provider adapters in ``foundry.providers`` translate to and
from provider-native message types. See docs/10-core-framework.md § Messages.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.types import CacheControl


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str
    cache_control: CacheControl | None = None


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: list[ContentBlock]
    is_error: bool = False


class ImageBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    source_type: Literal["base64", "url"]
    media_type: str
    data: str


ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | ImageBlock,
    Field(discriminator="type"),
]


class FoundryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: list[ContentBlock]


ToolResultBlock.model_rebuild()
FoundryMessage.model_rebuild()


__all__ = [
    "ContentBlock",
    "FoundryMessage",
    "ImageBlock",
    "MessageRole",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
]
