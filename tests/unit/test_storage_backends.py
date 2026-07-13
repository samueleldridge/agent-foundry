"""Storage backend unit coverage (docs/81 § Test expectations).

Filesystem backend is exercised for real under tmp_path; the cloud backends'
SDKs are not installed in this repo, so only their lazy-import failure path
(StorageBackendUnavailable naming the missing package) and env-driven
selection are covered here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from foundry.core.errors import StorageBackendUnavailable, StorageError, StorageKeyNotFound
from foundry.storage import (
    AzureBlobBackend,
    FilesystemBackend,
    GCSBackend,
    S3Backend,
    copy_between,
    select_backend,
)


def _skip_if_installed(module: str) -> None:
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:  # parent namespace package absent (e.g. azure.*)
        return
    if spec is not None:
        pytest.skip(f"{module} is installed; lazy-import failure path untestable")


# --- FilesystemBackend --------------------------------------------------------


@pytest.mark.unit
async def test_filesystem_put_get_roundtrip(tmp_path: Path) -> None:
    backend = FilesystemBackend(tmp_path)
    await backend.put("runs/2026/07/r1/meta.json", b'{"ok": true}')
    assert await backend.get("runs/2026/07/r1/meta.json") == b'{"ok": true}'
    assert await backend.exists("runs/2026/07/r1/meta.json")
    assert not await backend.exists("runs/2026/07/r1/absent.json")


@pytest.mark.unit
async def test_filesystem_root_defaults_to_foundry_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "fh"))
    backend = FilesystemBackend()
    assert backend.root == (tmp_path / "fh").resolve()


@pytest.mark.unit
async def test_filesystem_get_missing_key_raises(tmp_path: Path) -> None:
    backend = FilesystemBackend(tmp_path)
    with pytest.raises(StorageKeyNotFound):
        await backend.get("runs/absent.json")
    with pytest.raises(StorageKeyNotFound):
        await backend.get_metadata("runs/absent.json")
    with pytest.raises(StorageKeyNotFound):
        await backend.delete("runs/absent.json")


@pytest.mark.unit
@pytest.mark.parametrize("key", ["../escape", "runs/../../escape", "/etc/passwd", ""])
async def test_filesystem_refuses_traversal_keys(tmp_path: Path, key: str) -> None:
    backend = FilesystemBackend(tmp_path)
    with pytest.raises(StorageError):
        await backend.put(key, b"x")


@pytest.mark.unit
async def test_filesystem_list_respects_prefix_and_limit(tmp_path: Path) -> None:
    backend = FilesystemBackend(tmp_path)
    for name in ("runs/a/trace.jsonl", "runs/b/meta.json", "eval_results/e1/result.json"):
        await backend.put(name, b"content")
    all_runs = await backend.list("runs/")
    assert [entry.key for entry in all_runs] == ["runs/a/trace.jsonl", "runs/b/meta.json"]
    limited = await backend.list("runs/", limit=1)
    assert [entry.key for entry in limited] == ["runs/a/trace.jsonl"]
    assert len(await backend.list("")) == 3
    assert await backend.list("nope/") == []


@pytest.mark.unit
async def test_filesystem_metadata_and_content_type_inference(tmp_path: Path) -> None:
    backend = FilesystemBackend(tmp_path)
    await backend.put("runs/r1/trace.jsonl", b"line\n")
    meta = await backend.get_metadata("runs/r1/trace.jsonl")
    assert meta.key == "runs/r1/trace.jsonl"
    assert meta.size_bytes == 5
    assert meta.last_modified.tzinfo is not None
    assert meta.content_type == "application/x-ndjson"
    listed = await backend.list("runs/r1/")
    assert listed[0].content_type == "application/x-ndjson"
    await backend.put("runs/r1/meta.json", b"{}")
    assert (await backend.get_metadata("runs/r1/meta.json")).content_type == "application/json"
    await backend.put("runs/r1/blob.bin", b"\x00")
    expected = "application/octet-stream"
    assert (await backend.get_metadata("runs/r1/blob.bin")).content_type == expected


@pytest.mark.unit
async def test_filesystem_delete(tmp_path: Path) -> None:
    backend = FilesystemBackend(tmp_path)
    await backend.put("runs/r1/meta.json", b"{}")
    await backend.delete("runs/r1/meta.json")
    assert not await backend.exists("runs/r1/meta.json")


# --- select_backend -------------------------------------------------------------


@pytest.mark.unit
def test_select_backend_defaults_to_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOUNDRY_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("FOUNDRY_STORAGE_ROOT", raising=False)
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "fh"))
    backend = select_backend()
    assert isinstance(backend, FilesystemBackend)
    assert backend.root == (tmp_path / "fh").resolve()


@pytest.mark.unit
def test_select_backend_filesystem_root_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDRY_STORAGE_BACKEND", "filesystem")
    monkeypatch.setenv("FOUNDRY_STORAGE_ROOT", str(tmp_path / "custom"))
    backend = select_backend()
    assert isinstance(backend, FilesystemBackend)
    assert backend.root == (tmp_path / "custom").resolve()


@pytest.mark.unit
def test_select_backend_unknown_kind_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_STORAGE_BACKEND", "carrier_pigeon")
    with pytest.raises(StorageError, match="carrier_pigeon"):
        select_backend()


@pytest.mark.unit
def test_select_backend_s3_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_STORAGE_BACKEND", "s3")
    monkeypatch.delenv("FOUNDRY_STORAGE_BUCKET", raising=False)
    with pytest.raises(StorageError, match="FOUNDRY_STORAGE_BUCKET"):
        select_backend()


@pytest.mark.unit
def test_select_backend_s3_without_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_if_installed("boto3")
    monkeypatch.setenv("FOUNDRY_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("FOUNDRY_STORAGE_BUCKET", "example-artifacts")
    with pytest.raises(StorageBackendUnavailable, match="boto3"):
        select_backend()


# --- cloud lazy-import failure paths ---------------------------------------------


@pytest.mark.unit
def test_s3_backend_without_boto3_names_package() -> None:
    _skip_if_installed("boto3")
    with pytest.raises(StorageBackendUnavailable, match="boto3"):
        S3Backend(bucket="example-artifacts")


@pytest.mark.unit
def test_azure_backend_without_sdk_names_package() -> None:
    _skip_if_installed("azure.storage.blob")
    with pytest.raises(StorageBackendUnavailable, match="azure-storage-blob"):
        AzureBlobBackend(container="example-artifacts")


@pytest.mark.unit
def test_gcs_backend_without_sdk_names_package() -> None:
    _skip_if_installed("google.cloud.storage")
    with pytest.raises(StorageBackendUnavailable, match="google-cloud-storage"):
        GCSBackend(bucket="example-artifacts")


# --- copy_between ---------------------------------------------------------------


@pytest.mark.unit
async def test_copy_between_migrates_all_keys(tmp_path: Path) -> None:
    src = FilesystemBackend(tmp_path / "src")
    dst = FilesystemBackend(tmp_path / "dst")
    payloads = {
        "runs/2026/07/r1/meta.json": b'{"run": 1}',
        "runs/2026/07/r1/trace.jsonl": b"e1\ne2\n",
        "runs/2026/07/r2/meta.json": b'{"run": 2}',
    }
    for key, content in payloads.items():
        await src.put(key, content)
    copied = await copy_between(src, dst, "runs/")
    assert sorted(copied) == sorted(payloads)
    for key, content in payloads.items():
        assert await dst.get(key) == content
        assert await src.get(key) == content  # non-destructive


@pytest.mark.unit
async def test_copy_between_scoped_by_prefix(tmp_path: Path) -> None:
    src = FilesystemBackend(tmp_path / "src")
    dst = FilesystemBackend(tmp_path / "dst")
    await src.put("runs/r1/meta.json", b"{}")
    await src.put("eval_results/e1/result.json", b"{}")
    copied = await copy_between(src, dst, "runs/")
    assert copied == ["runs/r1/meta.json"]
    assert not await dst.exists("eval_results/e1/result.json")
