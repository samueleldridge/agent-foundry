# normalize_input

Deterministic pre-agent node: trims whitespace from each raw user turn and
drops empties, writing the cleaned list to `turns` (the field the agent's
turn loop consumes). Reads `[raw_turns]`, writes `[turns]`.
