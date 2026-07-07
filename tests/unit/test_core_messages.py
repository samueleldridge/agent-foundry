"""FoundryMessage round-trip tests (docs/10 § Messages)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.core import (
    FoundryMessage,
    ImageBlock,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


@pytest.mark.unit
def test_round_trip_every_block_type() -> None:
    msg = FoundryMessage(
        role=MessageRole.ASSISTANT,
        content=[
            TextBlock(text="hello"),
            ToolUseBlock(id="tu_1", name="lookup", input={"q": "x"}),
            ToolResultBlock(
                tool_use_id="tu_1",
                content=[TextBlock(text="result"), ImageBlock(
                    source_type="base64", media_type="image/png", data="aGk="
                )],
                is_error=False,
            ),
        ],
    )
    dumped = msg.model_dump_json()
    parsed = FoundryMessage.model_validate_json(dumped)
    assert parsed == msg


@pytest.mark.unit
def test_discriminator_rejects_unknown_block_type() -> None:
    with pytest.raises(ValidationError):
        FoundryMessage.model_validate(
            {"role": "user", "content": [{"type": "video", "data": "x"}]}
        )


@pytest.mark.unit
def test_roles_match_anthropic_vocabulary() -> None:
    assert {r.value for r in MessageRole} == {"system", "user", "assistant", "tool"}
