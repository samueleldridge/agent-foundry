"""The meta-agent (docs/60): a ``foundry.Agent`` whose tools are the
meta-toolkit and whose job is to configure OTHER agents.

Three load-bearing properties, straight from the spec:

1. **The meta-agent IS a foundry Agent.** It subclasses ``BaseAgent`` and
   executes on the EXISTING single-agent runtime: a synthetic
   ``CompiledProject`` (state = one ``directive`` field; tools = the
   meta-toolkit) runs through ``run_project`` — the same LangGraph
   node-sized slices, checkpointer wiring, and event stream as any
   configured project.
2. **Bounded autonomy.** The sandbox lives in the meta-tool layer; the
   budgets live in :class:`ForgeGuardrails`; the tool allowlist is fixed
   to :func:`foundry.configurator.tools.meta_tool_names`.
3. **Framework-versioned prompt.** ``prompts/v<N>.md`` ships with the
   framework; ``ACTIVE_PROMPT_VERSION`` pins the live one. The meta-agent's
   ``version`` content-hashes (model binding, prompt, toolkit) so two
   operators on different models are visibly not comparable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.config import EnvSecretsProvider
from foundry.config.schemas import (
    AgentSpec,
    FieldSpec,
    ObservabilityConfig,
    OutputSchemaRef,
    PromptRef,
    SingleFlow,
    StateSpec,
    StateVisibility,
    SystemSpec,
)
from foundry.config.secrets import SecretsProvider
from foundry.configurator.tools import (
    MetaToolContext,
    build_meta_tool_registry,
    meta_tool_names,
)
from foundry.core.agent import BaseAgent
from foundry.core.errors import ConfigValidationError
from foundry.core.session import Session
from foundry.core.tool import RetryPolicy
from foundry.orchestration.state_scope import compile_state
from foundry.providers import ModelBinding, ModelSettings, resolve
from foundry.runtime.compiled import CompiledProject
from foundry.versioning.git_backend import GitBackend

if TYPE_CHECKING:
    import httpx

    from foundry.runtime.execution import EventSink

ACTIVE_PROMPT_VERSION = "v1"
"""The framework-pinned meta-agent prompt (docs/60 § Versioning the
meta-agent itself). Bumping this is a framework release."""

_PROMPTS_DIR = Path(__file__).parent / "prompts"

DEFAULT_META_MODEL_BINDING = ModelBinding(
    provider="openai",
    model="gpt-5-mini",
    settings=ModelSettings(temperature=0.1, max_tokens=16384),
)
"""docs/60 § Recommended model binding: current default reasoning model,
temperature 0.1 (low, not zero — dropped on the wire for reasoning models,
which accept only default sampling params), 16384 tokens per turn.

The 16384 budget is deliberate: gpt-5-mini is a reasoning model, so the
adapter sends ``max_completion_tokens``, which pays for hidden reasoning
AND visible output from one pot — a 4096 budget starves the visible
completion."""


def forge_max_iter_default() -> int:
    """The global default for ``--max-iter`` / the forge route's
    ``max_iter`` when the caller omits it: ``FOUNDRY_FORGE_MAX_ITER``,
    else 5 (docs/60 § Safety guards). Explicit-but-invalid config is an
    error, never silently ignored."""
    import os

    raw = os.environ.get("FOUNDRY_FORGE_MAX_ITER", "").strip()
    if not raw:
        return 5
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigValidationError(
            f"FOUNDRY_FORGE_MAX_ITER must be an integer, got {raw!r}",
            context={"FOUNDRY_FORGE_MAX_ITER": raw},
            cause=exc,
        ) from exc
    if not 1 <= value <= 100:
        raise ConfigValidationError(
            f"FOUNDRY_FORGE_MAX_ITER must be within 1..100 (the "
            f"ForgeGuardrails bounds), got {value}",
            context={"FOUNDRY_FORGE_MAX_ITER": raw},
        )
    return value


class ForgeGuardrails(BaseModel):
    """The budgets the forge loop cannot exceed (docs/60 § Safety guards)."""

    model_config = ConfigDict(extra="forbid")

    max_iter: int = Field(default=5, ge=1, le=100)
    """Improvement iterations after bootstrap."""
    max_cost_usd: Decimal | None = None
    max_wall_time_s: float = Field(default=7200.0, gt=0)
    no_improvement_after: int = Field(default=3, ge=1, le=50)
    """Consecutive non-improving iterations before plateau termination."""
    tool_rounds_per_iteration: int = Field(default=60, ge=4, le=500)
    """The meta-agent's LLM-round cap per directive (its AgentSpec
    iteration_limit) — one directive is one bounded tool-use loop."""


ChangeKind = Literal[
    "bootstrap",
    "prompt_edit",
    "tool_binding_change",
    "flow_change",
    "state_visibility_change",
    "new_tool_scaffold",
    "agent_split",
    "rollback",
    "none",
]


class MetaAgentReport(BaseModel):
    """The meta-agent's structured answer to ONE forge directive.

    Self-report only — the session loop trusts the RECORDED tool activity
    (commits, eval runs) for scores and shas; this report supplies the
    reasoning trail (hypothesis, cluster, notes for the next iteration).
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["bootstrap_complete", "iteration_complete", "stuck"]
    summary: str
    change_kind: ChangeKind | None = None
    artifact: str | None = None
    cluster_id: str | None = None
    hypothesis: str | None = None
    applied: bool = True
    rolled_back: bool = False
    notes: str | None = None
    """Carried into the next iteration's directive verbatim."""


