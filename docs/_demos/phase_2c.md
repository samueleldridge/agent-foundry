# Phase 2c demo — three-layer memory + FunctionNodes (memory_hello)

## Hero command

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # live-key step — PENDING OPERATOR

uv run python -m foundry run projects/memory_hello --input '{
  "raw_turns": [
    "  hi, my name is Sam  ",
    "I am planning a trip to Paris in October",
    "what should I not miss there?",
    "and remind me — what is my name?"
  ]
}'
```

## Representative output

Captured from the dev sandbox with the HTTP layer faked
(`httpx.MockTransport` serving api.anthropic.com) — **no API keys exist in
the sandbox**, so the reply prose is canned; everything else (compile-time
wiring, sequential graph, function nodes, memory read/weave/write/
consolidate, artifacts) is the real path.

```text
[info] run.started              sequence=0
[info] function_node.started    sequence=1   # normalize_input
[info] function_node.completed  sequence=2   # fields_written=[turns]
[info] agent.started            sequence=3
[info] retrieval                sequence=4   # episode_store (BM25, episodic layer)
[info] memory.read              sequence=5   # layers_read=[short_term,user_facts,past_sessions]
[info] llm.started              sequence=6   # turn 1
[info] llm.completed            sequence=7
[info] memory.write             sequence=8   # episodic ingest of the turn
[info] ...                                   # turns 2-3
[info] memory.consolidate       sequence=19  # turn 3: user_facts rewritten
[info] ...                                   # turn 4 sees the synthesis
[info] agent.completed          sequence=25
[info] function_node.started    sequence=26  # format_output
[info] function_node.completed  sequence=27  # fields_written=[formatted_reply]
[info] run.completed            sequence=28
```

Final state (printed by the CLI for sequential flows; also
`~/.foundry/runs/<run_id>/final_state.json`):

```json
{
  "turns": ["hi, my name is Sam", "..."],
  "messages": ["... 8 FoundryMessages (4 user + 4 assistant) ..."],
  "user_facts": "- Name: Sam\n- Planning a Paris trip in October",
  "reply": "Your name is Sam — and your October Paris plans are safe with me.",
  "formatted_reply": "[memory_hello] Your name is Sam — and your October Paris plans are safe with me."
}
```

## What to look at

- **Working window**: `llm_calls.jsonl` → `prompt_messages` on the last
  turn holds exactly the last 5 conversation messages + the current turn
  (`memory.layers[short_term].window.max_messages: 5`).
- **Episodic layer**: the system prompt's tail carries
  `Relevant past context:` with `[EP-…]` snippets from `episodes.json`;
  each completed turn is ingested back (memory.write events).
- **Semantic layer**: `memory.consolidate` fires every 3 turns with real
  input/output token counts; `final_state.json` → `user_facts` holds the
  synthesis; from the next turn it renders at the TOP of the system prompt.
- **Function nodes**: deterministic Python at both ends of the flow, with
  state visibility structurally enforced (try returning an out-of-scope
  field from `format_output/function.py` — it's dropped with a warning
  event, and `reply` stays the agent's).

## Regression demos (Phase 2 cumulative)

```bash
# 2a hero — unchanged
uv run python -m foundry run projects/hello --input '{"name": "world"}'
# 2b hero — unchanged (semantic cache hit on the second run)
uv run python -m foundry run projects/rag_hello --input '{"query": "what is the capital of France?"}'
```
