"""`foundry.testing` — fixtures + state helpers for project-local pytest.

Public surface per docs/82 § `foundry.testing` module. The pytest glue lives
in ``foundry.testing.pytest_plugin`` (loaded by ``foundry test`` via ``-p``);
this package itself stays pytest-free so importing it never requires a test
runner.
"""

from __future__ import annotations

from foundry.testing.fixtures import (
    MockConnection,
    MockConnectionAccessor,
    MockEmbedder,
    MockProvider,
    MockReranker,
    MockRetriever,
    MockRetrieverAccessor,
    MockSecretsResolver,
    RunContextFixture,
    scripted_transport,
)
from foundry.testing.state import StateBuilder, assert_state_transition, make_state

__all__ = [
    "MockConnection",
    "MockConnectionAccessor",
    "MockEmbedder",
    "MockProvider",
    "MockReranker",
    "MockRetriever",
    "MockRetrieverAccessor",
    "MockSecretsResolver",
    "RunContextFixture",
    "StateBuilder",
    "assert_state_transition",
    "make_state",
    "scripted_transport",
]
