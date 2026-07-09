"""Phase 5 exit-gate integration tests: catalog promotion (docs/03 § 5).

Everything runs in a THROWAWAY temp git repo holding its own catalog/ +
projects/hello_lab — never against the real workspace. The lab project
carries:

- ``word_stats``  — pure tool, v1 + v2 (additive), passing eval.
- ``flaky_stats`` — pure tool whose eval FAILS (floor-gate fixture).
- ``fetch_json``  — requires the `service` connection slot; bound in
  system.yaml through the LOCAL connection ``time_api`` (the Phase 4 seam:
  connection-requiring tools are promotable via the project's bindings).
- ``orphan_fetch`` — requires a slot but is NOT bound → structured refusal.
- ``time_api``    — local connection (http_service@v1 shape) whose
  health.yaml gates its own promotion.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from foundry.catalog.loader import load_tool_version, load_versions_metadata
from foundry.catalog.promote import promote_artifact
from foundry.cli.catalog import execute_catalog_promote
from foundry.config.refs import FoundryRoots
from foundry.core.errors import CatalogPromotionRefused
from foundry.versioning import GitBackend, parse_artifact_ref, read_audit_entries

REPO_ROOT = Path(__file__).resolve().parents[2]
HTTP_SERVICE_V1 = REPO_ROOT / "catalog" / "connections" / "http_service" / "v1"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("LAB_SERVICE_API_KEY", "fake-lab-key-for-tests")
    # the temp repo's own catalog/ must be discovered by the upward walk
    monkeypatch.delenv("FOUNDRY_CATALOG_ROOTS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout


_SYSTEM_YAML = """\
name: hello_lab
description: Promotion-test project.
agents: [lab_agent]
flow:
  type: single
  agent: lab_agent
tools:
  fetch_json:
    ref: local/fetch_json
    version: v1
    connection_bindings:
      service: lab_service
connections:
  lab_service:
    ref: local/time_api
    version: v1
    config:
      base_url: https://time.test
      health_path: /health
    credentials_ref:
      kind: env
      value: LAB_SERVICE_API_KEY
