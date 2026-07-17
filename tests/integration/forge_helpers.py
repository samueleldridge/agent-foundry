"""Shared harness for the Phase 6 forge integration tests.

Everything runs in THROWAWAY temp git repos (the Phase 5 pattern). No
provider keys anywhere: ONE ``httpx.MockTransport`` serves BOTH sides —

- the META-AGENT's LLM turns (default binding ``openai/gpt-5-mini``, the
  chat.completions wire shape) are SCRIPTED: a fixed list of responses
  whose tool_calls drive the REAL meta-tools against the temp repo;
- the FORGED PROJECT's LLM turns (fixture-pinned ``anthropic`` /
  ``claude-haiku-4-5``, the messages wire shape) are COMPUTED by a
  deterministic responder whose correctness depends on MARKER strings in
  the live system prompt — so a real prompt-file edit + pin move by the
  meta-agent produces a real eval-score movement. Keeping the toy project
  on the anthropic wire also proves the forge stays provider-agnostic:
  meta traffic and project traffic ride different adapters through the
  same transport.

The toy problem (docs/03 § Phase 6 exit gate): a numeric-answer QA agent.
Question kinds: ``words: <phrase>`` (answered via the CATALOG word_count
tool — discovery + pinning), ``digitsum: <digits>`` (answered via a
PROJECT-LOCAL digit_sum tool the meta-agent scaffolds and iterates), and
``reverse: <text>`` (prompt-only skill). The responder answers a kind
correctly only when the pinned prompt carries its marker:
``DIGIT_RULE`` / ``REVERSE_RULE``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from foundry.security.injection import unwrap_tool_output

REPO_ROOT = Path(__file__).resolve().parents[2]
META_MODEL = "gpt-5-mini"  # DEFAULT_META_MODEL_BINDING (openai)
PROJECT_MODEL = "claude-haiku-4-5"

DIGIT_MARKER = "DIGIT_RULE"
REVERSE_MARKER = "REVERSE_RULE"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def make_repo(tmp_path: Path) -> Path:
    """Temp repo: real catalog copy + empty projects/ + initial commit."""
    repo = tmp_path / "repo"
    (repo / "projects").mkdir(parents=True)
    (repo / "projects" / ".gitkeep").write_text("")
    # Mirror the real repo: runtime state under .foundry/ is never tracked
    # (otherwise audit appends dirty the tree and block clean-tree checks).
    (repo / ".gitignore").write_text("__pycache__/\nprojects/*/.foundry/\n")
    shutil.copytree(
        REPO_ROOT / "catalog",
        repo / "catalog",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "op@example.com")
    git(repo, "config", "user.name", "Operator")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture: catalog + projects root")
    return repo


# --- project content (what the scripted meta-agent writes) ----------------------

EVAL_SPEC_YAML = """\
name: qa_numeric
description: Numeric-answer QA over three question kinds.
scope: project
target: qa_bot
cases:
  - id: words_fox
    input: { question: "words: the quick brown fox" }
    expected: { answer: "4" }
    tags: [word_questions]
  - id: words_hello
    input: { question: "words: hello world" }
    expected: { answer: "2" }
    tags: [word_questions]
  - id: words_one
    input: { question: "words: foundry" }
    expected: { answer: "1" }
    tags: [word_questions]
  - id: digits_1234
    input: { question: "digitsum: 1234" }
    expected: { answer: "10" }
    tags: [digit_questions]
  - id: digits_505
    input: { question: "digitsum: 505" }
    expected: { answer: "10" }
    tags: [digit_questions]
  - id: reverse_abc
    input: { question: "reverse: abc" }
    expected: { answer: "cba" }
    tags: [reverse_questions]
scorers:
  - kind: exact
    name: answer_match
    config: { field: answer }
threshold: 0.9
max_parallel: 2
deterministic: true
schema_version: 1
"""

STATE_YAML = """\
schema:
  question:
    type: str
    description: The question to answer.
  answer:
    type: str
    description: The numeric/text answer.
visibility:
  qa_agent:
    read: [question]
    write: [answer]
"""

SYSTEM_YAML = """\
name: qa_bot
description: Numeric-answer QA agent (word counts, digit sums, reversals).
agents: [qa_agent]
flow:
  type: single
  agent: qa_agent
tools:
  word_count:
    ref: catalog/word_count
    version: v1
  digit_sum:
    ref: local/digit_sum
    version: v1
schema_version: 1
"""

AGENT_YAML = """\
name: qa_agent
description: Answers numeric questions with tools.
model_binding:
  provider: anthropic
  model: claude-haiku-4-5
  settings:
    max_tokens: 256
    temperature: 0.0
prompt:
  version: v1
  path: prompts/v1.md
output:
  schema: output_schema.py::Output
