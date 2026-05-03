# 84 — Deployment

## Purpose

This doc consolidates the operator workflows for deploying foundry projects: container packaging, image lifecycle, sample manifests for major cloud platforms (Kubernetes, ECS, Cloud Run, Azure Container Apps, Fly.io, Nomad), CI/CD reference patterns (already in `70-api-layer.md` § CI/CD; cross-referenced here), the `foundry deploy` command, blue/green and canary patterns, the deployment-rollback vs config-rollback distinction, multi-environment workflows (dev/staging/prod), and compliance / data-residency considerations.

The principle: **the foundry is platform-agnostic**. Anywhere a Python container can run with env vars works. The foundry provides image-building conventions + pre-deploy gates + observability hooks; the institution's deploy team handles the actual rollout via their existing tooling.

Three load-bearing properties:

1. **`<project>:<system_version>` is the deployment unit.** Image tags map to content-hashed config state; "what's running where" is auditable from a single string.
2. **`foundry deploy` gates on quality, not on infrastructure**. Pre-deploy eval against staging dependencies; the framework refuses to roll out a regressed config. The actual rollout mechanism is delegated.
3. **Config rollback and deployment rollback are independent layers.** Operators get clear semantics: config-rollback is for iteration cycles; deployment-rollback is for production incidents.

## Module layout

```
src/foundry/
├── cli/
│   └── deploy.py            foundry deploy / compute-version / build-image (helpers)
└── deploy/
    ├── compute_version.py   foundry compute-version <project>
    ├── pre_deploy_eval.py   pre-deploy eval gate
    ├── deploy_recorder.py   audit + observability for deployments
    └── platforms/           platform-specific helpers (k8s, ecs, cloud_run, etc.)
        ├── kubectl.py
        ├── ecs.py
        ├── cloud_run.py
        └── nomad.py
```

The platform helpers are thin wrappers around platform CLIs (kubectl, aws, gcloud, nomad) — they parse output + pipe through standard observability. Operators using non-supported platforms get the foundry's pre-deploy gate + the image; they wire their own rollout.

## Container packaging

### Sample Dockerfile (institution responsibility)

Per `70-api-layer.md` § Sample Dockerfile. Recap with refinement for production:

```dockerfile
# Dockerfile (institution's repo per 86-multi-tenancy-and-ip.md)
FROM python:3.12-slim AS builder

ARG PROJECT
ARG COMMIT_SHA
ARG SYSTEM_VERSION

WORKDIR /app

# Install uv (fast resolver + installer)
RUN pip install --no-cache-dir uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (production-only; --no-dev)
RUN uv sync --frozen --no-dev

# Copy the project tree
COPY . .

# Production stage: smaller image, no build tools
FROM python:3.12-slim AS runtime

WORKDIR /app
COPY --from=builder /app /app

# Bake provenance metadata (visible in `docker inspect`)
LABEL foundry.project="${PROJECT}"
LABEL foundry.commit_sha="${COMMIT_SHA}"
LABEL foundry.system_version="${SYSTEM_VERSION}"
LABEL foundry.framework_version="1.3.0"

# Default env (overridable per environment)
ENV FOUNDRY_ENV=prod
ENV FOUNDRY_PROJECT=${PROJECT}
ENV FOUNDRY_HOST=0.0.0.0
ENV FOUNDRY_PORT=8080

# Healthcheck (Docker-level; cloud platforms typically use HTTP probes instead)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

# Use uv to run foundry; no shell for security + signal forwarding
ENTRYPOINT ["uv", "run", "foundry"]
CMD ["serve", "--project", "${FOUNDRY_PROJECT}", "--host", "${FOUNDRY_HOST}", "--port", "${FOUNDRY_PORT}", "--workers", "4"]
```

Notes:
- **Two-stage build** keeps the runtime image small (no `uv` binary, no build artifacts).
- **Distroless base** is an option for stricter security: `gcr.io/distroless/python3-debian12`. Smaller, no shell, harder to debug. Trade off per institution risk profile.
- **Non-root user** recommended for production; add `USER 1001` after the install steps + `chown` the working directory.
- **Image tags** double-tag: specific (`foundry-pipeline-recon:cb861da9abcd1234`) AND `latest` (or `main`). Specific tag for auditability; floating tag for "deploy current" convenience.

### `foundry compute-version`

```bash
foundry compute-version --project pipeline_recon
# → cb861da9abcd1234

foundry compute-version --project pipeline_recon --include-git-sha
# → cb861da9abcd1234@f1d1542
```

