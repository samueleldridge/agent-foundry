"""Phase 2b exit-gate integration tests against projects/rag_hello + catalog.

Same posture as the 2a suite: no live API keys exist — the full real path
(compile, retriever wiring, embedders, semantic + tool caches, hybrid
retrieval, rerank, LLM ⇄ tool loop) runs with only the HTTP layer replaced
by httpx.MockTransport.
"""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.run import execute_run

REPO_ROOT = Path(__file__).resolve().parents[2]
RAG_DIR = REPO_ROOT / "projects" / "rag_hello"

INPUT = '{"query": "what is the capital of France?"}'


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-voyage-key-for-tests")
    monkeypatch.setenv("COHERE_API_KEY", "fake-cohere-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _vector(text: str, dims: int = 1024) -> list[float]:
    """Deterministic pseudo-embedding: identical text → identical vector
    (cosine 1.0), different text → uncorrelated vector."""
    rng = random.Random(text)
    return [rng.uniform(-1, 1) for _ in range(dims)]


def _tool_use_turn(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "Searching."}, *blocks],
        "stop_reason": "tool_use",
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 100, "output_tokens": 30},
    }


def _search_block(block_id: str = "tu_1") -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": block_id,
        "name": "search_docs",
        "input": {"query": "capital of France"},
    }


def _final_turn() -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(
            {"answer": "Paris is the capital of France.",
             "sources": ["FR-001"]}
        )}],
        "stop_reason": "end_turn",
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 200, "output_tokens": 40},
    }


class RagTransport:
    """Scripted fake for the three vendors rag_hello touches."""

    def __init__(self, llm_turns: list[dict[str, Any]] | None = None) -> None:
        self.llm_turns = llm_turns or [
            _tool_use_turn(_search_block()), _final_turn()
        ]
        self.llm_requests: list[dict[str, Any]] = []
        self.embed_requests: list[dict[str, Any]] = []
        self.rerank_requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.anthropic.com":
            self.llm_requests.append(json.loads(request.content))
            index = min(len(self.llm_requests) - 1, len(self.llm_turns) - 1)
            return httpx.Response(200, json=self.llm_turns[index])
        if host == "api.voyageai.com":
            body = json.loads(request.content)
            self.embed_requests.append(body)
            return httpx.Response(200, json={
                "data": [{"index": i, "embedding": _vector(t)}
                         for i, t in enumerate(body["input"])],
                "usage": {"total_tokens": 7 * len(body["input"])},
            })
        if host == "api.cohere.com":
            body = json.loads(request.content)
            self.rerank_requests.append(body)
            count = len(body["documents"])
            # reverse the input order so reordering is observable
            results = [
                {"index": count - 1 - i, "relevance_score": 0.9 - 0.1 * i}
                for i in range(min(count, body.get("top_n", count)))
            ]
            return httpx.Response(200, json={
                "results": results,
                "meta": {"billed_units": {"search_units": 1}},
            })
        raise AssertionError(f"unexpected host: {host}")

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _run_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "foundry_home" / "runs"
    return sorted(root.iterdir()) if root.exists() else []


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]


def _metadata(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "metadata.json").read_text())


def _copy_rag(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    shutil.copytree(RAG_DIR, project)
    return project


def _by_event(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] == name]


# --- hero path: retrieval + rerank + caches, end to end ------------------------------