"""


def _stats_schemas(fields: str) -> str:
    return (
        '"""Schemas."""\n\n'
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class StatsIn(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n\n'
        "    text: str\n\n\n"
        "class StatsOut(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n\n'
        f"{fields}"
    )


def _stats_handler(body: str) -> str:
    return (
        '"""Handler."""\n\n'
        "from schemas import StatsIn, StatsOut\n\n"
        "from foundry.core.tool import RunContext\n\n\n"
        "async def handle(inputs: StatsIn, ctx: RunContext) -> StatsOut:\n"
        f"    return StatsOut({body})\n"
    )


def _tool_yaml(name: str, version: str, *, slot_accepts: str | None = None) -> str:
    slot = (
        "connections_required:\n"
        "  - slot: service\n"
        f"    accepts: [{slot_accepts}]\n"
        if slot_accepts
        else ""
    )
    return (
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: Test fixture tool {name}.\n"
        "input_schema: schemas.py::StatsIn\n"
        "output_schema: schemas.py::StatsOut\n"
        "handler: handler.py::handle\n"
        "standalone_eval: eval.yaml\n"
        f"{slot}"
        "schema_version: 1\n"
    )


def _stats_eval(name: str, version: str, expected: str) -> str:
    return (
        f"name: {name}_{version}_eval\n"
        "scope: tool\n"
        f"target: local/{name}@{version}\n"
        "cases:\n"
        "  - id: hello\n"
        "    input: { text: \"hello brave world\" }\n"
        f"    expected: {expected}\n"
        "scorers:\n"
        "  - kind: exact\n"
        "    name: exact_match\n"
        "threshold: 1.0\n"
        "deterministic: true\n"
        "schema_version: 1\n"
    )


def _write_tool_version(
    tool_dir: Path,
    name: str,
    version: str,
    *,
    schemas: str,
    handler: str,
    eval_yaml: str,
    slot_accepts: str | None = None,
) -> None:
    vdir = tool_dir / version
    vdir.mkdir(parents=True)
    (vdir / "tool.yaml").write_text(
        _tool_yaml(name, version, slot_accepts=slot_accepts)
    )
    (vdir / "schemas.py").write_text(schemas)
    (vdir / "handler.py").write_text(handler)
    (vdir / "eval.yaml").write_text(eval_yaml)
    (vdir / "README.md").write_text(f"# {name} {version}\n")


_FETCH_SCHEMAS = (
    '"""Schemas."""\n\n'
    "from pydantic import BaseModel, ConfigDict\n\n\n"
    "class StatsIn(BaseModel):\n"
    '    model_config = ConfigDict(extra="forbid")\n\n'
    '    text: str = "/"\n\n\n'
    "class StatsOut(BaseModel):\n"
    '    model_config = ConfigDict(extra="forbid")\n\n'
    "    status_code: int\n"
)

_FETCH_HANDLER = (
    '"""Handler (uses the service connection)."""\n\n'
    "from schemas import StatsIn, StatsOut\n\n"
    "from foundry.core.tool import RunContext\n\n\n"
    "async def handle(inputs: StatsIn, ctx: RunContext) -> StatsOut:\n"
    '    conn = await ctx.connections.get("service")\n'
    "    response = await conn.client.get(inputs.text)\n"
    "    return StatsOut(status_code=response.status_code)\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    project = repo / "projects" / "hello_lab"
    project.mkdir(parents=True)
    (project / "system.yaml").write_text(_SYSTEM_YAML)

    catalog = repo / "catalog"
    catalog.mkdir()
    (catalog / "index.yaml").write_text(
        "# lab catalog index — comments must survive promotion edits.\n"
        "schema_version: 1\n"
        "tools:\n"
        "  - seed_tool\n"
    )
    (catalog / "tools").mkdir()

    # word_stats: v1, then v2 adds `characters` (additive)
    ws = project / "tools" / "word_stats"
    _write_tool_version(
        ws, "word_stats", "v1",
        schemas=_stats_schemas("    words: int\n"),
        handler=_stats_handler("words=len(inputs.text.split())"),
        eval_yaml=_stats_eval("word_stats", "v1", "{ words: 3 }"),
    )
    _write_tool_version(
        ws, "word_stats", "v2",
        schemas=_stats_schemas("    words: int\n    characters: int\n"),
        handler=_stats_handler(
            "words=len(inputs.text.split()), characters=len(inputs.text)"
        ),
        eval_yaml=_stats_eval(
            "word_stats", "v2", "{ words: 3, characters: 17 }"
        ),
    )

    # flaky_stats: eval expectations are WRONG -> score 0.0
    _write_tool_version(
        project / "tools" / "flaky_stats", "flaky_stats", "v1",
        schemas=_stats_schemas("    words: int\n"),
        handler=_stats_handler("words=len(inputs.text.split())"),
        eval_yaml=_stats_eval("flaky_stats", "v1", "{ words: 999 }"),
    )

    # fetch_json (bound) + orphan_fetch (NOT bound): require `service`
    for tool_name in ("fetch_json", "orphan_fetch"):
        _write_tool_version(
            project / "tools" / tool_name, tool_name, "v1",
            schemas=_FETCH_SCHEMAS,
            handler=_FETCH_HANDLER,
            eval_yaml=_stats_eval(
                tool_name, "v1", "{ status_code: 200 }"
            ).replace('text: "hello brave world"', 'text: "/ping"'),
            slot_accepts="local/time_api",
        )

    # local connection time_api = http_service@v1 with the name rewritten
    conn_dir = project / "connections" / "time_api" / "v1"
    shutil.copytree(
        HTTP_SERVICE_V1, conn_dir,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    connection_yaml = conn_dir / "connection.yaml"
    connection_yaml.write_text(
        connection_yaml.read_text().replace(
            "name: http_service", "name: time_api", 1
        )
    )

    (repo / ".gitignore").write_text("projects/*/.foundry/\n__pycache__/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "promoter@example.com")
    _git(repo, "config", "user.name", "promoter")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline: hello_lab + empty catalog")
    return repo


def _lab_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "time.test"
        if request.url.path in ("/health", "/ping"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _promote(repo: Path, target: str, **kwargs: object) -> object:
    return promote_artifact(
        target,
        projects_root=repo / "projects",
        catalog_root=repo / "catalog",
        backend=GitBackend(repo),
        **kwargs,  # type: ignore[arg-type]
    )


# --- gate 5: promotion copies, records, indexes, commits ------------------------------


@pytest.mark.integration
def test_promote_tool_copies_records_indexes_and_commits(repo: Path) -> None:
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    result = _promote(repo, "hello_lab/tool/word_stats", notes="first cut")

    assert result.catalog_ref == "catalog/word_stats@v1"  # type: ignore[attr-defined]
    assert result.source_ref == "hello_lab/tools/word_stats@v2"  # type: ignore[attr-defined]
    assert result.eval_score == 1.0  # type: ignore[attr-defined]
    assert result.schema_change == "initial"  # type: ignore[attr-defined]

    dest = repo / "catalog" / "tools" / "word_stats" / "v1"
    for required in ("tool.yaml", "handler.py", "schemas.py", "eval.yaml",
                     "README.md"):
        assert (dest / required).is_file()
    # the copied yaml is rewritten to the CATALOG version number and the
    # promoted artifact loads through the standard 5-file loader
    roots = FoundryRoots(
        catalog_roots=[repo / "catalog"], projects_root=repo / "projects"
    )
    loaded = load_tool_version(
        parse_artifact_ref("catalog/word_stats@v1"), roots
    )
    assert loaded.spec.version == "v1"

    # versions.json: score, promoter identity, provenance, classification
    metadata = load_versions_metadata(dest.parent / "versions.json")
    entry = metadata.get("v1")
    assert entry is not None
    assert entry.eval_score == 1.0
    assert entry.eval_run_id is not None
    assert entry.created_by == "human"
    assert entry.promoted_by == "promoter@example.com"
    assert entry.source_ref == "hello_lab/tools/word_stats@v2"
    assert entry.schema_change == "initial"
    assert entry.notes == "first cut"

    # index.yaml gained the tool; comments + seed entry survive
    index_text = (repo / "catalog" / "index.yaml").read_text()
    assert "# lab catalog index" in index_text
    assert "  - seed_tool" in index_text
    assert "  - word_stats" in index_text

    # ONE commit, catalog files only
    head_after = _git(repo, "rev-parse", "HEAD").strip()
    assert head_after != head_before
    assert result.commit_sha == head_after  # type: ignore[attr-defined]
    changed = _git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert changed and all(f.startswith("catalog/") for f in changed)
    assert _git(repo, "status", "--porcelain").strip() == ""

    # audit entry in the SOURCE project's log (docs/52 § catalog promote)
    entries = read_audit_entries(
        repo / "projects" / "hello_lab", type="catalog"
    )
    assert len(entries) == 1
    assert entries[0].commit_sha == head_after
    assert entries[0].eval is not None and entries[0].eval.after_score == 1.0
    assert entries[0].operator.human_email == "promoter@example.com"


@pytest.mark.integration
def test_repromoting_identical_content_is_refused(repo: Path) -> None:
    _promote(repo, "hello_lab/tool/word_stats")
    head = _git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(CatalogPromotionRefused, match="content-identical"):
        _promote(repo, "hello_lab/tool/word_stats")
    # no new catalog version, no new commit
    versions = sorted(
        p.name for p in (repo / "catalog" / "tools" / "word_stats").iterdir()
        if p.is_dir()
    )
    assert versions == ["v1"]
    assert _git(repo, "rev-parse", "HEAD").strip() == head


@pytest.mark.integration
def test_existing_catalog_versions_are_never_overwritten(repo: Path) -> None:
    """The destination is always latest+1 BY CONSTRUCTION (every existing
    v<N>/ directory is counted before the slot is chosen), so a legitimate
    promotion can never land on an existing version. The residual case — the
    computed slot is occupied by something that is not a version directory —
    is refused outright rather than overwritten."""
    _promote(repo, "hello_lab/tool/word_stats")  # catalog v1

    # give the NEXT promotion something new to ship (additive local v3) ...
    _write_tool_version(
        repo / "projects" / "hello_lab" / "tools" / "word_stats",
        "word_stats", "v3",
        schemas=_stats_schemas(
            "    words: int\n    characters: int\n    lines: int\n"
        ),
        handler=_stats_handler(
            "words=len(inputs.text.split()), characters=len(inputs.text), "
            "lines=1"
        ),
        eval_yaml=_stats_eval(
            "word_stats", "v3", "{ words: 3, characters: 17, lines: 1 }"
        ),
    )
    # ... and squat the computed destination slot with a stray FILE
    squat = repo / "catalog" / "tools" / "word_stats" / "v2"
    squat.write_text("# squatting file\n")
    with pytest.raises(CatalogPromotionRefused, match="never overwritten"):
        _promote(repo, "hello_lab/tool/word_stats")
    assert squat.read_text() == "# squatting file\n"  # untouched


# --- gate 6: eval floor blocks promotion -----------------------------------------------


@pytest.mark.integration
def test_promotion_blocked_below_eval_floor(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(CatalogPromotionRefused, match="below the promotion floor") as exc_info:
        _promote(repo, "hello_lab/tool/flaky_stats")
    assert exc_info.value.context["score"] == 0.0
    assert exc_info.value.context["floor"] == 0.85
    assert not (repo / "catalog" / "tools" / "flaky_stats").exists()
    assert _git(repo, "rev-parse", "HEAD").strip() == head

    # the floor is configurable — a (deliberately absurd) floor of 0 passes
    result = _promote(repo, "hello_lab/tool/flaky_stats", floor=0.0)
    assert result.catalog_ref == "catalog/flaky_stats@v1"  # type: ignore[attr-defined]


# --- semver discipline (docs/50) ---------------------------------------------------------


@pytest.mark.integration
def test_breaking_promotion_warns_blocks_and_records(repo: Path) -> None:
    _promote(repo, "hello_lab/tool/word_stats")  # catalog v1 (from local v2)

    # local v3 REMOVES `words` from the output schema — breaking
    _write_tool_version(
        repo / "projects" / "hello_lab" / "tools" / "word_stats",
        "word_stats", "v3",
        schemas=_stats_schemas("    characters: int\n"),
        handler=_stats_handler("characters=len(inputs.text)"),
        eval_yaml=_stats_eval("word_stats", "v3", "{ characters: 17 }"),
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "word_stats v3 (breaking)")

    with pytest.raises(CatalogPromotionRefused, match="strict-semver"):
        _promote(repo, "hello_lab/tool/word_stats", strict_semver=True)

    # the operator can decline at the warning prompt
    with pytest.raises(CatalogPromotionRefused, match="declined"):
        _promote(
            repo, "hello_lab/tool/word_stats", confirm=lambda _msg: False
        )

    warnings: list[str] = []

    def confirm(message: str) -> bool:
        warnings.append(message)
        return True

    result = _promote(repo, "hello_lab/tool/word_stats", confirm=confirm)
    assert result.schema_change == "breaking"  # type: ignore[attr-defined]
    assert warnings and "removed field `words`" in warnings[0]
    metadata = load_versions_metadata(
        repo / "catalog" / "tools" / "word_stats" / "versions.json"
    )
    v2 = metadata.get("v2")
    assert v2 is not None and v2.schema_change == "breaking"
    assert any("removed field `words`" in b for b in v2.breaking_changes)


# --- the Phase 4 seam: connection-requiring tools ----------------------------------------


@pytest.mark.integration
def test_connection_requiring_tool_promotes_via_project_bindings(
    repo: Path,
) -> None:
    result = _promote(
        repo, "hello_lab/tool/fetch_json", transport=_lab_transport()
    )
    assert result.catalog_ref == "catalog/fetch_json@v1"  # type: ignore[attr-defined]
    assert result.eval_score == 1.0  # type: ignore[attr-defined]
    metadata = load_versions_metadata(
        repo / "catalog" / "tools" / "fetch_json" / "versions.json"
    )
    assert metadata.get("v1") is not None


@pytest.mark.integration
def test_unbound_connection_requiring_tool_is_a_structured_refusal(
    repo: Path,
) -> None:
    with pytest.raises(CatalogPromotionRefused) as exc_info:
        _promote(
            repo, "hello_lab/tool/orphan_fetch", transport=_lab_transport()
        )
    message = str(exc_info.value)
    assert "orphan_fetch" in message
    assert "does not bind tool" in message  # remediation is in the error
    assert not (repo / "catalog" / "tools" / "orphan_fetch").exists()


# --- connection promotion (health-gated) ---------------------------------------------------


@pytest.mark.integration
def test_connection_promotes_when_health_passes(repo: Path) -> None:
    result = _promote(
        repo, "hello_lab/connection/time_api", transport=_lab_transport()
    )
    assert result.catalog_ref == "catalog/time_api@v1"  # type: ignore[attr-defined]
    assert result.eval_score == 1.0  # type: ignore[attr-defined]
    dest = repo / "catalog" / "connections" / "time_api" / "v1"
    for required in ("connection.yaml", "auth.py", "schemas.py",
                     "health.yaml", "README.md"):
        assert (dest / required).is_file()
    index_text = (repo / "catalog" / "index.yaml").read_text()
    assert "connections:\n  - time_api" in index_text
    entries = read_audit_entries(
        repo / "projects" / "hello_lab", type="catalog"
    )
    assert entries[-1].summary.endswith("catalog/time_api@v1")


@pytest.mark.integration
def test_connection_promotion_blocked_when_health_fails(repo: Path) -> None:
    def unhealthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False})

    with pytest.raises(CatalogPromotionRefused, match="health check"):
        _promote(
            repo, "hello_lab/connection/time_api",
            transport=httpx.MockTransport(unhealthy),
        )
    assert not (repo / "catalog" / "connections" / "time_api").exists()


# --- CLI executor ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_promote_from_repo_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repo)
    code = execute_catalog_promote("hello_lab/tool/word_stats", assume_yes=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "Promoted hello_lab/tools/word_stats@v2 → catalog/word_stats@v1" in out
    assert "eval score: 1.00" in out

    # refusal path exits 1 with the structured reason on stderr
    code = execute_catalog_promote("hello_lab/tool/flaky_stats", assume_yes=True)
    captured = capsys.readouterr()
    assert code == 1
    assert "below the promotion floor" in captured.err


@pytest.mark.integration
def test_invalid_promotion_targets_are_refused(repo: Path) -> None:
    with pytest.raises(CatalogPromotionRefused, match="expected"):
        _promote(repo, "word_stats")
    with pytest.raises(CatalogPromotionRefused, match="unsupported promotion kind"):
        _promote(repo, "hello_lab/retriever/foo")
    with pytest.raises(CatalogPromotionRefused, match="no versions"):
        _promote(repo, "hello_lab/tool/does_not_exist")
    with pytest.raises(CatalogPromotionRefused, match="not found"):
        _promote(repo, "ghost_project/tool/word_stats")