Computes the project's `system_version` content hash (per `50-versioning-model.md`) — used as the image tag. Deterministic; same project state = same hash.

For CI: `VERSION=$(foundry compute-version --project pipeline_recon)` is the standard one-liner.

### `foundry build-image` (optional helper)

```bash
foundry build-image \
  --project pipeline_recon \
  --dockerfile ./Dockerfile \
  --tag-prefix foundry-pipeline-recon \
  --push registry.internal/
```

Wraps `docker build` + `docker push` with the version-stamping baked in. Optional convenience; institutions free to handle build via their existing pipelines.

## `foundry deploy` command

```bash
foundry deploy <project> \
  --image <full-image-ref> \
  [--target dev|staging|prod] \
  [--platform kubectl|ecs|cloud-run|fly|nomad|noop] \
  [--pre-deploy-eval <path>] \
  [--production-floor 0.90] \
  [--dry-run] \
  [--skip-eval] \
  [--rollout-strategy rolling|blue-green|canary]
```

Behaviour:

```
foundry deploy pipeline_recon --image foundry-pipeline-recon:cb861da9abcd1234 --target prod
   │
   ├── 1. PRE-FLIGHT
   │     - verify image exists + is accessible from this CI runner
   │     - read manifest labels; confirm system_version matches the deployed project's HEAD
   │     - load deployment config from projects/<p>/deploy/<target>.yaml
   │
   ├── 2. PRE-DEPLOY EVAL (if --pre-deploy-eval set)
   │     - pull image to a temp environment with target's connections
   │     - run the smoke eval (typically 5-10 cases)
   │     - assert score >= --production-floor
   │     - if fails: ABORT; record refusal to audit
   │
   ├── 3. APPLY DEPLOYMENT
   │     - dispatch to the platform helper (kubectl / aws ecs / gcloud run / etc.)
   │     - the helper wraps the platform's native deploy command
   │     - rollout strategy honoured per --rollout-strategy
   │
   ├── 4. WAIT FOR HEALTHY
   │     - poll the platform's deployment status
   │     - wait for readiness probe to succeed on new pods
   │     - timeout per --deploy-timeout (default 600s)
   │
   ├── 5. RECORD
   │     - write deployment metadata to audit (deployment.completed event)
   │     - foundry.deployment counter incremented in observability
   │     - stdout reports: status, image_sha, deploy_duration, rollout_strategy
   │
   └── On failure at any step: ABORT, audit the failure, do NOT roll back
       (operator decides whether to roll back; deployment is idempotent)
```

Exit codes:
- 0 — deployment succeeded.
- 1 — pre-deploy eval failed.
- 2 — platform deployment failed (timeout, platform error).
- 3 — image not found / not accessible.
- 4 — manifest mismatch (image's `system_version` doesn't match expected).

### Platform integration

The `--platform` flag selects a helper. Each helper translates `foundry deploy` semantics to the platform's native commands:

| Platform | Helper translates to |
|---|---|
| `kubectl` | `kubectl set image deployment/<name> <name>=<image>`; waits for rollout |
| `ecs` | `aws ecs update-service --service <name> --task-definition <new-rev>`; waits for stable |
| `cloud-run` | `gcloud run deploy <name> --image <image> --region <region>` |
| `fly` | `fly deploy --image <image>` |
| `nomad` | `nomad job run <jobspec-with-new-image>` |
| `noop` | Records the deployment in audit + observability but doesn't actually apply (for dry-run + custom platforms) |

Operators on platforms not directly supported use `--platform noop` + their own deploy step:

```bash
foundry deploy pipeline_recon --image ... --platform noop --pre-deploy-eval ...
# Pre-deploy eval runs; deployment recorded in audit; nothing actually applied
my-custom-deploy-script.sh foundry-pipeline-recon:cb861da9abcd1234
# Operator's script does the actual rollout
foundry deploy pipeline_recon --image ... --platform noop --post-deploy-record completed
# Records the completion in audit
```

## Sample manifests (per platform)

The institution writes these per their conventions; foundry doesn't generate them. Reference shapes:

### Kubernetes

