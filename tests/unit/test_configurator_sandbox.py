"""Meta-tool sandbox enforcement (docs/60 § Safety guards; docs/61).

Structural checks only — no LLM anywhere. Everything runs in a throwaway
temp git repo. The load-bearing property under test: prompt-level rules
are irrelevant; the TOOL LAYER refuses forbidden actions, records the
violation, and fires the session cancel token so the forge aborts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.configurator.tools import (
    VIOLATION_CANCEL_PREFIX,
    MetaToolContext,
    build_meta_tool_registry,
    meta_tool_names,
)
from foundry.configurator.tools.fs import (
    ReadFileIn,
    WriteFileIn,
    make_read_file,
    make_write_file,
)
from foundry.configurator.tools.git import (
    FORBIDDEN_GIT_VERBS,
    ensure_allowed_git,
)
from foundry.core.errors import (
    ConfigError,
    GitBackendError,
    ToolNotAllowedError,
)
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    project = repo / "projects" / "qa_bot"
    (project / "evals").mkdir(parents=True)
    (project / "evals" / "qa.yaml").write_text("name: qa\n")
    (repo / "catalog" / "tools" / "word_count" / "v1").mkdir(parents=True)
    (repo / "catalog" / "tools" / "word_count" / "v1" / "tool.yaml").write_text(
        "name: word_count\n"
    )
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


@pytest.fixture
def session() -> Session:
    return Session.new(project="qa_bot")


def _ctx(session: Session) -> RunContext:
    return RunContext(
        run_id=str(session.run_id),
        agent_name="meta_agent",
        session=session,
        tool_ref="meta/test@v1",
    )


# --- write_file sandbox --------------------------------------------------------


async def test_write_inside_project_ok(
    mctx: MetaToolContext, session: Session
) -> None:
    handle = make_write_file(mctx)
    result = await handle(
        WriteFileIn(
            path="projects/qa_bot/agents/a/agent.yaml", content="name: a\n"
        ),
        _ctx(session),
    )
    assert result.is_new is True
    assert (mctx.project_dir / "agents" / "a" / "agent.yaml").read_text() == (
        "name: a\n"
    )
    assert not session.cancel_token.cancelled()
    assert mctx.records.files_written


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "projects/other_project/system.yaml",
        "README.md",  # repo root, outside the project
    ],
)
async def test_write_outside_project_is_violation(
    mctx: MetaToolContext, session: Session, path: str
) -> None:
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError, match="outside the scoped project"):
        await handle(WriteFileIn(path=path, content="x"), _ctx(session))
    assert session.cancel_token.cancelled()
    assert (session.cancel_token.reason or "").startswith(
        VIOLATION_CANCEL_PREFIX
    )
    assert mctx.records.violations


async def test_write_to_catalog_is_violation(
    mctx: MetaToolContext, session: Session, repo: Path
) -> None:
    handle = make_write_file(mctx)
    target = repo / "catalog" / "tools" / "word_count" / "v1" / "handler.py"
    with pytest.raises(ConfigError, match="outside the scoped project"):
        await handle(WriteFileIn(path=str(target), content="x"), _ctx(session))
    assert not target.exists()
    assert session.cancel_token.cancelled()


async def test_write_to_framework_root_is_violation(
    mctx: MetaToolContext, session: Session
) -> None:
    handle = make_write_file(mctx)
    target = REPO_SRC / "configurator" / "evil.py"
    with pytest.raises(ConfigError):
        await handle(WriteFileIn(path=str(target), content="x"), _ctx(session))
    assert not target.exists()
    assert session.cancel_token.cancelled()


async def test_write_into_evals_is_violation(
    mctx: MetaToolContext, session: Session
) -> None:
    """The eval set is the target; the target doesn't move (docs/60)."""
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError, match="eval"):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/evals/qa.yaml", content="rigged"
            ),
            _ctx(session),
        )
    assert (mctx.project_dir / "evals" / "qa.yaml").read_text() == "name: qa\n"
    assert session.cancel_token.cancelled()


async def test_path_traversal_is_canonicalised_then_refused(
    mctx: MetaToolContext, session: Session
) -> None:
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/../../secrets.txt", content="x"
            ),
            _ctx(session),
        )
    assert session.cancel_token.cancelled()


