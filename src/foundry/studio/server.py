"""`foundry studio` runner (docs/72 § CLI).

- Production mode (default): serves ``/api/*`` plus the built SPA (or the
  Phase-10a "frontend not built" placeholder page) and opens the browser.
- ``--dev``: serves the API only and prints the Vite proxy workflow.
- Binding a non-loopback host REFUSES to start without a token
  (``--auth-token`` / ``FOUNDRY_STUDIO_TOKEN``) — mirrors the
  NoAuth-refuses-prod rule in docs/70.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

from foundry.core.errors import ConfigValidationError, FoundryError
from foundry.studio.app import create_studio_app, resolve_assets_dir

_DEV_WORKFLOW = """\
[studio --dev] API-only mode. Frontend dev workflow (Phase 10b+; the
frontend lives in its OWN repository, a sibling checkout):
  cd ../agent-foundry-studio && npm run dev
Vite's dev server (port 5173) proxies /api to this studio port
(configured in the frontend repo's vite.config.ts), giving HMR against
the live control plane."""


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_token(auth_token: str | None) -> str | None:
    return auth_token or os.environ.get("FOUNDRY_STUDIO_TOKEN") or None


def execute_studio(
    project_root: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8400,
    dev: bool = False,
    open_browser: bool = True,
    auth_token: str | None = None,
) -> int:
    """The `foundry studio` implementation. Returns the exit code."""
    try:
        token = resolve_token(auth_token)
        if not _is_loopback(host) and token is None:
            raise ConfigValidationError(
                f"refusing to bind non-loopback host {host!r} without a "
                "token — pass --auth-token or set FOUNDRY_STUDIO_TOKEN "
                "(docs/72 § Security posture)",
                context={"host": host},
            )
        repo_root = Path(project_root or Path.cwd()).resolve()
        app = create_studio_app(
            repo_root, auth_token=token, serve_assets=not dev
        )
        if dev:
            print(_DEV_WORKFLOW)
        elif resolve_assets_dir(repo_root) is None:
            print(
                "[studio] no built frontend assets found — serving the "
                "placeholder page. Build them (Phase 10b+) in the "
                "agent-foundry-studio repo (`npm run build`) and point "
                "FOUNDRY_STUDIO_DIST at its dist/ (a sibling checkout "
                "is found automatically)."
            )
        url = f"http://{host}:{port}"
        print(f"[studio] control plane listening at {url}/api")
        if open_browser and not dev:
            import webbrowser

            webbrowser.open(url)

        import uvicorn

        uvicorn.run(app, host=host, port=port, log_level="info")
        return 0
    except FoundryError as exc:
        from foundry.cli._helpers import print_foundry_error

        print_foundry_error(exc)
        return 2


__all__ = ["execute_studio", "resolve_token"]
