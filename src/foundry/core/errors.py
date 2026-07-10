"""FoundryError hierarchy.

Every error crossing a public boundary subclasses ``FoundryError`` and provides
a serialisable ``to_dict()`` for the audit trail. See docs/10-core-framework.md
§ Exception hierarchy for the contract.
"""

from __future__ import annotations

from typing import Any


def _walk_causes(exc: BaseException | None) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    cur: BaseException | None = exc
    while cur is not None:
        chain.append({"error_class": type(cur).__name__, "message": str(cur)})
        cur = cur.__cause__
    return chain


class FoundryError(Exception):
    """Root for all errors raised by foundry."""

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = dict(context or {})
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": type(self).__name__,
            "message": str(self),
            "context": self.context,
            "cause_chain": _walk_causes(self.__cause__),
        }


# --- Config -----------------------------------------------------------------


class ConfigError(FoundryError):
    """Anything wrong with loading or validating a config."""


class ConfigLoadError(ConfigError):
    """File missing, unreadable, YAML invalid, env var unresolved, etc."""


class ConfigValidationError(ConfigError):
    """Pydantic rejected a config shape; carries pointer + received/expected."""


class StateVisibilityError(ConfigError):
    """Compile-time: an agent reads or writes a forbidden state field."""


# --- Provider ---------------------------------------------------------------


class ProviderError(FoundryError):
    """Anything originating from a provider call."""

    retryable: bool = False


class ProviderAuthError(ProviderError):
    retryable = False


class ProviderRateLimitError(ProviderError):
    retryable = True


class ProviderTimeoutError(ProviderError):
    retryable = True


class ProviderContentPolicyError(ProviderError):
    retryable = False


class ProviderConfigError(ProviderError):
    """Unknown provider, unknown model, declared capability missing, etc."""

    retryable = False


class ProviderUnexpectedError(ProviderError):
    retryable = False


# --- Tool -------------------------------------------------------------------


class ToolError(FoundryError):
    pass


class ToolInputValidationError(ToolError):
    pass


class ToolOutputValidationError(ToolError):
    pass


class ToolHandlerError(ToolError):
    pass


class ToolNotAllowedError(ToolError):
    pass


class ToolNotFoundError(ToolError):
    pass


# --- Orchestration ----------------------------------------------------------


class OrchestrationError(FoundryError):
    pass


class UnknownPatternError(OrchestrationError):
    pass


class CompileError(OrchestrationError):
    pass


class CyclicDependencyError(OrchestrationError):
    pass


class MaxHopsExceededError(OrchestrationError):
    pass


class IterationLimitError(OrchestrationError):
    """Agent exceeded its per-invocation LLM-call round budget
    (AgentSpec.iteration_limit)."""


class CostBudgetExceeded(OrchestrationError):
    """Next provider call would breach Guardrails.max_cost_usd."""


# --- Checkpoint -------------------------------------------------------------


class CheckpointError(FoundryError):
    pass


class CheckpointWriteError(CheckpointError):
    pass


class CheckpointReadError(CheckpointError):
    pass


# --- Connection -------------------------------------------------------------


class ConnectionError(FoundryError):
    pass


class ConnectionConfigError(ConnectionError):
    pass


class ConnectionAuthError(ConnectionError):
    pass


class ConnectionTimeoutError(ConnectionError):
    pass


class ConnectionHealthCheckError(ConnectionError):
    pass


class ConnectionPoolExhausted(ConnectionError):
    pass


class ConnectionSlotNotDeclaredError(ConnectionError):
    pass


class ConnectionSlotNotBoundError(ConnectionError):
    pass


class ConnectionRefreshError(ConnectionError):
    pass


# --- Embedder ---------------------------------------------------------------


class EmbedderError(FoundryError):
    pass


class EmbedderConfigError(EmbedderError):
    pass


class EmbedderAuthError(EmbedderError):
    pass


class EmbedderTimeoutError(EmbedderError):
    pass


class EmbedderUnexpectedError(EmbedderError):
    pass


# --- Cache ------------------------------------------------------------------


class CacheError(FoundryError):
    pass


class CacheBackendError(CacheError):
    pass


class CacheCorruptedEntry(CacheError):
    pass


# --- Retrieval ----------------------------------------------------------------


