# agent-foundry container image (docs/84 § Container packaging), adapted for
# THIS repo's layout: framework (src/) + configured projects (projects/) +
# shared catalog (catalog/) in one tree.
#
# Build (tag = <project>:<system_version> — docs/84 invariant 1):
#   VERSION=$(uv run foundry compute-version projects/hello)
#   docker build -t foundry-hello:$VERSION \
#     --build-arg PROJECT=hello \
#     --build-arg COMMIT_SHA=$(git rev-parse --short HEAD) \
#     --build-arg SYSTEM_VERSION=$VERSION .
#
# Serve a different project by overriding CMD (exec-form CMD does NOT
# interpolate ${VARS}; the hello default is hardcoded deliberately):
#   docker run foundry-hello:$VERSION serve projects/team_hello --host 0.0.0.0 --port 8080

FROM python:3.12-slim AS builder

ARG PROJECT=hello
ARG COMMIT_SHA=dev
ARG SYSTEM_VERSION=dev

WORKDIR /app

# uv is the package manager of record (repo invariant).
RUN pip install --no-cache-dir uv

# Dependency layer first for build caching; --no-install-project defers the
# foundry package itself until src/ is present.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# The framework + configured projects + shared catalog.
COPY src/ src/
COPY projects/ projects/
COPY catalog/ catalog/
RUN uv sync --frozen --no-dev

# --- runtime stage: no build tools, non-root ---------------------------------
FROM python:3.12-slim AS runtime

ARG PROJECT=hello
ARG COMMIT_SHA=dev
ARG SYSTEM_VERSION=dev

WORKDIR /app

# uv re-installed in the slim runtime so the entrypoint can `uv run foundry`
# against the baked .venv (copied below).
RUN pip install --no-cache-dir uv

COPY --from=builder /app /app

# Provenance labels — `docker inspect` answers "what's running where".
LABEL foundry.project="${PROJECT}"
LABEL foundry.commit_sha="${COMMIT_SHA}"
LABEL foundry.system_version="${SYSTEM_VERSION}"

# Default env, overridable per environment (see deploy/env.template).
ENV FOUNDRY_ENV=prod
ENV FOUNDRY_HOST=0.0.0.0
ENV FOUNDRY_PORT=8080

# Non-root (docs/84 recommendation).
RUN chown -R 1001:1001 /app
USER 1001

EXPOSE 8080

# python:3.12-slim ships no curl; probe /health with the stdlib instead.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health')"]

# Exec form: no shell, clean signal forwarding for graceful drain (docs/71).
ENTRYPOINT ["uv", "run", "foundry"]
CMD ["serve", "projects/hello", "--host", "0.0.0.0", "--port", "8080"]
