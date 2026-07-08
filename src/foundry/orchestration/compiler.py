"""SystemSpec → CompiledSystem (docs/30 § Compile pipeline, docs/31).

The compile entry point: load + validate a project tree, resolve every
pinned artifact, and produce the value object the runtime executes. No
langgraph imports — graph wiring is the runtime adapter's job
(``foundry.runtime.langgraph_adapter``); this module is pure resolution +
validation and is reusable by the eval harness (Phase 4) and the API layer
(Phase 8).

Phase 3 compiles the ``single`` pattern (plus the Phase 2c one-agent
``sequential`` shape). parallel / supervisor / graph / multi-agent
sequential are stubbed in ``foundry.orchestration.patterns`` with
structured Phase 7 compile errors.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import httpx
from pydantic import BaseModel, ValidationError

from foundry.cache import prepare_semantic_cache
from foundry.catalog.loader import load_tool_version
from foundry.config import (
    ArtifactRef,
    EnvSecretsProvider,
    FoundryRoots,
    LoadedFunction,
    LoadedProject,
    StateVisibility,
    ToolSpec,
    load_project,
)
from foundry.config.secrets import SecretsProvider
from foundry.connections import (
    prepare_connections,
    validate_tool_connection_wiring,
)
from foundry.core import (
    RegisteredTool,
    RetryPolicy,
    ToolDescriptor,
    ToolRegistry,
)
from foundry.core.errors import (
    CompileError,
    ProviderConfigError,
    StateVisibilityError,
)
from foundry.memory import prepare_memory
from foundry.orchestration.patterns import (
    plan_flow,
    validate_flow_refs,
    validate_namespace,
)
from foundry.orchestration.state_scope import CompiledState, compile_state
from foundry.providers import resolve
from foundry.retrieval import prepare_retrievers
from foundry.runtime.compiled import (
    CompiledFunction,
    CompiledProject,
    CompileWarning,
    FunctionHandler,
)

_OVERRIDABLE_SETTINGS = ("timeout_s", "retry_policy")

CompiledSystem = CompiledProject
"""docs/31 names the compile product ``CompiledSystem``; Phase 2 shipped it
as ``CompiledProject`` (single-agent shape). One type until Phase 7 grows
the multi-agent registry form."""


def compile_project(
    project_dir: Path,
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledProject:
    """Load + validate a project; resolve provider, tools, connections,
    functions, state, retrievers, caches, memory.

    Every wiring error below is compile-time by construction: unbound slot →
    ConnectionSlotNotBoundError; accepts mismatch → CompileError; visibility
    hole → StateVisibilityError; missing version → RefResolutionError;
    namespace collision / dangling flow ref / memory-scope hole →
    CompileError; memory field misconfiguration → MemoryConfigError.
    """
    secrets = secrets or EnvSecretsProvider()
    project = load_project(project_dir)
    system_file = project.directory / "system.yaml"

    validate_namespace(project, system_file)
    validate_flow_refs(project, system_file)
    plan = plan_flow(project, system_file)
    agent_name, flow_steps = plan.agent_name, plan.steps

    agent = project.agents[agent_name]
    agent_yaml = agent.directory / "agent.yaml"

    output_model = _load_output_schema(agent.directory, agent.spec.output.schema_ref)

    # State: compile + validate visibility (docs/22) for EVERY node — agents
    # and functions share the same structural enforcement. Each node's own
    # YAML declaration must agree with state.yaml's entry.
    node_names = [*project.system.agents, *project.system.functions]
    compiled_state = compile_state(
        project.state,
        node_names,
        where=str(project.directory / project.system.state),
    )
    for loaded_agent in project.agents.values():
        _check_node_state_visibility(
            loaded_agent.spec.name,
            loaded_agent.spec.state_visibility,
            compiled_state,
            loaded_agent.directory / "agent.yaml",
            kind="agent",
        )
    for loaded_function in project.functions.values():
        _check_node_state_visibility(
            loaded_function.spec.name,
            loaded_function.spec.state_visibility,
            compiled_state,
            loaded_function.directory / "function.yaml",
            kind="function node",
        )

    functions = {
        name: _compile_function(name, loaded)
        for name, loaded in project.functions.items()
    }

    roots = FoundryRoots.for_project(project.directory)
    prepared_connections = prepare_connections(
        project.system, roots, secrets, system_file=system_file
    )

    registry = ToolRegistry()
    tool_slots: dict[str, dict[str, Any]] = {}
    uses_tool_cache = False
    for name, binding in project.system.tools.items():
        ref = ArtifactRef.parse(binding.ref, "tool", version=binding.version)
        loaded = load_tool_version(ref, roots)
        wired = validate_tool_connection_wiring(
            name, loaded.spec, binding, prepared_connections,
            system_file=system_file,
        )
        timeout_s, retry_policy = _apply_tool_overrides(
            name, loaded.spec, binding.settings, system_file
        )
        registry.register(
            RegisteredTool(
                descriptor=ToolDescriptor(
                    name=name,
                    ref=binding.ref,
                    version=binding.version,
                    description=loaded.spec.description,
                    tags=loaded.spec.tags,
                    connection_slots=sorted(wired),
                ),
                input_schema=loaded.input_model,
                output_schema=loaded.output_model,
                handler=loaded.handler,
                timeout_s=timeout_s,
                retry_policy=retry_policy,
                auth_error_retry=any(
                    p.refresh.mode == "on_auth_error" for p in wired.values()
                ),
                cacheable=loaded.spec.cacheable,
                cache_ttl_s=loaded.spec.cache_ttl_s,
                cache_scope=loaded.spec.cache_scope,
            )
        )
        tool_slots[name] = wired
        uses_tool_cache = uses_tool_cache or loaded.spec.cacheable

    # Agent allowlists reference logical names from SystemSpec.tools.
    for loaded_agent in project.agents.values():
        unknown_tools = sorted(
            set(loaded_agent.spec.tools) - set(project.system.tools)
        )
        if unknown_tools:
            raise CompileError(
                f"agent {loaded_agent.spec.name!r} allowlists tool(s) not in "
                f"system.yaml's `tools:` block: {', '.join(unknown_tools)} "
                f"(known: {', '.join(sorted(project.system.tools)) or '(none)'})",
                context={
                    "file": str(loaded_agent.directory / "agent.yaml"),
                    "pointer": "/tools",
                    "unknown_tools": unknown_tools,
                    "known_tools": sorted(project.system.tools),
                },
            )

    # Phase 2b compile-time wiring: retriever bindings (slot wiring, config
    # validation, embedder dimension check) + semantic cache preparation.
    # Every failure below is a load-time error — nothing has been called yet.
    prepared_retrievers = prepare_retrievers(
        agent.spec.retrievers,
        roots,
        prepared_connections,
        config_file=agent_yaml,
    )
    prepared_semantic_cache = prepare_semantic_cache(
        agent.spec,
        agent.prompt_text,
        project=project.system.name,
        secrets=secrets,
        transport=transport,
    )

    # An agent configuring BOTH memory and a semantic cache gets the cache
    # BYPASSED at runtime (its key covers the step's initial input, not the
    # evolving memory envelope — a hit could replay a response that ignores
    # state). Make the bypass visible at compile time (Phase 2c deviation 4).
    compile_warnings: list[CompileWarning] = []
    if agent.spec.memory is not None and prepared_semantic_cache is not None:
        compile_warnings.append(
            CompileWarning(
                agent_name=agent_name,
                category="cache.semantic.bypassed_by_memory",
                message=(
                    f"agent {agent_name!r} configures BOTH memory and "
                    "semantic_cache; the semantic cache is bypassed for "
                    "memory-enabled agents (its key covers the step's initial "
                    "input, not the evolving memory envelope). Remove "
                    "`semantic_cache:` from agent.yaml or drop `memory:` to "
                    "silence this warning."
                ),
            )
        )

    # Phase 2c: memory config validation — state-field existence + type
    # (MemoryConfigError), read/write scope + retriever-slot binding
    # (CompileError), consolidator prompt on disk (MemoryConfigError).
    agent_view = compiled_state.agent_views[agent_name]
    prepared_memory = prepare_memory(
        agent.spec,
        agent_dir=agent.directory,
        state_field_types={
            name: field_spec.type
            for name, field_spec in project.state.state_schema.items()
        },
        read_scope=agent_view.read,
        write_scope=agent_view.write,
    )

    try:
        provider = resolve(
            agent.spec.model_binding,
            secrets,
            transport=transport,
        )
    except ProviderConfigError as exc:
        # Preserve the registry's message; append the file + field context the
        # CLI user needs (exit gate: error identifies file and field).
        raise ProviderConfigError(
            f"{exc}\n  file: {agent_yaml}\n  pointer: /model_binding/provider",
            context={
                **exc.context,
                "file": str(agent_yaml),
                "pointer": "/model_binding/provider",
            },
            cause=exc,
        ) from exc

    return CompiledProject(
        project=project,
        agent_name=agent_name,
        agent=agent,
        output_model=output_model,
        provider=provider,
        pin_set_hash=_pin_set_hash(project),
        system_version=_git_sha(project.directory),
        roots=roots,
        compiled_state=compiled_state,
        tool_registry=registry,
        tool_slots=tool_slots,
        prepared_connections=prepared_connections,
        transport=transport,
        secrets=secrets,
        retrievers=prepared_retrievers,
        semantic_cache=prepared_semantic_cache,
        uses_tool_cache=uses_tool_cache,
        functions=functions,
        flow_steps=flow_steps,
        memory=prepared_memory,
        compile_warnings=tuple(compile_warnings),
    )


compile_system = compile_project
"""docs/31 naming: ``foundry.orchestration.compiler.compile_system``."""


def _check_node_state_visibility(
    node_name: str,
    declared: StateVisibility,
    compiled_state: CompiledState,
    config_path: Path,
    *,
    kind: str,
) -> None:
    view = compiled_state.agent_views[node_name]
    if set(declared.read) != set(view.read) or set(declared.write) != set(view.write):
        raise StateVisibilityError(
            f"{kind} {node_name!r} declares state_visibility "
            f"(read: {sorted(declared.read)}, write: {sorted(declared.write)}) "
            "that disagrees with state.yaml's visibility entry "
            f"(read: {sorted(view.read)}, write: {sorted(view.write)}); "
            "the two declarations must match",
            context={
                "file": str(config_path),
                "pointer": "/state_visibility",
                "node_declared": {"read": sorted(declared.read),
                                  "write": sorted(declared.write)},
                "state_yaml_declared": {"read": sorted(view.read),
                                        "write": sorted(view.write)},
            },
        )


def _compile_function(name: str, loaded: LoadedFunction) -> CompiledFunction:
    """Import the function handler + compute the content-hashed node_version
    (function source + config; docs/21 § What function nodes DO have)."""
    handler = _load_function_handler(loaded)
    digest = hashlib.sha256(
        (
            loaded.source_text
            + loaded.spec.model_dump_json()
        ).encode()
    ).hexdigest()[:12]
    return CompiledFunction(
        name=name,
        spec=loaded.spec,
        handler=handler,
        node_version=digest,
        directory=loaded.directory,
    )


def _load_function_handler(loaded: LoadedFunction) -> FunctionHandler:
    """Import 'function.py::callable_name' relative to the function dir and
    enforce the docs/12 signature: async def <name>(state_view, ctx)."""
    ref = loaded.spec.function
    where = str(loaded.directory / "function.yaml")
    if "::" not in ref:
        raise CompileError(
            f"function ref must look like 'function.py::callable_name'; "
            f"got {ref!r}",
            context={"file": where, "pointer": "/function", "received": ref},
        )
    file_part, callable_name = ref.split("::", 1)
    module_path = loaded.directory / file_part
    digest = hashlib.sha256(str(module_path).encode()).hexdigest()[:12]
    module_name = f"_foundry_function_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CompileError(
            f"could not import function module: {module_path}",
            context={"file": where, "ref": ref},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    handler = getattr(module, callable_name, None)
    if handler is None:
        raise CompileError(
            f"{callable_name!r} not found in {module_path}",
            context={"file": where, "ref": ref},
        )
    if not inspect.iscoroutinefunction(handler):
        raise CompileError(
            f"function node handler {ref!r} at {module_path} must be an "
            "async function (`async def <name>(state_view, ctx)`)",
            context={"file": where, "ref": ref},
        )
    params = list(inspect.signature(handler).parameters)
    if tuple(params[:2]) != ("state_view", "ctx") or len(params) != 2:
        raise CompileError(
            f"function node handler {ref!r} has signature "
            f"({', '.join(params)}); expected exactly (state_view, ctx) — "
            "the compiler introspects by name (docs/12 § FunctionNodeSpec)",
            context={"file": where, "ref": ref, "received_params": params},
        )
    return cast(FunctionHandler, handler)


def _apply_tool_overrides(
    name: str,
    spec: ToolSpec,
    settings: dict[str, Any],
    system_file: Path,
) -> tuple[float, RetryPolicy]:
    timeout_s = float(spec.timeout_s)
    retry_policy = spec.retry_policy
    for key, value in settings.items():
        if key not in _OVERRIDABLE_SETTINGS or key not in spec.overridable_settings:
            raise CompileError(
                f"tool {name!r} does not allow overriding setting {key!r} "
                f"(overridable: {', '.join(spec.overridable_settings)})",
                context={
                    "file": str(system_file),
                    "pointer": f"/tools/{name}/settings/{key}",
                    "overridable": spec.overridable_settings,
                },
            )
        if key == "timeout_s":
            timeout_s = float(value)
        elif key == "retry_policy":
            try:
                retry_policy = RetryPolicy.model_validate(value)
            except ValidationError as exc:
                raise CompileError(
                    f"tool {name!r} retry_policy override is invalid: "
                    f"{exc.errors()[0]['msg']}",
                    context={
                        "file": str(system_file),
                        "pointer": f"/tools/{name}/settings/retry_policy",
                    },
                    cause=exc,
                ) from exc
    return timeout_s, retry_policy


def _load_output_schema(agent_dir: Path, ref: str) -> type[BaseModel]:
    """Import 'module.py::ClassName' relative to the agent directory."""
    if "::" not in ref:
        raise CompileError(
            f"output schema ref must look like 'output_schema.py::ClassName'; "
            f"got {ref!r}",
            context={"agent_dir": str(agent_dir), "ref": ref},
        )
    file_part, class_name = ref.split("::", 1)
    module_path = agent_dir / file_part
    if not module_path.exists():
        raise CompileError(
            f"output schema module not found: {module_path}",
            context={"agent_dir": str(agent_dir), "ref": ref},
        )
    digest = hashlib.sha256(str(module_path).encode()).hexdigest()[:12]
    module_name = f"_foundry_output_schema_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CompileError(
            f"could not import output schema module: {module_path}",
            context={"ref": ref},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, class_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise CompileError(
            f"{class_name!r} in {module_path} is missing or not a Pydantic "
            "BaseModel subclass",
            context={"ref": ref, "module": str(module_path)},
        )
    return cls


def _pin_set_hash(project: LoadedProject) -> str:
    payload = json.dumps(
        {
            "system": project.system.model_dump(mode="json", by_alias=True),
            "agents": {
                name: a.spec.model_dump(mode="json", by_alias=True)
                for name, a in sorted(project.agents.items())
            },
            "functions": {
                name: f.spec.model_dump(mode="json", by_alias=True)
                for name, f in sorted(project.functions.items())
            },
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _git_sha(directory: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unversioned"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unversioned"


__all__ = [
    "CompiledSystem",
    "compile_project",
    "compile_system",
]
