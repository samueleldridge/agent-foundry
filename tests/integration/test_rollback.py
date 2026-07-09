"""Phase 5 exit-gate integration tests: rollback (docs/03 § Phase 5).

Everything runs against THROWAWAY temp git repos (the Phase 4 pin-set
pattern) — never the real workspace. The fixture repo holds a copy of
projects/hello plus a project-local tool `banner` with two versions:

- v2 (pinned): pure — input {text}, no connections.
- v1 (rollback target): CONTRACT-INCOMPATIBLE — input renamed to
  {message} AND a required `service` connection slot that system.yaml does
  not bind. Rolling back to it must succeed (with the schema warning
  confirmed) and the NEXT compile must fail with a compile-time error —
  docs/03 exit-gate item 8.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foundry.cli.rollback import execute_rollback_command
from foundry.core.errors import (
    CompileError,
    ConnectionSlotNotBoundError,
    FoundryError,
)
from foundry.versioning import (
    GitBackend,
    plan_project_rollback,
    plan_tool_rollback,
    read_audit_entries,
    read_prompt_pin,
    read_tool_pin,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)
    monkeypatch.delenv("FOUNDRY_PROJECT_BRANCH", raising=False)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


_BANNER_V2_SCHEMAS = '''\
"""Schemas for banner@v2."""

from pydantic import BaseModel, ConfigDict


class BannerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class BannerOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    banner: str
'''

_BANNER_V2_HANDLER = '''\
"""Handler for banner@v2 (pure)."""

from schemas import BannerIn, BannerOut

from foundry.core.tool import RunContext


async def handle(inputs: BannerIn, ctx: RunContext) -> BannerOut:
    return BannerOut(banner=f"== {inputs.text} ==")
'''

_BANNER_V1_SCHEMAS = '''\
"""Schemas for banner@v1 (older contract: `message`, not `text`)."""

from pydantic import BaseModel, ConfigDict


class BannerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class BannerOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    banner: str
'''

_BANNER_V1_HANDLER = '''\
"""Handler for banner@v1 (needs the `service` connection)."""

from schemas import BannerIn, BannerOut

from foundry.core.tool import RunContext


async def handle(inputs: BannerIn, ctx: RunContext) -> BannerOut:
    await ctx.connections.get("service")
    return BannerOut(banner=f"** {inputs.message} **")
'''


def _tool_yaml(version: str, *, with_slot: bool) -> str:
    slot = (
        "connections_required:\n"
        "  - slot: service\n"
        "    accepts: [catalog/http_service]\n"
        "    description: Service used to decorate the banner.\n"
        if with_slot
        else ""
    )
    return (
        "name: banner\n"
        f"version: {version}\n"
        "description: Wrap text in a banner.\n"
        "input_schema: schemas.py::BannerIn\n"
        "output_schema: schemas.py::BannerOut\n"
        "handler: handler.py::handle\n"
        "standalone_eval: eval.yaml\n"
        f"{slot}"
        "schema_version: 1\n"
    )


def _eval_yaml(version: str, input_field: str) -> str:
    return (
        f"name: banner_{version}_eval\n"
        "scope: tool\n"
        f"target: local/banner@{version}\n"
        "cases:\n"
        "  - id: basic\n"
        f"    input: {{ {input_field}: \"hi\" }}\n"
        "    expected: { }\n"
        "scorers:\n"
        "  - kind: exact\n"
        "    name: shape\n"
        "threshold: 0.0\n"
        "schema_version: 1\n"
    )


def _write_banner(project: Path) -> None:
    base = project / "tools" / "banner"
    v1, v2 = base / "v1", base / "v2"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    (v1 / "tool.yaml").write_text(_tool_yaml("v1", with_slot=True))
    (v1 / "schemas.py").write_text(_BANNER_V1_SCHEMAS)
    (v1 / "handler.py").write_text(_BANNER_V1_HANDLER)
    (v1 / "eval.yaml").write_text(_eval_yaml("v1", "message"))
    (v1 / "README.md").write_text("# banner v1\n")
    (v2 / "tool.yaml").write_text(_tool_yaml("v2", with_slot=False))
    (v2 / "schemas.py").write_text(_BANNER_V2_SCHEMAS)
    (v2 / "handler.py").write_text(_BANNER_V2_HANDLER)
    (v2 / "eval.yaml").write_text(_eval_yaml("v2", "text"))
    (v2 / "README.md").write_text("# banner v2\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Throwaway git repo: projects/hello + local tool banner (pinned v2)."""
    repo = tmp_path / "repo"
    project = repo / "projects" / "hello"
    project.parent.mkdir(parents=True)
    shutil.copytree(
        HELLO_DIR, project,
        ignore=shutil.ignore_patterns("__pycache__", ".foundry"),
    )
    _write_banner(project)
    system_yaml = project / "system.yaml"
    text = system_yaml.read_text().replace(
        "tools:\n",
        "tools:\n  banner:\n    ref: local/banner\n    version: v2\n",
        1,
    )
    system_yaml.write_text(text)
    # mirror the real repo's runtime-state ignore so audit writes never
    # dirty the tree
    (repo / ".gitignore").write_text("projects/*/.foundry/\n__pycache__/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "operator@example.com")
    _git(repo, "config", "user.name", "operator")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline: hello + banner v2")
    return repo


@pytest.fixture
def project(repo: Path) -> Path:
    return repo / "projects" / "hello"


# --- gate 1: per-tool rollback is a single-file pin edit -----------------------------


@pytest.mark.integration
def test_per_tool_rollback_updates_only_the_pin(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_rollback_command(
        str(project), tool="banner", to="v1", assume_yes=True
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "schema_compatible" in out and "FAILED" in out  # warned, confirmed

    # git diff HEAD~1 shows a SINGLE-file change (the exit-gate wording)
    changed = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert changed == ["projects/hello/system.yaml"]
    body = _git(repo, "diff", "HEAD~1", "HEAD")
    assert "-    version: v2" in body and "+    version: v1" in body
    assert read_tool_pin(project, "banner") == ("local/banner", "v1")
    # no other pins touched
    assert read_tool_pin(project, "get_time") == ("catalog/http_get_json", "v1")
    assert read_prompt_pin(project, "hello_agent")[0] == "v2"
    # conventional rollback commit message (docs/51)
    subject = _git(repo, "log", "-1", "--pretty=%s")
    assert subject.strip() == "rollback(hello/system.yaml): pin banner v2 → v1"

    # audit entry: run id + commit sha + artifact + operator + override
    entries = read_audit_entries(project, type="rollback")
    assert len(entries) == 1
    entry = entries[0]
    assert len(entry.id) == 26  # ULID == the op's run id
    assert entry.commit_sha == _git(repo, "rev-parse", "HEAD").strip()
    assert "banner" in entry.scope
    assert entry.operator.kind == "human"
    assert entry.operator.human_email == "operator@example.com"
    assert entry.overrides_used == ["schema_compatible"]
    assert entry.files_affected == ["projects/hello/system.yaml"]

    # v2 stays on disk — roll-forward stays possible (docs/52 invariant 2)
    assert (project / "tools" / "banner" / "v2" / "tool.yaml").is_file()


# --- gate 8: incompatible rollback surfaces at the next compile ----------------------


@pytest.mark.integration
def test_incompatible_rollback_fails_next_compile(project: Path) -> None:
    from foundry.orchestration.compiler import compile_project

    compile_project(project)  # sanity: compiles cleanly at v2

    code = execute_rollback_command(
        str(project), tool="banner", to="v1", assume_yes=True
    )
    assert code == 0  # the rollback itself succeeds (docs/03 gate 8)

    with pytest.raises((CompileError, ConnectionSlotNotBoundError)) as exc_info:
        compile_project(project)
    message = str(exc_info.value)
    assert "banner" in message and "service" in message


# --- gate 2: per-prompt rollback -----------------------------------------------------


@pytest.mark.integration
def test_per_prompt_rollback_touches_agent_yaml_only(
    repo: Path, project: Path
) -> None:
    code = execute_rollback_command(
        str(project), prompt="hello_agent", to="v1", assume_yes=True
    )
    assert code == 0
    changed = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert changed == ["projects/hello/agents/hello_agent/agent.yaml"]
    assert read_prompt_pin(project, "hello_agent") == ("v1", "prompts/v1.md")
    subject = _git(repo, "log", "-1", "--pretty=%s").strip()
    assert subject == "rollback(hello/agents/hello_agent): prompt v2 → v1"
    entries = read_audit_entries(project, type="rollback")
    assert entries[-1].overrides_used == []  # all checks passed cleanly


# --- gate 3: per-project rollback is atomic ------------------------------------------


@pytest.mark.integration
def test_per_project_rollback_restores_subtree_and_removes_added_files(
    repo: Path, project: Path
) -> None:
    baseline = _git(repo, "rev-parse", "HEAD").strip()
    # evolve the project: new prompt version + repin + a new eval file
    prompts = project / "agents" / "hello_agent" / "prompts"
    (prompts / "v3.md").write_text("# v3 prompt\nGreet tersely.\n")
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text()
        .replace("version: v2", "version: v3")
        .replace("path: prompts/v2.md", "path: prompts/v3.md")
    )
    (project / "evals" / "extra.yaml").write_text("# placeholder, not loaded\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "forge: prompt v3 + extra eval")

    backend = GitBackend(repo)
    plan = plan_project_rollback(project, baseline, backend=backend)
    removed = set(plan.removed_files)
    assert "projects/hello/agents/hello_agent/prompts/v3.md" in removed
    assert "projects/hello/evals/extra.yaml" in removed

    code = execute_rollback_command(
        str(project), to=baseline, assume_yes=True
    )
    assert code == 0
    # whole subtree restored: pin back, added files GONE, tree clean
    assert read_prompt_pin(project, "hello_agent") == ("v2", "prompts/v2.md")
    assert not (prompts / "v3.md").exists()
    assert not (project / "evals" / "extra.yaml").exists()
    assert _git(repo, "status", "--porcelain").strip() == ""
    subject = _git(repo, "log", "-1", "--pretty=%s").strip()
    assert subject.startswith(f"rollback(hello): bulk to {baseline[:8]}")
    entries = read_audit_entries(project, type="rollback")
    assert entries[-1].commit_sha == _git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.integration
def test_project_rollback_to_identical_state_is_refused(
    repo: Path, project: Path
) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    code = execute_rollback_command(str(project), to=head, assume_yes=True)
    assert code == 1
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert _git(repo, "status", "--porcelain").strip() == ""  # nothing half-applied


# --- gate 4: dirty working tree refusal ----------------------------------------------


@pytest.mark.integration
def test_rollback_refuses_dirty_tree_unless_force(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "state.yaml").write_text(
        (project / "state.yaml").read_text() + "# uncommitted operator edit\n"
    )
    head = _git(repo, "rev-parse", "HEAD").strip()
    code = execute_rollback_command(
        str(project), tool="banner", to="v1", assume_yes=True
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "working_tree_clean" in err
    assert _git(repo, "rev-parse", "HEAD").strip() == head  # no commit
    assert read_tool_pin(project, "banner") == ("local/banner", "v2")
    assert read_audit_entries(project) == []  # refusals write no audit entry

    # --force proceeds and the override is logged loudly (docs/52 gate 4)
    code = execute_rollback_command(
        str(project), tool="banner", to="v1", force=True
    )
    assert code == 0
    entries = read_audit_entries(project, type="rollback")
    assert "working_tree_clean" in entries[-1].overrides_used
    # the unrelated dirty edit is still in the working tree, uncommitted
    assert "state.yaml" in _git(repo, "status", "--porcelain")
    changed = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert changed == ["projects/hello/system.yaml"]


# --- dry-run + refusal shapes ---------------------------------------------------------


@pytest.mark.integration
def test_dry_run_changes_nothing(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    code = execute_rollback_command(
        str(project), tool="banner", to="v1", dry_run=True
    )
    assert code == 0
    assert "--dry-run" in capsys.readouterr().out
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert read_tool_pin(project, "banner") == ("local/banner", "v2")
    assert read_audit_entries(project) == []


@pytest.mark.integration
def test_missing_target_version_is_refused(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_rollback_command(
        str(project), tool="banner", to="v9", assume_yes=True
    )
    assert code == 1
    assert "target_exists" in capsys.readouterr().err
    assert read_tool_pin(project, "banner") == ("local/banner", "v2")


@pytest.mark.integration
def test_rollback_to_current_pin_is_refused(project: Path) -> None:
    code = execute_rollback_command(
        str(project), tool="banner", to="v2", assume_yes=True
    )
    assert code == 1


@pytest.mark.integration
def test_wrong_branch_is_a_hard_refusal(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When foundry/<project> exists, rollback must run FROM it — not
    bypassable, not even with --force (docs/52)."""
    backend = GitBackend(repo)
    backend.ensure_branch("foundry/hello")
    backend.run_git("checkout", "-q", "main")
    code = execute_rollback_command(
        str(project), tool="banner", to="v1", assume_yes=True, force=True
    )
    assert code == 1
    assert "correct_branch" in capsys.readouterr().err

    backend.run_git("checkout", "-q", "foundry/hello")
    code = execute_rollback_command(
        str(project), tool="banner", to="v1", assume_yes=True
    )
    assert code == 0


@pytest.mark.integration
def test_plan_tool_rollback_schema_check_details(project: Path) -> None:
    """The pre-flight schema check names the breaking movements."""
    backend = GitBackend.discover(project)
    plan = plan_tool_rollback(project, "banner", "v1", backend=backend)
    schema_check = next(
        c for c in plan.checks if c.name == "schema_compatible"
    )
    assert not schema_check.ok and schema_check.bypass == "confirm"
    assert "removed field `text`" in schema_check.detail
    assert "new REQUIRED field `message`" in schema_check.detail
    assert "new REQUIRED slot `service`" in schema_check.detail
    with pytest.raises(FoundryError, match="schema_compatible"):
        from foundry.versioning.rollback import enforce_preflight

        enforce_preflight(plan)  # no confirmation -> refused


# --- foundry versions / foundry diff (deliverable 8) ----------------------------------


@pytest.mark.integration
def test_versions_shows_commits_pins_and_available_versions(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.cli.versions import execute_versions

    assert execute_versions(str(project)) == 0
    out = capsys.readouterr().out
    assert "Project: hello" in out
    assert "baseline: hello + banner v2" in out  # commit history
    assert "hello_agent" in out and "*v2" in out  # active prompt pin marked
    assert "local/banner" in out
    assert "v1, *v2" in out  # both banner versions, pin marked
    assert "catalog/http_get_json" in out
    assert "time_service" in out  # connections section

    # --tool narrows to one binding
    assert execute_versions(str(project), tool="banner") == 0
    narrowed = capsys.readouterr().out
    assert "local/banner" in narrowed and "hello_agent" not in narrowed


@pytest.mark.integration
def test_diff_scopes_to_the_project_subtree(
    repo: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.cli.versions import execute_diff

    code = execute_rollback_command(
        str(project), tool="banner", to="v1", assume_yes=True
    )
    assert code == 0
    assert execute_diff(str(project), "HEAD~1", "HEAD") == 0
    out = capsys.readouterr().out
    assert "-    version: v2" in out and "+    version: v1" in out

    # --path narrows further; an untouched subtree diffs empty
    assert (
        execute_diff(
            str(project), "HEAD~1", "HEAD", path="agents/hello_agent/"
        )
        == 0
    )
    assert "no differences" in capsys.readouterr().out
