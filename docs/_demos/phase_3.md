# Phase 3 demo — checkpointed LangGraph runs: streaming + kill+resume

## Hero commands

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # live-key step — PENDING OPERATOR

# 1) Streaming: every RunEvent as JSONL, the moment it happens
uv run python -m foundry run projects/hello \
  --input '{"name": "world"}' --stream

# 2) Kill+resume: start with a sqlite checkpointer, Ctrl-C mid-run,
#    then rerun with the SAME run id — it resumes and completes.
uv run python -m foundry run projects/hello \
  --input '{"name": "world"}' --checkpoint sqlite --run-id 01KX20KRXSQS6PCC2HWA3ZE47P
```

## Representative output

Captured from the dev sandbox with the HTTP layer faked
(`httpx.MockTransport` serving api.anthropic.com + worldtimeapi.org) —
**no API keys exist in the sandbox**, so the reply prose is canned;
everything else (StateGraph, checkpointer, resume, spans, streaming) is
the real path.

### `--stream` (JSONL events, typed output last)

```text
{"run_id":"01KX...","sequence":0,"event":"run.started","project":"hello",...}
{"run_id":"01KX...","sequence":1,"event":"agent.started","agent_name":"hello_agent",...}
{"run_id":"01KX...","sequence":2,"event":"llm.started",...}
{"run_id":"01KX...","sequence":3,"event":"llm.completed","stop_reason":"tool_use",...}
{"run_id":"01KX...","sequence":4,"event":"connection","lifecycle":"acquire",...}
{"run_id":"01KX...","sequence":5,"event":"tool.started","tool_ref":"catalog/http_get_json",...}
{"run_id":"01KX...","sequence":6,"event":"tool.completed","success":true,...}
{"run_id":"01KX...","sequence":7,"event":"llm.started",...}
{"run_id":"01KX...","sequence":8,"event":"llm.completed","stop_reason":"end_turn",...}
{"run_id":"01KX...","sequence":9,"event":"agent.completed",...}
{"run_id":"01KX...","sequence":10,"event":"run.completed","status":"success",...}
{
  "greeting": "Hello, world! It's 10:00 UTC."
}
```

### Kill+resume (two processes, one run)

```text
# process 1 (--checkpoint sqlite): dies after the tool call
ProviderAuthError: anthropic rejected credentials (HTTP 401): ...   # exit 1

# process 2, SAME --run-id: picks up at the post-tool LLM round
{ "greeting": "Hello, world! It's 10:00 UTC." }                     # exit 0
```

What the artifact proves (`~/.foundry/runs/<run_id>/`):

- `events.jsonl` — ONE file for both processes, continuous `sequence`
  (0…n), `run.started` twice (once per process), `run.failed` once,
  `run.completed` once, `agent.started` / `tool.completed` exactly ONCE —
  the plan round and the tool were NOT re-executed.
- `metadata.json` — `"status": "completed", "resumed": true,
  "checkpointer": "sqlite"`.
- `~/.foundry/checkpoints/hello.sqlite` — the inspectable checkpoint db
  (`sqlite3 ... '.tables'` → `blobs  checkpoints  writes`).

### Trace spans (docs/01 taxonomy, OTel API)

```text
foundry.run   run_id=01KX... project=hello system_version=<sha> status=success
├── foundry.node  node=hello_agent          agent=hello_agent
├── foundry.node  node=hello_agent__llm     agent=hello_agent
│   └── foundry.llm  provider=anthropic model=claude-haiku-4-5 prompt_tokens=50 ...
├── foundry.node  node=hello_agent__tools   agent=hello_agent
├── foundry.node  node=hello_agent__llm     agent=hello_agent
│   └── foundry.llm  ... stop_reason=end_turn
└── foundry.node  node=hello_agent__finish  agent=hello_agent
```

## Why this matters

The graph is no longer decorative: the LLM ⇄ tool loop and the memory
turn loop are StateGraph nodes, so every boundary is a durable checkpoint.
A run that dies mid-conversation is a resumable object, not a lost one —
the substrate Phase 7 (multi-agent, HITL interrupts) and Phase 8 (API)
build on.