def compute_meta_agent_version(
    model_binding: ModelBinding, prompt_text: str
) -> str:
    """Content hash over (model binding, prompt, toolkit) — docs/60."""
    digest = hashlib.sha256()
    digest.update(model_binding.provider.encode())
    digest.update(model_binding.model.encode())
    digest.update(prompt_text.encode())
    digest.update(",".join(meta_tool_names()).encode())
    return f"{ACTIVE_PROMPT_VERSION}+{digest.hexdigest()[:12]}"


def _catalog_summary(mctx: MetaToolContext) -> str:
    from foundry.catalog.loader import catalog_entries

    lines: list[str] = []
    for entry in catalog_entries(mctx.roots()):
        if not entry.versions:
            continue
        lines.append(
            f"- catalog/{entry.name} ({entry.kind}; versions: "
            f"{', '.join(entry.versions)}; latest: {entry.versions[-1]})"
        )
    return "\n".join(lines) or "(catalog is empty)"


def render_prompt(mctx: MetaToolContext, prompt_text: str) -> str:
    replacements = {
        "{{scoped_project}}": mctx.scoped_project,
        "{{framework_root}}": str(mctx.framework_root),
        "{{catalog_roots}}": ", ".join(str(r) for r in mctx.catalog_roots),
        "{{projects_root}}": str(mctx.projects_root),
        "{{CATALOG_INDEX_SUMMARY}}": _catalog_summary(mctx),
    }
    for placeholder, value in replacements.items():
        prompt_text = prompt_text.replace(placeholder, value)
    return prompt_text


