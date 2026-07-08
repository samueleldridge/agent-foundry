# Phase 2a demo — one-tool agent through a catalog connection

## Hero commands

```bash
export ANTHROPIC_API_KEY="sk-ant-..."    # live-key step — PENDING OPERATOR
export HELLO_SERVICE_API_KEY=dummy       # worldtimeapi ignores auth; key still injected
uv run python -m foundry run projects/hello --input '{"name": "world"}'
uv run python -m foundry connections health projects/hello/time_service
```

## Representative output

Captured from the dev sandbox with the HTTP layer faked
(`httpx.MockTransport` serving both api.anthropic.com and
worldtimeapi.org) — **no API keys exist in the sandbox**, so the greeting
prose is canned; everything else (compile wiring, pool, auth header, tool
loop, events, artifacts, exit codes) is the real path.

```text
[info] run.starting     artifact_dir=~/.foundry/runs/01KX06K6CE05289ESR1NE5GCZF model=claude-haiku-4-5 project=hello provider=anthropic
[info] run.started      sequence=0
[info] agent.started    sequence=1
[info] llm.started      sequence=2
[info] llm.completed    sequence=3        # stop_reason=tool_use
[info] tool.started     sequence=4        # tool_ref=catalog/http_get_json v1
[info] connection       sequence=5        # lifecycle=acquire ref=catalog/http_service@v1 slot=service
[info] tool.completed   sequence=6        # success=true
[info] llm.started      sequence=7
[info] llm.completed    sequence=8        # stop_reason=end_turn
[info] agent.completed  sequence=9
[info] run.completed    sequence=10
{
  "greeting": "Hello, world! It's 12:34 UTC."
}
```

```text
$ uv run python -m foundry connections health projects/hello/time_service
time_service (catalog/http_service@v1): OK
  [ok] ping (0ms) — GET /api/ip -> 200
```

## The artifact trail

`~/.foundry/runs/<run_id>/` now carries the tool + connection audit:

```text
tool_calls.jsonl   {"tool_ref": "catalog/http_get_json", "tool_version": "v1",
                    "input_hash": "c697b99eec081514", "success": true, ...}
events.jsonl       ... {"event": "connection", "lifecycle": "acquire",
                    "connection_descriptor": {"ref": "catalog/http_service@v1",
                    "slot": "service", "auth_scheme": "api_key",
                    "redacted_config": {"base_url": "...", "timeout_s": 10.0,
                    "health_path": "/api/ip"}}} ...
metadata.json      "pins": {"tools": {"get_time": "catalog/http_get_json@v1"},
                            "connections": {"time_service": "catalog/http_service@v1"}},
                   "connection_pool": {"acquires": 1, "cache_hits": 0,
                                       "builds": 1, "evictions": 0, ...}
```

No secret appears anywhere in the trail — `redacted_config` is
allowlist-projected and the API key exists only inside the pooled client's
headers.

## Pin-swap demo

```bash
# system.yaml: get_time version v1 → v2 (one line), rerun:
#   tool_calls.jsonl shows "tool_version": "v2" and the tool result now
#   carries the fully-resolved request URL (v2's additive output field).

# system.yaml: time_service version v1 → v2 (api_key → basic auth), plus
#   export HELLO_SERVICE_API_KEY='{"username":"u","password":"p"}'
#   → the service sees "Authorization: Basic ..." with no tool change.
```

## Adversarial highlights (all structured, all compile-time where promised)

```text
# slot deleted from system.yaml:
ConnectionSlotNotBoundError: Tool 'get_time' slot 'service' is not bound.
  file: projects/hello/system.yaml
  pointer: /tools/get_time/connection_bindings
  declared slots: service
  bound slots: (none)
  hint: Add `connection_bindings: {service: <connection_name>}` ...

# slot bound to a cohere_rerank connection:
CompileError: Tool 'get_time' slot 'service' does not accept the bound
connection 'catalog/cohere_rerank@v1'.
  accepts: catalog/http_service

# agent reads a field state.yaml doesn't grant:
StateVisibilityError: agent 'hello_agent' declares state_visibility
(read: ['draft_plan', 'name'], ...) that disagrees with state.yaml's
visibility entry (read: ['name'], ...)

# credential pasted into connections.*.config:
ConfigLoadError: Detected likely secret literal at
/connections/time_service/config/api_key ...
```
