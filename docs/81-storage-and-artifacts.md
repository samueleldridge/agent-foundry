# 81 — Storage and Artifacts

## Purpose

The foundry produces and consumes a lot of artifacts: project configs (versioned, in git), run artifacts (per-run records), eval results, forge trajectories, audit logs, observability event mirrors, model manifests, secret-detection rules. This doc specifies where each lives, retention policies, the storage backend abstractions, cleanup workflows, and migration patterns when storage shape evolves.

Builds on `52-rollback-and-audit.md` (audit log), `80-observability.md` (event mirror), and Tier 8's other docs (`84-deployment.md` for prod storage backends).

Three load-bearing properties:

1. **Run artifacts are reproducibility's substrate.** Every run produces a frozen record (inputs, outputs, trace, costs, errors). The audit-trail design depends on these surviving as long as compliance requires.
2. **Storage backends are pluggable.** Filesystem default for dev; S3-compatible / Azure Blob / GCS for prod. The artifact-store interface is uniform.
3. **Retention is policy, not behaviour.** The framework provides the mechanism (TTL + archival + compaction); retention durations are operator configuration per regulatory regime.

## Module layout

```
src/foundry/storage/
├── __init__.py            public surface
├── paths.py               filesystem layout constants + path resolvers
├── backends/
│   ├── filesystem.py      local FS backend (~/.foundry/, projects/<p>/.foundry/)
│   ├── s3.py              S3 / S3-compatible (Minio, R2)
│   ├── azure_blob.py      Azure Blob Storage
│   └── gcs.py             GCP Cloud Storage
├── artifacts.py           RunArtifact / EvalRunResult / ForgeTrajectory writers
├── retention.py           TTL enforcement + archival + compaction
├── migrations.py          storage-format migrations
└── cli.py                 foundry storage <subcommand> dispatch
```

## The full filesystem layout (in scope)

### `~/.foundry/` — per-user / per-host state

```
~/.foundry/
├── config.yaml                user defaults (default model, default tracing, etc.)
├── observability.db           SQLite event mirror (per 80-observability.md)
├── runs/
│   └── <run_id>/              one directory per run
│       ├── meta.json          run metadata (project, system_version, started_at, duration, cost, status)
│       ├── trace.jsonl        full RunEvent stream (one event per line)
│       ├── inputs.json        request input (when capture_inputs: true)
│       ├── outputs.json       final output
│       ├── llm_calls.jsonl    per-LLM-call detail
│       ├── tool_calls.jsonl   per-tool-call detail
│       ├── state_transitions.jsonl  state mutations through the run
│       └── checkpoints/       LangGraph checkpointer files (when SqliteCheckpointer in use)
├── eval_results/
│   └── <eval_run_id>/
│       ├── result.json        EvalRunResult artifact
│       └── per_case/          per-case detail (input, expected, actual, score)
│           ├── case_001.json
│           └── ...
├── forge_runs/
│   └── <forge_run_id>/
│       ├── meta.json
│       ├── trajectory.jsonl   per-iteration IterationRecord
│       ├── events.jsonl       full RunEvent stream from the meta-agent's session
│       ├── interactions/      discuss-mode conversations (interactive mode only)
│       └── final_summary.md
├── checkpoints/
│   └── <run_id>.db            SQLite checkpointer files (default for foundry run)
├── caches/
│   ├── semantic.db            in-process semantic cache (per 24)
│   └── tool_result.db         in-process tool-result cache
├── locks/
│   └── <project>.lock         per-project forge lock (per 62)
├── archives/                  compacted older artifacts
│   ├── runs-2026-01.tar.gz
│   ├── eval_results-2026-01.tar.gz
│   └── ...
└── secret_patterns.yaml       user-extensible secret-detection patterns (per 12)
```

### `projects/<project>/.foundry/` — per-project state

```
projects/<project>/.foundry/
├── audit.jsonl                 append-only audit log (per 52-rollback-and-audit.md)
├── eval_history.jsonl          per-eval-run summary (one line per eval run; cross-references ~/.foundry/eval_results/<id>)
├── pinned_runs.txt             optional: list of run_ids the operator has marked for retention beyond the TTL
└── config.local.yaml           optional: per-project foundry overrides
```

### Project artifacts in git (`projects/<project>/`)

