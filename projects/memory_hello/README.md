# memory_hello

The Phase 2c example project: a sequential flow

```
normalize_input (function) → hello_agent (agent, 3 memory layers) → format_output (function)
```

- **Working memory** (`short_term`): the last 5 conversation messages,
  windowed from the `messages` state field.
- **Episodic memory** (`past_sessions`): BM25 retrieval over
  `episodes.json` (seeded past-session snippets), injected as a
  `system_suffix` block; each completed turn is ingested back.
- **Semantic memory** (`user_facts`): every 3 turns a consolidator prompt
  (running on the agent's own model binding) rewrites the `user_facts`
  state field.

Run it (multi-turn input drives the agent's turn loop):

```bash
export ANTHROPIC_API_KEY=...
uv run python -m foundry run projects/memory_hello --input '{
  "raw_turns": [
    "  hi, my name is Sam  ",
    "I am planning a trip to Paris in October",
    "what should I not miss there?",
    "and remind me — what is my name?"
  ]
}'
```

Inspect the artifact under `~/.foundry/runs/<run_id>/`:
`events.jsonl` (memory.read / memory.write / memory.consolidate /
function_node.*), `llm_calls.jsonl` (assembled prompts, capture_inputs
on), and `final_state.json` (the whole pipeline's product).