@pytest.mark.integration
def test_first_run_retrieves_reranks_and_populates_caches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = RagTransport()
    code = execute_run(RAG_DIR, INPUT, transport=transport.build())
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["answer"] == "Paris is the capital of France."

    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)

    # semantic cache: miss then store, all on this run_id
    assert len(_by_event(events, "cache.semantic.miss")) == 1
    assert len(_by_event(events, "cache.semantic.store")) == 1

    # hybrid retrieval fanned out: both branches + the merged event
    hybrid = [
        e for e in _by_event(events, "retrieval") if e["kind"] == "hybrid"
    ]
    assert len(hybrid) == 1
    assert set(hybrid[0]["branch_latency_ms"]) == {"dense", "sparse"}
    assert hybrid[0]["branches_failed"] == []
    assert hybrid[0]["returned"] > 0
    kinds = {e["kind"] for e in _by_event(events, "retrieval")}
    assert kinds == {"dense", "sparse", "hybrid"}

    # rerank: reordered (Cohere fake reverses), truncated to 3, cost present
    rerank = _by_event(events, "rerank")[0]
    assert rerank["before_ids"] != rerank["after_ids"]
    assert len(rerank["after_ids"]) == 3
    assert rerank["cost_estimate_usd"] is not None
    assert float(rerank["cost_estimate_usd"]) > 0

    # tool-result cache stored; embed events emitted; one run_id throughout
    assert len(_by_event(events, "cache.tool.store")) == 1
    assert len(_by_event(events, "embed")) >= 1
    assert {e["run_id"] for e in events} == {run_dir.name}

    metadata = _metadata(run_dir)
    assert metadata["llm_call_count"] == 2
    # no secret material anywhere in the artifact
    combined = "".join(p.read_text() for p in run_dir.iterdir() if p.is_file())
    for secret in ("fake-anthropic-key-for-tests", "fake-voyage-key-for-tests",
                   "fake-cohere-key-for-tests"):
        assert secret not in combined


@pytest.mark.integration
def test_semantic_cache_hit_on_rerun_skips_llm_and_reports_savings(
    tmp_path: Path,
) -> None:
    transport = RagTransport()
    assert execute_run(RAG_DIR, INPUT, transport=transport.build()) == 0
    llm_calls_after_first = len(transport.llm_requests)

    assert execute_run(RAG_DIR, INPUT, transport=transport.build()) == 0
    assert len(transport.llm_requests) == llm_calls_after_first  # NO new LLM call

    run1, run2 = _run_dirs(tmp_path)[-2:]
    hit = _by_event(_events(run2), "cache.semantic.hit")[0]
    assert hit["similarity"] >= hit["threshold"]
    assert hit["saved_cost_estimate_usd"] is not None
    assert float(hit["saved_cost_estimate_usd"]) > 0
    assert hit["saved_tokens_estimate"] > 0

    meta1, meta2 = _metadata(run1), _metadata(run2)
    assert meta1["llm_call_count"] == 2
    assert meta2["llm_call_count"] == 0
    assert meta2["output"] == meta1["output"]  # cached answer replayed


@pytest.mark.integration
def test_prompt_version_bump_invalidates_semantic_cache(tmp_path: Path) -> None:
    project = _copy_rag(tmp_path, "rag_bump")
    transport = RagTransport()
    assert execute_run(project, INPUT, transport=transport.build()) == 0

    # bump the prompt: new v2 file + agent.yaml pin
    agent_dir = project / "agents" / "rag_agent"
    v1 = (agent_dir / "prompts" / "v1.md").read_text()
    (agent_dir / "prompts" / "v2.md").write_text(
        v1 + "\nAlways answer in exactly one sentence.\n"
    )
    agent_yaml = agent_dir / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "version: v1\n  path: prompts/v1.md",
            "version: v2\n  path: prompts/v2.md",
        )
    )

    transport2 = RagTransport()
    assert execute_run(project, INPUT, transport=transport2.build()) == 0
    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)
    invalidate = _by_event(events, "cache.semantic.invalidate")
    assert len(invalidate) == 1
    assert invalidate[0]["reason"] == "agent_version_changed"
    assert invalidate[0]["previous_version"] != invalidate[0]["current_version"]
    assert len(_by_event(events, "cache.semantic.miss")) == 1
    assert _metadata(run_dir)["llm_call_count"] == 2  # the LLM really ran


# --- tool-result cache ---------------------------------------------------------------


@pytest.mark.integration
def test_tool_cache_hit_on_second_identical_call_in_same_run(
    tmp_path: Path,
) -> None:
    # two rounds calling search_docs with IDENTICAL input, then the answer
    transport = RagTransport(llm_turns=[
        _tool_use_turn(_search_block("tu_1")),
        _tool_use_turn(_search_block("tu_2")),
        _final_turn(),
    ])
    assert execute_run(RAG_DIR, INPUT, transport=transport.build()) == 0
    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)

    assert len(_by_event(events, "cache.tool.miss")) == 1
    assert len(_by_event(events, "cache.tool.hit")) == 1
    hit = _by_event(events, "cache.tool.hit")[0]
    assert hit["tool_ref"] == "local/search_docs"
    assert hit["cached_at"]

    # only ONE actual tool invocation (cache hits short-circuit tool.started)
    tool_calls = [
        json.loads(line)
        for line in (run_dir / "tool_calls.jsonl").read_text().splitlines()
    ]
    assert len(tool_calls) == 1
    # and retrieval ran only once — the second round was served from cache
    assert len([e for e in _by_event(events, "retrieval")
                if e["kind"] == "hybrid"]) == 1


