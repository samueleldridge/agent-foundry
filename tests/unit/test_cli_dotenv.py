"""Unit tests for the CLI-layer `.env` loader (foundry.cli.dotenv)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.cli.dotenv import find_env_file, load_local_env, parse_env_text


@pytest.mark.unit
def test_parses_common_shapes() -> None:
    text = (
        "# a comment\n"
        "\n"
        "OPENAI_API_KEY=sk-plain\n"
        "export EXPORTED=yes\n"
        'QUOTED="dq value"\n'
        "SQUOTED='sq value'\n"
        "  SPACED  =  trimmed  \n"
        "EMPTY=\n"
    )
    assert parse_env_text(text) == [
        ("OPENAI_API_KEY", "sk-plain"),
        ("EXPORTED", "yes"),
        ("QUOTED", "dq value"),
        ("SQUOTED", "sq value"),
        ("SPACED", "trimmed"),
        ("EMPTY", ""),
    ]


@pytest.mark.unit
def test_skips_malformed_lines_without_raising() -> None:
    text = "no_equals_here\n=novalue\n9BAD=x\nGOOD=1\n"
    assert parse_env_text(text) == [("GOOD", "1")]


@pytest.mark.unit
def test_load_sets_missing_and_never_overrides_real_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("FROM_FILE=filevalue\nALREADY_SET=fromfile\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.setenv("ALREADY_SET", "fromenv")  # real env must win

    applied = load_local_env()

    assert applied == ["FROM_FILE"]
    import os

    assert os.environ["FROM_FILE"] == "filevalue"
    assert os.environ["ALREADY_SET"] == "fromenv"


@pytest.mark.unit
def test_opt_out_disables_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("SHOULD_NOT_LOAD=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOUNDRY_NO_ENV_FILE", "1")
    monkeypatch.delenv("SHOULD_NOT_LOAD", raising=False)

    assert find_env_file() is None
    assert load_local_env() == []
    import os

    assert "SHOULD_NOT_LOAD" not in os.environ


@pytest.mark.unit
def test_explicit_path_and_upward_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_env = tmp_path / ".env"
    root_env.write_text("ROOT=1\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)

    # Upward search from a subdirectory finds the ancestor .env.
    monkeypatch.chdir(sub)
    monkeypatch.delenv("FOUNDRY_ENV_FILE", raising=False)
    assert find_env_file() == root_env

    # Explicit path overrides the search.
    other = tmp_path / "custom.env"
    other.write_text("CUSTOM=1\n")
    monkeypatch.setenv("FOUNDRY_ENV_FILE", str(other))
    assert find_env_file() == other
