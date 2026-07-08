"""LangGraph-facing type shims.

Permitted to import ``langgraph`` / ``langchain_core`` (import-boundary lint).
Phase 2 needs only the graph-state shape: the project's state dict threads
through every node; ``output`` carries the flow agent's final parsed output.
"""

from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    """State schema for the Phase 2 single/sequential StateGraph."""

    state: dict[str, Any]
    output: Any


__all__ = ["GraphState"]
