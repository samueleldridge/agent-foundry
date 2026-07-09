"""Human-gated promotion of project-local artifacts to the catalog
(docs/50 § Catalog promotion + semver discipline; docs/03 § Phase 5).

``promote_artifact("<project>/<kind>/<name>", ...)`` copies the artifact's
LATEST project-local version to the next contiguous catalog version. Gates,
in order — each refusal is a structured ``CatalogPromotionRefused`` before
any file is written:

1. **Eval floor** (configurable, default 0.85). Tools: the standalone eval
   runs, borrowing the source project's connection bindings when the tool
   requires connections (the Phase 4 seam, closed in Phase 5). Connections:
   the version's ``health.yaml`` must pass against the project's binding.
2. **No overwrite / no duplicate.** The destination is always latest+1 and
   must not exist; promoting content identical to the latest catalog
   version is refused (nothing to promote).
3. **Semver discipline.** Contract movement vs the prior catalog version is
   classified; ``breaking`` warns (and requires confirmation) by default,
   and is BLOCKED under ``strict_semver`` unless ``allow_breaking``.

On success: files copied (version rewritten in the artifact's yaml so the
directory stays self-consistent), ``versions.json`` appended (score,
schema_change, breaking_changes, promoter identity), ``index.yaml`` updated,
ONE commit, and an audit entry in the SOURCE project's log.

Promotion is the ONLY path that creates catalog versions (docs/50
invariant 6); it is never invoked by the meta-agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from foundry.catalog.schemas import VersionMetadata
from foundry.config import FoundryRoots, load_eval_spec, load_system_spec
from foundry.config.secrets import SecretsProvider
from foundry.core.errors import (
    CatalogPromotionRefused,
    ConnectionHealthCheckError,
    FoundryError,
)
from foundry.core.types import RunId
from foundry.observability.logging import run_logger
from foundry.observability.tracing import foundry_span
from foundry.versioning.audit import (
    EvalContext,
    Operator,
    append_audit_entry,
    new_audit_entry,
    resolve_operator,
)
from foundry.versioning.compat import (
    ContractDiff,
    connection_contract_diff,
    tool_contract_diff,
)
from foundry.versioning.git_backend import GitBackend
from foundry.versioning.pins import replace_nested_scalar
from foundry.versioning.refs import check_version_contiguity, latest_version

_KINDS: dict[str, str] = {
    "tool": "tools",
    "tools": "tools",
    "connection": "connections",
    "connections": "connections",
}
_SPEC_FILE = {"tools": "tool.yaml", "connections": "connection.yaml"}
_INDEX_KEY = {"tools": "tools", "connections": "connections"}

ConfirmFn = Callable[[str], bool]


@dataclass(frozen=True)
class PromotionResult:
    catalog_ref: str
    """e.g. ``catalog/word_stats@v3``."""
    kind: str
    source_ref: str
    """e.g. ``hello/tools/word_stats@v2``."""
    eval_score: float | None
    eval_run_id: str | None
    schema_change: str
    breaking_changes: list[str]
    commit_sha: str
    files: list[str] = field(default_factory=list)


def promote_artifact(
    target: str,
    *,
    projects_root: Path,
    catalog_root: Path,
    backend: GitBackend,
    floor: float = 0.85,
    strict_semver: bool = False,
    allow_breaking: bool = False,
    operator: Operator | None = None,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    confirm: ConfirmFn | None = None,
    notes: str = "",
) -> PromotionResult:
    """Promote ``<project>/<kind>/<name>`` to the catalog (human-gated)."""
    project_name, kind_subdir, name = _parse_target(target)
    project_dir = (projects_root / project_name).resolve()
    if not (project_dir / "system.yaml").is_file():
        raise CatalogPromotionRefused(
            f"project {project_name!r} not found at {project_dir} "
            "(no system.yaml)",
            context={"target": target, "project_dir": str(project_dir)},
        )
    source_dir = project_dir / kind_subdir / name
    source_version = latest_version(source_dir)
    if source_version is None:
        raise CatalogPromotionRefused(
            f"no versions of {kind_subdir}/{name} exist under {source_dir}",
            context={"target": target, "source_dir": str(source_dir)},
        )
    source_version_dir = source_dir / source_version

    catalog_artifact_dir = catalog_root / kind_subdir / name
    existing = (
        check_version_contiguity(catalog_artifact_dir)
        if catalog_artifact_dir.is_dir()
        else []
    )
    prior = catalog_artifact_dir / existing[-1] if existing else None
    dest_version = f"v{len(existing) + 1}"
    dest_dir = catalog_artifact_dir / dest_version
    if dest_dir.exists():
        raise CatalogPromotionRefused(
            f"catalog version {dest_dir} already exists; catalog versions "
            "are immutable — never overwritten (docs/50 invariant 1)",
            context={"target": target, "dest": str(dest_dir)},
        )
    if prior is not None and _tree_digest(prior) == _tree_digest(source_version_dir):
        raise CatalogPromotionRefused(
            f"{kind_subdir}/{name}@{source_version} is byte-identical to "
            f"catalog {name}@{existing[-1]}; nothing to promote (re-promoting "
            "would overwrite nothing and duplicate the version)",
            context={
                "target": target,
                "catalog_version": existing[-1],
                "source_version": source_version,
            },
        )

    # --- gate 1: eval / health floor -------------------------------------------
    score: float | None
    eval_run_id: str | None
    if kind_subdir == "tools":
        score, eval_run_id = _tool_eval_score(
            project_dir, name, source_version, source_version_dir,
            secrets=secrets, transport=transport,
        )
        if score < floor:
            raise CatalogPromotionRefused(
                f"standalone eval score {score:.2f} is below the promotion "
                f"floor {floor:.2f}; iterate before promoting "
                f"(eval run {eval_run_id})",
                context={
                    "target": target,
                    "score": score,
                    "floor": floor,
                    "eval_run_id": eval_run_id,
                },
            )
    else:
        score, eval_run_id = _connection_health_score(
            project_dir, name, source_version, transport=transport,
            secrets=secrets,
        )
        if score is not None and score < floor:
            raise CatalogPromotionRefused(
                f"connection health score {score:.2f} is below the "
                f"promotion floor {floor:.2f}; promotion refused",
                context={"target": target, "score": score, "floor": floor},
            )

    # --- gate 3: semver discipline ---------------------------------------------
    if prior is None:
        diff = ContractDiff()
        schema_change = "initial"
    else:
        diff = (
            tool_contract_diff(prior, source_version_dir)
            if kind_subdir == "tools"
            else connection_contract_diff(prior, source_version_dir)
        )
        schema_change = diff.classification
    if diff.breaking:
        warning = (
            f"Schema-breaking change vs catalog/{name}@{existing[-1]}:\n"
            + "\n".join(f"  - {b}" for b in diff.breaking)
            + f"\nThis will produce {dest_version}. Existing pins keep "
            "working; bumping them will require config edits."
        )
        if strict_semver and not allow_breaking:
            raise CatalogPromotionRefused(
                f"{warning}\nBlocked by --strict-semver "
                "(use --allow-breaking to override).",
                context={
                    "target": target,
                    "breaking_changes": diff.breaking,
                },
            )
        if confirm is not None and not confirm(warning):
            raise CatalogPromotionRefused(
                "promotion declined by operator after breaking-change warning",
                context={"target": target, "breaking_changes": diff.breaking},
            )

    # --- apply --------------------------------------------------------------------
    operator = operator or resolve_operator(git_email=backend.user_email())
    op_run_id = str(RunId.new())
    source_ref = f"{project_name}/{kind_subdir}/{name}@{source_version}"
    with foundry_span(
        "foundry.catalog.promote",
        {
            "run_id": op_run_id,
            "source_ref": source_ref,
            "catalog_ref": f"catalog/{name}@{dest_version}",
            "eval_score": -1.0 if score is None else score,
            "schema_change": schema_change,
        },
    ):
        _copy_version(source_version_dir, dest_dir)
        if source_version != dest_version:
            _rewrite_version(
                dest_dir / _SPEC_FILE[kind_subdir], dest_version
            )
        from foundry.versioning.artifacts import append_version_metadata

        append_version_metadata(
            catalog_artifact_dir,
            VersionMetadata(
                version=dest_version,
                created_at=datetime.now(UTC),
                created_by="human",
                eval_score=score,
                eval_run_id=eval_run_id,
                notes=notes,
                schema_change=schema_change,  # type: ignore[arg-type]
                breaking_changes=diff.breaking,
                promoted_by=operator.human_email or operator.human_supervisor,
                source_ref=source_ref,
            ),
        )
        index_path = catalog_root / "index.yaml"
        _add_to_index(index_path, _INDEX_KEY[kind_subdir], name)

        files = [
            *sorted(str(p) for p in dest_dir.rglob("*") if p.is_file()),
            str(catalog_artifact_dir / "versions.json"),
            str(index_path),
        ]
        commit_sha = backend.commit(
            [Path(f) for f in files],
            f"catalog({name}): promote {source_ref} → catalog "
            f"{name}@{dest_version}",
        )
        entry = new_audit_entry(
            type="catalog",
            scope=f"{project_name}/{kind_subdir}/{name}",
            summary=f"promoted {source_ref} → catalog/{name}@{dest_version}",
            operator=operator,
            commit_sha=commit_sha,
            files_affected=[backend.relpath(Path(f)) for f in files],
            eval_context=EvalContext(
                after_score=score, after_run_id=eval_run_id
            ),
            rationale=notes or None,
        ).model_copy(update={"id": op_run_id})
        append_audit_entry(project_dir, entry)
        run_logger(op_run_id).info(
            "catalog.promoted",
            source_ref=source_ref,
            catalog_ref=f"catalog/{name}@{dest_version}",
            eval_score=score,
            schema_change=schema_change,
            commit_sha=commit_sha,
        )
    return PromotionResult(
        catalog_ref=f"catalog/{name}@{dest_version}",
        kind=kind_subdir[:-1],
        source_ref=source_ref,
        eval_score=score,
        eval_run_id=eval_run_id,
        schema_change=schema_change,
        breaking_changes=diff.breaking,
        commit_sha=commit_sha,
        files=[backend.relpath(Path(f)) for f in files],
    )


# --- gates ------------------------------------------------------------------------


def _tool_eval_score(
    project_dir: Path,
    name: str,
    version: str,
    version_dir: Path,
    *,
    secrets: SecretsProvider | None,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[float, str]:
    """Run the tool's standalone eval; connection-requiring tools borrow the
    source project's bindings (structured refusal when it has none)."""
    # Lazy import: keeps `import foundry.catalog` free of the eval stack.
    from foundry.config.loader import load_tool_spec
    from foundry.eval import load_tool_target, run_eval

    spec = load_tool_spec(version_dir / "tool.yaml")
    if spec.standalone_eval is None:
        raise CatalogPromotionRefused(
            f"tool {name!r} declares no standalone eval "
            "(standalone_eval: null); promotion requires one — the eval IS "
            "the promotion gate (docs/50)",
            context={"tool": name, "version_dir": str(version_dir)},
        )
    eval_path = version_dir / spec.standalone_eval
    roots = FoundryRoots.for_project(project_dir)
    try:
        target = load_tool_target(
            f"local/{name}",
            roots,
            version=version,
            connections_from=project_dir,
            secrets=secrets,
        )
        eval_spec = load_eval_spec(eval_path)
        result = asyncio.run(
            run_eval(
                eval_spec,
                target,
                secrets=secrets,
                transport=transport,
                eval_spec_ref=str(eval_path),
            )
        )
    except CatalogPromotionRefused:
        raise
    except FoundryError as exc:
        raise CatalogPromotionRefused(
            f"tool {name!r} standalone eval could not run: "
            f"{type(exc).__name__}: {exc}",
            context={"tool": name, "cause": exc.to_dict()},
            cause=exc,
        ) from exc
    return result.score, str(result.eval_run_id)


