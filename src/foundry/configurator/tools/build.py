"""Scaffolding meta-tools: ``build_tool`` / ``build_agent`` /
``new_prompt_version`` (docs/61 § Scaffolding).

Structural guarantees enforced HERE, not in the prompt (docs/60 § Defense
in depth):

- ``build_tool`` REFUSES ``dangerous: true`` — dangerous tools require
  human authoring, always.
- ``build_tool`` refuses names that collide with catalog tools (pin the
  catalog tool instead).
- ``build_agent`` REFUSES ``provider_overrides`` — the meta-agent never
  populates provider-specific escape hatches.

Every scaffolded file validates against its Pydantic spec before it is
written, so a scaffold is always loadable (though its eval will fail until
the meta-agent fills in real logic — that is the point).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from foundry.config.schemas import AgentSpec
from foundry.configurator.tools.context import (
    MetaToolContext,
    check_write_path,
)
from foundry.configurator.tools.registry import _load_yaml, _versions_of
from foundry.core.errors import ConfigError
from foundry.core.tool import RunContext
from foundry.versioning.artifacts import (
    list_prompt_versions,
    next_prompt_path,
    prompts_dir,
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

KindHint = Literal[
    "database_query",
    "http_api_call",
    "messaging",
    "file_io",
    "validation",
    "transformation",
    "custom",
]

OutputSchemaKind = Literal[
    "classification", "extraction", "report", "decision", "freeform"
]


def _require_name(name: str, *, tool: str) -> None:
    if not _NAME_RE.match(name):
        raise ConfigError(
            f"{tool}: invalid name {name!r}; expected "
            "^[a-z][a-z0-9_-]{0,63}$",
            context={"name": name},
        )


# --- build_tool ---------------------------------------------------------------


class BuildToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    kind_hint: KindHint | None = None
    dangerous: bool = False
    """MUST stay false. Dangerous tools require human authoring; the
    meta-tool refuses regardless of the meta-agent's reasoning."""


class BuildToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_path: str
    version: str
    files_created: list[str]
    next_steps: list[str]


_TOOL_SCHEMAS_TEMPLATE = '''"""Schemas for {name}@{version}. Replace the placeholder fields."""

from pydantic import BaseModel, ConfigDict


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str
'''

_TOOL_HANDLER_TEMPLATE = '''"""Handler for {name}@{version}."""

from schemas import Input, Output

from foundry.core.tool import RunContext


async def handle(inputs: Input, ctx: RunContext) -> Output:
    raise NotImplementedError(
        "implement {name}, then run its standalone eval until it passes"
    )
'''

_TOOL_EVAL_TEMPLATE = """\
name: {name}_standalone
description: Standalone eval for {name}. Replace the placeholder case.
scope: tool
target: local/{name}@{version}
cases:
  - id: placeholder
    input: {{ text: "example" }}
    expected: {{ result: "example" }}
scorers:
  - kind: exact
    name: result_match
    config: {{ field: result }}
threshold: 1.0
schema_version: 1
"""

_TOOL_README_TEMPLATE = """\
# {name}

{description}

Scaffolded by the meta-agent (forge run {forge_run_id}).

## Contract

- Input: `schemas.py::Input`
- Output: `schemas.py::Output`
- Eval: `eval.yaml` (the promotion gate — keep it honest)
"""


def _tool_yaml(name: str, version: str, description: str,
               kind_hint: str | None) -> str:
    tags = f"tags: [{kind_hint}]\n" if kind_hint else ""
    return (
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {description}\n"
        "input_schema: schemas.py::Input\n"
        "output_schema: schemas.py::Output\n"
        "handler: handler.py::handle\n"
        "standalone_eval: eval.yaml\n"
        f"{tags}"
        "schema_version: 1\n"
    )


