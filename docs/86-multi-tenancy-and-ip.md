# 86 — Multi-Tenancy and IP Boundaries

## Purpose

`agent-foundry` is a framework. Its value grows when multiple engineers and institutions use it — but most institutions have hard constraints on what can leave their perimeter: proprietary processes, internal system schemas, regulated data (MNPI, PHI, PII), audit trails. This doc specifies the **multi-institution deployment pattern** that lets the framework be shared while keeping every institution's IP, data, and operational history fully private.

The separation is architectural, not procedural. The framework cannot accidentally leak an institution's artifacts because they live in a different repository the framework has no reference to.

## The three layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — Upstream framework (public or shared-private)             │
│  github.com/<owner>/agent-foundry                                    │
│                                                                      │
│    src/foundry/          the Python package                          │
│    catalog/public/       generic tools + connections                 │
│    docs/                 generic design docs                         │
│    tests/                framework test suite                        │
│                                                                      │
│  Released as a versioned package (`foundry==1.3.0`).                 │
│  Consumers pin exact versions in their own pyproject.toml.           │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                   ┌───────────────┴───────────────┐
                   │  pip / uv install foundry     │
                   │                               │
                   ▼                               ▼
┌───────────────────────────────┐   ┌───────────────────────────────┐
│  LAYER 2 — Institution repo   │   │  LAYER 2 — Institution repo   │
│  github.com/<A>/foundry-A     │   │  github.com/<B>/foundry-B     │
│  (private)                    │   │  (private)                    │
│                               │   │                               │
│  catalog/                     │   │  catalog/                     │
│    tools/                     │   │    tools/                     │
│      institution-specific     │   │      institution-specific     │
│    connections/               │   │    connections/               │
│      internal system connects │   │      internal system connects │
│                               │   │                               │
│  projects/                    │   │  projects/                    │
│    system configs, agents,    │   │    system configs, agents,    │
│    prompts, project-local     │   │    prompts, project-local     │
│    tools/connections, evals,  │   │    tools/connections, evals,  │
│    audit logs                 │   │    audit logs                 │
│                               │   │                               │
│  pyproject.toml               │   │  pyproject.toml               │
│    foundry ==1.3.0            │   │    foundry ==1.3.0            │
│  deploy/ (Dockerfile, k8s)    │   │  deploy/ (Dockerfile, k8s)    │
└───────────────────────────────┘   └───────────────────────────────┘
                   │                               │
                   ▼                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — Runtime (institution-operated, regulated)                 │