def _connection_health_score(
    project_dir: Path,
    name: str,
    version: str,
    *,
    secrets: SecretsProvider | None,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[float, str | None]:
    """The connection's health check IS its promotion gate (docs/03). The
    version under promotion must be bound (any pin) in the source project so
    config + credentials exist to build it."""
    from foundry.config import EnvSecretsProvider
    from foundry.connections import prepare_connection, run_connection_health

    system_file = project_dir / "system.yaml"
    system = load_system_spec(system_file)
    bare_ref = f"local/{name}"
    candidates = [
        (logical, binding)
        for logical, binding in system.connections.items()
        if binding.ref == bare_ref
    ]
    if not candidates:
        raise CatalogPromotionRefused(
            f"connection {name!r} is not bound in {system_file}; bind it "
            "(with credentials) so its health check can gate the promotion",
            context={
                "connection": name,
                "bound_connections": sorted(system.connections),
            },
        )
    logical, binding = candidates[0]
    # Health-check the version being PROMOTED, whatever the project pins.
    binding = binding.model_copy(update={"version": version})
    roots = FoundryRoots.for_project(project_dir)
    try:
        prepared = prepare_connection(
            logical, binding, roots, secrets or EnvSecretsProvider(),
            system_file=system_file,
        )
        report = asyncio.run(
            run_connection_health(
                prepared, project=system.name, http_transport=transport
            )
        )
    except ConnectionHealthCheckError as exc:
        raise CatalogPromotionRefused(
            f"connection {name!r} health check failed; promotion refused: "
            f"{exc}",
            context={"connection": name, "cause": exc.to_dict()},
            cause=exc,
        ) from exc
    except FoundryError as exc:
        raise CatalogPromotionRefused(
            f"connection {name!r} health check could not run: "
            f"{type(exc).__name__}: {exc}",
            context={"connection": name, "cause": exc.to_dict()},
            cause=exc,
        ) from exc
    ok_cases = sum(1 for c in report.cases if c.ok)
    score = ok_cases / len(report.cases) if report.cases else (1.0 if report.ok else 0.0)
    if not report.ok:
        raise CatalogPromotionRefused(
            f"connection {name!r} health check reported not-ok "
            f"({ok_cases}/{len(report.cases)} cases passed); promotion refused",
            context={"connection": name, "report": report.to_dict()},
        )
    return score, None


# --- file plumbing ----------------------------------------------------------------


def _parse_target(target: str) -> tuple[str, str, str]:
    parts = target.strip("/").split("/")
    if len(parts) != 3:
        raise CatalogPromotionRefused(
            f"invalid promotion target {target!r}; expected "
            "<project>/<kind>/<name> (e.g. hello/tool/word_stats)",
            context={"target": target},
        )
    project, kind, name = parts
    kind_subdir = _KINDS.get(kind)
    if kind_subdir is None:
        raise CatalogPromotionRefused(
            f"unsupported promotion kind {kind!r} (expected tool or "
            "connection; retriever promotion is not in v1's surface)",
            context={"target": target, "kind": kind},
        )
    return project, kind_subdir, name


def _tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(directory.rglob("*")):
        if not file.is_file() or "__pycache__" in file.parts:
            continue
        digest.update(file.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_version(source: Path, dest: Path) -> None:
    shutil.copytree(
        source, dest, ignore=shutil.ignore_patterns("__pycache__")
    )


def _rewrite_version(spec_file: Path, new_version: str) -> None:
    """Keep the copied version directory self-consistent: its yaml must
    declare the CATALOG version number (load_tool_version enforces)."""
    text = spec_file.read_text()
    new_text, _old = replace_nested_scalar(
        text, ["version"], new_version, file=spec_file
    )
    spec_file.write_text(new_text)


def _add_to_index(index_path: Path, key: str, name: str) -> bool:
    """Add ``name`` to the index's ``<key>:`` block list (surgical text
    edit; comments preserved). Creates the file/section when missing."""
    from foundry.catalog.loader import load_catalog_index

    if not index_path.exists():
        index_path.write_text(
            "# Catalog index — one per catalog root (docs/12 § CatalogIndex).\n"
            f"schema_version: 1\n{key}:\n  - {name}\n"
        )
        load_catalog_index(index_path.parent)  # validate what we wrote
        return True
    index = load_catalog_index(index_path.parent)
    if name in getattr(index, key):
        return False
    lines = index_path.read_text().splitlines(keepends=True)
    section_idx = None
    for i, line in enumerate(lines):
        if line.rstrip("\n").rstrip() == f"{key}:":
            section_idx = i
            break
    if section_idx is None:
        # section absent → append it at EOF
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}:\n  - {name}\n")
    else:
        insert_at = section_idx + 1
        cursor = section_idx + 1
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if stripped.startswith("-"):
                insert_at = cursor + 1
            elif stripped and not stripped.startswith("#"):
                break
            cursor += 1
        lines.insert(insert_at, f"  - {name}\n")
    index_path.write_text("".join(lines))
    load_catalog_index(index_path.parent)  # validate the edited index
    return True


__all__ = ["PromotionResult", "promote_artifact"]