These are git-tracked, NOT in `~/.foundry/`:

- `system.yaml`
- `state.yaml`
- `agents/<agent>/agent.yaml`, `prompts/v<N>.md`, `output_schema.py`
- `functions/<n>/function.yaml`, `function.py`
- `tools/<name>/v<N>/...`
- `connections/<name>/v<N>/...`
- `evals/<name>.yaml`
- `versions.json` files at artifact parents

These are the project's source of truth. The foundry doesn't manage their retention; git history does.

## Storage backend abstraction

```python
class StorageBackend(Protocol):
    """Backend for run artifacts, eval results, forge trajectories, archives.
    Audit log + observability.db remain on local filesystem regardless of
    backend (they're per-host / per-process state)."""

    async def put(self, key: str, content: bytes, content_type: str = "application/json") -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def list(self, prefix: str, limit: int = 1000) -> list[StorageKey]: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def get_metadata(self, key: str) -> StorageMetadata: ...

class StorageKey(BaseModel):
    key: str
    size_bytes: int
    last_modified: datetime
    content_type: str
```

Concrete implementations:

| Backend | When | Configuration |
|---|---|---|
| `FilesystemBackend` (default) | dev, single-host prod | `FOUNDRY_STORAGE_ROOT=~/.foundry/` (default) |
| `S3Backend` | multi-host prod, AWS | `FOUNDRY_STORAGE_BACKEND=s3 / FOUNDRY_STORAGE_BUCKET=foundry-artifacts / FOUNDRY_STORAGE_PREFIX=prod/` |
| `S3CompatibleBackend` | self-hosted MinIO, Cloudflare R2, etc. | `FOUNDRY_STORAGE_BACKEND=s3_compatible / FOUNDRY_STORAGE_ENDPOINT=https://...` |
| `AzureBlobBackend` | Azure-resident workloads | `FOUNDRY_STORAGE_BACKEND=azure_blob / FOUNDRY_STORAGE_CONTAINER=foundry-artifacts` |
| `GCSBackend` | GCP-resident workloads | `FOUNDRY_STORAGE_BACKEND=gcs / FOUNDRY_STORAGE_BUCKET=foundry-artifacts` |

Selection at startup via env vars. Auth via standard cloud-provider chains (boto3 default, Azure DefaultAzureCredential, etc.) — not foundry's `SecretsProvider` (storage backends are infrastructure, not connections).

### Per-run artifact path scheme

```
<storage_root_or_prefix>/runs/<yyyy>/<mm>/<run_id>/<file>
```

Example: `s3://foundry-artifacts/prod/runs/2026/04/01JKM4ABCDEF/trace.jsonl`.

Date-prefixed for natural archival (delete old months wholesale) + S3 listing efficiency (LIST scoped to a prefix is fast).

## Retention policy

Default retention per artifact kind. Operator-configurable via `~/.foundry/config.yaml` or `projects/<p>/.foundry/config.local.yaml`:

```yaml
# ~/.foundry/config.yaml
retention:
  runs:
    raw_days: 90                        # detailed run artifacts (trace, llm_calls, tool_calls)
    summary_days: 365                   # meta.json + summary persists longer
    archive_after_days: 90              # archive raw → archives/runs-yyyy-mm.tar.gz after this
    delete_archives_after_days: 1825    # 5 years; per regulatory regime

  eval_results:
    raw_days: 365
    summary_days: 1825
    archive_after_days: 365
    delete_archives_after_days: 1825

  forge_runs:
    raw_days: 365
    summary_days: 1825
    archive_after_days: 365
    delete_archives_after_days: 1825

  audit_log:
    compact_after_months: 12            # compact older months to gzip per 52
    retain_compacted: forever           # audit retention is regulatory; never delete

  observability_db:
    raw_event_days: 90
    aggregated_metrics_retain: forever  # rollups (per-day cost, per-week scores) kept indefinitely

  caches:
    semantic_cache_max_entries: 10000   # LRU eviction at this size
    tool_result_cache_max_entries: 50000

  checkpoints:
    completed_run_days: 30              # checkpoints for completed runs deleted after N days
    pending_run_days: 365               # interrupted/HITL-pending runs retained longer
```

### Why each default

