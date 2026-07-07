"""LangGraph-facing type shims.

Permitted to import ``langgraph`` / ``langchain_core`` (import-boundary lint).
Phase 1 needs only the graph-state shape for the single-node graph.
"""

from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    """State schema for the Phase 1 single-agent StateGraph."""

    input: dict[str, Any]
    output: Any


__all__ = ["GraphState"]
