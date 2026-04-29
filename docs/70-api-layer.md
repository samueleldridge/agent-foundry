# 70 — API Layer

## Purpose

This doc consolidates the foundry's HTTP/WebSocket surface: the auto-generated FastAPI app shape, the endpoint catalogue, the wire formats (JSON / SSE / WebSocket), authentication plug-points, endpoint-versioning patterns, and CI/CD integration. Builds on `01-architecture-overview.md` § API summary, `31-multi-agent-systems.md` § Auto-generated API endpoints, `32-human-in-the-loop.md` § Approval surface, and `85-batch-and-throughput.md` § Batch submission primitive.

Three load-bearing properties:

1. **No per-project handcrafted FastAPI code.** The foundry generates the app from the project's `SystemSpec` + `StateSpec` + agent output schemas. Adding a project = adding configs; no Python wiring.
2. **The OpenAPI schema is real, not a stub.** Pydantic → JSON Schema passthrough means clients can codegen typed bindings against `/openapi.json` and the bindings match the runtime contract.
3. **Endpoint versioning is operator-controlled, not framework-imposed.** Three patterns supported (implicit / URL-versioned / header-versioned); operator picks per project based on their consumer integration shape.

## Module layout

```
src/foundry/api/
├── __init__.py            FastAPI app factory + helpers
├── app.py                 main FastAPI app construction from CompiledSystem
├── routes.py              endpoint generation (per-project; auto from SystemSpec)
├── streaming.py           SSE encoder + WebSocket handler
├── batch.py               POST /batch executor (per 85-batch-and-throughput)
├── auth.py                bearer-token validator + plug-point for institution-specific auth
├── versioning.py          three endpoint-versioning patterns
├── health.py              GET /health (liveness + readiness)
├── errors.py              FoundryError → HTTP-status mapping
└── multi_project.py       multi-project serving (one process, many projects)
```

## Endpoint catalogue (the auto-generated surface)

For a project named `<project>` served via `foundry serve --project <project>`, the foundry generates these endpoints:

| Method | Path | Request | Response | Source |
|---|---|---|---|---|
| `POST` | `/run` | `<project_input>` | `<project_output>` | `31` |
| `POST` | `/stream` | `<project_input>` | SSE: `RunEvent` stream | `10` § Streaming events |
| `POST` | `/batch` | `BatchRequest[<project_input>]` | SSE: `RunEvent` stream tagged with `batch_id` + `item_id` | `85` § Batch submission primitive |
| `WS` | `/ws` | bidirectional: `InboundMessage` ↔ `RunEvent` | bidirectional JSON frames | `10` + `32` |
| `GET` | `/runs/{run_id}` | — | `RunStatus` | `31` |
| `GET` | `/runs/{run_id}/events?from_sequence=N` | — | SSE replay from persisted artifact | `10` § SSE envelope (Last-Event-ID) |
| `POST` | `/runs/{run_id}/resume` | `InboundMessage` (typically `ApprovalResponse`) | `RunResult` | `31` + `32` |
| `GET` | `/health` | — | `Health` (liveness) | this doc § Health |
| `GET` | `/health?deep=true` | — | `Health` with connection statuses (readiness) | this doc § Health |
| `GET` | `/config` | — | `ConfigSnapshot` (redacted) | this doc § Config endpoint |
| `GET` | `/openapi.json` | — | OpenAPI schema | FastAPI default |
| `GET` | `/docs` | — | Swagger UI | FastAPI default (gateable) |

Multi-project serving (per § Multi-project) namespaces each project under `/<project>/...`.

## Endpoint generation algorithm

```
foundry serve --project <name>
   │
   ├── compile project (per 31 § Compile pipeline) → CompiledSystem
   │
   ├── extract input schema from CompiledSystem.state:
   │     - project_input_fields = state fields without defaults
   │       that are read by the start node (per 31 § Input contract)
   │     - construct ProjectInput Pydantic model from those fields
   │
   ├── extract output schema from CompiledSystem.flow:
   │     - if flow.type in [single, sequential]: terminal agent's output_schema
   │     - if flow.type == parallel: join node's output_schema (else last branch's)
   │     - if flow.type == supervisor: supervisor's output_schema
   │     - if flow.type == graph: discriminated union over agents with `to: END` edges
   │       (each must declare a `result_kind: Literal["..."]` field)
   │     - construct ProjectOutput Pydantic type
   │
   ├── instantiate FastAPI app:
   │     - for each row in the endpoint catalogue:
   │         attach route with auto-generated request/response Pydantic types
   │     - mount auth dependency on every route except /health, /openapi.json
   │     - mount CORS middleware (configurable)
   │     - mount request-id middleware (X-Request-Id propagation)
   │     - mount observability middleware (foundry.api.* spans)
   │
   ├── (multi-project) prefix all routes with /<project>
   │
   └── return uvicorn-runnable app
```

