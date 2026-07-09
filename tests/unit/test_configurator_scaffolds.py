"""Scaffold + pinning meta-tools (docs/61 § Scaffolding / pin_version).

Temp git repos only. Covers the structural refusals the exit gate names:
``dangerous: true`` (build_tool) and ``provider_overrides`` (build_agent).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry.config import load_agent_spec, load_eval_spec, load_tool_spec
from foundry.configurator.tools import MetaToolContext
from foundry.configurator.tools.build import (
    BuildAgentIn,
    BuildToolIn,
    NewPromptVersionIn,
    make_build_agent,
    make_build_tool,
    make_new_prompt_version,
)
from foundry.configurator.tools.pins import PinVersionIn, make_pin_version
from foundry.core.errors import ConfigError, RefResolutionError
from foundry.core.session import Session
from foundry.core.tool import RunContext
from foundry.versioning.git_backend import GitBackend

REPO_SRC = Path(__file__).resolve().parents[2] / "src" / "foundry"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


_AGENT_YAML = """\
name: qa_agent
description: Answers questions.
model_binding:
  provider: anthropic
  model: claude-haiku-4-5
  settings: {max_tokens: 256, temperature: 0.0}
prompt:
  version: v1
  path: prompts/v1.md
output: {schema: output_schema.py::Output}
tools: [helper]
iteration_limit: 4
state_visibility: {read: [question], write: [answer]}
schema_version: 1
"""

_SYSTEM_YAML = """\
name: qa_bot
description: Toy QA system.
agents: [qa_agent]
flow: {type: single, agent: qa_agent}
tools:
  helper:
    ref: local/helper
    version: v2
