# publish_greeting

Publish a greeting to the team channel — the Phase 7 HITL example tool.

## Contract

- Input: `schemas.py::PublishIn` (`text`)
- Output: `schemas.py::PublishOut` (`published`, `detail`)
- Every fresh call raises `ApprovalRequired` with a stable approval id
  (`publish-<run_id>-<input hash>`); the run pauses until an operator
  answers via `foundry resume <run_id> --approve | --reject --reason`.
- On re-invocation after resolution the handler publishes (approved) or
  returns the operator's reason (rejected) — it never re-raises the same
  approval id (docs/32 § Re-execution semantics).
