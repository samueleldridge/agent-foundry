"""ArtifactRef parsing + resolution (docs/12 § ArtifactRef parsing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.config import ArtifactRef, FoundryRoots, list_versions, ref_matches_accept
from foundry.core.errors import RefResolutionError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _roots(tmp_path: Path) -> FoundryRoots:
    (tmp_path / "catalog" / "tools" / "demo" / "v1").mkdir(parents=True)
    (tmp_path / "catalog" / "tools" / "demo" / "v2").mkdir()
    (tmp_path / "catalog" / "connections" / "db" / "v1").mkdir(parents=True)
    (tmp_path / "projects" / "proj" / "tools" / "mine" / "v3").mkdir(parents=True)
    return FoundryRoots(
        catalog_roots=[tmp_path / "catalog"],
        projects_root=tmp_path / "projects",
        project_name="proj",
    )


@pytest.mark.unit
def test_parse_inline_version_round_trips() -> None:
    ref = ArtifactRef.parse("catalog/query_db@v2", "tool")
    assert (ref.scope, ref.kind, ref.name, ref.version) == (
        "catalog", "tool", "query_db", "v2",
    )
    assert ref.to_str() == "catalog/query_db@v2"
    assert ArtifactRef.parse(ref.to_str(), "tool") == ref


@pytest.mark.unit
def test_parse_binding_shape_takes_version_separately() -> None:
    ref = ArtifactRef.parse("local/validate", "tool", version="v3")
    assert ref.scope == "local" and ref.version == "v3"


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["query_db@v1", "catalog/Query@v1", "catalog/x@1", "shared/x@v1", ""]
)
def test_invalid_refs_rejected(bad: str) -> None:
    with pytest.raises(RefResolutionError):
        ArtifactRef.parse(bad, "tool", version="v1")


@pytest.mark.unit
def test_conflicting_inline_and_binding_versions_rejected() -> None:
    with pytest.raises(RefResolutionError):
        ArtifactRef.parse("catalog/x@v1", "tool", version="v2")


@pytest.mark.unit
def test_missing_version_anywhere_rejected() -> None:
    with pytest.raises(RefResolutionError):
        ArtifactRef.parse("catalog/x", "tool")


@pytest.mark.unit
def test_resolve_tool_and_connection_share_one_code_path(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    tool_dir = ArtifactRef.parse("catalog/demo@v1", "tool").resolve_path(roots)
    conn_dir = ArtifactRef.parse("catalog/db@v1", "connection").resolve_path(roots)
    assert tool_dir == tmp_path / "catalog" / "tools" / "demo" / "v1"
    assert conn_dir == tmp_path / "catalog" / "connections" / "db" / "v1"


@pytest.mark.unit
def test_local_refs_resolve_under_scoped_project(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    path = ArtifactRef.parse("local/mine@v3", "tool").resolve_path(roots)
    assert path == tmp_path / "projects" / "proj" / "tools" / "mine" / "v3"


@pytest.mark.unit
def test_missing_version_error_lists_available_versions(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    with pytest.raises(RefResolutionError) as excinfo:
        ArtifactRef.parse("catalog/demo@v9", "tool").resolve_path(roots)
    assert excinfo.value.context["available_versions"] == ["v1", "v2"]
    assert "v9" in str(excinfo.value)


@pytest.mark.unit
def test_unknown_artifact_error_names_checked_roots(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    with pytest.raises(RefResolutionError) as excinfo:
        ArtifactRef.parse("catalog/ghost@v1", "tool").resolve_path(roots)
    assert excinfo.value.context["checked"]


@pytest.mark.unit
def test_list_versions_sorts_numerically(tmp_path: Path) -> None:
    base = tmp_path / "artifact"
    for version in ("v10", "v2", "v1"):
        (base / version).mkdir(parents=True)
    (base / "not_a_version").mkdir()
    assert list_versions(base) == ["v1", "v2", "v10"]


@pytest.mark.unit
def test_accepts_matching_prefix_and_exact() -> None:
    ref = ArtifactRef.parse("catalog/postgres@v2", "connection")
    assert ref_matches_accept(ref, "catalog/postgres")
    assert ref_matches_accept(ref, "catalog/postgres@v2")
    assert not ref_matches_accept(ref, "catalog/postgres@v1")
    assert not ref_matches_accept(ref, "catalog/pgvector")
    assert not ref_matches_accept(ref, "local/postgres")


@pytest.mark.unit
def test_for_project_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "projects" / "p"
    project.mkdir(parents=True)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(tmp_path / "cat"))
    roots = FoundryRoots.for_project(project)
    assert roots.catalog_roots == [tmp_path / "cat"]
    assert roots.project_name == "p"


@pytest.mark.unit
def test_for_project_walks_up_to_repo_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOUNDRY_CATALOG_ROOTS", raising=False)
    roots = FoundryRoots.for_project(REPO_ROOT / "projects" / "hello")
    assert roots.catalog_roots == [REPO_ROOT / "catalog"]
