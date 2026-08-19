"""Shared fixtures/helpers for the Phase 10a studio integration suites.

Pattern: the REAL studio app (`create_studio_app` on a throwaway git repo
holding copies of the example projects + catalog) with only the provider
HTTP layer substituted (httpx.MockTransport — the established pattern).

Streaming: starlette's TestClient and httpx's ASGITransport both buffer
the FULL response body, so an endless session/forge SSE stream can't be
consumed through them. :func:`stream_sse` drives the ASGI app directly,
parses frames as chunks arrive, and sends ``http.disconnect`` once the
caller's stop condition fires — which is exactly how a browser
EventSource drops a stream.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"
TEAM_DIR = REPO_ROOT / "projects" / "team_hello"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def make_studio_repo(
    tmp_path: Path, *, projects: tuple[str, ...] = ("hello",)
) -> Path:
    """Throwaway git repo: copies of the requested example projects + the
    real catalog, committed on main. Never the real workspace."""
    repo = tmp_path / "studio_repo"
    (repo / "projects").mkdir(parents=True)
    for name in projects:
        shutil.copytree(
            REPO_ROOT / "projects" / name,
            repo / "projects" / name,
            ignore=shutil.ignore_patterns("__pycache__", ".foundry"),
        )
    shutil.copytree(
        REPO_ROOT / "catalog",
        repo / "catalog",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (repo / ".gitignore").write_text(
        "__pycache__/\nprojects/*/.foundry/\n"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "operator@example.com")
    git(repo, "config", "user.name", "Operator")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture: example projects + catalog")
    return repo


async def stream_sse(
    app: Any,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    stop_when: Any,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Collect SSE frames from ``GET path`` until ``stop_when(frame)`` is
    truthy, then disconnect (endless streams end cleanly, like a browser
    closing its EventSource). Frames: {id?, event, data}."""
    stop = asyncio.Event()
    frames: list[dict[str, Any]] = []
    buffer = b""

    async def receive() -> dict[str, Any]:
        await stop.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal buffer
        if message["type"] != "http.response.body":
            return
        buffer += message.get("body", b"")
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            frame: dict[str, Any] = {}
            for line in raw.decode().splitlines():
                key, _, value = line.partition(": ")
                if key == "id":
                    frame["id"] = int(value)
                elif key == "event":
                    frame["event"] = value
                elif key == "data":
                    frame["data"] = json.loads(value)
            frames.append(frame)
            if stop_when(frame):
                stop.set()

    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            # Loopback Host by default: the studio app's DNS-rebinding
            # guard (TrustedHostMiddleware) rejects anything else.
            (b"host", b"localhost"),
            *(
                (key.lower().encode(), value.encode())
                for key, value in (headers or {}).items()
            ),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8400),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout)
    return frames


def sse_events(frames: list[dict[str, Any]]) -> list[str]:
    return [str(frame.get("event", "")) for frame in frames]


# --- scripted team_hello transport (the Phase 7/8 pattern) ---------------------------

TEAM_INPUT = {"request": "the new release shipping", "audience": "the team"}

_AGENT_MARKERS = {
    "coordinator": "coordinator — system prompt",
    "drafter": "drafter — system prompt",
    "publisher": "publisher — system prompt",
}


def _text_turn(payload: dict[str, Any]) -> dict[str, Any]:
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
        "usage": {"prompt_tokens": 60, "completion_tokens": 30},
    }


def _tool_call_turn(
    name: str, arguments: dict[str, Any], call_id: str
) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }


def team_transport() -> httpx.MockTransport:
    """coordinator → drafter → publisher (HITL-gated tool) → coordinator →
    END; per-agent scripted openai chat.completions turns keyed on
    system-prompt markers (the system prompt is the ``role: system``
    message — openai has no top-level ``system`` key)."""
    turns: dict[str, list[dict[str, Any]]] = {
        "coordinator": [
            _tool_call_turn(
                "transfer_to_drafter",
                {"reason": "draft the greeting"},
                "tu_c1",
            ),
            _tool_call_turn(
                "transfer_to_publisher",
                {"reason": "publish the draft"},
                "tu_c2",
            ),
            _tool_call_turn(
                "transfer_to_end",
                {"reason": "published; all done"},
                "tu_c3",
            ),
            _text_turn(
                {
                    "final_summary": "Drafted and published the "
                    "release greeting."
                }
            ),
        ],
        "drafter": [
            _text_turn({"draft": "Hello team - the release shipped!"}),
        ],
        "publisher": [
            _tool_call_turn(
                "publish_greeting",
                {"text": "Hello team - the release shipped!"},
                "tu_p1",
            ),
            _text_turn({"publish_status": "published"}),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = next(
            (
                m["content"]
                for m in body["messages"]
                if m["role"] == "system"
            ),
            "",
        )
        agent = next(
            agent
            for agent, marker in _AGENT_MARKERS.items()
            if marker in system
        )
        queue = turns[agent]
        assert queue, f"no scripted turn left for {agent}"
        return httpx.Response(200, json=queue.pop(0))

    return httpx.MockTransport(handler)