tools: [word_count, digit_sum]
iteration_limit: 6
state_visibility:
  read: [question]
  write: [answer]
schema_version: 1
"""

OUTPUT_SCHEMA_PY = '''"""Output schema for qa_agent."""

from pydantic import BaseModel


class Output(BaseModel):
    answer: str
'''

PROMPT_BASE = (
    "You answer short numeric questions.\n"
    "For 'words:' questions call the word_count tool and answer with the "
    "word count.\n"
    "Respond ONLY with JSON {\"answer\": \"<value>\"}.\n"
)
PROMPT_WITH_DIGIT = (
    PROMPT_BASE
    + f"{DIGIT_MARKER}: for 'digitsum:' questions call the digit_sum tool "
    "and answer with its result.\n"
)
PROMPT_WITH_BOTH = (
    PROMPT_WITH_DIGIT
    + f"{REVERSE_MARKER}: for 'reverse:' questions answer with the text "
    "reversed.\n"
)

DIGIT_SCHEMAS_PY = '''"""Schemas for digit_sum."""

from pydantic import BaseModel, ConfigDict


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str
'''

DIGIT_HANDLER_BUGGY = '''"""Handler for digit_sum (first cut)."""

from schemas import Input, Output

from foundry.core.tool import RunContext


async def handle(inputs: Input, ctx: RunContext) -> Output:
    return Output(result=str(len(inputs.text)))
'''

DIGIT_HANDLER_FIXED = '''"""Handler for digit_sum."""

from schemas import Input, Output

from foundry.core.tool import RunContext


async def handle(inputs: Input, ctx: RunContext) -> Output:
    return Output(result=str(sum(int(c) for c in inputs.text if c.isdigit())))
'''

DIGIT_EVAL_YAML = """\
name: digit_sum_standalone
description: Standalone eval for digit_sum.
scope: tool
target: local/digit_sum@v1
cases:
  - id: sum_1234
    input: { text: "1234" }
    expected: { result: "10" }
  - id: sum_505
    input: { text: "505" }
    expected: { result: "10" }
scorers:
  - kind: exact
    name: result_match
    config: { field: result }
