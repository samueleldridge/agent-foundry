# Phase 1 demo — `foundry run projects/hello`

## Hero command

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # live-key step — PENDING OPERATOR
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

## Representative output

Captured from the dev sandbox with the provider HTTP layer faked
(`httpx.MockTransport`) — **no API keys exist in the sandbox**, so the prose
below is the fake's canned greeting; a live run returns real model prose.
Everything else (logs, events, artifact layout, exit codes) is the real path.

```text
2026-07-07T20:48:02.4Z [info] run.starting   artifact_dir=~/.foundry/runs/01KWZ5CYBJSB11BM06YESJP0M3 model=claude-haiku-4-5 project=hello provider=anthropic run_id=01KWZ5CYBJSB11BM06YESJP0M3
2026-07-07T20:48:02.4Z [info] run.started    run_id=01KWZ5CYBJSB11BM06YESJP0M3 sequence=0
2026-07-07T20:48:02.4Z [info] agent.started  run_id=01KWZ5CYBJSB11BM06YESJP0M3 sequence=1
2026-07-07T20:48:02.4Z [info] llm.started    run_id=01KWZ5CYBJSB11BM06YESJP0M3 sequence=2
2026-07-07T20:48:02.4Z [info] llm.completed  run_id=01KWZ5CYBJSB11BM06YESJP0M3 sequence=3
2026-07-07T20:48:02.4Z [info] agent.completed run_id=01KWZ5CYBJSB11BM06YESJP0M3 sequence=4
2026-07-07T20:48:02.4Z [info] run.completed  run_id=01KWZ5CYBJSB11BM06YESJP0M3 sequence=5
{
  "greeting": "Hello, world! Wonderful to see you."
}
```

Artifact left behind:

```text
~/.foundry/runs/01KWZ5CYBJSB11BM06YESJP0M3/
├── metadata.json     # run_id, project, status=completed, provider, model
├── events.jsonl      # run.started → ... → run.completed, sequence 0..5
└── llm_calls.jsonl   # token_usage (incl. reasoning_tokens), cost, latency
```

## The two other one-liners worth showing

Provider swap (edit two lines of `agent.yaml`: `provider: openai`,
`model: gpt-4o-mini`; same command) — PENDING OPERATOR (needs
`OPENAI_API_KEY`).

Unknown provider (`provider: foo`) — runs without keys today:

```text
$ uv run python -m foundry run projects/hello --input '{"name": "world"}'
ProviderConfigError: unknown provider 'foo'; available: anthropic, openai
  file: .../projects/hello/agents/hello_agent/agent.yaml
  pointer: /model_binding/provider
  provider: foo
$ echo $?
2
```

## Status

- Mock-verified end-to-end: ✅ (157 tests green; ruff + mypy --strict clean)
- Live-key smoke test (docs/_manual_tests/phase_1.md Tests 1, 2, 5, 6, 7, 8):
  **pending operator**