Determinism: same `CompiledSystem` → same OpenAPI schema → same generated routes. Reproducible across processes.

## `POST /run` — non-streaming run

Request body: `<ProjectInput>` (the auto-generated Pydantic model).

```json
POST /run
Content-Type: application/json

{
  "trade_id": "ABC123",
  "observed_mismatch_usd": 12500.0,
  "timestamp": "2026-04-27T16:30:00Z"
}
```

Response: `<ProjectOutput>` JSON, plus headers:
- `X-Foundry-Run-Id`: the `RunId` (ULID).
- `X-Foundry-System-Version`: the `system_version` content hash.
- `X-Foundry-Pin-Set-Hash`: the `pin_set_hash`.
- `X-Foundry-Worker-Id`: per `85` § Worker identification.

```json
HTTP/1.1 200 OK
X-Foundry-Run-Id: 01JKM4ABCDEF
X-Foundry-System-Version: cb861da9abcd1234
X-Foundry-Pin-Set-Hash: ef56ab78cdef9012
X-Foundry-Worker-Id: pod-abc123:42

{
  "result_kind": "auto_resolved",
  "root_cause": "late_amendment",
  "recommended_action": "auto_resolve",
  "confidence": 0.92,
  "evidence": [...],
  "cost_if_wrong_usd": 12500
}
```