async def test_symlink_escape_is_refused(
    mctx: MetaToolContext, session: Session, repo: Path
) -> None:
    outside = repo / "outside"
    outside.mkdir()
    (mctx.project_dir / "link").symlink_to(outside)
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError):
        await handle(
            WriteFileIn(path="projects/qa_bot/link/x.txt", content="x"),
            _ctx(session),
        )
    assert not (outside / "x.txt").exists()
    assert session.cancel_token.cancelled()


async def test_superseded_version_dir_is_frozen_but_latest_writable(
    mctx: MetaToolContext, session: Session
) -> None:
    for version in ("v1", "v2"):
        d = mctx.project_dir / "tools" / "t" / version
        d.mkdir(parents=True)
        (d / "handler.py").write_text("# original\n")
    handle = make_write_file(mctx)
    # Frozen: v1 (superseded). Recoverable ConfigError, NOT a violation.
    with pytest.raises(ConfigError, match="frozen version directory"):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/tools/t/v1/handler.py", content="x"
            ),
            _ctx(session),
        )
    assert not session.cancel_token.cancelled()
    assert not mctx.records.violations
    # Live: v2 (latest) — the iterate-the-scaffold path stays open.
    result = await handle(
        WriteFileIn(
            path="projects/qa_bot/tools/t/v2/handler.py", content="# new\n"
        ),
        _ctx(session),
    )
    assert result.is_overwrite is True
    # versions.json at the artifact level is always writable.
    await handle(
        WriteFileIn(
            path="projects/qa_bot/tools/t/versions.json",
            content='{"schema_version": 1, "versions": []}',
        ),
        _ctx(session),
    )


async def test_write_into_dot_foundry_is_violation(
    mctx: MetaToolContext, session: Session
) -> None:
    """Phase 6 review finding 2: the audit log + runtime state under
    .foundry/ cannot be overwritten silently."""
    dot_foundry = mctx.project_dir / ".foundry"
    dot_foundry.mkdir()
    (dot_foundry / "audit.jsonl").write_text('{"kind": "human"}\n')
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError, match=r"\.foundry"):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/.foundry/audit.jsonl", content="{}"
            ),
            _ctx(session),
        )
    assert (dot_foundry / "audit.jsonl").read_text() == '{"kind": "human"}\n'
    assert session.cancel_token.cancelled()
    assert mctx.records.violations


async def test_agent_yaml_provider_overrides_refused_via_write_file(
    mctx: MetaToolContext, session: Session
) -> None:
    """Phase 6 review finding 1: hand-writing agent.yaml with
    model_binding.provider_overrides is refused — the build_agent guard
    cannot be bypassed through the free-form write path."""
    handle = make_write_file(mctx)
    target = mctx.project_dir / "agents" / "a" / "agent.yaml"
    content = (
        "name: a\n"
        "model_binding:\n"
        "  provider: anthropic\n"
        "  model: claude-haiku-4-5\n"
        "  provider_overrides:\n"
        "    extra_headers: {anthropic-beta: something}\n"
    )
    with pytest.raises(ConfigError, match="provider_overrides"):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/agents/a/agent.yaml", content=content
            ),
            _ctx(session),
        )
    assert not target.exists()
    # Recoverable mistake (like build_agent's refusal), NOT a violation.
    assert not session.cancel_token.cancelled()
    assert not mctx.records.violations
    # Without the escape hatch the same write goes through.
    result = await handle(
        WriteFileIn(
            path="projects/qa_bot/agents/a/agent.yaml",
            content=(
                "name: a\n"
                "model_binding:\n"
                "  provider: anthropic\n"
                "  model: claude-haiku-4-5\n"
            ),
        ),
        _ctx(session),
    )
    assert result.is_new is True


async def test_superseded_prompt_versions_frozen_but_latest_writable(
    mctx: MetaToolContext, session: Session
) -> None:
    """Phase 6 review finding 3: prompts/v<N>.md freezes once superseded
    (N below the pinned/latest version); the latest version stays open for
    iteration."""
    prompts = mctx.project_dir / "agents" / "qa" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "v1.md").write_text("one\n")
    (prompts / "v2.md").write_text("two\n")
    (mctx.project_dir / "agents" / "qa" / "agent.yaml").write_text(
        "name: qa\nprompt: {version: v2, path: prompts/v2.md}\n"
    )
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError, match="superseded"):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/agents/qa/prompts/v1.md",
                content="rewritten\n",
            ),
            _ctx(session),
        )
    assert (prompts / "v1.md").read_text() == "one\n"
    assert not session.cancel_token.cancelled()  # recoverable, not violation
    # Latest stays writable — the iterate-then-pin loop.
    result = await handle(
        WriteFileIn(
            path="projects/qa_bot/agents/qa/prompts/v2.md",
            content="two improved\n",
        ),
        _ctx(session),
    )
    assert result.is_overwrite is True
    # A NEW version (v3, above the floor) is writable too.
    await handle(
        WriteFileIn(
            path="projects/qa_bot/agents/qa/prompts/v3.md", content="three\n"
        ),
        _ctx(session),
    )