threshold: 1.0
schema_version: 1
"""

BOOTSTRAP_FILES = [
    "projects/qa_bot/state.yaml",
    "projects/qa_bot/system.yaml",
    "projects/qa_bot/agents/qa_agent/agent.yaml",
    "projects/qa_bot/agents/qa_agent/prompts/v1.md",
    "projects/qa_bot/agents/qa_agent/output_schema.py",
    "projects/qa_bot/tools/digit_sum/v1/tool.yaml",
    "projects/qa_bot/tools/digit_sum/v1/schemas.py",
    "projects/qa_bot/tools/digit_sum/v1/handler.py",
    "projects/qa_bot/tools/digit_sum/v1/eval.yaml",
    "projects/qa_bot/tools/digit_sum/v1/README.md",
]


def write_scaffolded_project(
    repo: Path, *, prompt_v1: str = PROMPT_BASE
) -> Path:
    """A COMPLETE qa_bot (the post-bootstrap shape) written directly —
    for tests that start from an existing project instead of bootstrap."""
    project = repo / "projects" / "qa_bot"
    (project / "evals").mkdir(parents=True)
    (project / "evals" / "qa.yaml").write_text(EVAL_SPEC_YAML)
    (project / "state.yaml").write_text(STATE_YAML)
    (project / "system.yaml").write_text(SYSTEM_YAML)
    agent = project / "agents" / "qa_agent"
    (agent / "prompts").mkdir(parents=True)
    (agent / "agent.yaml").write_text(AGENT_YAML)
    (agent / "prompts" / "v1.md").write_text(prompt_v1)
    (agent / "output_schema.py").write_text(OUTPUT_SCHEMA_PY)
    tool = project / "tools" / "digit_sum" / "v1"
    tool.mkdir(parents=True)
    (tool / "tool.yaml").write_text(
        "name: digit_sum\n"
        "version: v1\n"
        "description: Sum the digits in a string.\n"
        "input_schema: schemas.py::Input\n"
        "output_schema: schemas.py::Output\n"
        "handler: handler.py::handle\n"
        "standalone_eval: eval.yaml\n"
        "schema_version: 1\n"
    )
    (tool / "schemas.py").write_text(DIGIT_SCHEMAS_PY)
    (tool / "handler.py").write_text(DIGIT_HANDLER_FIXED)
    (tool / "eval.yaml").write_text(DIGIT_EVAL_YAML)
    (tool / "README.md").write_text("# digit_sum\n")
    git(repo, "checkout", "-q", "-b", "foundry/qa_bot")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture: scaffolded qa_bot")
    return project


# --- the transport ---------------------------------------------------------------


def meta_tool_turn(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """One scripted meta turn on the openai chat.completions wire shape:
    ``tool_calls`` with JSON-STRING arguments, finish_reason tool_calls."""
    return {
        "model": META_MODEL,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Working.",
                    "tool_calls": [
                        {
                            "id": f"tu_{i}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(inputs),
                            },
                        }
                        for i, (name, inputs) in enumerate(calls)
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }


def meta_final(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": META_MODEL,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(report),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 30},
    }


def _project_tool_use(name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": "Using a tool."},
            {"type": "tool_use", "id": "ptu_1", "name": name, "input": inputs},
        ],
        "stop_reason": "tool_use",
        "model": PROJECT_MODEL,
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def _project_final(answer: str) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps({"answer": answer})}
        ],
        "stop_reason": "end_turn",
        "model": PROJECT_MODEL,
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def _tool_result_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content") or []
            for piece in inner:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    try:
                        # Phase 9: the runtime wraps tool results in the
                        # docs/83 typed boundary; the fake LLM unwraps it.
                        parsed = json.loads(unwrap_tool_output(piece["text"]))
                    except json.JSONDecodeError:
                        return None
                    return parsed if isinstance(parsed, dict) else None
    return None


def project_response(body: dict[str, Any]) -> dict[str, Any]:
    """The deterministic project-agent LLM: marker-gated correctness."""
    system = str(body.get("system", ""))
    messages = body["messages"]
    first_user = messages[0]["content"][0]["text"]
    question = str(json.loads(first_user).get("question", ""))
    tool_result = _tool_result_payload(messages[-1])

    if question.startswith("words:"):
        phrase = question.split(":", 1)[1].strip()
        if tool_result is None:
            return _project_tool_use("word_count", {"text": phrase})
        return _project_final(str(tool_result.get("words", "?")))
    if question.startswith("digitsum:"):
        digits = question.split(":", 1)[1].strip()
        if DIGIT_MARKER not in system:
            return _project_final("unknown")
        if tool_result is None:
            return _project_tool_use("digit_sum", {"text": digits})
        return _project_final(str(tool_result.get("result", "?")))
    if question.startswith("reverse:"):
        text = question.split(":", 1)[1].strip()
        if REVERSE_MARKER not in system:
            return _project_final("unknown")
        return _project_final(text[::-1])
    return _project_final("unknown")


class ForgeTransport:
    """Routes requests: scripted meta turns (openai host, gpt-5-mini) vs
    computed project turns (anthropic host), keyed on the request's host."""

    def __init__(self, meta_turns: list[dict[str, Any]]) -> None:
        self.meta_turns = meta_turns
        self.meta_index = 0
        self.meta_requests: list[dict[str, Any]] = []
        self.project_requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.host == "api.openai.com":
            assert body["model"] == META_MODEL, body["model"]
            # gpt-5-mini is a REASONING model: the adapter budgets hidden
            # reasoning + visible output via max_completion_tokens and
            # DROPS sampling params (reasoning models 400 on non-default
            # temperature/top_p) — so the binding's 0.1 never hits the wire.
            assert body["max_completion_tokens"] == 16384, body
            assert "max_tokens" not in body, body
            assert "temperature" not in body, body
            self.meta_requests.append(body)
            if self.meta_index >= len(self.meta_turns):
                system = next(
                    (
                        m["content"]
                        for m in body["messages"]
                        if m["role"] == "system"
                    ),
                    "",
                )
                raise AssertionError(
                    f"meta-agent made more LLM calls than scripted "
                    f"({len(self.meta_turns)}); last system prompt started: "
                    f"{str(system)[:120]}"
                )
            turn = self.meta_turns[self.meta_index]
            self.meta_index += 1
            return httpx.Response(200, json=turn)
        assert request.url.host == "api.anthropic.com", request.url
        assert body["model"] == PROJECT_MODEL, body["model"]
        self.project_requests.append(body)
        return httpx.Response(200, json=project_response(body))

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- canned meta scripts ------------------------------------------------------------

EVAL_SPEC_PATH = "projects/qa_bot/evals/qa.yaml"