```yaml
# deploy/k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pipeline-recon
  labels:
    app: pipeline-recon
    foundry.project: pipeline_recon
spec:
  replicas: 4
  selector:
    matchLabels:
      app: pipeline-recon
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: pipeline-recon
        foundry.project: pipeline_recon
    spec:
      serviceAccountName: pipeline-recon  # for vault / IAM-federated identity
      containers:
        - name: pipeline-recon
          image: foundry-pipeline-recon:cb861da9abcd1234   # placeholder; CI replaces
          ports:
            - containerPort: 8080
          env:
            - name: FOUNDRY_ENV
              value: prod
            - name: FOUNDRY_CHECKPOINTER
              valueFrom: { secretKeyRef: { name: foundry-prod, key: checkpointer-url } }
            - name: FOUNDRY_RATE_LIMITER
              valueFrom: { secretKeyRef: { name: foundry-prod, key: redis-url } }
            - name: FOUNDRY_AUDIT_STORE
              valueFrom: { secretKeyRef: { name: foundry-prod, key: audit-url } }
            - name: FOUNDRY_TRACING
              value: otel
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: http://otel-collector.observability:4317
            - name: FOUNDRY_SECRETS_PROVIDER
              value: vault
            - name: VAULT_ADDR
              value: https://vault.internal:8200
            - name: FOUNDRY_MAX_CONCURRENT_RUNS
              value: "100"
          resources:
            requests:
              cpu: "1"
              memory: 1Gi
            limits:
              cpu: "2"
              memory: 2Gi
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            periodSeconds: 10
            timeoutSeconds: 5
          readinessProbe:
            httpGet:
              path: /health?deep=true
              port: 8080
            periodSeconds: 30
            timeoutSeconds: 15
            failureThreshold: 3
          # Allow time for graceful drain (per 71-async-runtime § Graceful shutdown)
          terminationGracePeriodSeconds: 150
---
apiVersion: v1
kind: Service
metadata:
  name: pipeline-recon
spec:
  selector:
    app: pipeline-recon
  ports:
    - port: 80
      targetPort: 8080
```

### AWS ECS Fargate

```json
{
  "family": "pipeline-recon",
  "containerDefinitions": [{
    "name": "pipeline-recon",
    "image": "<account>.dkr.ecr.<region>.amazonaws.com/foundry-pipeline-recon:cb861da9abcd1234",
    "essential": true,
    "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
    "environment": [
      {"name": "FOUNDRY_ENV", "value": "prod"},
      {"name": "FOUNDRY_TRACING", "value": "otel"},
      {"name": "FOUNDRY_MAX_CONCURRENT_RUNS", "value": "100"}
    ],
    "secrets": [
      {"name": "FOUNDRY_CHECKPOINTER", "valueFrom": "arn:aws:secretsmanager:..."},
      {"name": "FOUNDRY_RATE_LIMITER", "valueFrom": "arn:aws:secretsmanager:..."}
    ],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"],
      "interval": 30,
      "timeout": 10,
      "retries": 3,
      "startPeriod": 30
    }
  }],
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "1024",
  "memory": "2048",
  "taskRoleArn": "arn:aws:iam::<account>:role/pipeline-recon-task-role",
  "executionRoleArn": "arn:aws:iam::<account>:role/ecs-execution-role"
}
```

### Google Cloud Run

```yaml
# cloud-run.yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: pipeline-recon
  annotations:
    run.googleapis.com/launch-stage: GA
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "1"
        autoscaling.knative.dev/maxScale: "20"
        run.googleapis.com/cpu-throttling: "false"
    spec:
      serviceAccountName: pipeline-recon@<project>.iam.gserviceaccount.com
      timeoutSeconds: 600
      containers:
        - image: gcr.io/<project>/foundry-pipeline-recon:cb861da9abcd1234
          ports:
            - containerPort: 8080
          env:
            - name: FOUNDRY_ENV
              value: prod
            - name: FOUNDRY_CHECKPOINTER
              valueFrom:
                secretKeyRef:
                  name: foundry-prod
                  key: checkpointer-url
          startupProbe:
            httpGet:
              path: /health?deep=true
              port: 8080
            failureThreshold: 30
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            periodSeconds: 10
          resources:
            limits:
              cpu: "2"
              memory: 2Gi
```

### Azure Container Apps

```yaml
# azure-containerapp.yaml (Bicep / ARM-friendly shape)
properties:
  managedEnvironmentId: <env-id>
  configuration:
    ingress:
      external: true
      targetPort: 8080
    secrets:
      - name: foundry-checkpointer
        keyVaultUrl: https://<vault>.vault.azure.net/secrets/checkpointer-url
        identity: <managed-identity-id>
  template:
    containers:
      - name: pipeline-recon
        image: <registry>.azurecr.io/foundry-pipeline-recon:cb861da9abcd1234
        env:
          - name: FOUNDRY_ENV
            value: prod
          - name: FOUNDRY_CHECKPOINTER
            secretRef: foundry-checkpointer
        probes:
          - type: liveness
            httpGet:
              path: /health
              port: 8080
          - type: readiness
            httpGet:
              path: /health?deep=true
              port: 8080
        resources:
          cpu: 1.0
          memory: 2Gi
    scale:
      minReplicas: 2
      maxReplicas: 10
      rules:
        - name: http-scaling
          http:
            metadata:
              concurrentRequests: "100"
```