def make_build_tool(
    mctx: MetaToolContext,
) -> Callable[[BuildToolIn, RunContext], Awaitable[BuildToolResult]]:
    async def handle(inputs: BuildToolIn, ctx: RunContext) -> BuildToolResult:
        _require_name(inputs.name, tool="build_tool")
        if inputs.dangerous:
            raise ConfigError(
                "build_tool: dangerous tools require human authoring "
                "(docs/61); the meta-agent cannot scaffold dangerous: true — "
                "this refusal is structural, not negotiable",
                context={"name": inputs.name},
            )
        for root in mctx.catalog_roots:
            if (root / "tools" / inputs.name).is_dir():
                raise ConfigError(
                    f"build_tool: name {inputs.name!r} collides with catalog "
                    f"tool 'catalog/{inputs.name}'; pin the catalog tool "
                    "instead, or pick a different local name",
                    context={"name": inputs.name, "catalog_root": str(root)},
                )
        tool_root = mctx.project_dir / "tools" / inputs.name
        existing = _versions_of(tool_root)
        version = f"v{len(existing) + 1}"
        version_dir = tool_root / version
        # Sandbox check on the computed target (defence in depth).
        check_write_path(
            mctx, ctx.session, str(version_dir / "tool.yaml"),
            tool="build_tool",
        )
        version_dir.mkdir(parents=True, exist_ok=False)

        if existing:
            # Next version seeds from the latest — the meta-agent iterates
            # from current content rather than an empty stub.
            prior = tool_root / existing[-1]
            for file in sorted(prior.iterdir()):
                if file.is_file():
                    shutil.copy2(file, version_dir / file.name)
            yaml_path = version_dir / "tool.yaml"
            text = yaml_path.read_text()
            yaml_path.write_text(
                re.sub(
                    r"(?m)^version: v\d+$", f"version: {version}", text
                ).replace(f"@{existing[-1]}", f"@{version}")
            )
            eval_path = version_dir / "eval.yaml"
            if eval_path.is_file():
                eval_path.write_text(
                    eval_path.read_text().replace(
                        f"@{existing[-1]}", f"@{version}"
                    )
                )
        else:
            (version_dir / "tool.yaml").write_text(
                _tool_yaml(
                    inputs.name, version, inputs.description, inputs.kind_hint
                )
            )
            (version_dir / "schemas.py").write_text(
                _TOOL_SCHEMAS_TEMPLATE.format(name=inputs.name, version=version)
            )
            (version_dir / "handler.py").write_text(
                _TOOL_HANDLER_TEMPLATE.format(name=inputs.name, version=version)
            )
            (version_dir / "eval.yaml").write_text(
                _TOOL_EVAL_TEMPLATE.format(name=inputs.name, version=version)
            )
            (version_dir / "README.md").write_text(
                _TOOL_README_TEMPLATE.format(
                    name=inputs.name,
                    description=inputs.description,
                    forge_run_id=mctx.forge_run_id,
                )
            )
        files = sorted(
            str(p) for p in version_dir.iterdir() if p.is_file()
        )
        mctx.records.files_written.extend(files)
        return BuildToolResult(
            tool_path=str(version_dir),
            version=version,
            files_created=files,
            next_steps=[
                "fill schemas.py with the real Input/Output models",
                "implement handle() in handler.py",
                "replace eval.yaml's placeholder cases with real ones "
                "derived from the project eval set",
                f"run_eval(scope='tool', target='local/{inputs.name}@"
                f"{version}') until it passes",
                "bind the tool in system.yaml and allowlist it on the agent",
            ],
        )

    return handle


# --- build_agent --------------------------------------------------------------


class BuildAgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"
    output_schema_kind: OutputSchemaKind = "freeform"
    state_read: list[str] = Field(default_factory=lambda: ["input"])
    state_write: list[str] = Field(default_factory=lambda: ["output"])
    max_tokens: int = Field(default=1024, ge=16, le=64000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    provider_overrides: dict[str, Any] | None = None
    """MUST stay unset. Provider-specific escape hatches are human-only;
    the meta-tool refuses any value here (docs/61 § build_agent)."""


class BuildAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_path: str
    files_created: list[str]
    next_steps: list[str]


_PROMPT_SKELETONS: dict[str, str] = {
    "classification": (
        "Classify the input into exactly one of the allowed labels. "
        "Explain nothing; output only the JSON object."
    ),
    "extraction": (
        "Extract the requested fields from the input verbatim where "
        "possible. Missing fields are null."
    ),
    "report": (
        "Produce a concise structured report over the input. Be factual; "
        "cite the input fields you used."
    ),
    "decision": (
        "Decide the requested action. State the decision and keep the "
        "rationale to one sentence."
    ),
    "freeform": "Answer the request accurately and concisely.",
}


def _agent_prompt_v1(description: str, kind: str) -> str:
    return (
        f"# Role\n\n{description}\n\n"
        f"# Task\n\n{_PROMPT_SKELETONS[kind]}\n\n"
        "# Output\n\nRespond ONLY with the JSON object required by the "
        "output schema. No code fences, no commentary.\n"
    )


def _agent_output_schema(state_write: list[str]) -> str:
    fields = "\n".join(f"    {name}: str" for name in state_write)
    return (
        '"""Output schema — the agent\'s structured contract."""\n\n'
        "from pydantic import BaseModel\n\n\n"
        "class Output(BaseModel):\n"
        f"{fields}\n"
    )


def make_build_agent(
    mctx: MetaToolContext,
) -> Callable[[BuildAgentIn, RunContext], Awaitable[BuildAgentResult]]:
    async def handle(inputs: BuildAgentIn, ctx: RunContext) -> BuildAgentResult:
        _require_name(inputs.name, tool="build_agent")
        if inputs.provider_overrides:
            raise ConfigError(
                "build_agent: provider_overrides are human-only (docs/61); "
                "the meta-agent cannot populate provider-specific escape "
                "hatches — use provider-neutral ModelSettings",
                context={"name": inputs.name,
                         "provider_overrides": sorted(inputs.provider_overrides)},
            )
        agent_dir = mctx.project_dir / "agents" / inputs.name
        if agent_dir.exists():
            raise ConfigError(
                f"build_agent: agent {inputs.name!r} already exists at "
                f"{agent_dir}; iterate its prompt via new_prompt_version "
                "instead of re-scaffolding",
                context={"agent_dir": str(agent_dir)},
            )
        check_write_path(
            mctx, ctx.session, str(agent_dir / "agent.yaml"), tool="build_agent"
        )
        agent_yaml = {
            "name": inputs.name,
            "description": inputs.description,
            "model_binding": {
                "provider": inputs.provider,
                "model": inputs.model,
                "settings": {
                    "max_tokens": inputs.max_tokens,
                    "temperature": inputs.temperature,
                },
            },
            "prompt": {"version": "v1", "path": "prompts/v1.md"},
            "output": {"schema": "output_schema.py::Output"},
            "tools": [],
            "iteration_limit": 8,
            "state_visibility": {
                "read": list(inputs.state_read),
                "write": list(inputs.state_write),
            },
            "schema_version": 1,
        }
        try:
            AgentSpec.model_validate(agent_yaml)
        except ValidationError as exc:
            raise ConfigError(
                f"build_agent: the scaffold would not validate as an "
                f"AgentSpec: {exc.errors()[0]['msg']}",
                context={"name": inputs.name},
                cause=exc,
            ) from exc
        agent_dir.mkdir(parents=True)
        (agent_dir / "prompts").mkdir()
        (agent_dir / "agent.yaml").write_text(
            yaml.safe_dump(agent_yaml, sort_keys=False)
        )
        (agent_dir / "prompts" / "v1.md").write_text(
            _agent_prompt_v1(inputs.description, inputs.output_schema_kind)
        )
        (agent_dir / "output_schema.py").write_text(
            _agent_output_schema(inputs.state_write)
        )
        files = [
            str(agent_dir / "agent.yaml"),
            str(agent_dir / "prompts" / "v1.md"),
            str(agent_dir / "output_schema.py"),
        ]
        mctx.records.files_written.extend(files)
        return BuildAgentResult(
            agent_path=str(agent_dir),
            files_created=files,
            next_steps=[
                "flesh out prompts/v1.md with real task guidance",
                "type output_schema.py::Output's fields properly",
                "add the agent to system.yaml's agents + flow, and its "
                "read/write fields to state.yaml (schema + visibility)",
                "allowlist the tools it needs in agent.yaml",
            ],
        )

    return handle


# --- new_prompt_version ---------------------------------------------------------


class NewPromptVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str


class NewPromptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_prompt_path: str
    new_version: str
    parent_prompt_version: str


def make_new_prompt_version(
    mctx: MetaToolContext,
) -> Callable[[NewPromptVersionIn, RunContext], Awaitable[NewPromptResult]]:
    async def handle(
        inputs: NewPromptVersionIn, ctx: RunContext
    ) -> NewPromptResult:
        _require_name(inputs.agent, tool="new_prompt_version")
        directory = prompts_dir(mctx.project_dir, inputs.agent)
        versions = list_prompt_versions(directory)
        if not versions:
            raise ConfigError(
                f"new_prompt_version: agent {inputs.agent!r} has no prompts "
                f"under {directory}; scaffold it with build_agent first",
                context={"agent": inputs.agent, "prompts_dir": str(directory)},
            )
        agent_yaml = _load_yaml(
            mctx.project_dir / "agents" / inputs.agent / "agent.yaml"
        )
        prompt = agent_yaml.get("prompt")
        pinned = (
            str(prompt.get("version", versions[-1]))
            if isinstance(prompt, dict)
            else versions[-1]
        )
        source = directory / f"{pinned}.md"
        if not source.is_file():
            source = directory / f"{versions[-1]}.md"
            pinned = versions[-1]
        target = next_prompt_path(directory)
        check_write_path(
            mctx, ctx.session, str(target), tool="new_prompt_version"
        )
        target.write_text(source.read_text())
        mctx.records.files_written.append(str(target))
        return NewPromptResult(
            new_prompt_path=str(target),
            new_version=target.stem,
            parent_prompt_version=pinned,
        )

    return handle


__all__ = [
    "BuildAgentIn",
    "BuildAgentResult",
    "BuildToolIn",
    "BuildToolResult",
    "KindHint",
    "NewPromptResult",
    "NewPromptVersionIn",
    "OutputSchemaKind",
    "make_build_agent",
    "make_build_tool",
    "make_new_prompt_version",
]
