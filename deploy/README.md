# deploy/ — reference deployment artifacts

Reference shapes for shipping a foundry project as a container (docs/84).
Each manifest serves `projects/hello`; adapt names/replicas/resources per
project and environment. The repo-root `Dockerfile` builds the image every
manifest references.

## The one invariant

**Image tag = `<project>:<system_version>`.**
`system_version` is the deterministic content hash of the project's config
tree: `uv run foundry compute-version projects/hello` (optionally
`--include-git-sha`). Build, tag, and deploy with that exact string —
"what's running where" must be auditable from the tag alone. CI replaces the
`SYSTEM_VERSION` placeholder in these manifests.

## What each file is for

| File | Used by |
|---|---|
| `../Dockerfile` | `docker build` — two-stage uv build; serves hello on 8080 by default; override via `docker run <image> serve projects/<p> ...` |
| `k8s/deployment.yaml` | `kubectl apply` — Deployment + Service; `foundry deploy --platform kubectl` then swaps the image and waits for rollout |
| `ecs/task-definition.json` | `aws ecs register-task-definition`; `foundry deploy --platform ecs` points the service at the new revision |
| `cloud-run/service.yaml` | `gcloud run services replace`, or `foundry deploy --platform cloud-run` for image-only rollouts |
| `azure/containerapp.yaml` | `az containerapp update --yaml` (no foundry helper; use `--platform noop` + your script) |
| `fly/fly.toml` | `fly deploy`; `foundry deploy --platform fly` wraps `fly deploy --image` |
| `nomad/hello.nomad` | `foundry deploy --platform nomad --jobspec deploy/nomad/hello.nomad` |
| `env.template` | Every FOUNDRY_* env var the runtime honours, with comments. Secrets come from your secrets provider, never this file |
| `docker-compose.otel.yaml` + `otel-collector-config.yaml` | Local end-to-end smoke: API + OTel collector printing spans/metrics |

Pre-deploy quality gate (any platform, opt-in but recommended):
`foundry deploy hello --image foundry-hello:<version> --pre-deploy-eval
projects/hello/evals/greeting.yaml --production-floor 0.9`. A score below the
floor refuses the rollout (exit 1) and records the refusal to audit.

## Config rollback vs deployment rollback

- **Config rollback** (`foundry rollback <project> --to ...`) edits pins and
  commits — it changes what the NEXT build deploys.
- **Deployment rollback** (`kubectl rollout undo`, ECS prior task-def, Cloud
  Run traffic shift) reverts the RUNNING image — the production-incident lever.
- They are independent layers; foundry never rolls back deployments. Full
  semantics + emergency runbook: docs/84 § Deployment rollback.