- **Runs raw 90 days**: typical debugging window for ops investigations.
- **Runs summary 365 days**: enables year-over-year cost / quality comparison without loading raw data.
- **Forge raw 365 days**: forges are infrequent + high-value evidence for "why did the agent change."
- **Eval results 1825 days (5y)**: matches typical financial-services audit retention (SEC 17a-4); shorter for healthcare / longer for some EU GDPR scenarios.
- **Audit retention forever**: compacted, not deleted. Tampering with audit history is the regulatory line; compacting (gzip per month) reduces size 5–10× without losing data.
- **Observability raw 90 days**: balance between debugging utility + storage cost.

Operators tune per institution / regulatory regime.

### Pinned retention

Operators can mark specific runs / eval-runs / forge-runs as pinned (via `foundry storage pin <kind> <id>`); pinned items are excluded from TTL deletion. Useful for runs referenced in incident reports, audit findings, or research datasets.

```bash
foundry storage pin run 01JKM4ABCDEF --reason "incident-2026-04-investigation"
foundry storage list-pinned
foundry storage unpin run 01JKM4ABCDEF
```

Pinned-list itself is in `projects/<p>/.foundry/pinned_runs.txt` for project-scoped pins or `~/.foundry/pinned_global.txt` for global.

## Archival pattern

Older run artifacts compress to monthly tarballs:

```
~/.foundry/archives/
├── runs-2026-01.tar.gz        all runs from January 2026
├── runs-2026-02.tar.gz
├── eval_results-2026-01.tar.gz
└── forge_runs-2026-01.tar.gz
```

