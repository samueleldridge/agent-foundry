"""Memory wiring: compile-time validation + run-start construction.

Compile time (``prepare_memory``): every docs/26 § Validation rule runs
before anything is called —

- layer state fields must exist in ``StateSpec.schema`` (MemoryConfigError);
- a working layer's source field must be ``list[FoundryMessage]`` or ``str``
  (MemoryConfigError);
- a layer's state field must be inside the agent's READ scope, and a
  semantic layer's field also inside its WRITE scope (CompileError — same
  class as every other wiring hole, per docs/03 § Phase 2c);
- an episodic layer's ``retriever_slot`` must be bound in
  ``AgentSpec.retrievers`` (CompileError, like tool allowlists);
- a semantic layer with a trigger needs its consolidator prompt ON DISK
  (MemoryConfigError); the text is loaded here so run time never touches
  the filesystem.

(Layer-name uniqueness + injection-rule references are schema-level
validators — they already failed at YAML load if violated.)

Run start (``build_memory``): construct the concrete layers in declared
order and hand them to ``DefaultMemory``.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from foundry.config import (
    AgentSpec,
    EpisodicMemoryLayerConfig,
    SemanticMemoryLayerConfig,
    WorkingMemoryLayerConfig,
)
from foundry.core import MemoryLayer, RetrieverAccessor
from foundry.core.errors import CompileError, MemoryConfigError
from foundry.core.tool import EmitFn
from foundry.memory.coordinator import DefaultMemory
from foundry.memory.layers import (
    EpisodicMemoryLayer,
    SemanticMemoryLayer,
    SupportsGenerate,
    WorkingMemoryLayer,
)

_WORKING_SOURCE_TYPES = {"list[FoundryMessage]", "str"}


@dataclass(frozen=True)
class PreparedMemory:
    """Everything the runtime needs to build the coordinator at run start."""

    spec: AgentSpec
    consolidator_prompts: dict[str, str] = field(default_factory=dict)
    """Semantic layer name → consolidator prompt text (loaded at compile)."""


def _require_field(
    layer_name: str,
    kind: str,
    field_name: str,
    field_role: str,
    state_field_types: dict[str, str],
    agent_yaml: Path,
) -> None:
    if field_name not in state_field_types:
        raise MemoryConfigError(
            f"memory layer {layer_name!r} ({kind}) references {field_role} "
            f"{field_name!r} which is not in the state schema (fields: "
            f"{', '.join(sorted(state_field_types)) or '(none)'})",
            context={
                "file": str(agent_yaml),
                "pointer": f"/memory/layers/{layer_name}",
                "layer": layer_name,
                "field": field_name,
                "schema_fields": sorted(state_field_types),
            },
        )


def _require_scope(
    layer_name: str,
    kind: str,
    field_name: str,
    scope_kind: str,
    scope: Collection[str],
    agent_name: str,
    agent_yaml: Path,
) -> None:
    if field_name not in scope:
        raise CompileError(
            f"memory layer {layer_name!r} ({kind}) uses state field "
            f"{field_name!r} which agent {agent_name!r} cannot {scope_kind} "
            f"({scope_kind} scope: {', '.join(sorted(scope)) or '(none)'}); "
            "memory access is bounded by the agent's own state visibility",
            context={
                "file": str(agent_yaml),
                "pointer": f"/memory/layers/{layer_name}",
                "layer": layer_name,
                "field": field_name,
                "scope_kind": scope_kind,
                "scope": sorted(scope),
            },
        )


def prepare_memory(
    spec: AgentSpec,
    *,
    agent_dir: Path,
    state_field_types: dict[str, str],
    read_scope: Collection[str],
    write_scope: Collection[str],
) -> PreparedMemory | None:
    """Validate an agent's MemoryConfig at compile time. Returns None when
    the agent has no memory configured (the zero-overhead default)."""
    memory = spec.memory
    if memory is None:
        return None
    agent_yaml = agent_dir / "agent.yaml"
    retriever_slots = {binding.slot for binding in spec.retrievers}
    consolidator_prompts: dict[str, str] = {}

    for layer in memory.layers:
        if isinstance(layer, WorkingMemoryLayerConfig):
            _require_field(
                layer.name, layer.kind, layer.source_field, "source_field",
                state_field_types, agent_yaml,
            )
            declared = state_field_types[layer.source_field].replace(" ", "")
            if declared not in _WORKING_SOURCE_TYPES:
                raise MemoryConfigError(
                    f"memory layer {layer.name!r} (working) source_field "
                    f"{layer.source_field!r} must be 'list[FoundryMessage]' "
                    f"or 'str'; the state schema declares {declared!r}",
                    context={
                        "file": str(agent_yaml),
                        "pointer": f"/memory/layers/{layer.name}/source_field",
                        "declared_type": declared,
                    },
                )
            _require_scope(
                layer.name, layer.kind, layer.source_field, "read",
                read_scope, spec.name, agent_yaml,
            )
        elif isinstance(layer, EpisodicMemoryLayerConfig):
            if layer.retriever_slot not in retriever_slots:
                raise CompileError(
                    f"memory layer {layer.name!r} (episodic) references "
                    f"retriever_slot {layer.retriever_slot!r} which is not "
                    f"bound in agent {spec.name!r}'s retrievers "
                    f"(bound slots: {', '.join(sorted(retriever_slots)) or '(none)'})",
                    context={
                        "file": str(agent_yaml),
                        "pointer": f"/memory/layers/{layer.name}/retriever_slot",
                        "layer": layer.name,
                        "slot": layer.retriever_slot,
                        "bound_slots": sorted(retriever_slots),
                    },
                )
        elif isinstance(layer, SemanticMemoryLayerConfig):
            _require_field(
                layer.name, layer.kind, layer.state_field, "state_field",
                state_field_types, agent_yaml,
            )
            _require_scope(
                layer.name, layer.kind, layer.state_field, "read",
                read_scope, spec.name, agent_yaml,
            )
            _require_scope(
                layer.name, layer.kind, layer.state_field, "write",
                write_scope, spec.name, agent_yaml,
            )
            if layer.consolidator_prompt is not None:
                prompt_path = agent_dir / layer.consolidator_prompt
                if not prompt_path.exists():
                    raise MemoryConfigError(
                        f"memory layer {layer.name!r} (semantic) consolidator "
                        f"prompt not found on disk: {prompt_path}",
                        context={
                            "file": str(agent_yaml),
                            "pointer": (
                                f"/memory/layers/{layer.name}/consolidator_prompt"
                            ),
                            "prompt_path": str(prompt_path),
                        },
                    )
                consolidator_prompts[layer.name] = prompt_path.read_text()

    return PreparedMemory(spec=spec, consolidator_prompts=consolidator_prompts)


def build_memory(
    prepared: PreparedMemory,
    *,
    agent_name: str,
    provider: SupportsGenerate | None = None,
    retrievers: RetrieverAccessor | None = None,
    emit: EmitFn | None = None,
) -> DefaultMemory:
    """Run-start construction: config → concrete layers → coordinator."""
    memory = prepared.spec.memory
    assert memory is not None  # prepare_memory returned a PreparedMemory
    layers: list[MemoryLayer] = []
    for layer_config in memory.layers:
        if isinstance(layer_config, WorkingMemoryLayerConfig):
            layers.append(WorkingMemoryLayer(layer_config))
        elif isinstance(layer_config, EpisodicMemoryLayerConfig):
            if retrievers is None:
                raise CompileError(
                    f"memory layer {layer_config.name!r} (episodic) needs "
                    "retriever access but none was built for this agent",
                    context={"layer": layer_config.name},
                )
            layers.append(
                EpisodicMemoryLayer(
                    layer_config,
                    retrievers.get(layer_config.retriever_slot),
                    emit=emit,
                    agent_name=agent_name,
                )
            )
        else:
            layers.append(
                SemanticMemoryLayer(
                    layer_config,
                    consolidator_prompt_text=prepared.consolidator_prompts.get(
                        layer_config.name
                    ),
                    provider=provider,
                    emit=emit,
                    agent_name=agent_name,
                )
            )
    return DefaultMemory(
        layers, config=memory, emit=emit, agent_name=agent_name
    )


__all__ = ["PreparedMemory", "build_memory", "prepare_memory"]
