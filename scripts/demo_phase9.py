#!/usr/bin/env python
"""Phase 9 top-to-bottom demo — mock-provider variant (docs/03 § Phase 9).

Walks the full operator loop in well under 5 minutes with NO API keys and
no network: only the provider HTTP layer is substituted (the same
httpx.MockTransport seam the whole test suite uses — every other layer is
the real framework).

    uv run python scripts/demo_phase9.py

Steps:
  1. bootstrap a project into a scratch git repo (stand-in for `foundry
     forge` — the live-key forge variant is docs/_manual_tests/phase_9.md
     § 4)
  2. eval it (foundry eval → score 1.00)
  3. serve it + hit the API (the real FastAPI app, POST /run)
  4. ship a deliberate prompt regression, watch the eval catch it, and
     roll it back with `foundry rollback --prompt` (score recovers)
  5. view cost metrics (`foundry obs cost --project hello --since 1d`)

The mock LLM is *marker-gated* (the forge-demo pattern): it greets by name
ONLY when the pinned prompt still contains the "addressed to that name"
instruction — so the regression in step 4 is a real behavioural
regression caught by the real eval harness, not a scripted failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKER = "addressed to that name"

BAD_PROMPT = """\
# hello_agent — system prompt v3 (DELIBERATE REGRESSION for the demo)

You are a terse greeter. Produce a short generic greeting. Do not use the
caller's name.
"""


def sh(*argv: str, cwd: Path) -> str:
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def banner(step: str) -> None:
    print(f"\n\033[1m=== {step} ===\033[0m")


def marker_gated_transport() -> httpx.MockTransport:
    """The stand-in LLM: correct behaviour iff the prompt keeps MARKER."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com", request.url.host
        body = json.loads(request.content)
        system = str(body.get("system", ""))
        user_text = body["messages"][0]["content"][0]["text"]
        try:
            name = json.loads(user_text).get("name", "world")
        except json.JSONDecodeError:
            name = "world"
        greeting = (
            f"Hello, {name} — wonderful to see you!"
            if MARKER in system
            else "Hello there."
        )
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps({"greeting": greeting})}],
            "stop_reason": "end_turn",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 60, "output_tokens": 18},
        }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def main() -> int:
    started = time.monotonic()
    workspace = Path(tempfile.mkdtemp(prefix="foundry_demo_"))
    print(f"workspace: {workspace}")

    # Environment: isolated FOUNDRY_HOME; the repo catalog; fake keys for
    # the mock transport (never sent anywhere).
    os.environ["FOUNDRY_HOME"] = str(workspace / ".foundry_home")
    os.environ["FOUNDRY_CATALOG_ROOTS"] = str(REPO_ROOT / "catalog")
    os.environ.setdefault("ANTHROPIC_API_KEY", "fake-anthropic-key-for-demo")
    os.environ.setdefault("HELLO_SERVICE_API_KEY", "fake-service-key-for-demo")
    transport = marker_gated_transport()

    # ---- 1. forge (stand-in): bootstrap the project into a git repo ------
    banner("1/5 forge a tiny project (bootstrap stand-in; live forge → manual §4)")
    sh("git", "init", "-q", cwd=workspace)
    sh("git", "config", "user.email", "demo@example.test", cwd=workspace)
    sh("git", "config", "user.name", "foundry demo", cwd=workspace)
    project_dir = workspace / "projects" / "hello"
    shutil.copytree(
        REPO_ROOT / "projects" / "hello",
        project_dir,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (workspace / ".gitignore").write_text(".foundry_home/\n.foundry/\n")
    sh("git", "checkout", "-q", "-b", "foundry/hello", cwd=workspace)
    sh("git", "add", "-A", cwd=workspace)
    sh("git", "commit", "-q", "-m", "forge(hello): bootstrap greeting project",
       cwd=workspace)
    os.chdir(workspace)
    print(f"project ready on branch foundry/hello ({project_dir})")

    from foundry.cli.eval import execute_eval
    from foundry.cli.obs import execute_cost
    from foundry.cli.rollback import execute_rollback_command

    # ---- 2. eval ---------------------------------------------------------
    banner("2/5 eval the project (foundry eval)")
    code = execute_eval(
        str(project_dir), ["evals/greeting.yaml"], transport=transport
    )
    if code != 0:
        print("unexpected: baseline eval failed", file=sys.stderr)
        return 1

    # ---- 3. serve + hit the API -------------------------------------------
    banner("3/5 serve the project + hit the API (real FastAPI app, POST /run)")
    from fastapi.testclient import TestClient

    from foundry.api.app import create_app

    app = create_app(project_dir, transport=transport, checkpoint="memory")
    with TestClient(app) as client:
        response = client.post("/run", json={"name": "Foundry"})
        print(f"POST /run -> {response.status_code}")
        body = response.json()
        print(json.dumps(body, indent=2)[:400])
        assert response.status_code == 200, body
        assert "Foundry" in body["greeting"]
        health = client.get("/health")
        print(f"GET /health -> {health.status_code} {health.json()['status']}")

    # ---- 4. regression + rollback -----------------------------------------
    banner("4/5 ship a prompt regression, catch it with the eval, roll back")
    prompts = project_dir / "agents" / "hello_agent" / "prompts"
    (prompts / "v3.md").write_text(BAD_PROMPT)
    agent_yaml = project_dir / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text()
        .replace("version: v2", "version: v3")
        .replace("path: prompts/v2.md", "path: prompts/v3.md")
    )
    sh("git", "add", "-A", cwd=workspace)
    sh("git", "commit", "-q", "-m",
       "forge(hello): prompt v3 — 'more concise greeting' (regression!)",
       cwd=workspace)
    print("prompt v3 pinned; re-running the eval gate:")
    regressed = execute_eval(
        str(project_dir), ["evals/greeting.yaml"], transport=transport
    )
    print(f"eval exit code: {regressed} (non-zero == regression caught)")
    if regressed == 0:
        print("unexpected: regression not caught", file=sys.stderr)
        return 1

    print("\nrolling back the prompt pin (foundry rollback --prompt):")
    code = execute_rollback_command(
        str(project_dir), prompt="hello_agent", to="v2", assume_yes=True
    )
    if code != 0:
        print("rollback failed", file=sys.stderr)
        return 1
    print("re-running the eval after rollback:")
    recovered = execute_eval(
        str(project_dir), ["evals/greeting.yaml"], transport=transport
    )
    if recovered != 0:
        print("unexpected: eval still failing post-rollback", file=sys.stderr)
        return 1
    print("score recovered — rollback is one command, audited, committed.")

    # ---- 5. cost metrics ----------------------------------------------------
    banner("5/5 view cost metrics (foundry obs cost --project hello --since 1d)")
    execute_cost(project="hello", since="1d", by="model", json_output=False)

    elapsed = time.monotonic() - started
    banner(f"demo complete in {elapsed:.1f}s (gate: ≤ 5 minutes)")
    print(f"artifacts: {os.environ['FOUNDRY_HOME']}")
    print("live variant (real keys, docker, OTel collector): "
          "docs/_manual_tests/phase_9.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