@pytest.mark.integration
def test_cacheable_without_ttl_fails_at_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_rag(tmp_path, "rag_no_ttl")
    tool_yaml = project / "tools" / "search_docs" / "v1" / "tool.yaml"
    tool_yaml.write_text(
        tool_yaml.read_text().replace("cache_ttl_s: 300\n", "")
    )
    code = execute_run(project, INPUT)
    assert code == 2
    err = capsys.readouterr().err
    assert "ConfigValidationError" in err
    assert "cache_ttl_s" in err
    assert not _run_dirs(tmp_path)  # load-time: no run artifact


# --- fail-open + degrade --------------------------------------------------------------


@pytest.mark.integration
def test_semantic_cache_backend_failure_fails_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_rag(tmp_path, "rag_broken_cache")
    agent_yaml = project / "agents" / "rag_agent" / "agent.yaml"
    broken_dir = tmp_path / "not_a_db"
    broken_dir.mkdir()
    # point the sqlite backend at a DIRECTORY → CacheBackendError on every op
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "  backend: in_process\n",
            f"  backend: in_process\n  backend_config:\n    path: {broken_dir}\n",
        )
    )
    transport = RagTransport()
    code = execute_run(project, INPUT, transport=transport.build())
    assert code == 0  # NEVER blocks the run
    printed = json.loads(capsys.readouterr().out)
    assert printed["answer"]
    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)
    warnings = [e for e in _by_event(events, "warning")
                if e["category"] == "cache.semantic.error"]
    assert warnings, "fail-open must be loud"
    assert _metadata(run_dir)["llm_call_count"] == 2  # LLM path was used


@pytest.mark.integration
def test_hybrid_degrades_when_sparse_branch_fails(tmp_path: Path) -> None:
    project = _copy_rag(tmp_path, "rag_sparse_down")
    agent_yaml = project / "agents" / "rag_agent" / "agent.yaml"
    # sparse branch points at a corpus that does not exist → branch fails
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "        config:\n          corpus_path: corpus.json\n      rrf_k: 60",
            "        config:\n          corpus_path: missing.json\n      rrf_k: 60",
        )
    )
    transport = RagTransport()
    assert execute_run(project, INPUT, transport=transport.build()) == 0
    events = _events(_run_dirs(tmp_path)[-1])
    warning = [e for e in _by_event(events, "warning")
               if e["category"] == "retrieval.branch_failed"]
    assert warning and "'sparse'" in warning[0]["message"]
    hybrid = next(e for e in _by_event(events, "retrieval")
                  if e["kind"] == "hybrid")
    assert hybrid["branches_failed"] == ["sparse"]
    assert hybrid["returned"] > 0  # dense results still flowed through


# --- compile-time dimension check ------------------------------------------------------


@pytest.mark.integration
def test_dimension_mismatch_fails_at_load_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_rag(tmp_path, "rag_dims")
    agent_yaml = project / "agents" / "rag_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace("dimensions: 1024", "dimensions: 1536")
    )
    transport = RagTransport()
    code = execute_run(project, INPUT, transport=transport.build())
    assert code == 2
    err = capsys.readouterr().err
    assert "EmbedderConfigError" in err
    assert "1024" in err and "1536" in err  # names both dims
    assert "voyage/voyage-3" in err        # and the disagreeing artifacts
    # AT LOAD: nothing was called, no artifact written
    assert transport.embed_requests == []
    assert transport.llm_requests == []
    assert not _run_dirs(tmp_path)


# --- scope guard ------------------------------------------------------------------------


@pytest.mark.integration
def test_no_phase_2c_fields_leaked_into_agent_spec() -> None:
    from foundry.config import AgentSpec

    assert "memory" not in AgentSpec.model_fields  # 2c, not 2b
