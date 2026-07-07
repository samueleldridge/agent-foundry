"""mtls scheme: mutual TLS via client certificate + key (docs/23 § mtls).

Builds an ``ssl.SSLContext`` the connection factory injects into its HTTP/DB
client. Credentials carry ``client_cert`` + ``client_key`` as PEM content
(written to a private temp file for the ssl API) or ``client_cert_path`` +
``client_key_path`` pointing at files on disk.
"""

from __future__ import annotations

import ssl
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from foundry.core.connection import ResolvedConnectionCredentials
from foundry.core.errors import ConnectionConfigError


class MTLSConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ca_bundle_ref: str | None = None
    """Path to the trust bundle; None = system trust store."""


def build_ssl_context(
    config: MTLSConfig, credentials: ResolvedConnectionCredentials
) -> ssl.SSLContext:
    context = ssl.create_default_context(
        cafile=config.ca_bundle_ref if config.ca_bundle_ref else None
    )
    fields = credentials.fields
    if "client_cert_path" in fields and "client_key_path" in fields:
        context.load_cert_chain(
            certfile=fields["client_cert_path"].reveal(),
            keyfile=fields["client_key_path"].reveal(),
        )
        return context
    if "client_cert" in fields and "client_key" in fields:
        # ssl only accepts file paths; stage PEM content in a private tmp dir.
        tmpdir = Path(tempfile.mkdtemp(prefix="foundry-mtls-"))
        cert_path = tmpdir / "client.crt"
        key_path = tmpdir / "client.key"
        cert_path.write_text(fields["client_cert"].reveal())
        key_path.write_text(fields["client_key"].reveal())
        key_path.chmod(0o600)
        cert_path.chmod(0o600)
        try:
            context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        finally:
            key_path.unlink(missing_ok=True)
            cert_path.unlink(missing_ok=True)
            tmpdir.rmdir()
        return context
    raise ConnectionConfigError(
        "mtls credentials must carry client_cert+client_key (PEM content) or "
        f"client_cert_path+client_key_path (present: {sorted(fields)})",
        context={"present_fields": sorted(fields)},
    )


__all__ = ["MTLSConfig", "build_ssl_context"]
