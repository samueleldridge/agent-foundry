# 32 — Human in the Loop

## Purpose

Some agent decisions need a human in the loop: tool calls with material consequences (sending an email to a counterparty, triggering a wire, escalating to legal), router branches that hit ambiguity, terminal outputs that require sign-off before deployment. This doc specifies the **HITL** (human-in-the-loop) subsystem: the `ApprovalRequired` control-flow exception, persistence semantics, the resume API, the WebSocket and SSE patterns for surfacing approvals to operators, timeout / expiration behaviour, audit trail, and testing.

The `ApprovalRequired` and `ApprovalResponse` types are in `10-core-framework.md`. Streaming events are in `10` § Streaming events. Resume API is in `31-multi-agent-systems.md`. This doc is the consolidating spec at the HITL level.

Three load-bearing properties:

1. **HITL is control flow, not error handling.** A pause for approval is intentional — it's part of the system's design, not a failure path. The framework persists pending state, surfaces the approval cleanly to operators, and resumes deterministically when the answer arrives.
2. **The pending state survives anything**. Process death, host failure, deployment swaps — the approval is durable in the checkpointer; resume from any worker.
3. **Approvals are auditable end-to-end**. Every `approval.required` and `approval.resolved` event is structured, timestamped, attributed to an operator, and persists in the audit trail alongside the run artifact.

## What HITL IS

| IS | IS NOT |
|---|---|
| A pause-and-resume control flow primitive | A general human-input API |
| Initiated by a tool, agent, or router | Initiated by external systems unprompted |
| Persisted in the checkpointer; durable across restarts | Volatile; lost on process death |
| Resumable from any worker that can reach the checkpointer | Sticky to one worker (HITL is the single workflow that explicitly supports cross-worker resume) |
| Audited with `approval.required` / `approval.resolved` events | A separate audit log requiring extra plumbing |

## Module layout

```
src/foundry/orchestration/
└── hitl.py                ApprovalRequired + ApprovalResponse handling; interrupt/resume integration with LangGraph

src/foundry/api/
└── routes.py              POST /runs/{run_id}/resume + WS handler for ApprovalResponse

src/foundry/cli/
└── resume.py              foundry resume <run_id> --approve|--reject
```

## Three places approvals are raised

### 1. Tool-level (most common)

A tool handler raises `ApprovalRequired` mid-execution:

```python
# in handler.py
async def handle(inputs: SendEmailIn, ctx: RunContext) -> SendEmailOut:
    if inputs.recipient.endswith("@external-counterparty.com"):
        raise ApprovalRequired(
            approval_id=f"send-email-{ctx.run_id}-{uuid4().hex[:8]}",
            prompt=f"Send email to external counterparty {inputs.recipient}?",
            context={
                "recipient": inputs.recipient,
                "subject": inputs.subject,
                "body_preview": inputs.body[:200],
                "tool_ref": ctx.tool_ref,
            },
        )

    # if approval previously granted, ApprovalRequired won't be raised again
    # (orchestration runtime tracks resolved approvals; see § Re-execution semantics)

    conn = await ctx.connections.get("email")
    await conn.client.send(...)
    return SendEmailOut(message_id="...")
```

The tool decides when approval is required based on its inputs. The framework catches the exception, persists pending state, emits `approval.required`, and waits.

### 2. Agent-level (next-step gating)

An agent's output indicates that the next step needs approval. Implemented as a special agent output field:

```python
# in output_schema.py
class InvestigationResult(BaseModel):
    root_cause: RootCause
    confidence: float
    recommended_action: RecommendedAction
    requires_approval: bool = False
    approval_prompt: str | None = None
```

The orchestration runtime, after the agent step, inspects the output: if `requires_approval: true`, raises `ApprovalRequired` with `approval_prompt` as the prompt. The pending state is the post-agent state (the `state_delta` is already applied).

This is useful for "always pause before the next worker" gating — the agent decides per-case.

### 3. Flow-level (always pause at this edge)

A graph or supervisor flow can declare approval-required edges:

```yaml
flow:
  type: graph
  edges:
    - from: investigator
      to: auto_resolver
      when: "state.investigation.recommended_action == 'auto_resolve'"
      requires_approval: true
      approval_prompt_template: |
        Auto-resolve recommendation:
        - root cause: {state.investigation.root_cause}
        - confidence: {state.investigation.confidence}
        - cost if wrong: ${state.investigation.cost_if_wrong_usd}

        Approve auto-resolution?
```

The compiler wraps the edge with an interrupt: when the edge fires, the runtime emits `approval.required` with the rendered prompt and pauses before invoking the destination node.

## Lifecycle

### Pause sequence

```
Tool / Agent / Flow raises ApprovalRequired
   │
   ▼
hitl.handle_interrupt(approval, ctx)
   │
   ├── Generate sequence number
   ├── Emit RunEvent.ApprovalRequired
   ├── Snapshot pending state to checkpointer with key:
   │     "pending:<run_id>:<approval_id>"
   ├── Mark run status = "approval_pending"
   ├── Mark current node as paused (LangGraph interrupt)
   │
   ▼
Run pauses. Process can continue handling other runs.
The checkpointer holds the pending state durably.
```

### Resume sequence

```
ApprovalResponse arrives via:
  - POST /runs/{run_id}/resume (HTTP), or
  - WS /ws inbound message (WebSocket), or
  - foundry resume <run_id> --approve (CLI)
   │
   ▼
hitl.resume(run_id, response)
   │
   ├── Load checkpointed state for run_id
   ├── Verify response.approval_id matches a pending approval
   ├── Apply response: mark approval as resolved (approved | rejected)
   ├── Emit RunEvent.ApprovalResolved
   ├── Update run status: in_progress
   │
   ├── Re-invoke the paused node:
   │     - If approval was approved: re-run the tool/agent/edge that
   │       raised ApprovalRequired; this time the same code path
   │       checks "is this approval already resolved?" and proceeds
   │       without re-raising (see § Re-execution semantics)
   │     - If rejected: skip the action; runtime emits a synthetic
   │       tool_result indicating rejection with the operator's
   │       reason, returning control to the agent's next step
   │
   ▼
Run continues from the resumed point. RunEvent sequence numbers
continue from the last persisted sequence (NOT 0).
```

### Re-execution semantics

When a tool's handler raises `ApprovalRequired`, the framework needs to re-invoke the handler after approval. The handler must be **idempotent on re-invocation** for the case where approval was granted — it should NOT re-raise `ApprovalRequired` for the same `approval_id`.

The framework provides this support automatically:

```python
async def handle(inputs: SendEmailIn, ctx: RunContext) -> SendEmailOut:
    # Compute the approval_id deterministically from the inputs
    # OR use ctx.approval_for(...) helper which threads the resolution status
    approval_id = f"send-email-{ctx.run_id}-{stable_hash(inputs)}"
    
    if not ctx.approval_resolved(approval_id):
        raise ApprovalRequired(
            approval_id=approval_id,
            prompt=f"Send email to {inputs.recipient}?",
            context={...},
        )

    # On the second invocation (after approval), this branch executes
    if ctx.approval_decision(approval_id) == "rejected":
        return SendEmailOut(
            message_id="<rejected>",
            sent=False,
            rejection_reason=ctx.approval_reason(approval_id),
        )

    # Approved — proceed
    conn = await ctx.connections.get("email")
    await conn.client.send(...)
    return SendEmailOut(message_id="...", sent=True)
```

`RunContext` carries:
- `approval_resolved(approval_id) -> bool` — whether the approval has been answered.
- `approval_decision(approval_id) -> Literal["approved", "rejected"]` — what the operator decided.
- `approval_reason(approval_id) -> str | None` — operator's reason (often required for `rejected`).

The framework guarantees these accessors return consistent values across re-invocation.

### Stable approval IDs

The `approval_id` MUST be stable across the pause/resume boundary. Three patterns:

- **Hash of inputs**: `f"send-email-{run_id}-{stable_hash(inputs)}"`. Best for tools where inputs uniquely determine the action. Survives even partial state mutations between attempts.
- **UUID + checkpoint**: generate once, persist in state. The framework helper `ctx.stable_approval_id(prefix)` does this.
- **Caller-supplied**: input field with the approval_id. Useful for clients that want to dedupe.

## Approval surface (operator side)

