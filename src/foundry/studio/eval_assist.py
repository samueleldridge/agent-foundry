"""AI-assisted eval authoring (docs/72 § Eval assistant).

A lightweight two-step LLM flow — clarifying questions, then a complete
``EvalSpec``-shaped YAML draft — running through the provider abstraction
(``ModelBinding`` + structured output), NOT through the meta-agent:
docs/41 makes the eval set the forge's untouchable objective, so the
optimizer must never author its own target. This assistant is a separate
pre-forge surface, and its output reaches disk ONLY through the human-
gated config-write route (review → explicit save → commit). Neither
route here writes a single byte into the project tree.

Observability: each request runs on its own :class:`Session` and emits
``run.started`` / ``llm.started`` / ``llm.completed`` / ``run.completed``
through the standard dispatcher, so assistant spend lands in the SQLite
mirror beside every other LLM call (project-attributed cost rows).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import yaml as yaml_lib
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from foundry.config.schemas import EvalSpec
from foundry.core import (
    FoundryMessage,
    LLMCallCompleted,
    LLMCallStarted,
    MessageRole,
    ModelResponse,
    RunCompleted,
    RunFailed,
    RunStarted,
    Session,
    TextBlock,
)
from foundry.core.errors import (
    ConfigValidationError,
    FoundryError,
    ProjectUnavailableError,
    ProviderAuthError,
    ProviderError,
)
from foundry.providers import (
    ModelBinding,
    ModelSettings,
    ProviderAdapter,
    ResponseFormat,
    resolve,
)
from foundry.runtime.execution import EventEmitter
from foundry.studio.context import StudioContext
from foundry.studio.events import emit_studio_event
from foundry.studio.schemas import (
    EvalAssistCase,
    EvalAssistDraftRequest,
    EvalAssistDraftResponse,
    EvalAssistQuestion,
    EvalAssistQuestionsRequest,
    EvalAssistQuestionsResponse,
    ValidationIssue,
    ValidationResult,
)

DEFAULT_ASSIST_MODEL = "openai/gpt-5-mini"
"""Same default as the meta-agent binding (docs/60 § model choice)."""

_ASSIST_MAX_TOKENS = 16384

_QUESTIONS_SYSTEM = """\
You help an AI engineer author an eval set for an LLM agent project.
Evals are the project's behavioural contract: concrete input cases with
expected outputs, scored deterministically. Before drafting anything,
ask the SMALLEST set of clarifying questions (3 to 5) whose answers you
genuinely need. Cover, as applicable:

- what the agent is required to do (the behaviour under test),
- the input shape (field names and example values),
- the expected output shape (field names and example values),
- edge cases and failure modes worth pinning down,
- how cases should balance across categories / difficulty.

Each question gets a short snake_case id, a one-sentence "why", and —
when you can make a sensible guess — a suggested_answer the human can
accept as-is. Respond ONLY with a JSON object of the shape:
{"questions": [{"id": str, "question": str, "why": str,
"suggested_answer": str | null}]}"""

_DRAFT_SYSTEM = """\
You draft a COMPLETE eval set for an agent-foundry project as YAML
matching the EvalSpec schema exactly:

name: <snake_case name>
description: <what a correct output looks like>
scope: project
target: {project}
cases:            # exactly {case_count} cases
  - id: <unique snake_case id>
    input: {{...}}       # object matching the input shape
    expected: {{...}}    # the output the HUMAN will verify and own
    tags: [<category>]
scorers:          # exact and/or numeric ONLY; weights sum to 1.0
  - kind: exact
    name: <name>
    config: {{ field: <output field> }}   # optional: case_sensitive, strip
  # numeric example:
  # - kind: numeric
  #   name: <name>
  #   config: {{ field: <output field>, op: eq,
  #              target_field: <expected field>, abs_tolerance: 0.01 }}
threshold: 0.9
deterministic: true
seed: 42
schema_version: 1

Hard rules:
- NEVER use llm_judge, rubric, or user scorers. Deterministic exact /
  numeric scorers only — the human may opt into judges later by editing
  the saved file.
- scope MUST be "project" and target MUST be "{project}".
- Case ids unique and stable; spread cases across typical inputs, edge
  cases, and each category the answers mention.
- expected values are DRAFTS for the human to review — make them
  concrete, never placeholders like TODO.

