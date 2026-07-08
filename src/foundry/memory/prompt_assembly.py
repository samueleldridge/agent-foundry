"""prompt_assembly.weave(): MemoryEnvelope → prompt pieces (docs/26 § Prompt
assembly).

Rules implemented:

1. Layers contribute in declared order; ``inject_into_prompt`` reorders by
   listing rules differently.
2. ``placement: messages`` contributes raw message content into the
   conversation; multiple such layers concatenate in rule order.
3. ``system_prefix`` / ``system_suffix`` wrap text around the hand-authored
   system prompt.
4. ``user_message_prefix`` renders as a typed ``<memory>`` boundary block at
   the start of the latest user message (docs/83 injection guardrails).
5. Templates use {content} / {docs} / {messages} matching the contribution's
   carrier type.
6. Per-rule ``max_tokens`` truncates that layer's contribution (the
   envelope-level cap was already applied by the coordinator).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from foundry.config import MemoryConfig, MemoryInjectionRule
from foundry.core import (
    FoundryMessage,
    MemoryContribution,
    MemoryEnvelope,
    MessageRole,
    TextBlock,
)
from foundry.memory._text import (
    is_empty,
    render_docs,
    render_messages,
    truncate_contribution,
)

_DEFAULT_PLACEMENTS: dict[str, str] = {
    "working": "messages",
    "episodic": "system_suffix",
    "semantic": "system_prefix",
}

_DEFAULT_TEMPLATES: dict[str, str] = {
    "episodic": "Relevant past context:\n{docs}",
    "semantic": "Persistent context:\n{content}",
}


@dataclass(frozen=True)
class WovenPrompt:
    """The pieces the runtime combines into the final message list."""

    system_text: str
    memory_messages: list[FoundryMessage] = field(default_factory=list)
    user_prefix: str = ""
    """Rendered <memory> boundary blocks to prepend to the latest user
    message ('' when no user_message_prefix rules matched)."""


def _default_rules(config: MemoryConfig) -> list[MemoryInjectionRule]:
    rules: list[MemoryInjectionRule] = []
    for layer in config.layers:
        placement = _DEFAULT_PLACEMENTS.get(layer.kind)
        if placement is None:
            continue
        rules.append(
            MemoryInjectionRule(
                layer=layer.name,
                placement=placement,  # type: ignore[arg-type]
                template=_DEFAULT_TEMPLATES.get(layer.kind),
            )
        )
    return rules


def _render_text(rule: MemoryInjectionRule, contribution: MemoryContribution) -> str:
    template = rule.template or _DEFAULT_TEMPLATES.get(contribution.layer_kind)
    content = contribution.content
    if isinstance(content, str):
        carrier, rendered = "{content}", content
    elif content and isinstance(content[0], FoundryMessage):
        carrier, rendered = "{messages}", render_messages(
            [m for m in content if isinstance(m, FoundryMessage)]
        )
    else:
        carrier, rendered = "{docs}", render_docs(
            [d for d in content if not isinstance(d, FoundryMessage)]
        )
    if template is None:
        return rendered
    return template.replace(carrier, rendered)


def _as_messages(contribution: MemoryContribution) -> list[FoundryMessage]:
    content = contribution.content
    if isinstance(content, str):
        return [
            FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=content)])
        ]
    if content and isinstance(content[0], FoundryMessage):
        return [m for m in content if isinstance(m, FoundryMessage)]
    # RetrievedDocument lists under `messages` placement render as one block.
    text = render_docs([d for d in content if not isinstance(d, FoundryMessage)])
    return [FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=text)])]


def weave(
    system_prompt: str,
    envelope: MemoryEnvelope,
    config: MemoryConfig,
) -> WovenPrompt:
    by_name = {c.layer_name: c for c in envelope.contributions}
    rules = config.inject_into_prompt or _default_rules(config)

    prefix_parts: list[str] = []
    suffix_parts: list[str] = []
    memory_messages: list[FoundryMessage] = []
    user_prefix_parts: list[str] = []

    for rule in rules:
        contribution = by_name.get(rule.layer)
        if contribution is None or is_empty(contribution.content):
            continue
        if rule.max_tokens is not None:
            contribution = truncate_contribution(contribution, rule.max_tokens)
            if is_empty(contribution.content):
                continue
        if rule.placement == "messages":
            memory_messages.extend(_as_messages(contribution))
            continue
        text = _render_text(rule, contribution)
        if not text.strip():
            continue
        if rule.placement == "system_prefix":
            prefix_parts.append(text)
        elif rule.placement == "system_suffix":
            suffix_parts.append(text)
        else:  # user_message_prefix — typed boundary block (docs/26)
            user_prefix_parts.append(
                f'<memory layer="{contribution.layer_name}" '
                f'kind="{contribution.layer_kind}">\n{text}\n</memory>'
            )

    system_text = "\n\n".join(
        part for part in (*prefix_parts, system_prompt.rstrip(), *suffix_parts)
        if part
    )
    return WovenPrompt(
        system_text=system_text,
        memory_messages=memory_messages,
        user_prefix="\n".join(user_prefix_parts),
    )


__all__ = ["WovenPrompt", "weave"]