class RetrievalError(FoundryError):
    """A retriever failed at call time (backing store error, both hybrid
    branches down, unknown slot, ...)."""


class RerankError(FoundryError):
    """A reranker call failed. Pipelines treat this as fall-through-with-
    warning by default (docs/25 § Failure modes)."""


# --- Memory -----------------------------------------------------------------


class MemoryError(FoundryError):
    pass


class MemoryConfigError(MemoryError):
    pass


class MemoryLayerError(MemoryError):
    pass


class MemoryConsolidateError(MemoryError):
    pass


# --- Control-flow (not errors per se) --------------------------------------


class ApprovalRequired(FoundryError):
    """Control flow — raised to signal a HITL pause, not a true error
    (docs/32 § Three places approvals are raised).

    Raised by tool handlers when an action needs a human decision. The
    runtime catches it, persists the pending state in the checkpointer,
    emits ``approval.required``, and pauses the run. The ``approval_id``
    MUST be stable across re-invocation (hash the inputs and/or thread
    ``ctx.run_id``) — after resolution the same handler re-runs and checks
    ``ctx.approval_resolved(approval_id)`` instead of re-raising.

    ``timeout_s`` / ``on_timeout`` are accepted for forward compatibility
    with the docs/32 timeout contract; the framework-level timer lands
    with the API layer (Phase 8) — v1 approvals wait indefinitely.
    """

    def __init__(
        self,
        *,
        approval_id: str,
        prompt: str,
        context: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        on_timeout: str = "reject",
    ) -> None:
        super().__init__(
            f"approval required [{approval_id}]: {prompt}",
            context={"approval_id": approval_id, **(context or {})},
        )
        self.approval_id = approval_id
        self.prompt = prompt
        self.approval_context: dict[str, Any] = dict(context or {})
        self.timeout_s = timeout_s
        self.on_timeout = on_timeout


class RunCancelled(FoundryError):
    """Raised when session.cancel_token transitions to cancelled."""


# --- Versioning -------------------------------------------------------------


class VersioningError(FoundryError):
    pass


class RefResolutionError(VersioningError):
    pass


class PinConflictError(VersioningError):
    pass


class RollbackError(VersioningError):
    pass


class GitBackendError(VersioningError):
    """A git subprocess operation failed (docs/51 § Failure modes).
    ``context`` carries the failing argv, returncode, and stderr."""


class CatalogPromotionRefused(VersioningError):
    """`foundry catalog promote` refused: eval below floor, overwrite
    attempt, schema-breaking under --strict-semver, or an artifact whose
    eval cannot run (docs/50 § Versioning failure modes)."""


__all__ = [
    "ApprovalRequired",
    "CacheBackendError",
    "CacheCorruptedEntry",
    "CacheError",
    "CatalogPromotionRefused",
    "CheckpointError",
    "CheckpointReadError",
    "CheckpointWriteError",
    "CompileError",
    "ConfigError",
    "ConfigLoadError",
    "ConfigValidationError",
    "ConnectionAuthError",
    "ConnectionConfigError",
    "ConnectionError",
    "ConnectionHealthCheckError",
    "ConnectionPoolExhausted",
    "ConnectionRefreshError",
    "ConnectionSlotNotBoundError",
    "ConnectionSlotNotDeclaredError",
    "ConnectionTimeoutError",
    "CostBudgetExceeded",
    "CyclicDependencyError",
    "EmbedderAuthError",
    "EmbedderConfigError",
    "EmbedderError",
    "EmbedderTimeoutError",
    "EmbedderUnexpectedError",
    "FoundryError",
    "GitBackendError",
    "IterationLimitError",
    "MaxHopsExceededError",
    "MemoryConfigError",
    "MemoryConsolidateError",
    "MemoryError",
    "MemoryLayerError",
    "OrchestrationError",
    "PinConflictError",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderContentPolicyError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnexpectedError",
    "RefResolutionError",
    "RerankError",
    "RetrievalError",
    "RollbackError",
    "RunCancelled",
    "StateVisibilityError",
    "ToolError",
    "ToolHandlerError",
    "ToolInputValidationError",
    "ToolNotAllowedError",
    "ToolNotFoundError",
    "ToolOutputValidationError",
    "UnknownPatternError",
    "VersioningError",
]
