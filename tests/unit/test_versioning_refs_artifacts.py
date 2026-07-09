"""foundry.versioning.refs + artifacts unit tests (docs/50 § Test
expectations 1, 2, 5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.catalog.schemas import VersionMetadata
from foundry.core.errors import (
    ConfigError,
    RefResolutionError,
    VersioningError,
)
from foundry.versioning.artifacts import (
    append_version_metadata,
    artifact_dir,
    create_next_version_dir,
    list_prompt_versions,
    next_prompt_path,
    next_version_name,
    read_versions_metadata,
)
from foundry.versioning.refs import (
    check_version_contiguity,
    latest_version,
    parse_artifact_ref,
)

# --- parse_artifact_ref (canonical docs/50 forms) --------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ref", "kind", "name", "scope"),
    [
        ("catalog/query_snowflake@v2", "tool", "query_snowflake", "catalog"),
        ("local/validate_deltas@v3", "tool", "validate_deltas", "local"),
        ("catalog/connections/pgvector@v1", "connection", "pgvector", "catalog"),
        ("local/connections/internal_api@v3", "connection", "internal_api",
         "local"),
        ("catalog/agent_templates/router@v1", "agent_template", "router",
         "catalog"),
        ("catalog/retrievers/hybrid_rrf@v1", "retriever", "hybrid_rrf",
         "catalog"),
    ],
)
def test_parse_canonical_forms_round_trip(
    ref: str, kind: str, name: str, scope: str
) -> None:
    parsed = parse_artifact_ref(ref)
    assert parsed.kind == kind
    assert parsed.name == name
    assert parsed.scope == scope
    # 2-segment refs re-serialise identically (docs/50 unit test 1);
    # kind-qualified forms re-serialise to the 2-segment canonical string.
    assert parsed.to_str() == f"{scope}/{name}@{parsed.version}"


@pytest.mark.unit
def test_parse_rejects_unknown_kind_segment() -> None:
    with pytest.raises(RefResolutionError, match="unknown kind segment"):
        parse_artifact_ref("catalog/gadgets/foo@v1")


@pytest.mark.unit
def test_parse_separate_version_pin() -> None:
    parsed = parse_artifact_ref("catalog/word_count", version="v2")
    assert parsed.version == "v2"


# --- contiguity (docs/50 invariant 2) ----------------------------------------------


def _mk_versions(base: Path, *versions: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    for v in versions:
        (base / v).mkdir()
    return base


@pytest.mark.unit
def test_contiguity_passes_and_orders_numerically(tmp_path: Path) -> None:
    base = _mk_versions(tmp_path / "t", "v1", "v2", "v10", "v3")
    # v1..v10 with a hole (v4..v9 missing) fails; build the full run instead
    (base / "v10").rmdir()
    assert check_version_contiguity(base) == ["v1", "v2", "v3"]
    assert latest_version(base) == "v3"


@pytest.mark.unit
def test_contiguity_hole_is_config_error(tmp_path: Path) -> None:
    base = _mk_versions(tmp_path / "t", "v1", "v3")
    with pytest.raises(ConfigError, match="not contiguous") as exc_info:
        check_version_contiguity(base)
    assert exc_info.value.context["missing"] == ["v2"]


@pytest.mark.unit
def test_latest_version_none_for_fresh_artifact(tmp_path: Path) -> None:
    assert latest_version(tmp_path / "does_not_exist") is None


# --- next version / prompt I/O ----------------------------------------------------


@pytest.mark.unit
def test_next_version_dir_is_latest_plus_one(tmp_path: Path) -> None:
    base = _mk_versions(tmp_path / "projects" / "p" / "tools" / "x", "v1", "v2")
    assert next_version_name(base) == "v3"
    created = create_next_version_dir(base)
    assert created == base / "v3" and created.is_dir()
    assert next_version_name(base) == "v4"


@pytest.mark.unit
def test_artifact_dir_layout_and_unknown_kind(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "p"
    assert artifact_dir(project, "tool", "x") == project / "tools" / "x"
    assert (
        artifact_dir(project, "connection", "c") == project / "connections" / "c"
    )
    with pytest.raises(VersioningError, match="unknown artifact kind"):
        artifact_dir(project, "gadget", "x")


@pytest.mark.unit
def test_prompt_versions_and_next_path(tmp_path: Path) -> None:
    prompts = tmp_path / "agents" / "a" / "prompts"
    prompts.mkdir(parents=True)
    for v in ("v1", "v2"):
        (prompts / f"{v}.md").write_text(f"# {v}\n")
    (prompts / "consolidate_v1.md").write_text("# not a default prompt\n")
    assert list_prompt_versions(prompts) == ["v1", "v2"]
    assert next_prompt_path(prompts) == prompts / "v3.md"


@pytest.mark.unit
def test_prompt_gap_is_config_error(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "v1.md").write_text("x")
    (prompts / "v3.md").write_text("x")
    with pytest.raises(ConfigError, match="not contiguous"):
        list_prompt_versions(prompts)


# --- versions.json metadata ---------------------------------------------------------


def _meta(version: str) -> VersionMetadata:
    return VersionMetadata(
        version=version,
        created_at=datetime.now(UTC),
        created_by="human",
        eval_score=0.9,
    )


@pytest.mark.unit
def test_versions_metadata_roundtrip_and_immutability(tmp_path: Path) -> None:
    base = tmp_path / "tools" / "x"
    base.mkdir(parents=True)
    assert read_versions_metadata(base) is None
    append_version_metadata(base, _meta("v1"))
    append_version_metadata(base, _meta("v2"))
    loaded = read_versions_metadata(base)
    assert loaded is not None
    assert [v.version for v in loaded.versions] == ["v1", "v2"]
    # re-recording an existing version is refused (docs/50 invariant 1)
    with pytest.raises(VersioningError, match="immutable"):
        append_version_metadata(base, _meta("v2"))
