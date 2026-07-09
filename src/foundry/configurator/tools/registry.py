"""Discovery meta-tools: ``list_catalog`` / ``list_tools`` / ``list_agents``
(docs/61 § Discovery).

Read-only views over the catalog roots + the scoped project. Deliberately
defensive: a half-scaffolded project (mid-bootstrap) must still be
listable, so raw YAML is read tolerantly instead of demanding fully-valid
specs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from foundry.catalog.loader import catalog_entries, load_versions_metadata
from foundry.configurator.tools.context import MetaToolContext
from foundry.core.errors import FoundryError
from foundry.core.tool import RunContext


class EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogEntryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    """'catalog/query_db' — pin a version via the binding's `version:`."""
    kind: str
    versions: list[str]
    latest: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    eval_scores: dict[str, float] = Field(default_factory=dict)


class CatalogIndexOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[CatalogEntryOut] = Field(default_factory=list)
    connections: list[CatalogEntryOut] = Field(default_factory=list)
    retrievers: list[CatalogEntryOut] = Field(default_factory=list)


class ToolDescriptorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    logical_name: str | None = None
    """The system.yaml `tools:` key when the tool is bound in the project."""
    pinned_version: str | None = None
    available_versions: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    connections_required: list[str] = Field(default_factory=list)


class ToolListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[ToolDescriptorOut] = Field(default_factory=list)


class AgentDescriptorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    available_prompt_versions: list[str] = Field(default_factory=list)
    output_schema: str = ""
    tools: list[str] = Field(default_factory=list)
    state_read: list[str] = Field(default_factory=list)
    state_write: list[str] = Field(default_factory=list)


class AgentListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: list[AgentDescriptorOut] = Field(default_factory=list)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_readme_paragraph(version_dir: Path) -> str:
    readme = version_dir / "README.md"
    if not readme.is_file():
        return ""
    for block in readme.read_text().split("\n\n"):
        text = " ".join(
            line.strip() for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if text:
            return text[:300]
    return ""


_SPEC_FILE_BY_KIND = {
    "tool": "tool.yaml",
    "connection": "connection.yaml",
    "retriever": "retriever.yaml",
}


def _spec_summary(version_dir: Path, kind: str) -> tuple[str, list[str]]:
    data = _load_yaml(version_dir / _SPEC_FILE_BY_KIND[kind])
    description = str(data.get("description", "")) or _first_readme_paragraph(
        version_dir
    )
    raw_tags = data.get("tags")
    tags = (
        [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    )
    return description, tags


def make_list_catalog(
    mctx: MetaToolContext,
) -> Callable[[EmptyIn, RunContext], Awaitable[CatalogIndexOut]]:
    async def handle(inputs: EmptyIn, ctx: RunContext) -> CatalogIndexOut:
        out = CatalogIndexOut()
        for entry in catalog_entries(mctx.roots()):
            if not entry.versions:
                continue
            latest = entry.versions[-1]
            artifact_dir = Path(entry.root) / f"{entry.kind}s" / entry.name
            description, tags = _spec_summary(artifact_dir / latest, entry.kind)
            scores: dict[str, float] = {}
            versions_json = artifact_dir / "versions.json"
            if versions_json.is_file():
                try:
                    metadata = load_versions_metadata(versions_json)
                    scores = {
                        v.version: v.eval_score
                        for v in metadata.versions
                        if v.eval_score is not None
                    }
                except FoundryError:
                    scores = {}
            record = CatalogEntryOut(
                ref=f"catalog/{entry.name}",
                kind=entry.kind,
                versions=entry.versions,
                latest=latest,
                description=description,
                tags=tags,
                eval_scores=scores,
            )
            getattr(out, f"{entry.kind}s").append(record)
        return out

    return handle


def _versions_of(artifact_dir: Path) -> list[str]:
    if not artifact_dir.is_dir():
        return []
    numbered = sorted(
        int(child.name[1:])
        for child in artifact_dir.iterdir()
        if child.is_dir() and child.name.startswith("v")
        and child.name[1:].isdigit()
    )
    return [f"v{n}" for n in numbered]


def _connection_slots(version_dir: Path) -> list[str]:
    data = _load_yaml(version_dir / "tool.yaml")
    slots = data.get("connections_required") or []
    if not isinstance(slots, list):
        return []
    return [str(s.get("slot", "")) for s in slots if isinstance(s, dict)]


def make_list_tools(
    mctx: MetaToolContext,
) -> Callable[[EmptyIn, RunContext], Awaitable[ToolListOut]]:
    async def handle(inputs: EmptyIn, ctx: RunContext) -> ToolListOut:
        system = _load_yaml(mctx.project_dir / "system.yaml")
        bindings = system.get("tools") or {}
        pinned: dict[str, tuple[str, str]] = {}
        if isinstance(bindings, dict):
            for logical, binding in bindings.items():
                if isinstance(binding, dict):
                    pinned[str(binding.get("ref", ""))] = (
                        str(logical),
                        str(binding.get("version", "")),
                    )

        out = ToolListOut()
        for entry in catalog_entries(mctx.roots()):
            if entry.kind != "tool" or not entry.versions:
                continue
            ref = f"catalog/{entry.name}"
            version_dir = (
                Path(entry.root) / "tools" / entry.name / entry.versions[-1]
            )
            description, tags = _spec_summary(version_dir, "tool")
            logical, version = pinned.get(ref, (None, None))
            out.tools.append(
                ToolDescriptorOut(
                    ref=ref,
                    logical_name=logical,
                    pinned_version=version or None,
                    available_versions=entry.versions,
                    description=description,
                    tags=tags,
                    connections_required=_connection_slots(version_dir),
                )
            )
        tools_root = mctx.project_dir / "tools"
        local_dirs = (
            sorted(p for p in tools_root.iterdir() if p.is_dir())
            if tools_root.is_dir()
            else []
        )
        for tool_dir in local_dirs:
            versions = _versions_of(tool_dir)
            if not versions:
                continue
            ref = f"local/{tool_dir.name}"
            description, tags = _spec_summary(tool_dir / versions[-1], "tool")
            logical, version = pinned.get(ref, (None, None))
            out.tools.append(
                ToolDescriptorOut(
                    ref=ref,
                    logical_name=logical,
                    pinned_version=version or None,
                    available_versions=versions,
                    description=description,
                    tags=tags,
                    connections_required=_connection_slots(
                        tool_dir / versions[-1]
                    ),
                )
            )
        return out

    return handle


def make_list_agents(
    mctx: MetaToolContext,
) -> Callable[[EmptyIn, RunContext], Awaitable[AgentListOut]]:
    async def handle(inputs: EmptyIn, ctx: RunContext) -> AgentListOut:
        out = AgentListOut()
        agents_dir = mctx.project_dir / "agents"
        if not agents_dir.is_dir():
            return out
        for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
            data = _load_yaml(agent_dir / "agent.yaml")
            binding = data.get("model_binding")
            binding = binding if isinstance(binding, dict) else {}
            prompt = data.get("prompt")
            prompt = prompt if isinstance(prompt, dict) else {}
            visibility = data.get("state_visibility")
            visibility = visibility if isinstance(visibility, dict) else {}
            output = data.get("output")
            output = output if isinstance(output, dict) else {}
            prompts_dir = agent_dir / "prompts"
            prompts = (
                sorted(p.stem for p in prompts_dir.glob("v*.md"))
                if prompts_dir.is_dir()
                else []
            )
            out.agents.append(
                AgentDescriptorOut(
                    name=agent_dir.name,
                    description=str(data.get("description", "")),
                    provider=str(binding.get("provider", "")),
                    model=str(binding.get("model", "")),
                    prompt_version=str(prompt.get("version", "")),
                    available_prompt_versions=prompts,
                    output_schema=str(output.get("schema", "")),
                    tools=[str(t) for t in data.get("tools") or []],
                    state_read=[str(f) for f in visibility.get("read") or []],
                    state_write=[str(f) for f in visibility.get("write") or []],
                )
            )
        return out

    return handle


__all__ = [
    "AgentDescriptorOut",
    "AgentListOut",
    "CatalogEntryOut",
    "CatalogIndexOut",
    "EmptyIn",
    "ToolDescriptorOut",
    "ToolListOut",
    "make_list_agents",
    "make_list_catalog",
    "make_list_tools",
]
