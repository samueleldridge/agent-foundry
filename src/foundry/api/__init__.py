"""FastAPI surface generated from SystemSpec (docs/70).

Boundary note: foundry.api does NOT import foundry.configurator — the
configurator is dev-time; the API is run-time (CLAUDE.md import
boundaries).
"""

from __future__ import annotations

from foundry.api.app import create_app, create_app_from_env
from foundry.api.auth import (
    AuthBackend,
    AuthContext,
    BearerTokenAuth,
    NoAuth,
    default_auth_backend,
)
from foundry.api.batch import BatchItem, BatchPolicy, BatchRequest
from foundry.api.runs import LiveRun, RunManager
from foundry.api.schemas import derive_input_model, derive_output_model
from foundry.api.worker import WorkerState, worker_id

__all__ = [
    "AuthBackend",
    "AuthContext",
    "BatchItem",
    "BatchPolicy",
    "BatchRequest",
    "BearerTokenAuth",
    "LiveRun",
    "NoAuth",
    "RunManager",
    "WorkerState",
    "create_app",
    "create_app_from_env",
    "default_auth_backend",
    "derive_input_model",
    "derive_output_model",
    "worker_id",
]
