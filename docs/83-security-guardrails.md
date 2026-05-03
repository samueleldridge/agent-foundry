# 83 — Security and Guardrails

## Purpose

This doc consolidates the foundry's security surface: input/output validation, prompt-injection defenses, tool allowlisting, sandbox enforcement (meta-agent + connections), PII / sensitive-data handling, secret-literal scanning, audit completeness as a security control, and compliance considerations. The framework's design has security baked into structural enforcement throughout; this doc is the single place that surfaces all the controls together so an operator (or auditor) can review them coherently.

The principle from Tier 0: **prompt-level rules are belt; structural enforcement is braces**. Every security guarantee in this doc is enforced at the framework level — config validation, tool dispatch, connection sandbox, meta-agent sandbox — not via the LLM following rules. An adversarial prompt cannot bypass these controls.

Three load-bearing properties:

1. **Defense in depth.** Most controls have multiple enforcement layers (config-load + dispatch + audit). A single layer's failure doesn't compromise the system.
2. **Sandboxes are structural, not procedural.** Path canonicalisation + allowlist checks happen in code, not in the LLM's prompt.
3. **Audit completeness is a security property.** Every consequential action emits a typed event; tampering with that stream is detectable.

## Threat model

What the foundry's security design protects against:

- **Compromised meta-agent prompt** (prompt injection via `read_file`'d content; corrupted prompt files; supply-chain on the framework). Result: meta-agent issues forbidden tool calls; structural enforcement refuses them.
- **Hallucinated tool calls** by user-facing agents (LLM tries to invoke an unallowed tool). Result: dispatcher refuses; LLM sees structured error.
- **Malicious tool input** (LLM passes adversarial content to a tool — e.g., SQL injection through a query tool). Result: input validation + tool author's own input sanitization (handler.py).
- **Credential exfiltration** (something tries to get raw secrets into observability / outputs). Result: redaction; structural exclusion of sensitive fields from spans.
- **Schema-bypass attempts** (config tries to use unsanctioned features). Result: Pydantic validation; `extra="forbid"` on every top-level model.
- **Audit tampering** (someone retrospectively edits audit log to hide a change). Result: append-only; git-tracked; tamper-evident via diff.
- **Cross-project contamination** (a meta-agent run on project A modifying project B's files). Result: meta-agent's path sandbox.
- **Cross-institution leakage** (institution A's code/config visible to institution B). Result: separate private repos; multi-tenancy boundaries (per `86-multi-tenancy-and-ip.md`).

Out of scope:

- **Physical security** of the host running foundry (operator concern).
- **TLS termination + cert management** (load balancer / cloud platform concern).
- **Multi-user isolation within a single foundry instance** beyond the project-scope sandbox (operators sharing a foundry instance trust each other).
- **Adversarial agents that successfully complete an unsafe action via LLM-only reasoning** before the structural check fires (mitigated by `dangerous: true` flag + connection sandbox; complete coverage requires ongoing prompt-injection research, which is v1.1+).
- **Side-channel attacks** (timing, memory layout) — not the foundry's responsibility.

## Layered controls

### Config-load layer (Pydantic + secret scan)

Every config file loads through:

1. **YAML parse** with `SafeLoader` only (no `!python` constructors; per `12-config-and-validation.md`).
2. **Secret-literal scan** over every scalar value:
   - AWS access key pattern (`AKIA[0-9A-Z]{16}`).
   - Anthropic key prefix (`sk-ant-`).
   - OpenAI key prefix (`sk-`).
   - User-extensible patterns from `~/.foundry/secret_patterns.yaml`.
   - Heuristic: any value at a key named `password|secret|token|api_key|apikey` longer than 8 chars.
3. **Pydantic validation** with `extra="forbid"` on every top-level schema.
4. **Cross-field validators** for semantic consistency.
5. **Compile-time semantic checks** (state visibility coverage, tool allowlist consistency, capability-required against provider manifest, etc.).

A config that fails any check at load is refused with a structured error. The runtime never sees half-validated configs.

### Pre-commit layer (institution repo)

Per `51-git-backbone.md` § Pre-commit hooks: the foundry installs pre-commit hooks at `foundry init`:

- **Secret-literal scan** (same patterns as the config-load layer; defense in depth).
- **Schema validation** for every `.yaml` file under `projects/<*>/` or `catalog/`.
- **Conventional message format check** (recommended; configurable).
- **Eval gate** (optional; off by default for speed).

`git commit --no-verify` skips hooks but is logged loudly to audit (`overrides_used: ["pre_commit_skip"]`).

### Tool-dispatch layer

Per `20-tool-system.md` § Dispatch:

- **Allowlist check**: agent's `tools` list enforced; LLM-hallucinated tool name → `ToolNotAllowedError`.
- **Input validation**: Pydantic-validated against `input_schema` before handler invocation.
- **Output validation**: Pydantic-validated against `output_schema` after handler returns.
- **`dangerous: true` flag**: surfaced in observability spans; meta-agent cannot scaffold; manual tool authoring + extra review required.

### Connection layer

Per `23-connections-and-auth.md` § Security considerations:

- **Tool handlers never touch credentials.** Only `ctx.connections.get(slot)` returns clients; `SecretValue.reveal()` is the only path to raw secrets and lives in `foundry/auth/` modules only.
- **`ConnectionDescriptor` redaction**: only allowlisted config fields surface to observability + logs.
- **Sensitive-pattern denylist**: span-export-time check drops fields whose names match `api_key|password|secret|token|private_key`.
- **Compile-time slot binding validation**: tools cannot access connections their `connections_required` doesn't declare.

### Meta-agent sandbox

Per `60-meta-agent.md` § Defense in depth and `61-meta-tools.md`:

- **Path scoping**: `read_file` accepts paths under `framework_root/`, `catalog_roots/`, `projects_root/<scoped_project>/` only. `write_file` only `projects_root/<scoped_project>/`.
- **Path canonicalisation + symlink resolution**: traversal attempts (`../`) and symlink escapes are caught.
- **Tool catalogue allowlisting**: meta-agent's `tools` allowlist is fixed at framework level; `ToolRegistry` refuses non-allowlisted names.
- **Forbidden git operations**: at the meta-tool layer, before subprocess invocation. `git_push`, `git_rebase`, `git reset --hard`, `git --force`, `git config`, `git checkout <branch>` (branch switch), `git merge`, `git tag` all refused.
- **`dangerous: true` refusal**: `build_tool` cannot set this flag.
- **`provider_overrides` refusal**: `build_agent` cannot populate this field.
- **Catalog write refusal**: `write_file` sandbox excludes all `catalog_roots`.
- **Eval set immutability**: meta-agent has no tool that writes to `projects/<p>/evals/`.
- **Iteration / cost / wall-time caps**: enforced at the forge loop level; meta-agent cannot exceed.

### API layer

Per `70-api-layer.md` § Authentication:

- **Mandatory auth in prod** (`FOUNDRY_ENV=prod` + `NoAuth` refuses to start).
- **Bearer token / mTLS / institution-specific OIDC** plug-points.
- **CORS configurable** per project (default: same-origin).
- **Rate limiting at the foundry layer** (Phase 8 polish; per `70` open question 5).
- **Structured error responses** (`FoundryError.to_dict()`); never raw stack traces.
- **Audit context capture**: operator identity from auth context propagates to every audit entry.

### Observability layer

Per `80-observability.md` § Privacy + redaction:

- **Field-level redaction**: known sensitive field names (`api_key|password|secret|token|private_key`) dropped from exported spans.
- **Configurable capture toggles**: `capture_inputs`, `capture_outputs`, `capture_tool_args`, `capture_state_diff` per project's `ObservabilityConfig`.
- **Hash-based correlation**: `input_hash` / `output_hash` on tool spans without revealing content.
- **Contract test for credential leak**: known fake key in fixtures must not appear in any exported span.

### Audit layer

Per `52-rollback-and-audit.md`:

- **Append-only** `.foundry/audit.jsonl` per project.
- **Git-tracked** (the file itself; tampering shows up as a diff).
- **Operator identity capture** (meta_agent / human / ci with email + forge_run_id when applicable).
- **Cross-event correlation** (commit_sha, audit_entry_id, eval_run_id, forge_run_id all linkable).

## Prompt-injection defenses

Prompt injection — adversarial content in tool outputs, retrieved documents, or user inputs that tries to override the agent's instructions — is a real attack vector. The foundry's posture:

### What the framework provides

1. **Structured tool boundaries**: per `83` § Tool-output boundary preservation (originally specified in `26-memory-and-context.md`). Tool outputs injected as `user_message_prefix` are wrapped in typed boundaries (`<tool_result tool="..." version="...">...</tool_result>`); agent prompts can reference these explicitly + treat them as untrusted by convention.
2. **Tool allowlist enforcement**: even if a prompt-injected agent "decides" to call a privileged tool, the dispatcher refuses if the tool isn't in the agent's allowlist.
3. **`dangerous: true` flag**: tools that genuinely accept arbitrary content (code-execution, web-fetch) require this flag, which the meta-agent can't scaffold + which surfaces in observability.
4. **Meta-agent sandbox**: a prompt-injected meta-agent reading malicious content via `read_file` cannot escape the sandbox. The structural enforcement is independent of the prompt.
5. **Connection-level sanitization**: handlers operating on tool inputs are responsible for parameterised queries / proper escaping (it's the handler author's job to use bound parameters in SQL, etc.). The framework provides connection clients that make this natural; doesn't enforce it.

### What's not in scope (v1)

- **Active prompt-injection scanning** on tool-output content before it enters the next prompt. Possible but error-prone (false positives + adversarial bypasses); complex enough to need its own subsystem. Marked as v1.1+ in the v1.1 backlog memory.
- **Adversarial input detection** at the API layer. Standard web-app concerns (rate limiting, payload size limits, Content-Type validation) handled by FastAPI + the institution's edge layer. Foundry-specific scanning is v1.1+.

### Recommended operator practices

- **Treat all tool outputs as untrusted** when designing prompts. Don't have agents follow URLs / instructions verbatim from tool results.
- **Use `dangerous: true` only when necessary** + apply strict input constraints (allowed URL patterns, language whitelist, file-size caps).
- **Review `read_file` access patterns** — the meta-agent reading content from external systems via tools could surface malicious content; the sandbox prevents action but the LLM sees the content. Constrain what content tools can return.
- **Keep eval sets clean**: malicious content in eval cases would steer iteration in adversarial directions. Eval sets are operator-authored + reviewed.

## PII / sensitive-data handling

The foundry doesn't have specific "PII detection" features; it has the controls that make handling PII manageable:

| Concern | Mechanism |
|---|---|
| PII in tool outputs leaking to observability | `capture_outputs: false` per `ObservabilityConfig`; redaction allowlist for `ConnectionDescriptor` |
| PII in eval cases (operator may have used real data) | Operator's responsibility; documented in `40-eval-harness.md` § Eval-set evolution and `83` § Recommended operator practices below |
| PII in run artifacts | `capture_inputs: false` + `capture_outputs: false`; only structural metadata persists |
| PII in audit log | Audit entries include `summary` (free-text) + `rationale`; operators avoid PII in these fields by convention |
| PII in checkpoints | Checkpointer stores full state; encrypted at rest at the storage backend (S3 SSE / Postgres TDE / etc.) |
| PHI in particular (HIPAA) | Provider must have BAA; data-residency configured at the provider level; `capture_*` flags off for PHI projects |
| MNPI / market-sensitive | Same as PHI mechanically; access-control on the foundry deployment |
| GDPR personal data | Article 30 records-of-processing satisfied by audit log + retention policy; right-to-erasure handled by the data system, not the foundry |

For the strictest cases (HIPAA, MNPI with individual identifiability): per `86-multi-tenancy-and-ip.md`, eval sets live in a data-access-controlled store (loaded via `EvalSpec.source: "s3://..."` at run time, not in the git repo).

### Recommended PII-aware project configuration

```yaml
# system.yaml
observability:
  trace: otel
  sample_rate: 1.0
  capture_inputs: false           # don't capture inputs
  capture_outputs: false          # don't capture outputs
  capture_tool_args: false        # don't capture tool args
  capture_state_diff: false

guardrails:
  max_iterations: 30
  max_cost_usd: 5.00
```

Costs / latencies / structural metadata still captured; content suppressed. Audit + observability remain functional for non-content debugging (timings, costs, error categories).

## Audit-as-security

A complete audit trail IS a security control. From `52-rollback-and-audit.md`:

- **Every change** to project artifacts produces a commit + an audit entry.
- **Operator identity captured** from auth context.
- **Tampering detection**: audit log is git-tracked; unexpected diffs are visible.
- **Forge attribution**: meta-agent changes link to a `forge_run_id` + supervising human operator.
- **Cross-references**: commit sha ↔ audit entry id ↔ eval_run_id ↔ run_id form a graph.

Compliance-relevant queries:

```bash
# Who changed pipeline_recon last week?
foundry obs audit pipeline_recon --since 7d --by operator

# What tools were used in production runs against PHI data?
foundry obs runs phi_project --status completed | foundry obs trace ...

# Show all rollbacks across all projects this quarter:
foundry obs rollbacks --since 90d
```

These are first-class via the `foundry obs` CLI. No separate compliance tooling needed.

## Compliance-adjacent considerations (not foundry-enforced)

The foundry provides hooks; institutions own compliance:

| Regime | Foundry's contribution | Operator's responsibility |
|---|---|---|
| **SEC books-and-records (financial)** | Audit log retention; commit history; eval results retained per policy | Configure retention; ensure backups / archival; periodic compliance review |
| **HIPAA (healthcare)** | Provider with BAA; capture_* flags off; on-prem inference for strictest cases | BAA execution; IRB approval for research uses; access controls on the deployment; PHI redaction in eval sets |
| **GDPR (EU)** | Article 30 records via audit log; data residency via provider selection; structured personal-data capture controls | DPA with providers; lawful basis for processing; right-to-erasure handling at the data store |
| **SOX (financial reporting)** | 4-eyes via HITL approval workflow; audit attribution per `52` | Define which automated decisions trigger 4-eyes; review process |
| **MIFID II (EU financial)** | Audit log retention; transaction-decision linkage via run_id | Map run_ids to trade decisions in your ops system |
| **AI-specific regulation** (EU AI Act, NIST AI RMF) | Risk classification per project (metadata); model + version traceability | Risk assessments; documented testing protocols; human oversight design |

Operators using the foundry for regulated workloads consult their compliance / legal teams. The foundry doesn't promise regulatory compliance — it provides the engineering hooks that make compliance feasible.

## Failure modes

| Cause | Surfaced as | Recovery |
|---|---|---|
| Secret literal in YAML | `ConfigLoadError` at load OR pre-commit hook refusal | Remove + use credentials_ref; or `# foundry:allow-literal` if false positive |
| Meta-agent attempts forbidden write | `ConfigError("write_file: path outside sandbox")` | Sandbox refused; meta-agent reads error + adapts |
| Meta-agent attempts forbidden git op | `GitBackendError` at meta-tool layer (before subprocess) | Refused; logged |
| Tool name not in allowlist | `ToolNotAllowedError` on dispatch | LLM sees error + can recover |
| Capture-output leak attempt (sensitive content not redacted) | Contract test catches at CI; production: known-fake-key contract test continuous | Tighten denylist patterns |
| Audit log tampering | git diff on `.foundry/audit.jsonl` shows unexpected changes | Investigate; restore from git history |
| Pre-commit hook bypassed (`--no-verify`) | Audit entry records `overrides_used: ["pre_commit_skip"]` | Compliance review; institution policy decides |

## Invariants

1. **Every config file's contents pass through Pydantic + secret scan before reaching runtime.**
2. **Tool dispatch enforces allowlist + input/output validation regardless of LLM behaviour.**
3. **Connection clients are the only authenticated path to external systems**; tools cannot bypass.
4. **Meta-agent's write paths are structurally constrained**; prompt-only safety is belt-and-braces.
5. **Forbidden git operations refused at the meta-tool layer**, not at the git binary.
6. **Sensitive field names dropped from observability** by default-deny denylist.
7. **Audit log is append-only + git-tracked**; tampering is visible.
8. **Operator identity captured on every state-changing operation**.

## Test expectations

### Unit

1. **Secret detection**: each documented pattern fires at config load; `# foundry:allow-literal` opts out.
2. **Sandbox**: each documented bypass attempt refused (path traversal, symlink, catalog write, framework write).
3. **Forbidden git ops**: each documented op refused at meta-tool layer.
4. **Allowlist enforcement**: agent without tool in `tools` list cannot dispatch it.
5. **Capability-required check**: `provider_overrides` not populatable by `build_agent`.
6. **`dangerous: true` refusal**: `build_tool` rejects; manual edit possible (after framework code change).

### Contract

1. **Credential leak test**: end-to-end run with known-fake-key; scan all observability outputs (OTel, audit log, run artifacts); zero hits.
2. **Audit completeness**: every meta-agent action produces an audit entry; gap detection in CI.
3. **Sandbox path canonicalisation**: a 50-case fuzz test of malicious paths (`../`, symlinks, absolute paths, encoded sequences) — all refused.
4. **`dangerous: true` lint**: catalog promotion of a `dangerous: true` tool requires explicit human flag in CI.

### Integration (Phase 9 exit gate)

1. End-to-end forge with adversarial-prompt fixture (meta-agent's prompt corrupted by a malicious file content via `read_file`); structural enforcement refuses all forbidden operations; forge terminates cleanly with violations recorded.
2. PII-aware project: configure `capture_outputs: false`; run end-to-end; assert tool outputs do not appear in any observability surface.
3. Forbidden git op fuzz: 100 attempts at each forbidden operation; all refused.

## Open questions

1. **Active prompt-injection scanner**: in v1.1+ backlog. Tradeoff: false positives vs detection coverage.
2. **Per-project secret rotation hooks**: the framework respects rotation via `SecretsProvider` re-resolution; should there be hooks that detect rotation events + force connection refresh? Lean: yes, additive — `SecretsProvider.on_rotation(callback)` Phase 9 polish.
3. **Differential privacy noise** on observability cost / count metrics for cross-tenant analytics. Out of v1 scope; v1.1+ if real demand.
4. **Hardware security module (HSM) integration** for credential signing / token generation. Lean: out of scope; institutions wire HSMs at the secrets-provider layer (Vault Transit, AWS KMS); foundry doesn't reinvent.
5. **Formal threat model document** beyond this section. Useful for compliance audits; lives in `docs/_compliance/threat_model.md` if institutions need it. Defer until requested.