def bootstrap_turns() -> list[dict[str, Any]]:
    """The scripted bootstrap: discovery → scaffold local tool → iterate
    its handler against its STANDALONE eval until green → scaffold agent +
    configs → one commit → baseline project eval → report."""
    tool_dir = "projects/qa_bot/tools/digit_sum/v1"
    agent_dir = "projects/qa_bot/agents/qa_agent"
    return [
        meta_tool_turn(("list_catalog", {})),
        meta_tool_turn(
            (
                "build_tool",
                {
                    "name": "digit_sum",
                    "description": "Sum the digits in a string.",
                    "kind_hint": "transformation",
                },
            )
        ),
        meta_tool_turn(
            (
                "write_file",
                {"path": f"{tool_dir}/schemas.py", "content": DIGIT_SCHEMAS_PY},
            ),
            (
                "write_file",
                {
                    "path": f"{tool_dir}/handler.py",
                    "content": DIGIT_HANDLER_BUGGY,
                },
            ),
            (
                "write_file",
                {"path": f"{tool_dir}/eval.yaml", "content": DIGIT_EVAL_YAML},
            ),
        ),
        meta_tool_turn(
            ("run_eval", {"scope": "tool", "target": "local/digit_sum@v1"})
        ),
        meta_tool_turn(
            (
                "write_file",
                {
                    "path": f"{tool_dir}/handler.py",
                    "content": DIGIT_HANDLER_FIXED,
                },
            )
        ),
        meta_tool_turn(
            ("run_eval", {"scope": "tool", "target": "local/digit_sum@v1"})
        ),
        meta_tool_turn(
            (
                "build_agent",
                {
                    "name": "qa_agent",
                    "description": "Answers numeric questions with tools.",
                    "provider": "anthropic",
                    "model": PROJECT_MODEL,
                    "state_read": ["question"],
                    "state_write": ["answer"],
                },
            )
        ),
        meta_tool_turn(
            (
                "write_file",
                {"path": f"{agent_dir}/agent.yaml", "content": AGENT_YAML},
            ),
            (
                "write_file",
                {"path": f"{agent_dir}/prompts/v1.md", "content": PROMPT_BASE},
            ),
            (
                "write_file",
                {
                    "path": f"{agent_dir}/output_schema.py",
                    "content": OUTPUT_SCHEMA_PY,
                },
            ),
            (
                "write_file",
                {"path": "projects/qa_bot/state.yaml", "content": STATE_YAML},
            ),
            (
                "write_file",
                {
                    "path": "projects/qa_bot/system.yaml",
                    "content": SYSTEM_YAML,
                },
            ),
        ),
        meta_tool_turn(
            (
                "git_commit",
                {
                    "files": BOOTSTRAP_FILES,
                    "scope": "qa_bot",
                    "summary": "bootstrap: qa_agent + digit_sum tool",
                    "body": "Scaffolded per description; digit_sum "
                    "standalone eval green before wiring.",
                },
            )
        ),
        meta_tool_turn(
            (
                "run_eval",
                {
                    "scope": "project",
                    "target": "qa_bot",
                    "eval_spec_path": EVAL_SPEC_PATH,
                },
            )
        ),
        meta_final(
            {
                "action": "bootstrap_complete",
                "summary": "Scaffolded qa_bot: qa_agent + catalog "
                "word_count + local digit_sum.",
                "change_kind": "bootstrap",
                "artifact": "qa_bot",
                "applied": True,
                "notes": "digit + reverse questions still failing; "
                "prompt lacks the rules.",
            }
        ),
    ]


def prompt_iteration_turns(
    *,
    new_version: str,
    content: str,
    cluster_id: str,
    summary: str,
    eval_before: float,
) -> list[dict[str, Any]]:
    """One improvement iteration: new prompt version → edit → pin →
    commit → project re-eval → report."""
    agent_dir = "projects/qa_bot/agents/qa_agent"
    return [
        meta_tool_turn(("new_prompt_version", {"agent": "qa_agent"})),
        meta_tool_turn(
            (
                "write_file",
                {
                    "path": f"{agent_dir}/prompts/{new_version}.md",
                    "content": content,
                },
            )
        ),
        meta_tool_turn(
            (
                "pin_version",
                {
                    "file": "agents/qa_agent/agent.yaml",
                    "key_path": "prompt.version",
                    "new_version": new_version,
                },
            )
        ),
        meta_tool_turn(
            (
                "git_commit",
                {
                    "files": [
                        f"{agent_dir}/agent.yaml",
                        f"{agent_dir}/prompts/{new_version}.md",
                    ],
                    "scope": "qa_bot/agents/qa_agent",
                    "summary": summary,
                    "cluster_id": cluster_id,
                    "eval_before": eval_before,
                },
            )
        ),
        meta_tool_turn(
            (
                "run_eval",
                {
                    "scope": "project",
                    "target": "qa_bot",
                    "eval_spec_path": EVAL_SPEC_PATH,
                },
            )
        ),
        meta_final(
            {
                "action": "iteration_complete",
                "summary": summary,
                "change_kind": "prompt_edit",
                "artifact": f"agents/qa_agent/prompts/{new_version}.md",
                "cluster_id": cluster_id,
                "hypothesis": "the prompt lacks an explicit rule for this "
                "question kind",
                "applied": True,
            }
        ),
    ]