async def test_prompt_freeze_honours_pin_above_latest_file(
    mctx: MetaToolContext, session: Session
) -> None:
    """The freeze floor is max(pinned, latest-on-disk): a pin at v3 with
    only v1/v2 on disk freezes both files."""
    prompts = mctx.project_dir / "agents" / "qb" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "v1.md").write_text("one\n")
    (prompts / "v2.md").write_text("two\n")
    (mctx.project_dir / "agents" / "qb" / "agent.yaml").write_text(
        "name: qb\nprompt: {version: v3, path: prompts/v3.md}\n"
    )
    handle = make_write_file(mctx)
    with pytest.raises(ConfigError, match="superseded"):
        await handle(
            WriteFileIn(
                path="projects/qa_bot/agents/qb/prompts/v2.md", content="x\n"
            ),
            _ctx(session),
        )


# --- read_file sandbox -----------------------------------------------------------


async def test_read_project_catalog_and_framework_ok(
    mctx: MetaToolContext, session: Session, repo: Path
) -> None:
    handle = make_read_file(mctx)
    for path in (
        "projects/qa_bot/evals/qa.yaml",
        str(repo / "catalog" / "tools" / "word_count" / "v1" / "tool.yaml"),
        str(REPO_SRC / "configurator" / "prompts" / "v1.md"),
    ):
        content = await handle(ReadFileIn(path=path), _ctx(session))
        assert content.size_bytes > 0


async def test_read_outside_sandbox_is_violation(
    mctx: MetaToolContext, session: Session
) -> None:
    handle = make_read_file(mctx)
    with pytest.raises(ConfigError, match="outside sandbox"):
        await handle(ReadFileIn(path="/etc/passwd"), _ctx(session))
    assert session.cancel_token.cancelled()


async def test_read_binary_refused(
    mctx: MetaToolContext, session: Session
) -> None:
    binary = mctx.project_dir / "blob.bin"
    binary.write_bytes(b"ab\x00cd")
    handle = make_read_file(mctx)
    with pytest.raises(ConfigError, match="binary"):
        await handle(
            ReadFileIn(path="projects/qa_bot/blob.bin"), _ctx(session)
        )
    assert not session.cancel_token.cancelled()  # recoverable, not violation


async def test_read_missing_file_is_recoverable_error(
    mctx: MetaToolContext, session: Session
) -> None:
    handle = make_read_file(mctx)
    with pytest.raises(ConfigError, match="no file"):
        await handle(
            ReadFileIn(path="projects/qa_bot/nope.yaml"), _ctx(session)
        )
    assert not session.cancel_token.cancelled()


# --- allowlist + forbidden git ops ------------------------------------------------


async def test_non_meta_tool_name_refused_at_dispatch(
    mctx: MetaToolContext, session: Session
) -> None:
    """docs/60 unit test 2: a tool name outside the fixed allowlist is
    refused by the dispatcher regardless of what the prompt says."""
    registry = build_meta_tool_registry(mctx)
    with pytest.raises(ToolNotAllowedError):
        await registry.dispatch(
            "git_push",
            meta_tool_names(),
            {},
            _ctx(session),
        )


def test_registry_matches_allowlist(mctx: MetaToolContext) -> None:
    registry = build_meta_tool_registry(mctx)
    assert sorted(d.name for d in registry.list_all()) == sorted(
        meta_tool_names()
    )


@pytest.mark.parametrize("verb", sorted(FORBIDDEN_GIT_VERBS))
def test_forbidden_git_verbs_refused_before_subprocess(verb: str) -> None:
    with pytest.raises(GitBackendError, match="forbidden"):
        ensure_allowed_git(verb)


def test_forbidden_git_flags_refused() -> None:
    with pytest.raises(GitBackendError, match="force"):
        ensure_allowed_git("commit", "--force")
    # The verbs the meta-tools actually use pass.
    ensure_allowed_git("add", "--", "projects/qa_bot/system.yaml")
    ensure_allowed_git("commit", "-m", "forge(qa_bot): x")
