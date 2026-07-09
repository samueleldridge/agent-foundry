"""Artifact contract diffing (docs/50 § Catalog promotion + semver discipline).

Compares two versions of a tool (input/output schemas + connection slots)
or connection (config schema + auth scheme) and classifies the movement:

- ``breaking``  — a consumer valid against the baseline can fail against
  the candidate: removed field, changed type/shape, new REQUIRED input
  field, removed or newly-required connection slot, auth-scheme change.
- ``additive``  — only optional additions (or no schema movement at all).
- ``initial``   — no baseline to compare against.

Used by rollback's schema-compatibility pre-flight (baseline = currently
pinned version, candidate = rollback target) and by catalog promotion
(baseline = prior catalog version, candidate = the version being promoted).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from foundry.catalog.loader import load_connection_contract, load_tool_contract

SchemaChange = str  # "initial" | "additive" | "breaking"


@dataclass(frozen=True)
class ContractDiff:
    breaking: list[str] = field(default_factory=list)
    additions: list[str] = field(default_factory=list)

    @property
    def classification(self) -> SchemaChange:
        return "breaking" if self.breaking else "additive"

    def describe(self) -> list[str]:
        return [f"BREAKING: {b}" for b in self.breaking] + [
            f"additive: {a}" for a in self.additions
        ]


def _normalized_properties(model: type[BaseModel]) -> tuple[dict[str, str], set[str]]:
    """(field -> shape fingerprint, required field names) from the model's
    JSON schema. Titles/descriptions are stripped — doc-only changes are
    never breaking (docs/50 § Additive changes)."""
    schema = model.model_json_schema()
    properties: dict[str, Any] = schema.get("properties", {})
    required: set[str] = set(schema.get("required", []))

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: strip(v)
                for k, v in sorted(node.items())
                if k not in ("title", "description")
            }
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    fingerprints = {
        name: json.dumps(strip(prop), sort_keys=True)
        for name, prop in properties.items()
    }
    return fingerprints, required


def diff_models(
    baseline: type[BaseModel],
    candidate: type[BaseModel],
    *,
    label: str,
    new_required_is_breaking: bool,
) -> ContractDiff:
    """Field-level diff between two Pydantic models' JSON schemas."""
    base_props, base_required = _normalized_properties(baseline)
    cand_props, cand_required = _normalized_properties(candidate)
    breaking: list[str] = []
    additions: list[str] = []
    for name in sorted(base_props):
        if name not in cand_props:
            breaking.append(f"{label}: removed field `{name}`")
        elif base_props[name] != cand_props[name]:
            breaking.append(f"{label}: changed shape of field `{name}`")
    for name in sorted(cand_props):
        if name in base_props:
            continue
        if name in cand_required and new_required_is_breaking:
            breaking.append(f"{label}: new REQUIRED field `{name}`")
        else:
            additions.append(f"{label}: new field `{name}`")
    for name in sorted(cand_required - base_required):
        if name in base_props and new_required_is_breaking:
            breaking.append(f"{label}: field `{name}` became required")
    return ContractDiff(breaking=breaking, additions=additions)


def _merge(*diffs: ContractDiff) -> ContractDiff:
    return ContractDiff(
        breaking=[b for d in diffs for b in d.breaking],
        additions=[a for d in diffs for a in d.additions],
    )


def tool_contract_diff(baseline_dir: Path, candidate_dir: Path) -> ContractDiff:
    """Contract movement between two tool version directories."""
    base_spec, base_in, base_out = load_tool_contract(baseline_dir)
    cand_spec, cand_in, cand_out = load_tool_contract(candidate_dir)
    input_diff = diff_models(
        base_in, cand_in, label="input_schema", new_required_is_breaking=True
    )
    output_diff = diff_models(
        base_out, cand_out, label="output_schema", new_required_is_breaking=False
    )
    base_slots = {s.slot: s for s in base_spec.connections_required}
    cand_slots = {s.slot: s for s in cand_spec.connections_required}
    slot_breaking: list[str] = []
    slot_additions: list[str] = []
    for name in sorted(base_slots):
        if name not in cand_slots:
            slot_breaking.append(f"connections_required: removed slot `{name}`")
    for name in sorted(cand_slots):
        if name in base_slots:
            continue
        if cand_slots[name].optional:
            slot_additions.append(
                f"connections_required: new optional slot `{name}`"
            )
        else:
            slot_breaking.append(
                f"connections_required: new REQUIRED slot `{name}` "
                "(consumers must bind it)"
            )
    slots = ContractDiff(breaking=slot_breaking, additions=slot_additions)
    return _merge(input_diff, output_diff, slots)


def connection_contract_diff(
    baseline_dir: Path, candidate_dir: Path
) -> ContractDiff:
    """Contract movement between two connection version directories."""
    base_spec, base_config = load_connection_contract(baseline_dir)
    cand_spec, cand_config = load_connection_contract(candidate_dir)
    config_diff = diff_models(
        base_config,
        cand_config,
        label="config_schema",
        new_required_is_breaking=True,
    )
    auth: ContractDiff
    if base_spec.auth_scheme != cand_spec.auth_scheme:
        auth = ContractDiff(
            breaking=[
                "auth_scheme changed: "
                f"{base_spec.auth_scheme} -> {cand_spec.auth_scheme} "
                "(docs/50: auth-scheme changes are major)"
            ]
        )
    else:
        auth = ContractDiff()
    return _merge(config_diff, auth)


__all__ = [
    "ContractDiff",
    "SchemaChange",
    "connection_contract_diff",
    "diff_models",
    "tool_contract_diff",
]
