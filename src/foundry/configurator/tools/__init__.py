"""The meta-toolkit (docs/61): every operation the meta-agent can perform.

``build_meta_tool_registry`` binds each meta-tool handler to one shared
:class:`MetaToolContext` and registers them in an ordinary
``ToolRegistry`` — meta-tools ARE foundry tools (same dispatch, same
allowlist enforcement, same events). Project agents never see this
registry; the meta-agent never sees any other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from foundry.configurator.tools import build as build_tools
from foundry.configurator.tools import eval as eval_tools
from foundry.configurator.tools import fs as fs_tools
from foundry.configurator.tools import git as git_tools
from foundry.configurator.tools import pins as pin_tools
from foundry.configurator.tools import registry as discovery_tools
from foundry.configurator.tools import rollback as rollback_tools
from foundry.configurator.tools.context import (
    VIOLATION_CANCEL_PREFIX,
    ForgeRecords,
    MetaToolContext,
)
from foundry.core.tool import (
    RegisteredTool,
    RetryPolicy,
    RunContext,
    ToolDescriptor,
    ToolRegistry,
)

_NO_RETRY = RetryPolicy(max_attempts=1)

_Handler = Callable[[Any, RunContext], Awaitable[BaseModel]]


def _tool(
    name: str,
    description: str,
    input_schema: type[BaseModel],
    output_schema: type[BaseModel],
    handler: _Handler,
    *,
    timeout_s: float,
    tags: list[str],
) -> RegisteredTool:
    return RegisteredTool(
        descriptor=ToolDescriptor(
            name=name,
            ref=f"meta/{name}",
            version="v1",
            description=description,
            tags=["meta", *tags],
        ),
        input_schema=input_schema,
        output_schema=output_schema,
        handler=handler,
        timeout_s=timeout_s,
        retry_policy=_NO_RETRY,
    )


def build_meta_tool_registry(mctx: MetaToolContext) -> ToolRegistry:
    """The fixed meta-tool set for one forge run (docs/61 § Tool
    registration). The meta-agent's allowlist is exactly these names."""
    registry = ToolRegistry()
    specs: list[RegisteredTool] = [
        _tool(
            "read_file",
            "Read a text file: the scoped project (read-write), the "
            "framework root, or any catalog root (both read-only).",
            fs_tools.ReadFileIn,
            fs_tools.FileContent,
            fs_tools.make_read_file(mctx),
            timeout_s=30.0,
            tags=["fs"],
        ),
        _tool(
            "write_file",
            "Write a text file INSIDE the scoped project (atomic; parents "
            "created). Refused: anything outside the project, the evals/ "
            "tree, and superseded version directories.",
            fs_tools.WriteFileIn,
            fs_tools.WriteResult,
            fs_tools.make_write_file(mctx),
            timeout_s=30.0,
            tags=["fs"],
        ),
        _tool(
            "list_catalog",
            "The shared catalog index: tools, connections, retrievers, "
            "with versions, descriptions, tags, and known eval scores. "
            "Prefer catalog artifacts over building new ones.",
            discovery_tools.EmptyIn,
            discovery_tools.CatalogIndexOut,
            discovery_tools.make_list_catalog(mctx),
            timeout_s=60.0,
            tags=["discovery"],
        ),
        _tool(
            "list_tools",
            "Tools in scope: catalog tools plus the project's local tools, "
            "with pinned versions where bound in system.yaml.",
            discovery_tools.EmptyIn,
            discovery_tools.ToolListOut,
            discovery_tools.make_list_tools(mctx),
            timeout_s=60.0,
            tags=["discovery"],
        ),
        _tool(
            "list_agents",
            "Agents in the scoped project: model bindings, prompt "
            "versions, tools, and state visibility.",
            discovery_tools.EmptyIn,
            discovery_tools.AgentListOut,
            discovery_tools.make_list_agents(mctx),
            timeout_s=30.0,
            tags=["discovery"],
        ),
        _tool(
            "build_tool",
            "Scaffold a project-local tool: the next v<N>/ directory with "
            "the 5-file shape (tool.yaml, schemas.py, handler.py, "
            "eval.yaml, README.md). Refuses dangerous:true and catalog "
            "name collisions.",
            build_tools.BuildToolIn,
            build_tools.BuildToolResult,
            build_tools.make_build_tool(mctx),
            timeout_s=60.0,
            tags=["scaffold"],
        ),
        _tool(
            "build_agent",
            "Scaffold an agent: agent.yaml + prompts/v1.md + "
            "output_schema.py. Refuses provider_overrides.",
            build_tools.BuildAgentIn,
            build_tools.BuildAgentResult,
            build_tools.make_build_agent(mctx),
            timeout_s=60.0,
            tags=["scaffold"],
        ),
        _tool(
            "new_prompt_version",
            "Create the next prompt version file for an agent (a copy of "
            "the live prompt). Does NOT change the pin — edit the file, "
            "then pin_version.",
            build_tools.NewPromptVersionIn,
            build_tools.NewPromptResult,
            build_tools.make_new_prompt_version(mctx),
            timeout_s=30.0,
            tags=["scaffold"],
        ),
        _tool(
            "pin_version",
            "Move a version pin: tools.<name>.version / "
            "connections.<name>.version in system.yaml, or prompt.version "
            "in agents/<agent>/agent.yaml (prompt.path moves with it).",
            pin_tools.PinVersionIn,
            pin_tools.PinResult,
            pin_tools.make_pin_version(mctx),
            timeout_s=30.0,
            tags=["pinning"],
        ),
        _tool(
            "run_eval",
            "Run an eval: scope=tool (standalone tool eval), scope=agent, "
            "or scope=project (requires eval_spec_path). Returns the "
            "score, failure clusters, and failing-case previews. Eval "
            "spend counts against the forge cost budget.",
            eval_tools.RunEvalIn,
            eval_tools.EvalRunOut,
            eval_tools.make_run_eval(mctx),
            timeout_s=1800.0,
            tags=["eval"],
        ),
        _tool(
            "read_eval_results",
            "Re-read a stored eval result by eval_run_id (no re-run).",
            eval_tools.ReadEvalResultsIn,
            eval_tools.EvalRunOut,
            eval_tools.make_read_eval_results(mctx),
            timeout_s=60.0,
            tags=["eval"],
        ),
        _tool(
            "compare_versions",
            "Compare eval scores across versions: scope=tool with "
            "versions, or scope=project with git refs (requires "
            "eval_spec_path). Use this to validate every change BEFORE "
            "accepting it; rollback on regression.",
            eval_tools.CompareVersionsIn,
            eval_tools.ComparisonOut,
            eval_tools.make_compare_versions(mctx),
            timeout_s=1800.0,
            tags=["eval"],
        ),
        _tool(
            "git_commit",
            "Stage + commit files inside the scoped project on the "
            "project branch, with the structured forge(...) message. "
            "Every iteration ends in exactly one commit.",
            git_tools.GitCommitIn,
            git_tools.CommitResult,
            git_tools.make_git_commit(mctx),
            timeout_s=120.0,
            tags=["versioning"],
        ),
        _tool(
            "git_show",
            "Show a commit (message + files + diff), scoped to the "
            "project's branch.",
            git_tools.GitShowIn,
            git_tools.CommitDetail,
            git_tools.make_git_show(mctx),
            timeout_s=60.0,
            tags=["versioning"],
        ),
        _tool(
            "list_versions",
            "Recent commits on the project branch (target=None), or the "
            "versions of one artifact ('tool/<name>', 'connection/<name>', "
            "'agent/<name>/prompts').",
            git_tools.ListVersionsIn,
            git_tools.VersionListing,
            git_tools.make_list_versions(mctx),
            timeout_s=60.0,
            tags=["versioning"],
        ),
        _tool(
            "rollback",
            "Per-artifact rollback: scope=tool/prompt (pin edit) or "
            "scope=project (subtree restore to a commit). Use after "
            "compare_versions shows a regression.",
            rollback_tools.RollbackIn,
            rollback_tools.RollbackOut,
            rollback_tools.make_rollback(mctx),
            timeout_s=300.0,
            tags=["versioning"],
        ),
    ]
    for spec in specs:
        registry.register(spec)
    return registry


def meta_tool_names() -> list[str]:
    """The fixed allowlist (used by the MetaAgent's AgentSpec.tools)."""
    return [
        "read_file",
        "write_file",
        "list_catalog",
        "list_tools",
        "list_agents",
        "build_tool",
        "build_agent",
        "new_prompt_version",
        "pin_version",
        "run_eval",
        "read_eval_results",
        "compare_versions",
        "git_commit",
        "git_show",
        "list_versions",
        "rollback",
    ]


__all__ = [
    "VIOLATION_CANCEL_PREFIX",
    "ForgeRecords",
    "MetaToolContext",
    "build_meta_tool_registry",
    "meta_tool_names",
]
