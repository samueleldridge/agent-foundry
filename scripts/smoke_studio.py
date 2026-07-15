"""Studio control-plane smoke: exercise EVERY route group against the
example projects with the provider HTTP layer mocked (docs/03 § Phase 10a
exit gate; also the manual smoke test's backbone).

Runs against a THROWAWAY git repo (copies of projects/hello +
projects/team_hello + the catalog) — never this workspace. No provider
keys needed.

    uv run python scripts/smoke_studio.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "projects").mkdir(parents=True)
    for name in ("hello", "team_hello"):
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
    (repo / ".gitignore").write_text("__pycache__/\nprojects/*/.foundry/\n")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "operator@example.com")
    git("config", "user.name", "Operator")
    git("add", "-A")
    git("commit", "-q", "-m", "smoke fixture")
    return repo


async def main() -> int:
    import httpx
    from studio_helpers import stream_sse, team_transport

    from foundry.studio.app import create_studio_app

    tmp = Path(tempfile.mkdtemp(prefix="studio-smoke-"))
    os.environ["FOUNDRY_HOME"] = str(tmp / "foundry_home")
    os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic-key-for-smoke"
    os.environ["HELLO_SERVICE_API_KEY"] = "fake-service-key-for-smoke"
    repo = make_repo(tmp)
    os.environ["FOUNDRY_CATALOG_ROOTS"] = str(repo / "catalog")

    def hello_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = str(body.get("system", ""))
        if "system prompt" in system and "hello_agent" not in system:
            return team_handler(request)
        user_text = body["messages"][0]["content"][0]["text"]
        try:
            name = json.loads(user_text).get("name", "world")
        except json.JSONDecodeError:
            name = "world"
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"greeting": f"Hello, {name}!"}),
                    }
                ],
                "stop_reason": "end_turn",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        )

    team_mock = team_transport()

    def team_handler(request: httpx.Request) -> httpx.Response:
        return team_mock.handler(request)  # type: ignore[attr-defined]

    app = create_studio_app(
        repo, transport=httpx.MockTransport(hello_handler), serve_assets=True
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://studio",
            timeout=120,
        ) as client:

            async def get(path: str, expect: int = 200) -> Any:
                response = await client.get(path)
                check(f"GET {path}", response.status_code == expect,
                      f"{response.status_code}: {response.text[:200]}")
                return response

            async def post(
                path: str, body: dict[str, Any], expect: int = 200
            ) -> Any:
                response = await client.post(path, json=body)
                check(f"POST {path}", response.status_code == expect,
                      f"{response.status_code}: {response.text[:200]}")
                return response

            print("== health / meta ==")
            await get("/api/health")
            await get("/api/openapi.json")
            placeholder = await client.get("/")
            check("GET / (placeholder or SPA)", placeholder.status_code == 200)

            print("== projects + configs ==")
            await get("/api/projects")
            await get("/api/projects/hello")
            await get("/api/projects/hello/files")
            await get("/api/projects/hello/files/system.yaml")
            await get("/api/schemas/agent")
            bad = await post(
                "/api/projects/hello/validate",
                {
                    "path": "agents/hello_agent/agent.yaml",
                    "content": "name: hello_agent\nstate_visibilty: {}\n",
                },
            )
            check(
                "validate reports structured issues",
                bad.json()["ok"] is False
                and bad.json()["issues"][0]["pointer"] is not None,
            )
            loaded = (
                await get("/api/projects/hello/files/agents/hello_agent/agent.yaml")
            ).json()
            saved = await client.put(
                "/api/projects/hello/files/agents/hello_agent/agent.yaml",
                json={
                    "content": loaded["content"] + "\n# smoke edit\n",
                    "base_hash": loaded["content_hash"],
                },
            )
            check(
                "PUT config commits studio(hello): edit ...",
                saved.status_code == 200
                and saved.json()["commit_message"].startswith("studio(hello)"),
                saved.text[:200],
            )
            refused = await client.put(
                "/api/projects/hello/files/evals/greeting.yaml",
                json={"content": "tampered: true"},
            )
            check(
                "sandbox refuses evals/ write (403)",
                refused.status_code == 403
                and refused.json()["error_class"] == "SandboxViolation",
            )

            print("== catalog / doctor / obs / storage ==")
            await get("/api/catalog")
            await get("/api/catalog/tools/http_get_json")
            await get("/api/catalog/tools/http_get_json/v1/files")
            gated = await post(
                "/api/catalog/promote", {"target": "hello/tool/x"}, expect=400
            )
            check(
                "promote is confirm-gated",
                "confirm" in gated.json().get("message", ""),
            )
            await post(
                "/api/catalog/deprecate",
                {"ref": "tools/http_get_json", "version": "v1", "reason": "x"},
                expect=400,
            )
            await get("/api/doctor")
            for route in (
                "/api/obs/cost",
                "/api/obs/latency",
                "/api/obs/tool-failures",
                "/api/obs/eval-trend",
                "/api/obs/runs",
            ):
                await get(route)
            await get("/api/storage/stats")
            await get("/api/storage/pins")
            await post(
                "/api/storage/gc", {"older_than": "90d", "dry_run": True}
            )
            await post(
                "/api/storage/archive", {"older_than": "90d", "dry_run": True}
            )
            pinned = await client.post(
                "/api/storage/pins",
                json={"kind": "run", "artifact_id": "SMOKE", "reason": "x"},
            )
            check("POST /api/storage/pins", pinned.status_code == 201)
            removed = await client.delete(
                "/api/storage/pins?kind=run&artifact_id=SMOKE"
            )
            check("DELETE /api/storage/pins", removed.status_code == 200)

            print("== chat / runs / approvals ==")
            sid = (
                await post("/api/chat/hello/sessions", {}, expect=201)
            ).json()["session_id"]
            run_id = (
                await post(
                    f"/api/chat/hello/sessions/{sid}/messages",
                    {"text": '{"name": "smoke"}'},
                )
            ).json()["run_id"]
            frames = await stream_sse(
                app,
                f"/api/chat/hello/sessions/{sid}/events",
                stop_when=lambda f: f.get("event") == "run.completed",
            )
            check(
                "chat SSE streams llm.delta → run.completed",
                any(f.get("event") == "llm.delta" for f in frames)
                and frames[-1]["data"]["final_output"]["greeting"]
                == "Hello, smoke!",
            )
            await get("/api/chat/hello/sessions")
            await get("/api/runs")
            await get(f"/api/runs/{run_id}")
            await get(f"/api/runs/{run_id}/artifact")
            await get(f"/api/runs/{run_id}/events")  # bounded: run is done
            await get("/api/approvals")

            print("== team chat approval round-trip ==")
            team_sid = (
                await post("/api/chat/team_hello/sessions", {}, expect=201)
            ).json()["session_id"]
            await post(
                f"/api/chat/team_hello/sessions/{team_sid}/messages",
                {
                    "text": json.dumps(
                        {
                            "request": "the new release shipping",
                            "audience": "the team",
                        }
                    )
                },
            )
            frames = await stream_sse(
                app,
                f"/api/chat/team_hello/sessions/{team_sid}/events",
                stop_when=lambda f: f.get("event") == "approval.required",
            )
            approval_id = frames[-1]["data"]["approval_id"]
            await post(
                f"/api/chat/team_hello/sessions/{team_sid}/approvals",
                {"approval_id": approval_id, "decision": "approved"},
            )
            frames = await stream_sse(
                app,
                f"/api/chat/team_hello/sessions/{team_sid}/events",
                stop_when=lambda f: f.get("event") == "run.completed"
                and f["data"].get("status") == "success",
            )
            check(
                "approval round-trip resumes to run.completed",
                frames[-1]["data"]["status"] == "success",
            )

            print("== evals / versions / graph / connections / deploy ==")
            launched = await post(
                "/api/evals",
                {
                    "scope": "project",
                    "target": "hello",
                    "eval_set": "evals/greeting.yaml",
                },
                expect=202,
            )
            task_id = launched.json()["task_id"]
            for _ in range(300):
                status = (await get(f"/api/tasks/{task_id}")).json()
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.2)
            check(
                "project eval task completes",
                status["status"] == "completed"
                and status["result"]["cases_total"] > 0,
                json.dumps(status)[:300],
            )
            await get("/api/evals")
            eval_run_id = status["result"]["eval_run_id"]
            await get(f"/api/evals/{eval_run_id}")
            await get("/api/projects/hello/versions")
            await get(
                "/api/projects/hello/diff?ref1=HEAD~1&ref2=HEAD"
            )
            dry = await post(
                "/api/projects/hello/rollback",
                {"prompt": "hello_agent", "to": "v1"},
            )
            check("rollback defaults to dry-run", dry.json()["dry_run"] is True)
            await get("/api/projects/hello/compute-version")
            await get("/api/projects/hello/graph")
            await get("/api/projects/team_hello/graph")
            await get("/api/projects/hello/connections")
            await get("/api/projects/hello/connections/time_service")
            refreshed = await post(
                "/api/projects/hello/connections/time_service/refresh", {}
            )
            check("connection refresh", refreshed.json()["refreshed"] is True)
            deploy = await post(
                "/api/projects/hello/deploy",
                {"image": "example.com/hello:latest", "dry_run": True,
                 "skip_eval": True},
            )
            deploy_task = deploy.json()["task_id"]
            for _ in range(300):
                status = (await get(f"/api/tasks/{deploy_task}")).json()
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.2)
            check("deploy dry-run task completes", status["status"] == "completed")

            print("== forge (list is empty; launch exercised in tests) ==")
            await get("/api/forge")

            print("== layouts ==")
            layout = {
                "version": 1,
                "active": "default",
                "dashboards": {"default": {"widgets": []}},
            }
            put = await client.put("/api/layouts", json=layout)
            check("PUT /api/layouts", put.status_code == 200)
            await get("/api/layouts")

    failed = [name for name, ok in CHECKS if not ok]
    print()
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("studio smoke: ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
