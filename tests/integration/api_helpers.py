"""Shared fixtures/helpers for the Phase 8 API integration suites.

Pattern: the REAL FastAPI app (create_app on a real project) with only the
provider HTTP layer substituted (httpx.MockTransport — the established
Phase 1 pattern). starlette's TestClient runs the app's lifespan (the run
manager's task group) on a portal thread, so SSE/WS interactions exercise
real concurrency.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"
TEAM_DIR = REPO_ROOT / "projects" / "team_hello"


def openai_ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(payload),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }


def hello_transport() -> httpx.MockTransport:
    """Greets with whatever name the prompt carried (input-reflecting)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # openai wire shape: user message content is a plain string
        user_text = next(
            m for m in body["messages"] if m["role"] == "user"
        )["content"]
        try:
            name = json.loads(user_text).get("name", "world")
        except json.JSONDecodeError:
            name = "world"
        return httpx.Response(
            200, json=openai_ok({"greeting": f"Hello, {name}!"})
        )

    return httpx.MockTransport(handler)


class GatedTransport:
    """First N calls hang (cancellably) until released; later calls answer
    immediately. Drives the kill-mid-stream / cancel exit gates."""

    def __init__(self, hang_calls: int = 1) -> None:
        self.hang_calls = hang_calls
        self.calls = 0
        self.release = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.hang_calls and not self.release.is_set():
            await self.release.wait()
        return httpx.Response(
            200, json=openai_ok({"greeting": "Hello, late world!"})
        )

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def parse_sse(text: str) -> list[dict[str, Any]]:
    """SSE body → list of {id?, event, data} with data JSON-decoded."""
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            if key == "id":
                frame["id"] = int(value)
            elif key == "event":
                frame["event"] = value
            elif key == "data":
                frame["data"] = json.loads(value)
        frames.append(frame)
    return frames


def sse_events(frames: list[dict[str, Any]]) -> list[str]:
    return [f["event"] for f in frames]


def read_artifact_events(
    tmp_path: Path, run_id: str
) -> list[dict[str, Any]]:
    path = tmp_path / "foundry_home" / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def read_artifact_metadata(tmp_path: Path, run_id: str) -> dict[str, Any]:
    path = tmp_path / "foundry_home" / "runs" / run_id / "metadata.json"
    return json.loads(path.read_text())