Compression typically achieves 5–10× reduction (JSONL with repeated fields compresses well). Read access requires extraction; write archives are append-only (operator can't add to a closed month).

`foundry storage archive` runs the archival pass:

```bash
foundry storage archive --kind runs --older-than 90d
# Compacts runs older than 90d into monthly tarballs; deletes uncompressed
```

CI typically runs this nightly via a cron job. The foundry doesn't run it automatically (storage management is operator policy).

## Cleanup commands

```bash
foundry storage stats                      # disk usage by kind
foundry storage ls runs --since 7d         # list recent runs
foundry storage ls runs --status failed
foundry storage rm run <run_id>            # delete a specific run (irreversible)
foundry storage gc --kind runs --older-than 90d   # garbage collect per retention policy
foundry storage gc --kind runs --older-than 90d --dry-run     # preview what would be deleted
foundry storage compact-audit <project>    # compact audit log per 52
foundry storage migrate <from-backend> <to-backend>   # migrate artifacts between backends
```

`foundry storage gc` respects pinned items; `--force` overrides (dangerous; logged).

## Multi-host storage considerations

For multi-host deployments (per `85-batch-and-throughput.md`):

- **Run artifacts**: written by the worker handling the run; read access from any worker requires shared storage (S3 / Azure / GCS). Filesystem backend doesn't work for multi-host.
- **Audit log**: per-project file under `projects/<p>/.foundry/audit.jsonl` lives in the **project's git repo**, NOT shared storage. Workers read the latest version from the local checkout. For frequently-mutating audit trails (high-iteration forges), the audit log is also mirrored to the audit store (Postgres) for queryability.
- **Observability DB**: per-worker SQLite mirror. Each worker has its own. The OTel stream is the cross-worker source of truth; SQLite is per-worker convenience.
- **Checkpoints**: per `85`, Postgres checkpointer (not filesystem) for multi-host. Checkpoint files don't apply.
- **Caches**: per `24`, Redis-backed for multi-worker; filesystem cache is per-worker only.

Operators set up the storage backends per their deployment; foundry's role is honouring the configured backends.

## Audit-log + observability storage divergence

Two related but distinct concerns:

| Concern | Substrate | Backend in prod |
|---|---|---|
| Audit log (`52-rollback-and-audit.md`) | per-project JSONL in git + mirror | Postgres in prod for queryability |
| Observability events (`80-observability.md`) | OTel stream + per-host SQLite | OTel collector → backend (Datadog / Langfuse / Grafana stack) |

They overlap: a forge iteration produces an audit entry AND an observability event. The audit entry is the **regulatory record** (immutable, retained for compliance); the observability event is the **operational signal** (queried for monitoring, retention shorter).

Cross-reference: every audit entry's `commit_sha` correlates with the corresponding `RunEvent.run_completed`'s `run_id`. Operators investigating an incident can pivot between audit and observability via these cross-references.

## Storage migrations

When the foundry's storage format evolves between framework versions:

```bash
foundry storage migrate-format
# Detects schema_version in stored artifacts
# Applies migrations from foundry/storage/migrations.py
# Idempotent; safe to re-run
```

Migrations are kept forever (same discipline as config-schema migrations per `50-versioning-model.md` § Schema evolution): never remove old migrations.

For backend migration (filesystem → S3, S3 → GCS):

```bash
foundry storage migrate filesystem s3 \
  --kinds runs,eval_results,forge_runs \
  --since 2026-01-01 \
  --batch-size 100
```

Reads from source, writes to destination, optionally deletes source after verification. Non-destructive by default (`--delete-source` opt-in).

## Failure modes

| Cause | Surfaced as | Recovery |
|---|---|---|
| Storage backend unavailable | metric alert (`foundry.observability.degraded`); writes queue in memory; eventual drop | restore backend; re-emit if buffered |
| Disk full (filesystem backend) | write fails; metric alert; subsequent runs fail-fast | clear archives or expand disk |
| S3 IAM auth failed | `StorageError`; foundry-serve refuses to start (catch at startup) | fix IAM |
| Retention deletion accidentally removes pinned | impossible if `foundry storage gc` is used; deliberate `rm` ignores pin check by default with `--force` |
| Archive corruption | `StorageError` on read; specific tarball replaced from backup if available |
| Migration fails midway | partial state; safe to re-run (idempotent); operator may need to clean partial migrated entries |

## Invariants

1. **Run artifacts are immutable once written.** Editing a `meta.json` after run completion is a bug.
2. **Audit log is append-only.** Per `52-rollback-and-audit.md`.
3. **Pinned items are exempt from TTL.** Pin checks happen before deletion in `foundry storage gc`.
4. **Storage backends are pluggable.** No code path assumes filesystem-specific semantics outside `FilesystemBackend`.
5. **Migrations are forever.** Old format always loadable.
6. **Date-prefixed paths**: enables S3 prefix scans + monthly archival.

## Test expectations

### Unit

1. **Backend interface conformance**: each backend (FS, S3, Azure, GCS) implements all methods; round-trip put/get/list/delete works.
2. **Retention math**: `gc --older-than 90d` correctly identifies items past TTL; pinned items excluded.
3. **Archival**: monthly tarball produced; uncompressed deleted; tarball is reading-readable.
4. **Migration application**: v1 fixture artifact + v1→v2 migration → loads as v2; idempotent.

### Contract

1. **No data loss in backend migration**: copy fixture set from FS → S3; verify all artifacts present + readable; no silent drops.
2. **Audit log is git-tracked but not in `~/.foundry/`**: lint check / structural test.

### Integration (Phase 9 exit gate)

1. Multi-host setup: run on worker A; read run artifact from worker B via S3; identical content.
2. Retention enforcement: configure 1-day retention; run a fixture run; advance clock; `foundry storage gc` removes it.
3. Pinned protection: pin a run; configure 1-day retention; `gc` does NOT delete pinned.

## Open questions

1. **Encryption at rest**. Currently relies on backend-level encryption (S3 SSE, Azure SSE, etc.). Should the foundry add per-artifact application-level encryption? Lean: no for v1 — backend encryption is standard; double-encryption adds operational complexity. v1.1+ if regulatory regime demands.
2. **Artifact deduplication**. Two runs with identical inputs produce similar artifacts; content-addressed storage could dedupe. Lean: defer; complexity not justified for v1's scale.
3. **Streaming append to artifacts during a run**. Currently artifacts are written at run completion. Streaming append (similar to OTel) would enable mid-run inspection. Lean: defer; the OTel stream + SQLite mirror cover the live-inspection case.
4. **External pinning (regulatory hold)**. Some institutions need to mark specific runs as "subject to legal hold; cannot delete regardless of retention." Lean: yes, additive — `legal_hold: bool` flag on pinned items; never deleted by GC even with `--force`. Phase 9 polish.
5. **Per-project artifact backend**. Different projects in the same deployment using different backends (compliance-isolation). Lean: yes, supported via `projects/<p>/.foundry/config.local.yaml` overriding `FOUNDRY_STORAGE_BACKEND`. Phase 9 polish.