Respond ONLY with a JSON object of the shape:
{{"yaml": "<the full YAML document>", "notes": ["<caveats the human
should check, one per note>"]}}"""

_REPAIR_USER = """\
Your previous draft failed validation against the real EvalSpec loader.
Fix EVERY issue and return the corrected draft in the same JSON shape
({{"yaml": ..., "notes": [...]}}). Do not change anything unrelated.

Validation issues:
{issues}

Previous draft:
```yaml
{previous}
```"""


class _QuestionsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    questions: list[EvalAssistQuestion] = Field(min_length=1)


class _DraftPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    yaml: str = Field(min_length=1)
    notes: list[str] = Field(default_factory=list)


def _parse_binding(model: str | None) -> ModelBinding:
    raw = (model or DEFAULT_ASSIST_MODEL).strip()
    if "/" not in raw:
        raise ConfigValidationError(
            f"model must be '<provider>/<model>', got {raw!r}",
            context={"model": raw},
        )
    provider, model_name = raw.split("/", 1)
    return ModelBinding(
        provider=provider,
        model=model_name,
        settings=ModelSettings(
            temperature=0.2, max_tokens=_ASSIST_MAX_TOKENS
        ),
    )


def _missing_key_env(provider: str) -> str:
    """The provider's default credentials env var, for the 424 envelope."""
    from foundry.studio.providers import PROVIDERS

    for entry in PROVIDERS:
        if entry.name == provider and entry.credentials_env:
            return entry.credentials_env
    return ""


@dataclass
class _AssistCall:
    """One HTTP request's LLM plumbing: a run-shaped event bracket so the
    call(s) mirror into the observability store with project attribution."""

    project: str
    provider: ProviderAdapter
    binding: ModelBinding
    session: Session
    emitter: EventEmitter
    agent_name: str
    started: float = field(default_factory=time.monotonic)
    total_in: int = 0
    total_out: int = 0
    total_cost: Decimal | None = None

    @classmethod
    def open(
        cls,
        ctx: StudioContext,
        project: str,
        model: str | None,
        *,
        agent_name: str,
        inputs_digest: str,
    ) -> _AssistCall:
        from foundry.config.secrets import EnvSecretsProvider

        binding = _parse_binding(model)
        try:
            provider = resolve(
                binding, EnvSecretsProvider(), transport=ctx.transport
            )
        except ProviderAuthError as exc:
            env_var = _missing_key_env(binding.provider) or str(
                exc.context.get("env_var", "")
            )
            raise ProjectUnavailableError(
                f"eval assistant unavailable: no API key configured for "
                f"provider {binding.provider!r}",
                project=project,
                env_vars=[env_var] if env_var else [],
                remedy=(
                    "add a key in the studio Providers panel (or set "
                    f"{env_var or 'the provider key env var'} in the "
                    "backend environment), or pick a different model"
                ),
                cause=exc,
            ) from exc
        session = Session.new(project=project)
        emitter = EventEmitter(session, None)
        call = cls(
            project=project,
            provider=provider,
            binding=binding,
            session=session,
            emitter=emitter,
            agent_name=agent_name,
        )
        emitter.emit(
            RunStarted,
            project=project,
            system_version="",
            pin_set_hash="",
            inputs_hash=inputs_digest,
        )
        return call

    @property
    def model_ref(self) -> str:
        return f"{self.binding.provider}/{self.binding.model}"

    async def ask(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> str:
        settings = self.binding.settings.model_copy(
            update={
                "response_format": ResponseFormat.model_validate(
                    {"type": "json_schema", "schema": schema}
                )
            }
        )
        messages = [
            FoundryMessage(
                role=MessageRole.SYSTEM, content=[TextBlock(text=system)]
            ),
            FoundryMessage(
                role=MessageRole.USER, content=[TextBlock(text=user)]
            ),
        ]
        self.emitter.emit(
            LLMCallStarted,
            agent_name=self.agent_name,
            provider=self.provider.name,
            model=self.provider.model,
        )
        response = await self.provider.generate(
            messages, [], settings, self.session
        )
        self.emitter.emit(
            LLMCallCompleted,
            agent_name=self.agent_name,
            usage=response.usage,
            cost_estimate_usd=response.cost_estimate_usd,
            latency_ms=response.latency_ms,
            stop_reason=response.stop_reason,
        )
        self.total_in += response.usage.input_tokens
        self.total_out += response.usage.output_tokens
        if response.cost_estimate_usd is not None:
            self.total_cost = (
                self.total_cost or Decimal("0")
            ) + response.cost_estimate_usd
        return _response_text(response)

    def finish(self) -> None:
        self.emitter.emit(
            RunCompleted,
            status="success",
            total_input_tokens=self.total_in,
            total_output_tokens=self.total_out,
            total_cost_estimate_usd=self.total_cost,
            duration_ms=int((time.monotonic() - self.started) * 1000),
        )

    def fail(self, exc: FoundryError) -> None:
        self.emitter.emit(RunFailed, error=exc.to_dict())


def _response_text(response: ModelResponse) -> str:
    text = "".join(
        block.text
        for block in response.message.content
        if isinstance(block, TextBlock)
    ).strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


# --- draft validation ------------------------------------------------------------------


_GENERATED_SCORER_KINDS = frozenset({"exact", "numeric"})


def _guardrail_issues(
    project: str, draft_yaml: str
) -> tuple[list[ValidationIssue], EvalSpec | None]:
    """Assistant-specific constraints ON TOP of the loader: scope/target
    pinned to the project, and no non-deterministic scorers in a machine-
    generated draft (the human opts into judges by editing the saved
    file — docs/72 § Eval assistant)."""
    try:
        data = yaml_lib.safe_load(draft_yaml)
        spec = EvalSpec.model_validate(data)
    except Exception:  # the loader pass already reported the real issues
        return [], None
    issues: list[ValidationIssue] = []
    if spec.scope != "project":
        issues.append(
            ValidationIssue(
                severity="error",
                message=(
                    f"assistant drafts must use scope: project (got "
                    f"{spec.scope!r})"
                ),
                pointer="/scope",
            )
        )
    if spec.target != project:
        issues.append(
            ValidationIssue(
                severity="error",
                message=(
                    f"assistant drafts must target the project "
                    f"{project!r} (got {spec.target!r})"
                ),
                pointer="/target",
            )
        )
    for index, scorer in enumerate(spec.scorers):
        if scorer.kind not in _GENERATED_SCORER_KINDS:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=(
                        f"generated drafts may only use exact/numeric "
                        f"scorers — scorer {scorer.name!r} is "
                        f"{scorer.kind!r}. The human can opt into it by "
                        "editing the saved eval set."
                    ),
                    pointer=f"/scorers/{index}/kind",
                )
            )
    if not spec.deterministic:
        issues.append(
            ValidationIssue(
                severity="warning",
                message=(
                    "draft sets deterministic: false — assistant drafts "
                    "default to deterministic: true with a seed"
                ),
                pointer="/deterministic",
            )
        )
    return issues, spec


def _validate_draft(
    ctx: StudioContext, project: str, draft_yaml: str
) -> tuple[ValidationResult, EvalSpec | None]:
    """Run the draft through the REAL EvalSpec loader (the same shadow-
    copy validation the config editor uses) + the assistant guardrails.
    Nothing touches the project tree."""
    from foundry.studio.configs import validate_content

    project_dir = ctx.project_dir(project, allow_bootstrap=True)
    result = validate_content(
        project_dir, f"evals/{project}_draft.yaml", draft_yaml
    )
    guardrails, spec = _guardrail_issues(project, draft_yaml)
    issues = [*result.issues, *guardrails]
    ok = result.ok and not any(i.severity == "error" for i in guardrails)
    return ValidationResult(ok=ok, issues=issues, kind="eval"), spec


def _case_rows(spec: EvalSpec | None, draft_yaml: str) -> list[EvalAssistCase]:
    """The review table's rows, with best-effort jump-to-line targets
    (first line whose ``id:`` matches the case id)."""
    if spec is None:
        return []
    lines = draft_yaml.splitlines()
    rows: list[EvalAssistCase] = []
    for case in spec.cases:
        line_no: int | None = None
        pattern = re.compile(
            rf"""^\s*-?\s*id:\s*["']?{re.escape(case.id)}["']?\s*$"""
        )
        for index, line in enumerate(lines, start=1):
            if pattern.match(line):
                line_no = index
                break
        rows.append(
            EvalAssistCase(
                id=case.id,
                input=dict(case.input),
                expected=case.expected,
                line=line_no,
            )
        )
    return rows


def _parse_payload[PayloadT: BaseModel](
    text: str, payload_cls: type[PayloadT], what: str
) -> PayloadT:
    try:
        return payload_cls.model_validate_json(text)
    except ValidationError as exc:
        raise ProviderError(
            f"eval assistant returned malformed {what} (expected the "
            f"structured JSON shape): {text[:200]!r}",
            context={"what": what},
            cause=exc,
        ) from exc


def _answers_block(body: EvalAssistDraftRequest) -> str:
    if not body.answers:
        return "(the operator skipped the clarifying questions)"
    return "\n".join(
        f"- {answer.id}: {answer.answer}" for answer in body.answers
    )


def _suggested_path(ctx: StudioContext, project: str) -> str:
    """Where the reviewed draft should be SAVED (by the human, through
    the config-write route): the starter-eval convention path."""
    _ = ctx
    return f"evals/{project}.yaml"


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/evals/assist/questions",
        response_model=EvalAssistQuestionsResponse,
    )
    async def assist_questions(
        body: EvalAssistQuestionsRequest, request: Request
    ) -> EvalAssistQuestionsResponse:
        ctx.project_dir(body.project, allow_bootstrap=True)  # 404 first
        call = _AssistCall.open(
            ctx,
            body.project,
            body.model,
            agent_name="eval_assist:questions",
            inputs_digest=_digest(body.project, body.description),
        )
        user = (
            f"Project: {body.project}\n"
            f"What the operator wants the agent to do:\n{body.description}"
        )
        try:
            text = await call.ask(
                _QUESTIONS_SYSTEM,
                user,
                _QuestionsPayload.model_json_schema(),
            )
            payload = _parse_payload(text, _QuestionsPayload, "questions")
        except FoundryError as exc:
            call.fail(exc)
            raise
        call.finish()
        emit_studio_event(
            "studio.eval_assist_questions",
            project=body.project,
            studio_request_id=getattr(
                request.state, "studio_request_id", ""
            ),
            model=call.model_ref,
            question_count=len(payload.questions),
        )
        return EvalAssistQuestionsResponse(
            project=body.project,
            model=call.model_ref,
            questions=payload.questions[:5],
        )

    @router.post(
        "/evals/assist/draft", response_model=EvalAssistDraftResponse
    )
    async def assist_draft(
        body: EvalAssistDraftRequest, request: Request
    ) -> EvalAssistDraftResponse:
        ctx.project_dir(body.project, allow_bootstrap=True)  # 404 first
        call = _AssistCall.open(
            ctx,
            body.project,
            body.model,
            agent_name="eval_assist:draft",
            inputs_digest=_digest(
                body.project,
                body.description,
                json.dumps(
                    [a.model_dump() for a in body.answers], sort_keys=True
                ),
            ),
        )
        system = _DRAFT_SYSTEM.format(
            project=body.project, case_count=body.case_count
        )
        user = (
            f"Project: {body.project}\n"
            f"What the agent must do:\n{body.description}\n\n"
            f"Clarifying answers:\n{_answers_block(body)}\n\n"
            f"Draft exactly {body.case_count} cases."
        )
        notes: list[str] = []
        try:
            text = await call.ask(
                system, user, _DraftPayload.model_json_schema()
            )
            payload = _parse_payload(text, _DraftPayload, "draft")
            draft_yaml = payload.yaml
            notes.extend(payload.notes)
            validation, spec = _validate_draft(ctx, body.project, draft_yaml)

            if not validation.ok:
                # ONE automatic repair round-trip: feed the loader's own
                # issues back to the model, then re-validate.
                issue_text = "\n".join(
                    f"- {issue.message}" for issue in validation.issues
                )
                repair_text = await call.ask(
                    system,
                    _REPAIR_USER.format(
                        issues=issue_text, previous=draft_yaml
                    ),
                    _DraftPayload.model_json_schema(),
                )
                repaired = _parse_payload(
                    repair_text, _DraftPayload, "repaired draft"
                )
                draft_yaml = repaired.yaml
                notes = [*repaired.notes]
                validation, spec = _validate_draft(
                    ctx, body.project, draft_yaml
                )
                if validation.ok:
                    notes.append(
                        "First draft failed validation; automatically "
                        "repaired once and now validates."
                    )
                else:
                    notes.append(
                        "Draft still fails validation after one automatic "
                        "repair — fix the remaining issues in the review "
                        "editor before saving."
                    )
        except FoundryError as exc:
            call.fail(exc)
            raise
        call.finish()
        emit_studio_event(
            "studio.eval_assist_draft",
            project=body.project,
            studio_request_id=getattr(
                request.state, "studio_request_id", ""
            ),
            model=call.model_ref,
            valid=validation.ok,
            case_count=len(spec.cases) if spec is not None else 0,
        )
        return EvalAssistDraftResponse(
            project=body.project,
            model=call.model_ref,
            yaml=draft_yaml,
            validation=validation,
            cases=_case_rows(spec, draft_yaml),
            suggested_path=_suggested_path(ctx, body.project),
            notes=notes,
        )

    return router


__all__ = ["DEFAULT_ASSIST_MODEL", "build_router"]