Three operational surfaces for resolving approvals; choose what fits your workflow.

### 1. CLI (interactive ops)

```bash
$ foundry approvals list pipeline_recon
RUN_ID                APPROVAL_ID         AGE   PROMPT
01JKM4...             send-email-01J...   2m    Send email to external@counterparty.com?
01JKM5...             auto-resolve-01J... 5m    Auto-resolve recommendation: ...

$ foundry approvals show 01JKM4... send-email-01J...
Run: pipeline_recon (project)
Pending since: 2026-04-25T10:45:23Z (2m ago)
Prompt:
  Send email to external counterparty external@counterparty.com?
Context:
  recipient: external@counterparty.com
  subject: Re: settlement break ABC123
  body_preview: |
    Dear ...
  tool_ref: catalog/send_email_via_ses@v2

$ foundry approvals approve 01JKM4... send-email-01J... --reason "verified with desk head"

$ foundry approvals reject 01JKM5... auto-resolve-01J... --reason "manual review preferred for >$30k"
```

### 2. WebSocket (custom UIs)

A web UI maintains a WebSocket connection (one per run, or one aggregated stream subscribed to multiple runs). On `approval.required` events, the UI presents a form; on submit, sends an `ApprovalResponse` inbound message.

```javascript
// Client pseudocode
const ws = new WebSocket(`wss://foundry/ws?run_id=${run_id}`);

ws.onmessage = (msg) => {
  const event = JSON.parse(msg.data).event;
  if (event.event === "approval.required") {
    showApprovalForm(event.approval_id, event.prompt, event.context);
  }
};

function submitApproval(approval_id, decision, reason) {
  ws.send(JSON.stringify({
    direction: "inbound",
    message: {
      kind: "approval_response",
      run_id: run_id,
      client_sequence: nextClientSeq(),
      approval_id,
      decision,
      reason,
    },
  }));
}
```

The WebSocket envelope is in `10-core-framework.md` § Streaming events.

### 3. SSE + separate POST (proxy-friendly UIs)

For environments where WebSocket is awkward (corporate proxies, simple front-ends):

```javascript
// Subscribe to events
const sse = new EventSource(`/runs/${run_id}/stream`);

sse.addEventListener("approval.required", (e) => {
  const event = JSON.parse(e.data);
  showApprovalForm(event.approval_id, event.prompt, event.context);
});

// Submit approval via POST
async function submitApproval(approval_id, decision, reason) {
  await fetch(`/runs/${run_id}/resume`, {
    method: "POST",
    body: JSON.stringify({
      kind: "approval_response",
      approval_id,
      decision,
      reason,
    }),
  });
}
```

Equivalent semantics to WebSocket, slightly higher latency for the inbound POST. Recommended default for UIs that don't need other inbound message types.

### 4. External systems (Slack, PagerDuty, email)

Higher-order pattern: a "notification adapter" agent or tool subscribes to `approval.required` events and forwards them to an external system (Slack action button, PagerDuty incident, email with click-to-approve link). The external system's response webhook calls back to the foundry's `POST /runs/{run_id}/resume`.

Not a built-in primitive; implementable as a project-local tool + a connection to the notification system. Catalog templates for common patterns (Slack approval, PagerDuty escalation) are sensible v1.1 additions.

## Timeout and expiration

Approvals can specify a deadline:

```python
raise ApprovalRequired(
    approval_id="...",
    prompt="...",
    context={...},
    timeout_s=900,    # 15 minutes
    on_timeout="reject",   # "reject" | "approve" | "escalate"
)
```

If `timeout_s` elapses without a response:
- `on_timeout: "reject"` (default): synthetic rejection with reason `"timeout after 900s"`; run continues as if rejected.
- `on_timeout: "approve"`: synthetic approval with reason `"timeout default-approve"`; run continues as if approved. Use carefully — this is "fail-open" behaviour.
- `on_timeout: "escalate"`: emits an `approval.escalated` event with the original prompt + an escalation hint; the run remains paused for human attention. The framework does not auto-escalate; this is just an event signal for external escalation tooling.

Implementation: a background timer per pending approval; on expiry, generates a synthetic `ApprovalResponse` with the configured decision.

## Multi-approver scenarios

Two patterns supported via the same primitives:

### Sequential approval chain

A tool that needs sign-off from multiple operators raises a sequence of approvals:

```python
async def handle(inputs, ctx):
    if not ctx.approval_resolved("desk-approval"):
        raise ApprovalRequired(approval_id="desk-approval", prompt="Desk head approval?", ...)
    
    if not ctx.approval_resolved("compliance-approval"):
        raise ApprovalRequired(approval_id="compliance-approval", prompt="Compliance approval?", ...)
    
    # Both approved — proceed
    ...