### Fly.io

```toml
# fly.toml
app = "foundry-pipeline-recon"
primary_region = "lhr"

[build]
  dockerfile = "Dockerfile"

[env]
  FOUNDRY_ENV = "prod"
  FOUNDRY_HOST = "0.0.0.0"
  FOUNDRY_PORT = "8080"
  FOUNDRY_TRACING = "otel"
  FOUNDRY_MAX_CONCURRENT_RUNS = "100"

# Secrets via `fly secrets set FOUNDRY_CHECKPOINTER=... FOUNDRY_RATE_LIMITER=...`

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = false
  auto_start_machines = true
  min_machines_running = 2
  processes = ["app"]

[[http_service.checks]]
  type = "http"
  interval = "10s"
  timeout = "5s"
  grace_period = "30s"
  method = "GET"
  path = "/health"

[[http_service.checks]]
  type = "http"
  interval = "30s"
  timeout = "15s"
  grace_period = "60s"
  method = "GET"
  path = "/health?deep=true"

[[vm]]
  cpu_kind = "shared"
  cpus = 2
  memory_mb = 2048
```

### HashiCorp Nomad

```hcl
# pipeline-recon.nomad
job "pipeline-recon" {
  datacenters = ["dc1"]
  type        = "service"
  
  group "app" {
    count = 4
    
    network {
      port "http" {
        to = 8080
      }
    }
    
    service {
      name = "pipeline-recon"
      port = "http"
      
      check {
        type     = "http"
        path     = "/health"
        interval = "10s"
        timeout  = "5s"
      }
      
      check {
        type     = "http"
        path     = "/health?deep=true"
        interval = "30s"
        timeout  = "15s"
        check_restart {
          limit = 3
          grace = "60s"
        }
      }
    }
    
    task "pipeline-recon" {
      driver = "docker"
      
      config {
        image = "registry.internal/foundry-pipeline-recon:cb861da9abcd1234"
        ports = ["http"]
      }
      
      env {
        FOUNDRY_ENV = "prod"
        FOUNDRY_TRACING = "otel"
        OTEL_EXPORTER_OTLP_ENDPOINT = "http://otel-collector:4317"
      }
      
      template {
        data = <<EOH
{{ with secret "secret/foundry/prod" }}
FOUNDRY_CHECKPOINTER={{ .Data.checkpointer }}
FOUNDRY_RATE_LIMITER={{ .Data.redis }}
{{ end }}
EOH
        destination = "secrets/foundry.env"
        env         = true
      }
      
      resources {
        cpu    = 1000
        memory = 2048
      }
    }
  }
}
```

## Multi-environment workflows

Standard environment progression:

```
dev → staging → prod

Local dev:
  foundry run pipeline_recon ...           # against dev connections
  foundry test projects/pipeline_recon
  foundry eval pipeline_recon evals/q1.yaml

CI on PR:
  → pre-merge gates run against test fixtures

CI on merge to main:
  → build image foundry-pipeline-recon:<system_version>
  → push to registry
  → deploy to staging:
      foundry deploy pipeline_recon \
        --image foundry-pipeline-recon:<system_version> \
        --target staging \
        --pre-deploy-eval projects/pipeline_recon/evals/staging_smoke.yaml \
        --production-floor 0.85 \
        --rollout-strategy rolling

Manual / scheduled / approved promotion to prod:
  foundry deploy pipeline_recon \
    --image foundry-pipeline-recon:<system_version> \
    --target prod \
    --pre-deploy-eval projects/pipeline_recon/evals/prod_smoke.yaml \
    --production-floor 0.90 \
    --rollout-strategy blue-green
```

Per-environment configuration in `projects/<p>/deploy/<env>.yaml`:

```yaml
# projects/pipeline_recon/deploy/prod.yaml
target: prod
platform: kubectl
namespace: prod
deployment_name: pipeline-recon
expected_replicas: 4
production_floor: 0.90
deploy_timeout_s: 600
rollout_strategy: blue-green
notification_webhook: https://slack.internal/...
```