│                                                                      │
│    Deployed processes (uvicorn workers, batch runners)               │
│    Shared state: Postgres checkpointer, Redis rate limiter, audit    │
│    Secrets: Vault / AWS SM / GCP SM / Azure KV                       │
│    Regulated data stores: real trade DBs, patient records, etc.      │
│                                                                      │
│  Never touches Layer 1 other than reading the installed package.     │
└──────────────────────────────────────────────────────────────────────┘
```

## What lives in each layer — normative reference

### Layer 1: Upstream framework

**In scope:**
- The entire `src/foundry/*` Python package.
- `catalog/public/` — artifacts that are useful to *any* institution and contain no proprietary information: `http_get`, `query_postgres` (generic DSN), `send_email_via_ses`, `slack_workspace`, `aws_session`, `azure_entra`, `gmail_oauth`, `github_app`, `oauth2` / `jwt_bearer` / `mtls` helpers.
- Design docs, developer docs, contribution guide.
- Framework unit, contract, and integration tests against public artifacts.
- Example/template projects under `examples/` — clearly labelled, no real data, used for framework testing.

**Out of scope:**
- Any tool, connection, project, prompt, or eval set that references internal system names, schemas, table names, URLs, accounts, or rubrics.
- Any real or realistic-looking data (trades, patients, customers, transactions).
- Any credentials, account IDs, or secrets in any form.

### Layer 2: Institution repo

**In scope:**
- `catalog/tools/` — tools that reference internal systems.
- `catalog/connections/` — connections to internal systems (Snowflake with the institution's account, internal APIs with real host names, etc.).
- `projects/` — the full multi-agent systems the institution runs: `SystemSpec`, `StateSpec`, agents, prompts, project-local tools/connections, eval sets.
- `versions.json` metadata per artifact.
- `.foundry/audit.jsonl` per project (or Postgres audit for prod).
- `deploy/` — environment-specific manifests (Dockerfile, k8s, Terraform, env templates minus secrets).
- `pyproject.toml` with `foundry` pinned to a specific version.

**Out of scope:**
- Credentials in any form. Always referenced via `CredentialsRef` → `SecretsProvider` → external vault.
- Raw regulated data (trade records, patient files). Eval sets may contain anonymised derivatives if the institution's compliance/ethics process permits.

### Layer 3: Runtime

**In scope:**
- Running processes (uvicorn workers, batch runners, CLI invocations during ops).
- Shared state: Postgres checkpointer, Redis rate limiter/circuit breaker, audit store.
- Secret-manager connections (Vault token, AWS IAM role, GCP workload identity, Azure managed identity).
- Connection-pool instances holding authenticated clients to real internal systems.

**Out of scope:**
- Any persistence of raw secrets on disk. Secrets stay in the vault; in-memory handles are short-lived, redacted on log emit.

## Runtime root resolution

Environment variables wire Layer 1 + Layer 2 together at process start:

```
FOUNDRY_CATALOG_ROOTS="/opt/foundry/catalog/public,/opt/foundry-acme/catalog"
FOUNDRY_PROJECTS_ROOT="/opt/foundry-acme/projects"
```

Catalog ref resolution walks roots left-to-right. First entry containing the named artifact wins. Later roots may shadow via warning; strict mode (`FOUNDRY_ALLOW_SHADOWING=strict`) makes any shadowing an error.

The meta-agent's write sandbox is pinned to `FOUNDRY_PROJECTS_ROOT/<scoped_project>/` only. It cannot write to any catalog root or to the framework package.

## Consumer repo template

What an institution's repo looks like at the start:

```
foundry-acme/
├── README.md                 Internal README: how to run, who owns what
├── pyproject.toml            foundry ==1.3.0 + internal deps
├── uv.lock                   committed
├── .python-version           3.12
├── .gitignore                .venv, __pycache__, .env, etc.
├── catalog/
│   ├── tools/
│   │   └── .gitkeep          populated as institution builds new tools
│   ├── connections/
│   │   └── .gitkeep
│   └── index.yaml            institution's catalog index
├── projects/
│   └── .gitkeep              projects added over time
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml    (staging)
│   ├── k8s/                  (prod)
│   └── env.template          env vars minus secrets; secrets documented separately
├── scripts/
│   └── deploy_prod.sh        institution-specific deploy wrapper
└── tests/
    └── smoke/                institution smoke tests (connections healthy, etc.)
```

Initialised from a tiny template repo (published alongside the framework) via `foundry init --kind institution <name>`. First `foundry project new <project>` populates `projects/<project>/`.

## Version evolution across the boundary

### Framework upgrades

- Framework releases follow semver (`1.3.0` → `1.4.0` minor; → `2.0.0` major).
- Consumer bumps by editing `pyproject.toml` + running `uv lock`.
- Release notes enumerate any breaking changes (rare for minor) and required migrations.
- Framework contract tests run in the institution's CI against their catalog to catch integration regressions before deploy.

### Public catalog item upgrades

- Each public catalog item has its own version line. `catalog/http_get/v1` → `v2`.
- Institution's projects explicitly pin (e.g. `catalog/http_get@v1`).
- Bumping an institution's pin is a deliberate commit in the institution's repo, usually paired with a local eval run.
- An institution may stay on `v1` indefinitely if `v2`'s behaviour doesn't suit them.

### Institution-specific artifact evolution

- Entirely in the institution's repo. Framework has no visibility.
- Meta-agent iterations, rollbacks, version pins all land as commits on the institution's `foundry/<project>` branch.

## Contribution flow

### Generic improvements → upstream PR

- Framework bugs, new auth schemes, new public catalog tools that have zero institutional flavour.
- Goes via PR to `github.com/<owner>/agent-foundry`.
- Reviewed by the framework maintainers.
- Released in the next minor/patch version.
- Before opening such a PR from an institution's work: strip every reference to internal systems, schemas, and data. The PR should be framework-useful in isolation.

### Selective promotion of an institution-private artifact

- Institution realises they've built a genuinely generic tool (e.g. `query_postgres_read_replica` connection pattern).
- Institution engineer strips it of any institution specifics (renames, removes internal config defaults, generalises the schema).
- Opens a PR to upstream `catalog/public/tools/<name>/v1/` (or `connections/<name>/v1/`).
- Once merged upstream, other institutions can adopt.

### Institution-specific → NEVER shared

- Project configs, prompts that reference proprietary processes, eval sets with real (even anonymised) data, audit logs, internal system connections, internal rubrics.
- No path from institution repo → upstream for these. No audit flow, no "share with peer institution" button. The boundary is structural.

## Data-handling considerations

Repo separation is the cheap part. Data handling is the costly part — and most of it is operational discipline, not framework behaviour. The framework provides hooks; each institution owns the policy.

### Regulated data in eval sets

Eval sets are real power — but they're where regulated data most plausibly enters the repository perimeter.

- Eval cases that include real trades, patient records, customer PII, etc. MUST go through the institution's data-handling review before landing in git.
- Typical mitigations: anonymisation, tokenisation, synthetic derivation, or outright prohibition (eval only in ephemeral runs against a non-persisted DB).
- For the strictest cases (HIPAA, MNPI with individual identifiability): eval sets live in a data-access-controlled store, not the git repo. The foundry supports this via `EvalSpec.source: "s3://..."` / `"postgres://..."` (loaded at eval-run time, never written to the repo).

### LLM provider selection

- For most enterprise institutions, the LLM provider must have:
  - An executed enterprise agreement (not a personal ChatGPT key).
  - Data-processing terms matching the regulatory regime (BAA for HIPAA, SOC 2 for most financial services, GDPR terms for EU).
  - Data-residency matching institution requirements.
- The framework's capability-required compile-time check makes this visible: if the institution bans `cache_control` or requires on-prem, the relevant provider is selected in `ModelBinding` and capability-incompatible configs fail compile.

### Audit trails are themselves regulated

- Financial services: SEC books-and-records retention rules.
- Healthcare: HIPAA access logs.
- EU: GDPR Article 30 records-of-processing.
- The foundry's audit store (`foundry.versioning.audit`, OTel event stream, run artifacts) is the system of record for these. Storage lifecycle, access control, and retention are institutional decisions configured via `FOUNDRY_AUDIT_STORE=...`.

### Prompt content is IP

- A well-engineered prompt encoding a firm's exception-triage rubric IS the firm's IP. Treating the prompt file as source code (access-controlled, reviewed, audit-logged) is correct.
- Prompts referencing internal process documents, control frameworks, or proprietary classifiers should never appear in the upstream framework or in an upstream PR.

### Cross-jurisdiction inference

- Sending an EU-resident customer's data to a US-hosted LLM endpoint is a GDPR matter regardless of whether the code is clean. Provider-selection config must reflect the data's origin.
- The framework doesn't introspect request content for jurisdiction — that's the calling pipeline's responsibility — but the provider abstraction makes it routine to pick the right endpoint.

## Deployment patterns by institution profile

### Financial services

- **Provider**: Anthropic via AWS Bedrock in a specific region, OR Azure OpenAI in a specific tenant.
- **Checkpointer**: internal Postgres (not managed RDS in a different region).
- **Rate limiter / run registry / audit**: internal Redis + Postgres, not hosted services.
- **Secrets**: HashiCorp Vault or AWS Secrets Manager with strict RBAC.
- **Observability**: OTel → internal collector → internal Datadog/Grafana. No cloud-vendor-hosted observability without explicit review.
- **Audit retention**: 7 years typical (SEC); audit store mirrors to compliance archival.
- **Deployment**: on-prem or VPC-resident, not internet-exposed.

### Regulated non-financial (healthcare, research, public-sector workloads handling sensitive personal data)

- **Provider**: Azure OpenAI (BAA / DPA available) OR an on-prem inference stack for strictest cases.
- **Checkpointer**: institutional Postgres; encrypted at rest; access via IAM-federated roles.
- **Secrets**: institutional vault.
- **Observability**: on-prem collector; no third-party observability carrying sensitive payloads.
- **Audit retention**: per institutional policy + regulatory minimums.
- **Ethics / governance touch**: any use case touching identifiable records typically requires institutional review (ethics board, IRB equivalent); the foundry doesn't gate this — operational discipline does.
- **Data residency**: all inference and storage within the approved perimeter.

### Open-source research or early-stage product

- Can run on Anthropic / OpenAI direct; no BAA required.
- Public GitHub repo for institution code is fine if nothing proprietary.
- Credentials still via vault / env, never in repo.

## Security checklist (for an institution adopting the framework)

Before the first production deployment:

- [ ] Secrets plumbed via `SecretsProvider` backing an approved vault.
- [ ] No credential literals in YAML (secret-literal scan on by default; confirm alerts are reviewed).
- [ ] Provider selection matches the data-handling regime (BAA / DPA / SOC 2 where required).
- [ ] Eval-set sourcing reviewed: where does the data come from, what redaction, what retention?
- [ ] Audit-store retention configured to match regulatory minimum.
- [ ] Deployment runs inside the approved perimeter (VPC, on-prem, regional).
- [ ] Access control on the institution repo: who can commit, who can push to `main`, who can approve `foundry catalog promote`.
- [ ] `FOUNDRY_ALLOW_SHADOWING=strict` for prod — accidental catalog shadowing is an error, not a warning.
- [ ] Kill switches documented: how do you disable the project's API endpoint in under 60 seconds?
- [ ] Runbook: what does an operator do when a `run.failed` event fires at 3am?

## CLI surface for institution setup

- `foundry init --kind institution <name>` — scaffold an institution repo from the template.
- `foundry init --kind project <name>` — create a new project inside an institution repo.
- `foundry doctor` — print resolved catalog roots, check shadowing, validate sandbox setup, confirm Postgres/Redis reachability, print effective provider capabilities per binding.
- `foundry catalog ls` — list all visible catalog entries, grouped by root, with a `[public]` / `[private]` tag and pinned versions for the current project.
- `foundry catalog promote <project>/<tool>` — promote a local tool to the institution's private catalog (human-gated within the institution).
- `foundry catalog upstream-pr <kind>/<name>` — open a PR against the upstream framework repo with a selected catalog artifact (checks: no institution references in content, signed commit). Convenience; the same can be done manually.

## Invariants

1. **The upstream framework's `catalog/public/` contains zero institution-specific content.** Enforced by a linter in the upstream CI: no references to institution names, internal host names, proprietary rubric text, or realistic-looking regulated data in any `catalog/public/` file.
2. **An institution repo does not modify the framework package.** The framework is pinned as a dependency, not vendored or forked. (Forking is allowed if an institution wants to, but it's a deliberate choice and loses upstream updates.)
3. **The meta-agent cannot write to any catalog root.** Sandbox: absolute-path canonicalisation + prefix check against the catalog roots list and the framework root.
4. **Meta-agent `write_file` is scoped to `projects_root/<project>/`.** Any attempt outside → `ConfigError` with context naming the attempted path and the allowed prefix.
5. **Secrets never appear in any git repository at any layer.** Secret-literal scan runs on config load. Pre-commit hook in the institution template catches commits before they land.
6. **Shadowing is visible.** Public/private catalog shadowing logs at startup; strict mode blocks it.
7. **Cross-institution artifact visibility does not exist.** No runtime code path reads from another institution's repo or data store. Auth is structural (different repos) not procedural.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Catalog ref not found in any root | `RefResolutionError` with context listing each root checked |
| Two catalog roots contain same name, strict mode | `ConfigError("catalog shadowing in strict mode")` |
| Meta-agent attempts write outside projects_root/<project>/ | `ConfigError` at the sandbox boundary; run aborts |
| Secret literal detected in institution YAML | `ConfigLoadError` at load |
| Deploy to runtime without `FOUNDRY_CATALOG_ROOTS` | Framework defaults to `<framework_root>/catalog/public` only; institution tools fail to resolve; clear error on first ref |
| Institution repo pins framework version no longer on PyPI | `uv` install error; document framework version-retention policy |

## Test expectations

### Upstream framework tests

1. **`catalog/public/` lint**: no references to known institution names, internal host patterns, or realistic-looking regulated data.
2. **Sandbox**: a test meta-agent attempts writes outside `projects_root`; confirms each is rejected.
3. **Multi-root resolution**: simulated two-root setup; ref uniqueness test; shadowing test (strict + lenient).

### Institution-template tests

1. **`foundry init`** produces a valid skeleton that passes `foundry doctor`.
2. **Example private project** in the template scaffolds, runs a trivial eval against a fake provider, and leaves no artefacts outside the intended tree.
3. **`foundry catalog upstream-pr`** content-checks a selected artifact for institution references before producing a diff.

## Operational documentation for the consumer

Each institution repo should carry an internal README covering at least:

- Which framework version is pinned + upgrade cadence policy.
- Which catalogue entries are institution-specific and why.
- Who owns each project + on-call escalation.
- Deployment runbook (how to ship, how to roll back, who approves).
- Compliance touch-points (who reviews prompt changes, who reviews eval-set changes, who reviews catalog promotion).

This doc is generic to the framework; the institution replaces placeholders.

## Open questions

1. **Sub-institution tenancy.** A big bank with 4 desks might want further isolation (desk A's projects invisible to desk B). Supported? Recommend: not built-in; each desk runs its own foundry instance with its own catalog + projects. If a single deployment must serve multiple desks, a "tenant" axis on `SystemSpec` could be added later; defer.
2. **Shared but controlled catalog between friendly institutions.** Two institutions want to share generic tools with each other but not with third parties. Recommend: a private fork of `agent-foundry` with a shared `catalog/public_shared/` tree, mounted into both institutions' `FOUNDRY_CATALOG_ROOTS`. No framework change needed.
3. **Template repo distribution.** Should `foundry init` download the institution-template from a fixed URL, or carry it in the framework package? Lean: carry it in the package so offline installs work, update cadence is tied to framework release.
4. **Detecting institution-flavour creep in the public catalog.** Automated linter is one hop; manual review catches what the linter can't. Recommend: both, with a linter baseline + maintainer-reviewed PRs.
5. **Legal.** The upstream framework's licence (open-source vs shared-private vs proprietary) is an institutional decision beyond this doc's scope; licensing affects how Layer 1 can be used, not where Layer 2 lives.