class MetaAgent(BaseAgent):
    """The configurator. Construct once per project; ``bind()`` attaches a
    forge run (run id + tool context + compiled runtime); the session loop
    drives ``BoundMetaAgent.step`` once per iteration."""

    def __init__(
        self,
        scoped_project: str,
        *,
        projects_root: Path,
        model_binding: ModelBinding | None = None,
        guardrails: ForgeGuardrails | None = None,
        framework_root: Path | None = None,
        catalog_roots: list[Path] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        secrets: SecretsProvider | None = None,
    ) -> None:
        self.scoped_project = scoped_project
        self.projects_root = projects_root.resolve()
        self.model_binding = model_binding or DEFAULT_META_MODEL_BINDING
        self.guardrails = guardrails or ForgeGuardrails()
        self.framework_root = (
            framework_root or Path(__file__).resolve().parents[1]
        )
        if catalog_roots is None:
            from foundry.config.refs import FoundryRoots

            catalog_roots = list(
                FoundryRoots.for_project(
                    self.projects_root / scoped_project
                ).catalog_roots
            )
        self.catalog_roots = [root.resolve() for root in catalog_roots]
        self.transport = transport
        self.secrets = secrets or EnvSecretsProvider()
        self.prompt_source = (
            _PROMPTS_DIR / f"{ACTIVE_PROMPT_VERSION}.md"
        ).read_text()
        super().__init__(
            name="meta_agent",
            version=compute_meta_agent_version(
                self.model_binding, self.prompt_source
            ),
        )

    def bind(self, forge_run_id: str, backend: GitBackend) -> BoundMetaAgent:
        """Attach a forge run: meta-tool context + compiled runtime."""
        mctx = MetaToolContext(
            scoped_project=self.scoped_project,
            projects_root=self.projects_root,
            framework_root=self.framework_root,
            catalog_roots=tuple(self.catalog_roots),
            backend=backend,
            forge_run_id=forge_run_id,
            transport=self.transport,
            secrets=self.secrets,
            git_email=backend.user_email(),
        )
        return BoundMetaAgent(
            agent=self,
            context=mctx,
            compiled=self._build_compiled(mctx),
        )

    def _build_compiled(self, mctx: MetaToolContext) -> CompiledProject:
        from foundry.config.loader import LoadedAgent, LoadedProject

        registry = build_meta_tool_registry(mctx)
        tool_names = meta_tool_names()
        system = SystemSpec(
            name=self.scoped_project,
            description=(
                f"meta-agent forge session over project "
                f"{self.scoped_project!r}"
            ),
            agents=["meta_agent"],
            flow=SingleFlow(agent="meta_agent"),
            observability=ObservabilityConfig(),
        )
        state = StateSpec.model_validate(
            {
                "schema": {
                    "directive": FieldSpec(
                        type="str", description="The forge directive."
                    ),
                    "summary": FieldSpec(
                        type="str",
                        default="",
                        description="The meta-agent's report summary.",
                    ),
                },
                "visibility": {
                    "meta_agent": StateVisibility(
                        read=["directive"], write=["summary"]
                    )
                },
            }
        )
        spec = AgentSpec(
            name="meta_agent",
            description="The agent-foundry configurator.",
            model_binding=self.model_binding,
            prompt=PromptRef(
                version=ACTIVE_PROMPT_VERSION,
                path=f"prompts/{ACTIVE_PROMPT_VERSION}.md",
            ),
            output=OutputSchemaRef.model_validate(
                {"schema": "meta_agent.py::MetaAgentReport"}
            ),
            tools=tool_names,
            state_visibility=StateVisibility(
                read=["directive"], write=["summary"]
            ),
            iteration_limit=self.guardrails.tool_rounds_per_iteration,
        )
        loaded_agent = LoadedAgent(
            spec=spec,
            directory=Path(__file__).parent,
            prompt_text=render_prompt(mctx, self.prompt_source),
        )
        project = LoadedProject(
            directory=mctx.project_dir,
            system=system,
            state=state,
            agents={"meta_agent": loaded_agent},
        )
        provider = resolve(
            self.model_binding,
            self.secrets,
            retry_policy=RetryPolicy(),
            transport=self.transport,
        )
        return CompiledProject(
            project=project,
            agent_name="meta_agent",
            agent=loaded_agent,
            output_model=MetaAgentReport,
            provider=provider,
            pin_set_hash=self.version,
            system_version=self.version,
            roots=mctx.roots(),
            compiled_state=compile_state(
                state, ["meta_agent"], where="<meta-agent state>"
            ),
            tool_registry=registry,
            transport=self.transport,
            secrets=self.secrets,
        )

    async def forge(
        self,
        *,
        description: str,
        eval_spec_path: Path,
        threshold: float = 0.9,
        event_sink: EventSink | None = None,
    ) -> Any:
        """Library-mode entry (docs/62 § Library mode). Returns a
        ``ForgeResult``; typed as Any here to avoid a circular import —
        the session module owns the result shapes."""
        from foundry.configurator.session import ForgeSession

        session = ForgeSession(
            meta_agent=self,
            description=description,
            eval_spec_path=eval_spec_path,
            threshold=threshold,
            event_sink=event_sink,
        )
        return await session.run()


@dataclass
class BoundMetaAgent:
    """A MetaAgent attached to one forge run."""

    agent: MetaAgent
    context: MetaToolContext
    compiled: CompiledProject

    async def step(
        self,
        directive: str,
        session: Session,
        event_sink: EventSink | None = None,
        *,
        start_sequence: int = 0,
    ) -> MetaAgentReport:
        """One forge directive → one bounded LLM ⇄ meta-tool loop on the
        existing single-agent runtime → one structured report."""
        from foundry.runtime.langgraph_adapter import run_project

        result = await run_project(
            self.compiled,
            {"directive": directive},
            session,
            event_sink,
            checkpointer="memory",
            start_sequence=start_sequence,
        )
        return MetaAgentReport.model_validate(result.output)


__all__ = [
    "ACTIVE_PROMPT_VERSION",
    "DEFAULT_META_MODEL_BINDING",
    "BoundMetaAgent",
    "ForgeGuardrails",
    "MetaAgent",
    "MetaAgentReport",
    "compute_meta_agent_version",
    "forge_max_iter_default",
    "render_prompt",
]
