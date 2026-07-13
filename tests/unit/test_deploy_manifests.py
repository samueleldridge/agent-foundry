"""Syntax validation for every repo-root deployment artifact (docs/84
§ Sample manifests): the reference manifests must at minimum PARSE, and the
Dockerfile must keep its load-bearing properties (two-stage uv build,
frozen no-dev sync, port 8080, healthcheck, exec-form entrypoint).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"


@pytest.mark.unit
def test_k8s_manifest_is_valid_multi_doc_yaml() -> None:
    docs = list(
        yaml.safe_load_all((DEPLOY / "k8s" / "deployment.yaml").read_text())
    )
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Deployment", "Service"]
    deployment = docs[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].startswith("foundry-hello:")
    env_names = {e["name"] for e in container["env"]}
    assert {"FOUNDRY_TRACING", "OTEL_EXPORTER_OTLP_ENDPOINT"} <= env_names
    secret_refs = [
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in container["env"]
        if "valueFrom" in e
    ]
    assert secret_refs and set(secret_refs) == {"foundry-prod"}
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert (
        container["readinessProbe"]["httpGet"]["path"] == "/health?deep=true"
    )
    template_spec = deployment["spec"]["template"]["spec"]
    assert template_spec["terminationGracePeriodSeconds"] == 150


@pytest.mark.unit
def test_ecs_task_definition_is_valid_json() -> None:
    payload = json.loads(
        (DEPLOY / "ecs" / "task-definition.json").read_text()
    )
    assert payload["family"] == "hello"
    assert payload["requiresCompatibilities"] == ["FARGATE"]
    container = payload["containerDefinitions"][0]
    assert container["portMappings"][0]["containerPort"] == 8080
    assert container["secrets"], "secrets must ride the secrets provider"


@pytest.mark.unit
def test_cloud_run_service_is_valid_yaml() -> None:
    doc = yaml.safe_load((DEPLOY / "cloud-run" / "service.yaml").read_text())
    assert doc["apiVersion"] == "serving.knative.dev/v1"
    assert doc["kind"] == "Service"
    container = doc["spec"]["template"]["spec"]["containers"][0]
    assert container["ports"][0]["containerPort"] == 8080


@pytest.mark.unit
def test_azure_containerapp_is_valid_yaml() -> None:
    doc = yaml.safe_load(
        (DEPLOY / "azure" / "containerapp.yaml").read_text()
    )
    template = doc["properties"]["template"]
    assert template["containers"][0]["name"] == "hello"
    assert doc["properties"]["configuration"]["ingress"]["targetPort"] == 8080


@pytest.mark.unit
def test_fly_toml_parses() -> None:
    doc = tomllib.loads((DEPLOY / "fly" / "fly.toml").read_text())
    assert doc["app"] == "foundry-hello"
    assert doc["http_service"]["internal_port"] == 8080
    check_paths = {c["path"] for c in doc["http_service"]["checks"]}
    assert check_paths == {"/health", "/health?deep=true"}


@pytest.mark.unit
def test_nomad_jobspec_shape() -> None:
    text = (DEPLOY / "nomad" / "hello.nomad").read_text()
    assert 'job "hello"' in text
    assert text.count("{") == text.count("}")
    assert "foundry-hello:" in text


@pytest.mark.unit
def test_dockerfile_load_bearing_properties() -> None:
    lines = (REPO_ROOT / "Dockerfile").read_text().splitlines()
    meaningful = [
        line.strip() for line in lines if line.strip() and not
        line.strip().startswith("#")
    ]
    assert meaningful[0].startswith("FROM")
    text = "\n".join(meaningful)
    assert text.count("FROM ") == 2, "two-stage build"
    assert "uv sync --frozen --no-dev" in text
    assert "EXPOSE 8080" in text
    assert "HEALTHCHECK" in text
    assert "USER 1001" in text
    assert 'ENTRYPOINT ["uv", "run", "foundry"]' in text
    assert "curl" not in text, "python:slim has no curl; probe via stdlib"


@pytest.mark.unit
def test_env_template_documents_every_honoured_var() -> None:
    text = (DEPLOY / "env.template").read_text()
    expected = [
        "FOUNDRY_ENV", "FOUNDRY_SERVE_PROJECT", "FOUNDRY_HOME",
        "FOUNDRY_CATALOG_ROOTS", "FOUNDRY_CHECKPOINTER",
        "FOUNDRY_RATE_LIMITER", "FOUNDRY_RATE_LIMIT_RPS",
        "FOUNDRY_RATE_LIMIT_BURST", "FOUNDRY_MAX_CONCURRENT_RUNS",
        "FOUNDRY_DRAIN_TIMEOUT_S", "FOUNDRY_API_TOKENS",
        "FOUNDRY_CORS_ORIGINS", "FOUNDRY_ROUTE_PREFIX", "FOUNDRY_TRACING",
        "OTEL_EXPORTER_OTLP_ENDPOINT", "FOUNDRY_STORAGE_BACKEND",
        "FOUNDRY_STORAGE_BUCKET", "FOUNDRY_STORAGE_PREFIX",
        "FOUNDRY_STORAGE_ENDPOINT", "FOUNDRY_STORAGE_CONTAINER",
        "LANGSMITH_API_KEY", "LANGFUSE_",
    ]
    for var in expected:
        assert var in text, f"env.template must document {var}"
    assert "SECRETS COME FROM YOUR SECRETS PROVIDER" in text


@pytest.mark.unit
def test_docker_compose_stack_parses_and_wires_the_collector() -> None:
    compose_path = DEPLOY / "docker-compose.otel.yaml"
    compose = yaml.safe_load(compose_path.read_text())
    services = compose["services"]
    assert set(services) == {"foundry-api", "otel-collector"}
    assert services["foundry-api"]["build"]["context"] == ".."
    assert "otel-collector-config.yaml" in compose_path.read_text()
    collector_cfg = yaml.safe_load(
        (DEPLOY / "otel-collector-config.yaml").read_text()
    )
    protocols = collector_cfg["receivers"]["otlp"]["protocols"]
    assert {"grpc", "http"} <= set(protocols)
    pipelines = collector_cfg["service"]["pipelines"]
    assert {"traces", "metrics"} <= set(pipelines)
    assert pipelines["traces"]["exporters"] == ["debug"]