```

Each approval is a separate event; each can be granted/rejected independently. The handler re-invokes between approvals.

### Quorum approval

For "any 2 of 3 approvers must agree" — implementable but out of scope for v1 framework primitives. Recommend a project-local tool that aggregates approvals from a separate ticketing system and only returns to the run when quorum is met.

## Audit trail

Every approval lifecycle is recorded:

| Event | Attributes |
|---|---|
| `approval.required` | `run_id`, `agent_name`, `approval_id`, `prompt`, `context`, `timeout_s`, sequence |
| `approval.resolved` | `run_id`, `approval_id`, `decision`, `reason`, `resolved_by` (operator id from auth context if available), sequence |
| `approval.escalated` (timeout-with-escalate) | `run_id`, `approval_id`, `original_prompt`, sequence |

The `resolved_by` attribute requires the resume API endpoint to extract operator identity from the auth bearer token (`70-api-layer.md`). For unauthenticated dev contexts, it's `null`.

For compliance use cases (financial services 4-eyes / SOX, healthcare physician sign-off), the `resolved_by` chain is the audit trail of who approved what. Stored alongside the run artifact in the audit store; queryable via:

```bash
$ foundry obs approvals --project pipeline_recon --since 30d
```

## Composition with multi-worker deployment (recap)

From `85-batch-and-throughput.md`:

- Pending approvals are persisted in the checkpointer (Postgres in prod) — durable across worker death.
- The resume API is worker-agnostic: any worker can resume any pending run, because the run state is in shared storage.
- WebSocket clients that lose their socket should fall back to SSE on reconnect (per `85`'s Strategy 1 / Strategy 2 routing); the resume POST works regardless of which worker accepts it.
- `foundry approvals list` queries the audit store directly; works across the whole cluster.

This is one of the few foundry workflows where worker affinity does NOT matter — the durability of pending state is the design point.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Approval response arrives for an unknown `approval_id` | API: `404 ApprovalNotFound`; CLI: clear error |
| Approval response arrives for an already-resolved approval | API: `409 ApprovalAlreadyResolved`; idempotent if same decision, else error |
| Operator submits malformed `ApprovalResponse` | API: `400` with field-level errors |
| Timeout fires + `on_timeout: escalate` + no human picks up | Run remains paused indefinitely; metric alert triggers external paging |
| Tool handler is non-idempotent on re-invocation (re-raises after approval) | Detected as a loop; framework raises `OrchestrationError("non-idempotent approval flow")` after 2 same-id raises in same run |
| Checkpointer unavailable when approval arrives | API: `503 CheckpointerUnavailable`; CLI shows error; client retries |
| Run cancelled while approval pending | `approval.required` superseded by `run.cancelled`; pending approval becomes invalid; subsequent resume returns `409` |

Every failure mode emits a structured event and structured API error.

## Invariants

1. **`ApprovalRequired` is control flow, never an error.** Tier 1 says this; HITL is the realisation.
2. **Pending state is durable.** Survives any single-process restart given a non-volatile checkpointer.
3. **`approval_id` is stable across re-invocation.** Same id on second-invocation handler call → framework knows the approval is the same one.
4. **Resolution is idempotent in same direction.** Re-submitting the same decision for an already-resolved approval is a no-op (returns success); re-submitting a different decision is an error.
5. **Re-invocation must not re-raise.** Once an approval is resolved, the same `approval_id` raise is treated as a non-idempotent flow bug.
6. **Audit trail is complete.** Every `approval.required` has exactly one terminal event (`approval.resolved` OR superseded by `run.cancelled` / `run.failed`).
7. **`resolved_by` is captured when auth is on.** For unauthenticated dev contexts, `null` is recorded explicitly.
8. **Timeout is enforced at the framework level.** Tools should not re-implement timer logic; the framework's background timer is the source of truth.

## Test expectations

### Unit

1. **`ApprovalRequired` propagation**: tool raises; framework catches and persists pending state.
2. **`approval_resolved` accessor**: after resume, `ctx.approval_resolved(id) -> True` on re-invocation.
3. **Decision plumbing**: rejection decision + reason flow into the synthetic tool_result so the agent sees the rejection.
4. **Stable id pattern**: hash of inputs produces the same id across re-invocation.
5. **Non-idempotent flow detection**: handler that re-raises same id after approval → `OrchestrationError` with diagnostic.
6. **Timeout fires**: `timeout_s: 1` + no response → synthetic rejection at expiry.
7. **`on_timeout: approve`**: synthetic approval at expiry.
8. **`on_timeout: escalate`**: `approval.escalated` event fires; run remains paused.
9. **Already-resolved**: second `ApprovalResponse` for same id with same decision → no-op; with different decision → API error.
10. **`approval_id` collision detection**: two pending approvals with the same id (programming error) → framework rejects the second `raise` with `OrchestrationError`.

### Contract

1. **Audit completeness**: for every `approval.required` event, there's exactly one terminal event (`approval.resolved` / `run.cancelled` / `run.failed`).
2. **Resume worker-agnostic**: a run paused on worker A is resumable on worker B (test with two-worker fixture + shared Postgres checkpointer).
3. **Cross-process durability**: pause a run, kill the process, start a new one, resume — produces correct final result.

### Integration (Phase 7 exit gate)

1. **End-to-end CLI flow**: trivial project with a `send_email` tool requiring approval; `foundry run` pauses; `foundry approvals approve` resumes; final output reflects approval.
2. **End-to-end SSE flow**: `POST /stream` emits `approval.required`; client sends `POST /runs/{id}/resume`; stream emits `approval.resolved`; run completes.
3. **End-to-end WebSocket flow**: same workflow over `WS /ws` with bidirectional inbound `ApprovalResponse`.
4. **External Slack approval pattern**: a project with a Slack-notification tool + `foundry serve` + a Slack webhook backing → end-to-end approval via Slack action button (integration with a Slack mock).
5. **Multi-step chained approvals**: tool requires desk + compliance approvals; both granted in sequence → tool executes; one rejected → tool returns rejection.

## Operational CLI

- `foundry approvals list [<project>]` — pending approvals across one or all projects.
- `foundry approvals show <run_id> <approval_id>` — full details (prompt, context, age).
- `foundry approvals approve <run_id> <approval_id> [--reason "..."]` — approve.
- `foundry approvals reject <run_id> <approval_id> --reason "..."` — reject (reason required).
- `foundry approvals stats [<project>] --since 7d` — aggregate stats: count, median wait time, approve/reject ratio.

## Open questions

1. **Approval routing**. Currently every approval is one-and-done — any operator with API access can resolve. Some scenarios need routing (this approval to the desk; that one to compliance). Lean: defer; route via external systems (Slack channel, JIRA queue) and let operators self-select. Adding routing primitives risks reinventing a workflow engine.
2. **Approval expiration policy per project**. Currently `timeout_s` is per-approval; a project-wide default would be useful. Lean: yes, additive — `Guardrails.default_approval_timeout_s: int | None`.
3. **Bulk approvals**. Operator wants to approve "all auto-resolve recommendations from the last 5 minutes for trades under $10k". Implementable as an external batch script that walks `foundry approvals list` and calls `approve` for each. Lean: don't build into framework; ship the CLI primitives that make scripts trivial.
4. **Approval-required on flow edges with predicates**. Currently `requires_approval: true` is unconditional. A `requires_approval_when: <predicate>` field would enable conditional approval gating without writing a router agent. Lean: yes, additive in v1.1; preserves the edge-as-data principle.
5. **Approval as agent output vs separate flag.** Today: agents indicate approval need via output schema (`requires_approval: bool` + `approval_prompt: str | None`). Alternative: a special `ApprovalRequiredFromOutput` field type the framework recognises. Lean: keep current pattern; simpler. Document the convention so projects implement it consistently.
