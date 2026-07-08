"""Catalog loading: 5-file shape, versions.json, index, artifact listing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.catalog import (
    catalog_entries,
    load_catalog_index,
    load_connection_version,
    load_tool_version,
    load_versions_metadata,
)
from foundry.config import ArtifactRef, FoundryRoots
from foundry.core.errors import CompileError, ConfigLoadError, ConfigValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_roots() -> FoundryRoots:
    return FoundryRoots(
        catalog_roots=[REPO_ROOT / "catalog"],
        projects_root=REPO_ROOT / "projects",
        project_name="hello",
    )


@pytest.mark.unit
def test_seeded_tool_loads_with_schemas_and_handler() -> None:
    ref = ArtifactRef.parse("catalog/word_count@v1", "tool")
    loaded = load_tool_version(ref, _repo_roots())
    assert loaded.spec.name == "word_count"
    assert loaded.input_model.__name__ == "WordCountIn"
    assert loaded.output_model.__name__ == "WordCountOut"
    assert callable(loaded.handler)


@pytest.mark.unit
def test_seeded_connection_loads_factory_and_config_model() -> None:
    ref = ArtifactRef.parse("catalog/http_service@v1", "connection")
    loaded = load_connection_version(ref, _repo_roots())
    assert loaded.spec.auth_scheme.value == "api_key"
    assert loaded.config_model.__name__ == "HTTPServiceConfig"
    assert callable(loaded.factory)
    assert loaded.health_check_path is not None and loaded.health_check_path.exists()


@pytest.mark.unit
def test_handler_and_output_schema_share_class_identity() -> None:
    """Regression: schemas.py loaded once per FILE, not per role — otherwise
    isinstance-based output validation fails on identical-looking classes."""
    import sys

    ref = ArtifactRef.parse("catalog/utc_now@v1", "tool")
    loaded = load_tool_version(ref, _repo_roots())
    handler_module = sys.modules[loaded.handler.__module__]
    assert handler_module.__dict__["UtcNowOut"] is loaded.output_model


@pytest.mark.unit
def test_missing_file_fails_compile_naming_the_file(tmp_path: Path) -> None:
    version_dir = tmp_path / "catalog" / "tools" / "broken" / "v1"
    version_dir.mkdir(parents=True)
    (version_dir / "tool.yaml").write_text("name: broken\n")
    roots = FoundryRoots(
        catalog_roots=[tmp_path / "catalog"],
        projects_root=tmp_path,
        project_name=None,
    )
    with pytest.raises(CompileError) as excinfo:
        load_tool_version(ArtifactRef.parse("catalog/broken@v1", "tool"), roots)
    missing = excinfo.value.context["missing_files"]
    assert set(missing) == {"handler.py", "schemas.py", "eval.yaml", "README.md"}


@pytest.mark.unit
def test_spec_directory_mismatch_rejected(tmp_path: Path) -> None:
    src = REPO_ROOT / "catalog" / "tools" / "word_count" / "v1"
    dst = tmp_path / "catalog" / "tools" / "renamed" / "v1"
    dst.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_file():  # skip __pycache__ from earlier imports
            (dst / f.name).write_text(f.read_text())
    roots = FoundryRoots(
        catalog_roots=[tmp_path / "catalog"],
        projects_root=tmp_path,
        project_name=None,
    )
    with pytest.raises(CompileError) as excinfo:
        load_tool_version(ArtifactRef.parse("catalog/renamed@v1", "tool"), roots)
    assert "immutable" in str(excinfo.value)


@pytest.mark.unit
def test_versions_metadata_loads_for_every_seeded_artifact() -> None:
    for versions_file in (REPO_ROOT / "catalog").rglob("versions.json"):
        metadata = load_versions_metadata(versions_file)
        assert metadata.versions, versions_file
        parent = versions_file.parent
        for entry in metadata.versions:
            assert (parent / entry.version).is_dir(), (
                f"{versions_file} lists {entry.version} but the directory "
                "does not exist"
            )


@pytest.mark.unit
def test_versions_metadata_structured_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError):
        load_versions_metadata(tmp_path / "missing.json")
    bad = tmp_path / "versions.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigLoadError):
        load_versions_metadata(bad)
    bad.write_text(json.dumps({"schema_version": 1, "versions": [{"version": "1"}]}))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_versions_metadata(bad)
    assert excinfo.value.context["pointer"].startswith("/versions/0")


@pytest.mark.unit
def test_catalog_index_lists_seeded_artifacts() -> None:
    index = load_catalog_index(REPO_ROOT / "catalog")
    assert "http_get_json" in index.tools
    assert {"http_service", "postgres", "pgvector", "cohere_rerank"} <= set(
        index.connections
    )


@pytest.mark.unit
def test_catalog_entries_list_tools_and_connections_with_versions() -> None:
    entries = {(e.kind, e.name): e for e in catalog_entries(_repo_roots())}
    assert entries[("tool", "http_get_json")].versions == ["v1", "v2"]
    assert entries[("tool", "http_get_json")].latest == "v2"
    assert entries[("connection", "http_service")].versions == ["v1", "v2"]
    assert entries[("connection", "postgres")].versions == ["v1"]
    assert entries[("connection", "pgvector")].versions == ["v1"]
    assert entries[("connection", "cohere_rerank")].versions == ["v1"]
