"""Shared text helpers for the memory subsystem.

Token counts are ESTIMATES (``len(text) // 4``) — good enough for windowing
and envelope caps at dev scale without a tokenizer dependency. The estimate
feeds ``memory.read`` events and truncation decisions only; provider-billed
token counts always come from real ``TokenUsage``.
"""

from __future__ import annotations

from foundry.core import (
    FoundryMessage,
    MemoryContribution,
    RetrievedDocument,
    TextBlock,
)

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def message_text(message: FoundryMessage) -> str:
    return "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )


def render_messages(messages: list[FoundryMessage]) -> str:
    """Transcript rendering for {messages} / {recent_messages} carriers."""
    return "\n".join(
        f"{message.role.value}: {message_text(message)}" for message in messages
    )


def render_docs(docs: list[RetrievedDocument]) -> str:
    """Snippet rendering for the {docs} carrier."""
    return "\n".join(f"[{doc.id}] {doc.text}" for doc in docs)


def contribution_text(
    content: list[FoundryMessage] | list[RetrievedDocument] | str,
) -> str:
    """Flatten any contribution content to text (token estimation input)."""
    if isinstance(content, str):
        return content
    if not content:
        return ""
    if isinstance(content[0], FoundryMessage):
        return render_messages([m for m in content if isinstance(m, FoundryMessage)])
    return render_docs([d for d in content if isinstance(d, RetrievedDocument)])


def contribution_tokens(
    content: list[FoundryMessage] | list[RetrievedDocument] | str,
) -> int:
    return estimate_tokens(contribution_text(content))


def is_empty(content: list[FoundryMessage] | list[RetrievedDocument] | str) -> bool:
    return len(content) == 0


def truncate_contribution(
    contribution: MemoryContribution, max_tokens: int
) -> MemoryContribution:
    """Cut a contribution down to ``max_tokens`` (estimate).

    - str content keeps its head (synthesised content leads with structure).
    - RetrievedDocument lists drop whole docs from the END (lowest-ranked).
    - FoundryMessage lists drop from the FRONT (keep the most recent turns).
    """
    if contribution.tokens_estimate <= max_tokens:
        return contribution
    content = contribution.content
    if isinstance(content, str):
        new_content: list[FoundryMessage] | list[RetrievedDocument] | str = (
            content[: max_tokens * _CHARS_PER_TOKEN]
        )
    elif content and isinstance(content[0], FoundryMessage):
        messages = [m for m in content if isinstance(m, FoundryMessage)]
        while messages and contribution_tokens(messages) > max_tokens:
            messages.pop(0)
        new_content = messages
    else:
        docs = [d for d in content if isinstance(d, RetrievedDocument)]
        while docs and contribution_tokens(docs) > max_tokens:
            docs.pop()
        new_content = docs
    return MemoryContribution(
        layer_name=contribution.layer_name,
        layer_kind=contribution.layer_kind,
        content=new_content,
        tokens_estimate=contribution_tokens(new_content),
        metadata={**contribution.metadata, "truncated": True},
    )


__all__ = [
    "contribution_text",
    "contribution_tokens",
    "estimate_tokens",
    "is_empty",
    "message_text",
    "render_docs",
    "render_messages",
    "truncate_contribution",
]