## Blue/green and canary patterns

Both delegated to platform tooling; foundry's role is providing the inputs.

### Blue/green

Deploy new version alongside old; switch traffic atomically.

| Platform | Mechanism |
|---|---|
| Kubernetes | Two Deployments (blue + green) + a Service that switches `selector` between them; Argo Rollouts / Flagger automates |
| ECS | Two services + a ALB target group switch |
| Cloud Run | Tag-based routing: deploy with `--no-traffic`; promote via `gcloud run services update-traffic` |
| Nomad | Two jobs + a Consul service switch |

`foundry deploy --rollout-strategy blue-green` translates to the platform's native blue/green flow + waits for the cutover to complete.

### Canary

Deploy new version; route a small fraction of traffic; gradually expand if metrics OK.

| Platform | Mechanism |
|---|---|
| Kubernetes | Argo Rollouts / Flagger with metrics-based promotion |
| ECS | ALB weighted target groups + step adjustments |
| Cloud Run | Tag-based traffic split: `gcloud run services update-traffic --to-tags=v2=10` |
| Fly.io | Multiple machine groups with weighted traffic |

For canary, foundry's pre-deploy eval acts as the first quality gate; live observability (latency / error-rate / cost trends) drives the gradual promotion. Foundry doesn't reinvent the canary controller; the platform's tooling does the rollout, foundry surfaces the metrics that gate it.

## Deployment rollback (vs config rollback)

Two layers, independent. From `52-rollback-and-audit.md`:

| Layer | Operation | Mechanism |
|---|---|---|
| **Config rollback** | `foundry rollback pipeline_recon --tool ... --to v2` | Edit `system.yaml` pin + commit; affects what would be deployed next |
| **Deployment rollback** | `kubectl rollout undo deployment/pipeline-recon` (or platform equivalent) | Revert running container to previous image tag |

After deployment rollback (k8s rolled back to `pipeline-recon:abc12345`):

- The previous `system_version` is now serving traffic.
- The current commit on `foundry/pipeline_recon` is still ahead.
- To align: `foundry rollback pipeline_recon --to <commit-of-abc12345>`. Now config + deployed match.

After config rollback (`foundry rollback pipeline_recon --tool x --to v2`):

- A new commit is on the project branch with the rolled-back pin.
- Existing deployments still serve the old (post-rollback-target) version.
- Next CI cycle picks up the rolled-back config; builds a new image; deploys.

The two operations don't need to know about each other. Document this clearly in operator runbooks; conflating them is the most common source of confusion.

### Emergency rollback runbook (sketch)

For institutions to fill in:

```
Production incident: pipeline_recon serving wrong outputs

1. Triage:
   foundry obs failures pipeline_recon --since 1h
   foundry obs trace <bad_run_id>
   
2. Decide: deployment-level or config-level cause?
   - if deployment-level (recent deploy correlation): proceed to step 3
   - if config-level (always was this way; recent forge): proceed to step 4
   
3. Deployment rollback (immediate):
   kubectl rollout undo deployment/pipeline-recon
   # OR aws ecs update-service --service pipeline-recon --task-definition <prior-rev>
   # OR gcloud run services update-traffic --to-revisions=<prior-rev>=100
   
4. Config rollback (within forge cycle):
   foundry versions pipeline_recon
   foundry rollback pipeline_recon --to <last-known-good-commit>
   git push                    # CI picks up; deploys
   
5. Post-incident:
   foundry obs audit pipeline_recon --since 24h    # what happened
   foundry obs forge <related_forge_run_id>        # if a forge introduced it
   # Add the bad case to the eval set so it's caught next time:
   foundry eval capture --project pipeline_recon --case <run_id>
   git commit + push
```

## Compliance + data residency considerations

Per `86-multi-tenancy-and-ip.md` § Data-handling considerations + `83-security-guardrails.md` § Compliance-adjacent considerations.

For deployment specifically:

