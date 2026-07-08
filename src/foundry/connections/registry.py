"""Connection preparation: spec discovery, config validation, credentials
resolution, and compile-time slot-wiring validation (docs/23).

"Prepared" means everything short of actually building the client: the
factory is imported, the binding config is validated against the version's
config schema, credentials are resolved, and the descriptor + policies are
computed. The pool builds lazily on first acquire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from foundry.catalog.loader import LoadedConnectionVersion, load_connection_version
from foundry.config import (
    ArtifactRef,
    ConnectionBinding,
    ConnectionSlot,
    FoundryRoots,
    PoolPolicy,
    RefreshPolicy,
    SystemSpec,
    ToolBinding,
    ToolSpec,
    ref_matches_accept,
)
from foundry.config.secrets import SecretsProvider
from foundry.connections.descriptors import build_descriptor, config_hash
from foundry.core.connection import (
    AuthScheme,
    ConnectionDescriptor,
    ResolvedConnectionCredentials,
    SecretValue,
)
from foundry.core.errors import (
    CompileError,
    ConnectionAuthError,
    ConnectionSlotNotBoundError,
)
from foundry.core.types import CredentialsRef

# For schemes whose credentials are a single secret string, the field name
# that string populates. Multi-field schemes require a JSON-object secret.
_PRIMARY_FIELD: dict[AuthScheme, str | None] = {
    AuthScheme.API_KEY: "api_key",
    AuthScheme.BASIC_AUTH: None,
    AuthScheme.OAUTH2_CLIENT_CREDENTIALS: None,
    AuthScheme.OAUTH2_REFRESH_TOKEN: "refresh_token",
    AuthScheme.JWT_BEARER: "private_key",
    AuthScheme.SIGV4: None,
    AuthScheme.MTLS: None,
    AuthScheme.CUSTOM: "secret",
}


def resolve_connection_credentials(
    ref: CredentialsRef | None,
    scheme: AuthScheme,
    secrets: SecretsProvider,
) -> ResolvedConnectionCredentials:
    """CredentialsRef → scheme-typed multi-field credentials.

    The resolved secret string is either the scheme's primary field value
    (api_key, refresh_token, ...) or — for multi-field schemes like
    basic_auth / oauth2_client_credentials / sigv4 — a JSON object mapping
    field names to values. An optional "principal" key is lifted out as
    plain (non-secret) identity metadata.
    """
    resolved = secrets.resolve(ref)
    if resolved.secret is None:
        return ResolvedConnectionCredentials(scheme=scheme, fields={})
    raw = resolved.secret
    principal: str | None = None
    fields: dict[str, SecretValue] = {}
    parsed: object = None
    if raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if key == "principal":
                principal = str(value)
            else:
                fields[str(key)] = SecretValue(str(value))
    else:
        primary = _PRIMARY_FIELD.get(scheme)
        if primary is None:
            raise ConnectionAuthError(
                f"auth scheme {scheme.value!r} needs multiple credential "
                "fields; the resolved secret must be a JSON object "
                '(e.g. \'{"username": "...", "password": "..."}\')',
                context={"scheme": scheme.value},
            )
        fields[primary] = SecretValue(raw)
    return ResolvedConnectionCredentials(
        scheme=scheme, fields=fields, principal=principal
    )


@dataclass(frozen=True)
class PreparedConnection:
    """A binding resolved to everything the pool needs to build lazily."""

    name: str
    """Logical name — the SystemSpec.connections key tool slots bind to."""
    ref: ArtifactRef
    loaded: LoadedConnectionVersion
    config: BaseModel
    config_hash: str
    credentials: ResolvedConnectionCredentials
    descriptor: ConnectionDescriptor
    refresh: RefreshPolicy
    pool_policy: PoolPolicy

    @property
    def canonical_ref(self) -> str:
        return self.ref.to_str()


def prepare_connection(
    name: str,
    binding: ConnectionBinding,
    roots: FoundryRoots,
    secrets: SecretsProvider,
    *,
    system_file: Path | None = None,
) -> PreparedConnection:
    ref = ArtifactRef.parse(binding.ref, "connection", version=binding.version)
    loaded = load_connection_version(ref, roots)
    try:
        config = loaded.config_model.model_validate(binding.config)
    except ValidationError as exc:
        missing = sorted(
            "/".join(str(p) for p in e["loc"])
            for e in exc.errors()
            if e["type"] == "missing"
        )
        unexpected = sorted(
            str(e["loc"][-1]) for e in exc.errors() if e["type"] == "extra_forbidden"
        )
        detail_bits = []
        if missing:
            detail_bits.append(f"missing required fields: {', '.join(missing)}")
        if unexpected:
            detail_bits.append(f"unexpected fields: {', '.join(unexpected)}")
        if not detail_bits:
            first = exc.errors()[0]
            detail_bits.append(
                f"{'/'.join(str(p) for p in first['loc'])}: {first['msg']}"
            )
        raise CompileError(
            f"ConnectionBinding config incompatible with connection version.\n"
            f"  connection: {ref.to_str()}\n"
            + (f"  file: {system_file}\n" if system_file else "")
            + f"  pointer: /connections/{name}/config\n"
            + "".join(f"  {bit}\n" for bit in detail_bits)
            + f"  hint: see {loaded.directory / 'README.md'}.",
            context={
                "connection": ref.to_str(),
                "binding_name": name,
                "missing_fields": missing,
                "unexpected_fields": unexpected,
            },
            cause=exc,
        ) from exc

    credentials = resolve_connection_credentials(
        binding.credentials_ref, loaded.spec.auth_scheme, secrets
    )
    config_dict = config.model_dump(mode="json")
    descriptor = build_descriptor(
        ref=ref.to_str(),
        auth_scheme=loaded.spec.auth_scheme,
        config=config_dict,
        non_sensitive_config_fields=loaded.spec.non_sensitive_config_fields,
        principal=credentials.principal,
    )
    return PreparedConnection(
        name=name,
        ref=ref,
        loaded=loaded,
        config=config,
        config_hash=config_hash(config_dict),
        credentials=credentials,
        descriptor=descriptor,
        refresh=binding.refresh_overrides or loaded.spec.refresh,
        pool_policy=binding.pool_overrides or loaded.spec.pool,
    )


def prepare_connections(
    system: SystemSpec,
    roots: FoundryRoots,
    secrets: SecretsProvider,
    *,
    system_file: Path | None = None,
) -> dict[str, PreparedConnection]:
    return {
        name: prepare_connection(
            name, binding, roots, secrets, system_file=system_file
        )
        for name, binding in system.connections.items()
    }


def validate_connection_slot_wiring(
    *,
    owner_kind: str,
    owner_name: str,
    declared_slots: list[ConnectionSlot],
    connection_bindings: dict[str, str],
    prepared: dict[str, PreparedConnection],
    pointer_prefix: str,
    config_file: Path | None = None,
) -> dict[str, PreparedConnection]:
    """Generic compile-time slot wiring checks (docs/23 § Slot binding) —
    shared by tools (Phase 2a) and retrievers/rerankers (Phase 2b). Returns
    the slot → PreparedConnection map the runtime hands to the accessor."""
    owner = f"{owner_kind} {owner_name!r}"
    declared = {slot.slot: slot for slot in declared_slots}
    file_line = f"  file: {config_file}\n" if config_file else ""

    unknown = sorted(set(connection_bindings) - set(declared))
    if unknown:
        raise CompileError(
            f"{owner} binds unknown connection slot(s): "
            f"{', '.join(unknown)}.\n"
            + file_line
            + f"  pointer: {pointer_prefix}/connection_bindings\n"
            f"  declared slots: {', '.join(sorted(declared)) or '(none)'}",
            context={
                owner_kind.lower(): owner_name,
                "unknown_slots": unknown,
                "declared_slots": sorted(declared),
            },
        )

    wired: dict[str, PreparedConnection] = {}
    for slot_name, slot in declared.items():
        bound_name = connection_bindings.get(slot_name)
        if bound_name is None:
            if slot.optional:
                continue
            raise ConnectionSlotNotBoundError(
                f"{owner} slot {slot_name!r} is not bound.\n"
                + file_line
                + f"  pointer: {pointer_prefix}/connection_bindings\n"
                f"  declared slots: {', '.join(sorted(declared))}\n"
                f"  bound slots: "
                f"{', '.join(sorted(connection_bindings)) or '(none)'}\n"
                f"  hint: Add `connection_bindings: {{{slot_name}: "
                f"<connection_name>}}` and ensure `<connection_name>` appears "
                "in system.yaml's `connections:` block.",
                context={
                    owner_kind.lower(): owner_name,
                    "slot": slot_name,
                    "declared_slots": sorted(declared),
                    "bound_slots": sorted(connection_bindings),
                },
            )
        prepared_conn = prepared.get(bound_name)
        if prepared_conn is None:
            raise CompileError(
                f"{owner} slot {slot_name!r} is bound to "
                f"{bound_name!r}, which is not in system.yaml's "
                f"`connections:` block (known: "
                f"{', '.join(sorted(prepared)) or '(none)'}).\n"
                + file_line
                + f"  pointer: {pointer_prefix}/connection_bindings/{slot_name}",
                context={
                    owner_kind.lower(): owner_name,
                    "slot": slot_name,
                    "bound_name": bound_name,
                    "known_connections": sorted(prepared),
                },
            )
        if not any(
            ref_matches_accept(prepared_conn.ref, accept) for accept in slot.accepts
        ):
            raise CompileError(
                f"{owner} slot {slot_name!r} does not accept the "
                f"bound connection {prepared_conn.canonical_ref!r}.\n"
                + file_line
                + f"  pointer: {pointer_prefix}/connection_bindings/{slot_name}\n"
                f"  accepts: {', '.join(slot.accepts)}\n"
                f"  bound: {bound_name} → {prepared_conn.canonical_ref}",
                context={
                    owner_kind.lower(): owner_name,
                    "slot": slot_name,
                    "accepts": slot.accepts,
                    "rejected_ref": prepared_conn.canonical_ref,
                },
            )
        wired[slot_name] = prepared_conn
    return wired


def validate_tool_connection_wiring(
    tool_name: str,
    spec: ToolSpec,
    binding: ToolBinding,
    prepared: dict[str, PreparedConnection],
    *,
    system_file: Path | None = None,
) -> dict[str, PreparedConnection]:
    """Tool-shaped wrapper over the generic slot wiring (kept for the Phase 2a
    call sites and their exact error text)."""
    return validate_connection_slot_wiring(
        owner_kind="Tool",
        owner_name=tool_name,
        declared_slots=spec.connections_required,
        connection_bindings=binding.connection_bindings,
        prepared=prepared,
        pointer_prefix=f"/tools/{tool_name}",
        config_file=system_file,
    )


__all__ = [
    "PreparedConnection",
    "prepare_connection",
    "prepare_connections",
    "resolve_connection_credentials",
    "validate_connection_slot_wiring",
    "validate_tool_connection_wiring",
]
