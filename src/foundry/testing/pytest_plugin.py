"""Pytest plugin exposing foundry fixtures to project tests (docs/82).

Loaded automatically by ``foundry test`` via ``-p foundry.testing.pytest_plugin``
(no operator conftest changes needed); operators can also add it to their own
pytest runs the same way. This is the ONLY foundry.testing module allowed to
import pytest.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from foundry.core.model import ModelResponse
from foundry.core.tool import RunContext
from foundry.testing.fixtures import (
    MockConnection,
    MockConnectionAccessor,
    MockProvider,
    RunContextFixture,
)


@pytest.fixture
def foundry_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point FOUNDRY_HOME at a fresh temp directory for the test, restoring
    the previous value afterwards."""
    home = tmp_path_factory.mktemp("foundry_home")
    previous = os.environ.get("FOUNDRY_HOME")
    os.environ["FOUNDRY_HOME"] = str(home)
    try:
        yield home
    finally:
        if previous is None:
            os.environ.pop("FOUNDRY_HOME", None)
        else:
            os.environ["FOUNDRY_HOME"] = previous


@pytest.fixture
def run_context() -> RunContext:
    """A default-built RunContext (no connections/retrievers bound)."""
    return RunContextFixture().build()


@pytest.fixture
def mock_connections() -> Callable[[dict[str, Any]], MockConnectionAccessor]:
    """Factory: ``mock_connections({"slot": client_or_mock_connection})`` →
    a fully-bound ``MockConnectionAccessor``."""

    def factory(clients: dict[str, Any]) -> MockConnectionAccessor:
        return MockConnectionAccessor(
            {
                slot: value
                if isinstance(value, MockConnection)
                else MockConnection(client=value)
                for slot, value in clients.items()
            }
        )

    return factory


@pytest.fixture
def mock_provider() -> Callable[..., MockProvider]:
    """Factory: ``mock_provider(response_a, response_b)`` → a scripted
    ``MockProvider``."""

    def factory(
        *responses: ModelResponse, name: str = "mock", model: str = "mock-model"
    ) -> MockProvider:
        return MockProvider(name=name, model=model, responses=list(responses))

    return factory
