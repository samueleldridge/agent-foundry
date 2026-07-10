# team_hello

The multi-agent example system (Phase 7): a **supervisor** (`coordinator`)
drives two workers via compile-generated `transfer_to_*` handoff tools —
`drafter` writes a greeting draft, `publisher` publishes it through an
**HITL-gated** local tool (`publish_greeting` always raises
`ApprovalRequired`), then the coordinator finishes with `transfer_to_end`
and a final summary.

State visibility is deliberately narrow (docs/22): the drafter cannot see
the publish outcome, and the publisher sees only the `draft` — never the
raw request. Workers literally cannot read fields outside their
declared scope.

## Run it

```bash
export ANTHROPIC_API_KEY=...

# 1. Start the run — it PAUSES at the publish approval:
uv run python -m foundry run projects/team_hello \
  --input '{"request": "the new release shipping", "audience": "the team"}' \
  --checkpoint sqlite

# 2. See what is pending:
uv run python -m foundry approvals list
uv run python -m foundry resume <RUN_ID>

# 3. Resolve it — the run continues to the coordinator's final summary:
uv run python -m foundry resume <RUN_ID> --approve
#   ...or:
uv run python -m foundry resume <RUN_ID> --reject --reason "tone is off"
```