schema_version: 1
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    project = repo / "projects" / "qa_bot"
    (project / "evals").mkdir(parents=True)
    (repo / "catalog" / "tools" / "word_count" / "v1").mkdir(parents=True)
    (project / "system.yaml").write_text(_SYSTEM_YAML)
    agent_dir = project / "agents" / "qa_agent"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(_AGENT_YAML)
    (agent_dir / "prompts" / "v1.md").write_text("Answer well.\n")
    for version in ("v1", "v2"):
        d = project / "tools" / "helper" / version
        d.mkdir(parents=True)
        (d / "tool.yaml").write_text(f"name: helper\nversion: {version}\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "op@example.com")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


@pytest.fixture
def mctx(repo: Path) -> MetaToolContext:
    return MetaToolContext(
        scoped_project="qa_bot",
        projects_root=repo / "projects",
        framework_root=REPO_SRC,
        catalog_roots=(repo / "catalog",),
        backend=GitBackend(repo),
        forge_run_id="01TESTFORGERUNID0000000000",
    )


def _ctx() -> RunContext:
    session = Session.new(project="qa_bot")
    return RunContext(
        run_id=str(session.run_id),
        agent_name="meta_agent",
        session=session,
        tool_ref="meta/test@v1",
    )


# --- build_tool -----------------------------------------------------------------


async def test_build_tool_creates_five_file_shape(
    mctx: MetaToolContext,
) -> None:
    result = await make_build_tool(mctx)(
        BuildToolIn(
            name="digit_sum",
            description="Sum digits.",
            kind_hint="transformation",
        ),
        _ctx(),
    )
    version_dir = Path(result.tool_path)
    assert result.version == "v1"
    assert sorted(p.name for p in version_dir.iterdir()) == [
        "README.md",
        "eval.yaml",
        "handler.py",
        "schemas.py",
        "tool.yaml",
    ]
    spec = load_tool_spec(version_dir / "tool.yaml")
    assert spec.name == "digit_sum" and spec.version == "v1"
    eval_spec = load_eval_spec(version_dir / "eval.yaml")
    assert eval_spec.scope == "tool"
    assert eval_spec.target == "local/digit_sum@v1"
    assert result.next_steps


async def test_build_tool_refuses_dangerous(mctx: MetaToolContext) -> None:
    with pytest.raises(ConfigError, match="dangerous"):
        await make_build_tool(mctx)(
            BuildToolIn(name="rm_rf", description="…", dangerous=True),
            _ctx(),
        )
    assert not (mctx.project_dir / "tools" / "rm_rf").exists()


async def test_build_tool_refuses_catalog_collision(
    mctx: MetaToolContext,
) -> None:
    with pytest.raises(ConfigError, match="collides with catalog"):
        await make_build_tool(mctx)(
            BuildToolIn(name="word_count", description="…"), _ctx()
        )


async def test_build_tool_next_version_seeds_from_latest(
    mctx: MetaToolContext,
) -> None:
    await make_build_tool(mctx)(
        BuildToolIn(name="digit_sum", description="Sum digits."), _ctx()
    )
    v1 = mctx.project_dir / "tools" / "digit_sum" / "v1"
    (v1 / "handler.py").write_text("# implemented v1\n")
    result = await make_build_tool(mctx)(
        BuildToolIn(name="digit_sum", description="Sum digits."), _ctx()
    )
    assert result.version == "v2"
    v2 = Path(result.tool_path)
    assert (v2 / "handler.py").read_text() == "# implemented v1\n"
    assert "version: v2" in (v2 / "tool.yaml").read_text()
    assert "local/digit_sum@v2" in (v2 / "eval.yaml").read_text()


def test_build_tool_invalid_kind_hint_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        BuildToolIn(name="x", description="d", kind_hint="dangerous")  # type: ignore[arg-type]


# --- build_agent -----------------------------------------------------------------


async def test_build_agent_scaffold_loads_as_agent_spec(
    mctx: MetaToolContext,
) -> None:
    result = await make_build_agent(mctx)(
        BuildAgentIn(
            name="solver",
            description="Solves.",
            state_read=["question"],
            state_write=["answer"],
        ),
        _ctx(),
    )
    agent_dir = Path(result.agent_path)
    spec = load_agent_spec(agent_dir / "agent.yaml")
    assert spec.name == "solver"
    assert spec.prompt.version == "v1"
    assert (agent_dir / "prompts" / "v1.md").is_file()
    assert "answer: str" in (agent_dir / "output_schema.py").read_text()


async def test_build_agent_refuses_provider_overrides(
    mctx: MetaToolContext,
) -> None:
    with pytest.raises(ConfigError, match="provider_overrides"):
        await make_build_agent(mctx)(
            BuildAgentIn(
                name="solver",
                description="Solves.",
                provider_overrides={"anthropic_beta": "yes"},
            ),
            _ctx(),
        )
    assert not (mctx.project_dir / "agents" / "solver").exists()


async def test_build_agent_refuses_existing_agent(
    mctx: MetaToolContext,
) -> None:
    with pytest.raises(ConfigError, match="already exists"):
        await make_build_agent(mctx)(
            BuildAgentIn(name="qa_agent", description="dup"), _ctx()
        )


# --- new_prompt_version ------------------------------------------------------------


async def test_new_prompt_version_copies_pinned_without_pinning(
    mctx: MetaToolContext,
) -> None:
    result = await make_new_prompt_version(mctx)(
        NewPromptVersionIn(agent="qa_agent"), _ctx()
    )
    assert result.new_version == "v2"
    assert result.parent_prompt_version == "v1"
    assert Path(result.new_prompt_path).read_text() == "Answer well.\n"
    # Pin unchanged: pinning is a separate, deliberate act.
    agent_yaml = (
        mctx.project_dir / "agents" / "qa_agent" / "agent.yaml"
    ).read_text()
    assert "version: v1" in agent_yaml


async def test_new_prompt_version_requires_existing_agent(
    mctx: MetaToolContext,
) -> None:
    with pytest.raises(ConfigError, match="no prompts"):
        await make_new_prompt_version(mctx)(
            NewPromptVersionIn(agent="ghost"), _ctx()
        )


# --- pin_version ---------------------------------------------------------------------


async def test_pin_tool_version_is_surgical(mctx: MetaToolContext) -> None:
    result = await make_pin_version(mctx)(
        PinVersionIn(
            file="system.yaml",
            key_path="tools.helper.version",
            new_version="v1",
        ),
        _ctx(),
    )
    assert result.old_version == "v2" and result.new_version == "v1"
    text = (mctx.project_dir / "system.yaml").read_text()
    assert "version: v1" in text
    assert text.startswith("name: qa_bot")  # formatting preserved


async def test_pin_prompt_version_moves_path_too(
    mctx: MetaToolContext,
) -> None:
    (
        mctx.project_dir / "agents" / "qa_agent" / "prompts" / "v2.md"
    ).write_text("Better.\n")
    result = await make_pin_version(mctx)(
        PinVersionIn(
            file="agents/qa_agent/agent.yaml",
            key_path="prompt.version",
            new_version="v2",
        ),
        _ctx(),
    )
    assert result.related_field_updates == {"prompt.path": "prompts/v2.md"}
    text = (
        mctx.project_dir / "agents" / "qa_agent" / "agent.yaml"
    ).read_text()
    assert "version: v2" in text and "prompts/v2.md" in text


async def test_pin_unknown_key_path_refused(mctx: MetaToolContext) -> None:
    with pytest.raises(ConfigError, match="unknown pin key_path"):
        await make_pin_version(mctx)(
            PinVersionIn(
                file="system.yaml",
                key_path="guardrails.max_iterations",
                new_version="v1",
            ),
            _ctx(),
        )


async def test_pin_to_missing_version_refused(
    mctx: MetaToolContext,
) -> None:
    with pytest.raises(RefResolutionError):
        await make_pin_version(mctx)(
            PinVersionIn(
                file="system.yaml",
                key_path="tools.helper.version",
                new_version="v9",
            ),
            _ctx(),
        )
    with pytest.raises(RefResolutionError, match="does not exist"):
        await make_pin_version(mctx)(
            PinVersionIn(
                file="agents/qa_agent/agent.yaml",
                key_path="prompt.version",
                new_version="v9",
            ),
            _ctx(),
        )