Status codes (foundry-specific):
- `200` — successful run; response is the project output.
- `400` — invalid request body (Pydantic validation); body is structured error per `errors.py`.
- `401` — auth required.
- `403` — auth provided but unauthorised.
- `409` — run cannot complete cleanly (e.g., `ApprovalRequired` raised but not interactive); response includes `run_id` + status hint.
- `499` — client disconnected / cancelled (analogous to nginx's 499); no retry expected.
- `500` — unexpected runtime failure; structured `FoundryError.to_dict()` in body.
- `503` — checkpointer / shared infrastructure unavailable; Retry-After header set.

## `POST /stream` — SSE streaming run

Same request body. Response: `Content-Type: text/event-stream`. Body is a sequence of SSE events:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
X-Foundry-Run-Id: 01JKM4ABCDEF
Cache-Control: no-cache
Connection: keep-alive

id: 0
event: run.started
data: {"run_id":"01JKM4ABCDEF","sequence":0,"event":"run.started",...}

id: 1
event: agent.started
data: {"run_id":"01JKM4ABCDEF","sequence":1,"event":"agent.started",...}

id: 2
event: llm.delta
data: {"run_id":"01JKM4ABCDEF","sequence":2,"event":"llm.delta","delta":{"type":"text","text":"Investigat"}}

...

id: 87
event: run.completed
data: {"run_id":"01JKM4ABCDEF","sequence":87,"event":"run.completed","status":"success",...}
```

Per `10-core-framework.md` § SSE envelope: `id` is the sequence number; client tracking + `Last-Event-ID` reconnect supported.

### Reconnect via `Last-Event-ID`

```
GET /runs/{run_id}/events?from_sequence=42
Last-Event-ID: 42
```

Server replays `events.jsonl` for that run from `sequence > 42` onwards. If the run is still active, replay catches up to the current live sequence and continues seamlessly. If the run has terminated, replay completes at the terminal event.

This works regardless of which worker accepted the reconnect (per `85` § Streaming under multi-worker — SSE is worker-agnostic).

## `POST /batch` — batch submission

Per `85-batch-and-throughput.md` § Batch submission primitive. Recap:

```json
POST /batch
Content-Type: application/json

{
  "batch_id": "01JKM5BATCH",          // optional; server generates if omitted
  "items": [
    {"item_id": "trade_001", "input": {...}},
    {"item_id": "trade_002", "input": {...}}
  ],
  "policy": {
    "max_parallel": 32,
    "max_cost_usd": 500.0,
    "per_item_timeout_s": 300,
    "stop_on_budget_exceeded": true,
    "streaming": true
  }
}
```

Response (when `streaming: true`): SSE stream, each event tagged with `batch_id` + `item_id`. Terminal `batch.completed` event summarises pass/fail counts + total cost.

Response (when `streaming: false`): `202 Accepted` with `{batch_id}`; client polls `GET /batches/{batch_id}` for status.

## `WS /ws` — bidirectional WebSocket

Per `10-core-framework.md` § WebSocket envelope and `32-human-in-the-loop.md` § WebSocket flow:

```javascript
const ws = new WebSocket(`wss://recon.internal/ws?run_id=${run_id}`);

ws.onmessage = (msg) => {
  const frame = JSON.parse(msg.data);
  if (frame.direction === "outbound") {
    // frame.event is a RunEvent
    handleEvent(frame.event);
  }
};

// Send an inbound message (e.g., approval response):
ws.send(JSON.stringify({
  direction: "inbound",
  message: {
    kind: "approval_response",
    run_id: run_id,
    client_sequence: 1,
    approval_id: "send-email-...",
    decision: "approved",
    reason: "verified by desk head"
  }
}));
```

Multi-worker stickiness per `85` § Streaming under multi-worker: WebSocket connections route to the worker owning the run (LB hash on `run_id`, or run-registry lookup). On worker death, client falls back to SSE reconnect.

### Initiating a new run via WebSocket

The client can initiate a run by sending an `InitRun` inbound message:

```json
{
  "direction": "inbound",
  "message": {
    "kind": "init_run",
    "input": {...},
    "client_sequence": 0
  }
}
```

Server mints a `run_id`, attaches the WebSocket to it, starts the run, and streams events. Equivalent semantics to `POST /run` but interactive from the start.

`InitRun` is added to `InboundMessage` union in `10-core-framework.md` (small extension; backward compatible).

## `GET /runs/{run_id}` — run status

Read-only; no run mutation.

```json
HTTP/1.1 200 OK
Content-Type: application/json

{
  "run_id": "01JKM4ABCDEF",
  "project": "pipeline_recon",
  "system_version": "cb861da9abcd1234",
  "status": "in_progress" | "approval_pending" | "completed" | "failed" | "cancelled",
  "started_at": "2026-04-27T16:30:00Z",
  "completed_at": null,
  "current_node": "investigator",
  "iteration_count": 3,
  "tokens_used": 4521,
  "cost_so_far_usd": 0.18,
  "pending_approval": {                        // populated when status == approval_pending
    "approval_id": "send-email-...",
    "prompt": "Send email to ...?",
    "context": {...}
  },
  "events_url": "/runs/01JKM4ABCDEF/events"
}
```

## `POST /runs/{run_id}/resume` — resume / approve / inject

Body: `InboundMessage` (typically `ApprovalResponse` for approval-pending runs, `InjectInput` for chat-style continuations).

```json
POST /runs/01JKM4ABCDEF/resume
Content-Type: application/json

{
  "kind": "approval_response",
  "approval_id": "send-email-...",
  "decision": "approved",
  "reason": "verified by desk head"
}
```

Response: `RunResult` shape; same status codes as `/run`.

For SSE clients: after resuming via `POST /resume`, subscribe to `/runs/{run_id}/events?from_sequence=<last>` to continue receiving events.

## `GET /health` — liveness vs readiness

```
GET /health
→ 200 OK {"status": "alive", "uptime_s": 1234, "worker_id": "..."}

GET /health?deep=true
→ 200 OK {
    "status": "ready",
    "uptime_s": 1234,
    "worker_id": "...",
    "checkpointer": {"ok": true, "latency_ms": 4},
    "rate_limiter": {"ok": true, "latency_ms": 2},
    "audit_store": {"ok": true, "latency_ms": 6},
    "connections": [
      {"name": "prod_snowflake", "ok": true, "latency_ms": 142},
      {"name": "ops_slack", "ok": true, "latency_ms": 65}
    ]
  }

  → 503 if any connection unhealthy:
  {
    "status": "degraded",
    "connections": [
      {"name": "prod_snowflake", "ok": false, "error": "auth failed"}
    ]
  }
```

K8s usage:
- `livenessProbe` → `GET /health` (cheap; restarts on hang).
- `readinessProbe` → `GET /health?deep=true` (gates traffic on dependency health).

## `GET /config` — config snapshot

Read-only; redacted.

Returns the `CompiledSystem`'s metadata: project name, `system_version`, `pin_set_hash`, framework version, declared agents/functions/connections (with versions), but NOT secrets, NOT raw config file contents, NOT internal handler bodies. Used by ops dashboards + debugging.

```json
{
  "project": "pipeline_recon",
  "system_version": "cb861da9abcd1234",
  "pin_set_hash": "ef56ab78cdef9012",
  "framework_version": "1.3.0",
  "agents": ["orchestrator", "break_detector", "root_cause_investigator", "resolver"],
  "functions": [],
  "tools_pinned": {
    "query_snowflake": "catalog/query_snowflake@v2",
    "validate_deltas": "local/validate_deltas@v3",
    ...
  },
  "connections": {
    "prod_snowflake": {"ref": "catalog/snowflake", "version": "v2", "auth_scheme": "key_pair"},
    ...
  },
  "guardrails": {"max_iterations": 30, "max_hops": 15, "max_cost_usd": 5.0, "max_wall_time_s": 600},
  "compiled_at": "2026-04-27T16:00:00Z"
}
```

## Authentication

The auth plug-point is a FastAPI dependency:

```python
# foundry/api/auth.py
class AuthBackend(Protocol):
    async def authenticate(self, request: Request) -> AuthContext:
        """Raises HTTPException(401/403) on rejection.
        Returns AuthContext with operator identity for audit."""
```

Built-in implementations:
- `BearerTokenAuth` (default) — validates `Authorization: Bearer <token>` against a configured token list / JWT signature.
- `MTLSAuth` — validates client cert from TLS handshake.
- `NoAuth` — for dev only; refuses to start if `FOUNDRY_ENV=prod`.

Institution-specific backends (OIDC, SSO, custom HMAC) plug in via:

```python
# In the institution's deploy entrypoint:
from foundry.api import create_app, AuthBackend

class MyOIDCAuth(AuthBackend):
    async def authenticate(self, request):
        # validate against institution's OIDC IdP
        ...

app = create_app(project="pipeline_recon", auth_backend=MyOIDCAuth())
```

The `AuthContext` carries operator identity (email, roles) which propagates into `RunEvent.run.started.operator` for audit (per `52-rollback-and-audit.md`'s `Operator` shape).

`/health` and `/openapi.json` are exempt from auth by default (operationally necessary). Configurable via `--require-auth-for-health`.

## Endpoint versioning

Three operator-controlled patterns. The foundry doesn't impose; operators pick per project based on their consumer integration shape.

### Pattern 1: Implicit (default — recommended for tightly-coupled internal callers)

Same URL across foundry iterations; the served `system_version` is surfaced in:
- Response header `X-Foundry-System-Version` (already specified).
- Every `RunStarted` event in the streaming surface.
- `GET /config` response.

The schema doesn't change between iterations (additive prompt + tool-binding tweaks don't affect the input/output shape), so callers don't see a breaking change. Operator iterates freely; consumers transparently get the latest.

```
POST https://recon.internal/pipeline_recon/run     (always this URL)
→ X-Foundry-System-Version: cb861da9abcd1234       (varies per deploy)
```

**Use when**: your consumers are in the same organisation, you control their deployment cadence, and schema changes happen rarely (and when they happen, they're coordinated upgrades).

### Pattern 2: URL-versioned (recommended for consumer-facing schema breaks)

When a project's input or output schema changes incompatibly (a state field is renamed, an output discriminator changes), bump a major version explicitly via URL prefix:

```bash
# Old version still serving:
foundry serve --project pipeline_recon --route-prefix /v1 --port 8080 \
  --pin-system-version cb861da9abcd1234

# New version starts in parallel:
foundry serve --project pipeline_recon --route-prefix /v2 --port 8081 \
  --pin-system-version <new_sha>
```

Two processes (or two route prefixes if multi-mount support is configured); load balancer routes:
- `https://recon.internal/v1/...` → port 8080 (old version).
- `https://recon.internal/v2/...` → port 8081 (new version).

Consumers migrate at their own pace. When `/v1` traffic drops to zero, decommission. Standard blue/green deploy pattern.

The `--pin-system-version` flag freezes the served version at a specific commit hash; the version doesn't move with new commits to the project branch (those land in `/v2` or a future `/v3`).

**Use when**: you have external consumers, breaking changes are unavoidable, and you want a controlled migration window.

### Pattern 3: Header-versioned (advanced)

Single URL; `X-Foundry-System-Version: <pin_set_hash>` request header pins the served version. Multiple `system_version`s served from one URL; the foundry's serve layer routes by header.

```
POST https://recon.internal/run
X-Foundry-System-Version: cb861da9abcd1234
→ routes to v1 implementation

POST https://recon.internal/run
X-Foundry-System-Version: ef56ab78cdef9012
→ routes to v2 implementation
```

Implementable today via a thin proxy in front of two `foundry serve` processes (one per version). No built-in foundry feature in v1; document the pattern.

**Use when**: you have sophisticated traffic-management infrastructure already in place and want minimal URL churn for clients. Out of v1 scope as a built-in feature.

### Migration patterns when schema changes

When a project's schema breaks (input or output field rename / type change):

1. **Decision**: pick Pattern 1 (coordinate consumer migration) OR Pattern 2 (parallel URL versions).
2. **For Pattern 2**: deploy `/v2` alongside `/v1`; announce migration window; monitor traffic shift; decommission `/v1` when traffic is zero.
3. **Audit trail**: every served version is captured in run artifacts + observability; cross-version comparison is via the standard `compare_versions` workflow.

## CI/CD integration

The foundry's role in CI/CD is providing stable exit codes + structured outputs that pipeline tooling can gate on. Standard pipeline shape:

### Pre-merge (PR validation)

```yaml
# .github/workflows/foundry-pr.yml
name: Foundry PR validation
on:
  pull_request:
    branches: [main]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # full history needed for foundry diff

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install uv + dependencies
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          uv sync

      - name: foundry validate (all configs load)
        run: uv run foundry validate

      - name: foundry doctor (sandbox + roots + secrets resolve)
        run: uv run foundry doctor

      - name: foundry connections health (test fixtures)
        env:
          FOUNDRY_SECRETS_PROVIDER: env
          # Test credentials provided by CI secrets:
          SNOWFLAKE_TEST_ACCOUNT: ${{ secrets.SF_TEST_ACCOUNT }}
          SNOWFLAKE_TEST_USER: ${{ secrets.SF_TEST_USER }}
          SNOWFLAKE_TEST_PASSWORD: ${{ secrets.SF_TEST_PASSWORD }}
        run: uv run foundry connections health --against test-fixtures

      - name: foundry test (project-local pytest)
        run: uv run foundry test projects/pipeline_recon

      - name: foundry eval (quality gate)
        run: |
          uv run foundry eval pipeline_recon \
            projects/pipeline_recon/evals/q1.yaml \
            --fail-under 0.90 \
            --json > eval_result.json

      - name: Upload eval result artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: foundry-eval-result
          path: eval_result.json
```

Exit codes (per `40-eval-harness.md` § CI integration):
- 0 — all gates pass.
- 1 — eval below threshold.
- 2 — infrastructure failure (auth, connection, etc.).

Different exit codes mean different CI behaviour: 1 blocks merge with "quality regression"; 2 blocks merge with "investigate test infrastructure."

### Post-merge build

```yaml
# .github/workflows/foundry-build.yml
on:
  push:
    branches: [main, 'foundry/*']

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Compute system_version
        id: version
        run: |
          # foundry compute-version reads SystemSpec + pinned files,
          # produces the content-hash that becomes the image tag
          VERSION=$(uv run foundry compute-version --project pipeline_recon)
          echo "version=$VERSION" >> $GITHUB_OUTPUT

      - name: Build container image
        run: |
          docker build \
            --build-arg PROJECT=pipeline_recon \
            --build-arg COMMIT_SHA=${{ github.sha }} \
            --build-arg SYSTEM_VERSION=${{ steps.version.outputs.version }} \
            -t foundry-pipeline-recon:${{ steps.version.outputs.version }} \
            -t foundry-pipeline-recon:latest \
            .

      - name: Push to registry
        run: |
          docker push foundry-pipeline-recon:${{ steps.version.outputs.version }}
          docker push foundry-pipeline-recon:latest
```

The image tag = `<project>:<system_version>`. Trivial to audit: "what's running where" maps directly to a git commit + pin set.

### Pre-deploy gate (optional but recommended for production)

```yaml
deploy-staging:
  needs: build
  steps:
    - name: foundry pre-deploy eval (production smoke)
      run: |
        # Pull the image; run a quick smoke eval against staging dependencies;
        # refuse deploy if the smoke eval fails the production floor.
        uv run foundry deploy pipeline_recon \
          --image foundry-pipeline-recon:${{ steps.version.outputs.version }} \
          --pre-deploy-eval projects/pipeline_recon/evals/production_smoke.yaml \
          --production-floor 0.90 \
          --target staging
```

`foundry deploy` is the admin command that:
1. Pulls + verifies the image.
2. Runs the pre-deploy eval against staging dependencies (configurable).
3. If eval passes the production floor: applies the deployment (k8s rollout / ArgoCD app sync / etc.).
4. If eval fails: refuses deployment; CI fails.
5. Records the deployment metadata (image SHA, eval result, operator) in audit + deployment registry.

The actual rollout mechanism (k8s rolling update, blue/green, canary) is delegated to the institution's infrastructure tooling (Istio, Linkerd, ArgoCD). The foundry doesn't reinvent rollout strategy.

### Deployment rollback (separate from config rollback)

Two layers of rollback, independent:

| Layer | Operation | Effect |
|---|---|---|
| **Config rollback** (per `52-rollback-and-audit.md`) | `foundry rollback pipeline_recon --tool ... --to v2` | Edits the config; what *would* be deployed next time. |
| **Deployment rollback** | `kubectl rollout undo deployment/pipeline-recon` (or ArgoCD app revert) | Reverts the running container image to the previous tag. |

Config rollback is for iteration cycles (forge produced a regression; revert config). Deployment rollback is for production incidents (something's wrong with the live service; revert image). They don't need to know about each other:

- After deployment rollback: the previous image is serving; if you `foundry rollback` the config to match, next deploy will match the served version.
- After config rollback: next deployment cycle picks up the new config; existing deployments serve until they're rolled.

Document this clearly so operators don't conflate the two.

### Sample Dockerfile (institution responsibility but foundry-friendly shape)

```dockerfile
# Dockerfile (in the institution's repo per 86-multi-tenancy-and-ip.md)
FROM python:3.12-slim

ARG PROJECT
ARG COMMIT_SHA
ARG SYSTEM_VERSION

WORKDIR /app

# Install uv
RUN pip install uv

# Copy + install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy the project (includes catalog/ + projects/<name>/ + state)
COPY . .

# Bake the system_version into the image for audit
LABEL foundry.project="${PROJECT}"
LABEL foundry.commit_sha="${COMMIT_SHA}"
LABEL foundry.system_version="${SYSTEM_VERSION}"

# Default env vars; can be overridden at runtime
ENV FOUNDRY_ENV=prod
ENV FOUNDRY_PROJECT=${PROJECT}

ENTRYPOINT ["uv", "run", "foundry"]
CMD ["serve", "--project", "${FOUNDRY_PROJECT}", "--host", "0.0.0.0", "--port", "8080"]
```

Standard Python container shape; no foundry-specific magic. The institution's deploy team can adapt to their conventions (multi-stage builds, distroless base, etc.).

## Multi-project serving

`foundry serve --project a --project b` runs multiple projects in one process. Each project's endpoints are namespaced under `/<project>/...`:

```
POST /pipeline_recon/run
POST /contract_review/run
WS   /pipeline_recon/ws
GET  /pipeline_recon/health
GET  /contract_review/health
GET  /health                     ← aggregate health across all projects
```

Trade-offs:
- **One process for many projects**: lower resource overhead; shared event loop; shared connection pools where projects use the same connections.
- **One process per project**: better isolation; per-project resource controls; standard pattern for k8s deployments (one project = one deployment = one replicaset).

Pick per institution policy. Both supported. Single-project default in `foundry serve`; multi-project explicit.

## Failure modes

| Cause | Status | Body |
|---|---|---|
| Invalid request body | 400 | `{"error_class":"ConfigValidationError","message":...,"context":{"field":"trade_id","reason":"required"}}` |
| Auth missing | 401 | `{"error":"authentication required"}` |
| Auth invalid | 401 | `{"error":"invalid token"}` |
| Auth valid but unauthorised | 403 | `{"error":"forbidden"}` |
| Run not found | 404 | `{"error":"run_id not found"}` |
| ApprovalRequired raised but client requested non-streaming | 409 | `{"run_id":"...","status":"approval_pending","approval":{...}}` |
| Client disconnected mid-stream | (no response; logs `RunCancelled`) | server cancels run; checkpoint preserves state |
| Provider auth fails | 502 | structured `ProviderAuthError.to_dict()` |
| Connection unavailable | 503 | structured error + Retry-After header |
| Cost budget exceeded | 200 with status="failed" | `RunResult` shape with `error.error_class="CostBudgetExceeded"` |
| Output validation fails (after auto-repair) | 200 with status="failed" | `RunResult` with `error.error_class="OutputValidationError"` |
| Internal server error | 500 | `FoundryError.to_dict()` |

The API never returns a stack trace. All errors are structured `FoundryError`s serialised via `to_dict()` (per `10-core-framework.md`).

## Invariants

1. **Endpoints are auto-generated; no per-project handcrafted FastAPI code.**
2. **OpenAPI is real**: `/openapi.json` matches the runtime contract; client codegen against it works.
3. **Versioning is operator-controlled**: three patterns supported; foundry doesn't impose URL structure.
4. **Auth plug-point is mandatory in prod**: `FOUNDRY_ENV=prod` + `NoAuth` refuses to start.
5. **Health distinction**: liveness is cheap; readiness checks dependencies.
6. **Errors are structured**: `FoundryError.to_dict()`; never raw stack traces.
7. **Multi-project is opt-in**: single-project default to avoid accidental cross-project deployments.
8. **Image tag = `<project>:<system_version>`**: deployment auditability is first-class.

## Test expectations

### Unit

1. **Endpoint generation**: a hello-world `SystemSpec` produces an app with all expected routes; `/openapi.json` validates.
2. **Input validation**: invalid `POST /run` body produces 400 with structured error naming the failing field.
3. **Output schema correctness**: `POST /run` response matches the project's output schema (single OR discriminated union).
4. **Header propagation**: `X-Foundry-Run-Id`, `X-Foundry-System-Version`, `X-Foundry-Pin-Set-Hash` present on every response.
5. **Auth enforcement**: missing token → 401; invalid → 401; valid → request proceeds.
6. **Health check**: `/health` returns 200; `/health?deep=true` returns 503 if any connection unhealthy.

### Contract

1. **OpenAPI client codegen**: generate a typed client (TypeScript / Python) from `/openapi.json`; compile + run against the live API; round-trips work.
2. **SSE Last-Event-ID resume**: kill client mid-stream; reconnect with `Last-Event-ID`; receive missing events.
3. **WebSocket inbound dispatch**: send each `InboundMessage` kind; expected behaviour fires.
4. **Versioning Pattern 2**: two `foundry serve` processes with different `--route-prefix`; client routes correctly to each based on URL.

### Integration (Phase 8 exit gate)

1. End-to-end CI/CD pipeline: GitHub Actions YAML executes; pre-merge validates; post-merge builds image; pre-deploy eval gates; deploy succeeds.
2. Multi-project serving: two projects in one process; namespace isolation; per-project healths; cross-project dispatch works.
3. Endpoint versioning: deploy `/v2` alongside `/v1`; both serve; consumer migration via URL change.

## Open questions

1. **GraphQL surface**. Some institutions prefer GraphQL for typed schema discovery + selective field returns. Lean: defer; OpenAPI + REST is well-supported. Build a GraphQL adapter if real demand surfaces.
2. **gRPC**. Higher-throughput than HTTP; better for service-to-service. Lean: defer; gRPC adds complexity; HTTP/2 + JSON is fast enough for v1.
3. **Multi-mount in one process** (`/v1` + `/v2` from one foundry-serve process). Implementable; saves a process. Lean: defer; standard k8s deployment shape is one version per process.
4. **Webhooks for run completion**: foundry POSTs to a configured URL when a run completes. Useful for event-driven architectures. Lean: defer; consumers can poll `GET /runs/{id}` or subscribe to SSE.
5. **API rate limiting at the foundry layer** (separate from provider rate limiting). Lean: yes — institutions need this for consumer fairness; ship in Phase 8 polish; uses the same `RateLimiter` infrastructure as provider rate limiting per `11-provider-abstraction.md` § Rate limiting.