| Concern | Implementation |
|---|---|
| **Data residency** (EU data must stay in EU) | Deploy region constraints (Cloud Run `--region=europe-west1`, Azure regions, etc.); LLM provider region matched (Bedrock eu-west, Azure OpenAI in EU region) |
| **HIPAA BAA-required providers** | Deploy on Azure or AWS GovCloud; LLM provider Azure OpenAI or Bedrock-Anthropic with BAA; never public Anthropic / OpenAI |
| **MNPI** (financial) | Deploy on-prem or in approved VPC; provider in approved region; observability on-prem (no third-party SaaS) |
| **Sovereign clouds** (gov / regulated) | Deploy on AWS GovCloud / Azure Gov / Google Sovereign Cloud / on-prem K8s |
| **Encryption at rest** | All storage backends configured with encryption (S3 SSE-KMS, Azure SSE, GCS CMEK, Postgres TDE) |
| **Encryption in transit** | TLS 1.2+ everywhere; mTLS where institution policy requires |
| **Network isolation** | Foundry runs in private subnet; only the load balancer is public-facing; all dependencies (Postgres, Redis, providers) in same VPC or via PrivateLink / Private Service Connect |

These are deployment-time decisions; the foundry doesn't enforce, but every primitive in the framework supports them (provider-agnostic, configurable storage, configurable observability).

## Failure modes

| Cause | Surfaced as | Recovery |
|---|---|---|
| Image not found in registry | `foundry deploy` exit 3 | Verify image push completed; retry |
| Pre-deploy eval fails | exit 1; audit records refusal | Investigate eval failure (forge again or fix manually) |
| Platform deploy times out | exit 2; partial state possible | Manual investigation via platform console; potential rollback |
| Manifest version mismatch | exit 4 | CI/operator misalignment; ensure `compute-version` ran on same state as build |
| Health probes failing post-deploy | platform-specific (k8s rollback, ECS service event) | Check `foundry connections health`; verify env vars; check provider credentials |
| Network partition between foundry + checkpointer post-deploy | runs queue in memory until restored; metric alert; eventual graceful shutdown if persistent | Restore network; check Postgres health |

## Invariants

1. **Image tag = `<project>:<system_version>`.** Auditability of "what's running where" is non-negotiable.
2. **Foundry doesn't roll back deployments**; it provides config rollback. Deployment rollback uses platform tooling.
3. **Pre-deploy eval is opt-in but recommended**; production floor configurable per environment.
4. **Platform helpers wrap, don't replace.** Operators retain full access to kubectl/aws/gcloud/etc.
5. **Multi-environment is `target` config + per-target deploy YAML.** No bespoke environments hard-coded.
6. **Deployment metadata always recorded** in audit + observability. No silent rollouts.

## Test expectations

### Unit

1. **`foundry compute-version`** is deterministic across processes for the same project state.
2. **Pre-deploy eval gate**: simulated eval below floor → deploy refused; above → proceeds.
3. **Platform helper translation**: each helper produces the documented platform-native command for known inputs.
4. **Audit recording**: every `foundry deploy` invocation produces a deployment audit entry.

### Contract

1. **Image label provenance**: built image's `docker inspect` shows `foundry.system_version` matching `compute-version` output for the same state.
2. **Pre-deploy eval is real**: a fixture that fails the smoke eval refuses deploy; passes → applies.
3. **No silent deployment**: every `foundry deploy` produces exactly one `deployment.completed` or `deployment.failed` audit entry.

### Integration (Phase 9 exit gate)

1. End-to-end Kubernetes deploy: build image; push; `foundry deploy --platform kubectl`; pods become healthy; traffic flows; observability captures events.
2. End-to-end Cloud Run deploy: same shape, different platform.
3. Pre-deploy eval refusal: contrived bad config → CI's `foundry deploy` fails; image is built but not deployed.
4. Deployment rollback recovery: deploy v2; identify regression in production observability; `kubectl rollout undo`; previous image serving; foundry-side state remains coherent.

## Open questions

1. **Helm chart generation**. Operators on k8s often want a Helm chart; foundry could generate one from `SystemSpec`. Lean: defer; institutions write their own per their conventions; foundry's concern is the image + the gate.
2. **Multi-region active/active deployments**. Foundry runs cleanly per-region (per `86-multi-tenancy-and-ip.md` and `85-batch-and-throughput.md`); cross-region coordination is the institution's infrastructure decision. Lean: defer; document patterns when real demand surfaces.
3. **Operator access control on `foundry deploy`** (4-eyes for production deploys). Lean: yes, additive — `--require-approval` flag that emits an `ApprovalRequired` and waits; Phase 9 polish.
4. **Continuous deployment vs continuous delivery**. Default in this doc: deploys are explicit operator actions. Some institutions want auto-deploy on merge. Lean: support both; operator chooses via CI workflow YAML; foundry doesn't impose.
5. **Disaster recovery patterns** (cross-region failover, backup restore). Defer to institution's DR playbook; foundry's data is in standard cloud-provider backed storage that supports DR via standard tooling.
