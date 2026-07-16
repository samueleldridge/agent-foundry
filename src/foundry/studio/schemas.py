"""Pydantic request/response models for every studio route (docs/72 —
these shapes are NORMATIVE; the 10b frontend generates its types from the
OpenAPI schema these models produce)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- validation (docs/72 § Configs + validation — normative) -------------------------


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    message: str
    """Human-readable; the SAME text the CLI prints."""
    pointer: str | None = None
    """JSON pointer into the doc, e.g. "/model_binding/provider"."""
    line: int | None = None
    """1-based line in the submitted content."""
    column: int | None = None
    hint: str | None = None
    """Levenshtein "did you mean" etc. (docs/12)."""


class ValidationResult(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    kind: str = ""
    """Which schema validated it (system / agent / ...)."""


# --- projects ------------------------------------------------------------------------


class ProjectSummary(BaseModel):
    name: str
    branch: str = ""
    agent_count: int = 0
    tool_count: int = 0
    last_commit: str | None = None
    last_commit_subject: str | None = None
    last_eval_score: float | None = None
    healthy: bool = True
    health_detail: str = ""


class ProjectAgent(BaseModel):
    name: str
    model_binding: str = ""
    prompt_version: str = ""
    tools: list[str] = Field(default_factory=list)
    state_read: list[str] = Field(default_factory=list)
    state_write: list[str] = Field(default_factory=list)


class ProjectUnavailableInfo(BaseModel):
    """Why a project cannot RUN in this studio process (missing runtime
    secrets) — stored state stays browsable; the UI banners this."""

    env_vars: list[str] = Field(default_factory=list)
    remedy: str = ""


class ProjectDetail(BaseModel):
    name: str
    description: str = ""
    flow_pattern: str = ""
    agents: list[ProjectAgent] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    tools: dict[str, str] = Field(default_factory=dict)
    """logical name → pinned ref, e.g. "catalog/http_get_json@v1"."""
    connections: dict[str, str] = Field(default_factory=dict)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    system_version: str = ""
    unavailable: ProjectUnavailableInfo | None = None
    """Set when the project's runtime secrets are missing (HTTP 424 on
    run-shaped routes); None for a runnable project."""


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class ProjectCreateResponse(BaseModel):
    name: str
    branch: str
    project_dir: str


class TaskLaunched(BaseModel):
    task_id: str
    events_url: str = ""


# --- config files ----------------------------------------------------------------------

ConfigKind = Literal[
    "system", "state", "agent", "prompt", "tool", "connection",
    "retriever", "eval", "function", "python", "markdown", "other",
]


class FileEntry(BaseModel):
    path: str
    """Project-relative posix path."""
    kind: ConfigKind
    editable: bool


class FileTree(BaseModel):
    project: str
    files: list[FileEntry] = Field(default_factory=list)


class FileContent(BaseModel):
    path: str
    kind: ConfigKind
    content: str
    content_hash: str
    """sha256 of the content — the PUT If-Match token (docs/72 §
    Concurrent-edit safety)."""
    schema_url: str | None = None
    editable: bool = True


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    base_hash: str | None = None
    """The content_hash the editor loaded; mismatch → 409, never a silent
    overwrite."""


class WriteResult(BaseModel):
    path: str
    commit_sha: str
    commit_message: str


# --- catalog ------------------------------------------------------------------------


class CatalogEntryModel(BaseModel):
    name: str
    kind: str
    versions: list[str] = Field(default_factory=list)
    latest: str | None = None
    root: str = ""


class CatalogVersionModel(BaseModel):
    version: str
    created_at: datetime | None = None
    created_by: str = ""
    eval_score: float | None = None
    eval_run_id: str | None = None
    notes: str | None = None
    deprecated: bool = False
    deprecation_reason: str | None = None
    schema_change: str | None = None


class CatalogArtifactDetail(BaseModel):
    name: str
    kind: str
    versions: list[CatalogVersionModel] = Field(default_factory=list)


class CatalogFile(BaseModel):
    path: str
    content: str


class CatalogFiles(BaseModel):
    ref: str
    version: str
    files: list[CatalogFile] = Field(default_factory=list)


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    """<project>/<kind>/<name>, e.g. hello/tool/word_stats."""
    floor: float = 0.85
    strict_semver: bool = False
    allow_breaking: bool = False
    notes: str = ""
    confirm: bool = False
    """The human gate: the UI's explicit confirm step (--yes semantics)."""


class PromoteResponse(BaseModel):
    catalog_ref: str
    kind: str
    eval_score: float | None = None
    schema_change: str | None = None
    commit_sha: str = ""


class DeprecateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    """Catalog artifact name, e.g. http_get_json (kind inferred) or
    <kind>/<name>."""
    version: str
    reason: str
    confirm: bool = False


class DeprecateResponse(BaseModel):
    ref: str
    version: str
    deprecated: bool
    commit_sha: str = ""


# --- doctor -------------------------------------------------------------------------


class DoctorCheckModel(BaseModel):
    check: str
    status: Literal["ok", "warn", "fail"]
    detail: str
    remedy: str | None = None
    """v1 doctor checks carry the remedy inside `detail`; kept for schema
    stability."""


class DoctorReport(BaseModel):
    checks: list[DoctorCheckModel] = Field(default_factory=list)
    ok: bool = True


# --- observability ---------------------------------------------------------------------


class ObsRows(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


# --- storage ------------------------------------------------------------------------


class StorageStats(BaseModel):
    foundry_home: str
    kinds: list[dict[str, Any]] = Field(default_factory=list)


class GcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "runs"
    older_than: str = "90d"
    dry_run: bool = True
    force: bool = False


class GcReportModel(BaseModel):
    kind: str
    dry_run: bool
    candidates: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    skipped_pinned: list[str] = Field(default_factory=list)
    forced: bool = False


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "runs"
    older_than: str = "90d"
    dry_run: bool = True


class ArchiveReportModel(BaseModel):
    kind: str
    dry_run: bool = False
    archives: list[str] = Field(default_factory=list)
    archived: list[str] = Field(default_factory=list)
    skipped_pinned: list[str] = Field(default_factory=list)


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = "run"
    artifact_id: str
    reason: str | None = None


class PinnedItemModel(BaseModel):
    kind: str
    id: str
    reason: str | None = None
    scope: str = "global"


# --- runs + approvals ---------------------------------------------------------------------


class RunListItem(BaseModel):
    run_id: str
    project: str = ""
    status: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    total_cost_usd: float | None = None
    total_tokens: int = 0
    error_class: str | None = None


class RunArtifactView(BaseModel):
    run_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] | None = None
    outputs: Any = None
    state_transitions: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    event_count: int = 0


class ApprovalItem(BaseModel):
    run_id: str
    project: str
    approval_id: str
    prompt: str = ""
    agent_name: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = None


class ResumeResponse(BaseModel):
    run_id: str
    status: str
    events_url: str = ""


# --- evals -------------------------------------------------------------------------


class EvalLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["project", "agent", "tool"]
    target: str
    """project name (project/agent scope) or tool ref (tool scope)."""
    agent: str | None = None
    eval_set: str | None = None
    """Eval spec path (project-relative or repo-relative); default: the
    project's single evals/*.yaml."""
    fail_under: float | None = None


class EvalRunRow(BaseModel):
    eval_run_id: str
    eval_name: str = ""
    project: str = ""
    target_ref: str = ""
    target_version: str = ""
    score: float = 0.0
    threshold: float = 0.0
    passed: bool = False
    completed_at: str | None = None


class EvalCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    versions: list[str] = Field(default_factory=list)
    project: str | None = None
    pin_sets: list[str] = Field(default_factory=list)
    eval_set: str | None = None


# --- versions / diff / rollback ---------------------------------------------------------


class CommitModel(BaseModel):
    sha: str
    short_sha: str
    author: str = ""
    date: str = ""
    subject: str = ""


class ArtifactVersions(BaseModel):
    name: str
    kind: str
    ref: str = ""
    versions: list[str] = Field(default_factory=list)
    pinned: str = ""
    latest_unpinned: str | None = None


class VersionsResponse(BaseModel):
    project: str
    branch: str = ""
    commits: list[CommitModel] = Field(default_factory=list)
    prompts: list[ArtifactVersions] = Field(default_factory=list)
    tools: list[ArtifactVersions] = Field(default_factory=list)
    connections: list[ArtifactVersions] = Field(default_factory=list)


class FileDiff(BaseModel):
    path: str
    hunks: str
    """The unified-diff body for this file."""


class DiffResponse(BaseModel):
    project: str
    ref1: str
    ref2: str
    files: list[FileDiff] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    prompt: str | None = None
    to: str
    force: bool = False
    dry_run: bool = True
    """Dry-run is the DEFAULT: the UI always previews first (docs/72)."""


class PreflightCheckModel(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    bypass: str = "none"


class RollbackResponse(BaseModel):
    granularity: str
    target: str
    dry_run: bool
    plan: str
    checks: list[PreflightCheckModel] = Field(default_factory=list)
    commit_sha: str | None = None
    audit_entry_id: str | None = None
    overrides_used: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ComputeVersionResponse(BaseModel):
    project: str
    system_version: str


# --- connections ----------------------------------------------------------------------


class ConnectionInfo(BaseModel):
    name: str
    ref: str
    version: str
    auth_scheme: str = ""
    principal: str | None = None
    redacted_config: dict[str, Any] = Field(default_factory=dict)


class HealthCaseModel(BaseModel):
    case_id: str
    ok: bool
    latency_ms: int = 0
    message: str = ""


class ConnectionHealthResponse(BaseModel):
    connection: str
    ref: str
    ok: bool
    checked_at: datetime | None = None
    cases: list[HealthCaseModel] = Field(default_factory=list)


# --- forge -------------------------------------------------------------------------


class ForgeLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str
    description: str
    eval_path: str
    threshold: float = 0.9
    max_iter: int = 5
    max_cost_usd: str | None = None
    model: str | None = None
    no_improvement_after: int = 3


class ForgeLaunchResponse(BaseModel):
    forge_run_id: str
    project: str
    events_url: str = ""


class ForgeRunInfo(BaseModel):
    forge_run_id: str
    project: str = ""
    status: str = ""
    """running / completed / cancelled / failed."""
    threshold: float | None = None
    final_score: float | None = None
    best_score: float | None = None
    iterations: int = 0
    termination_reason: str | None = None
    termination_detail: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    total_cost_usd: str | None = None
    trajectory: list[dict[str, Any]] = Field(default_factory=list)


# --- chat -------------------------------------------------------------------------


class ChatSessionInfo(BaseModel):
    session_id: str
    project: str
    created_at: datetime | None = None
    run_ids: list[str] = Field(default_factory=list)
    multi_turn: bool = False
    """True when the project declares the `turns` read-scope convention;
    otherwise each message is an independent run (single-turn project)."""
    events_url: str = ""


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class ChatMessageResponse(BaseModel):
    session_id: str
    run_id: str
    events_url: str = ""


# --- graph export (docs/72 § Flow-graph visualisation — normative) --------------------


class AgentSummary(BaseModel):
    model_binding: str
    """"anthropic/claude-opus-4-7"."""
    prompt_version: str
    tools: list[str] = Field(default_factory=list)
    """Pinned refs, e.g. "catalog/word_stats@v2"."""
    state_read: list[str] = Field(default_factory=list)
    state_write: list[str] = Field(default_factory=list)


class FunctionSummary(BaseModel):
    version: str
    state_read: list[str] = Field(default_factory=list)
    state_write: list[str] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str
    kind: Literal["agent", "function", "start", "end"]
    role: (
        Literal["single", "supervisor", "worker", "step", "branch", "join"]
        | None
    ) = None
    label: str
    group: str | None = None
    agent: AgentSummary | None = None
    function: FunctionSummary | None = None


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: Literal["sequential", "handoff", "conditional", "parallel", "join"]
    label: str | None = None
    """Predicate source for conditional; None otherwise."""
    bidirectional: bool = False


class GraphExport(BaseModel):
    project: str
    system_version: str
    pattern: str
    primary_agent: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


# --- deploy -------------------------------------------------------------------------


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    target: str = "dev"
    platform: str = "noop"
    pre_deploy_eval: str | None = None
    production_floor: float = 0.9
    dry_run: bool = True
    """Dry-run is the DEFAULT (docs/72 § Deploy)."""
    skip_eval: bool = False
    deployment_name: str | None = None
    namespace: str | None = None
    region: str | None = None
    jobspec: str | None = None


class DeployResponse(BaseModel):
    project: str
    dry_run: bool
    exit_code: int
    report: str = ""


# --- layouts / tasks / health -------------------------------------------------------------


class LayoutsDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    active: str = "default"
    dashboards: dict[str, Any] = Field(default_factory=dict)


class TaskInfo(BaseModel):
    task_id: str
    kind: str
    status: Literal["running", "completed", "failed"]
    created_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class StudioHealth(BaseModel):
    status: str = "ok"
    version: str
    uptime_s: float = 0.0
    active_forge_runs: int = 0
    active_chat_sessions: int = 0
    run_manager_pool: int = 0


__all__ = [
    "AgentSummary",
    "ApprovalItem",
    "ArchiveReportModel",
    "ArchiveRequest",
    "ArtifactVersions",
    "CatalogArtifactDetail",
    "CatalogEntryModel",
    "CatalogFile",
    "CatalogFiles",
    "CatalogVersionModel",
    "ChatMessageRequest",
    "ChatMessageResponse",
    "ChatSessionInfo",
    "CommitModel",
    "ComputeVersionResponse",
    "ConfigKind",
    "ConnectionHealthResponse",
    "ConnectionInfo",
    "DeployRequest",
    "DeployResponse",
    "DeprecateRequest",
    "DeprecateResponse",
    "DiffResponse",
    "DoctorCheckModel",
    "DoctorReport",
    "EvalCompareRequest",
    "EvalLaunchRequest",
    "EvalRunRow",
    "FileContent",
    "FileDiff",
    "FileEntry",
    "FileTree",
    "ForgeLaunchRequest",
    "ForgeLaunchResponse",
    "ForgeRunInfo",
    "FunctionSummary",
    "GcReportModel",
    "GcRequest",
    "GraphEdge",
    "GraphExport",
    "GraphNode",
    "HealthCaseModel",
    "LayoutsDocument",
    "ObsRows",
    "PinRequest",
    "PinnedItemModel",
    "PreflightCheckModel",
    "ProjectAgent",
    "ProjectCreateRequest",
    "ProjectCreateResponse",
    "ProjectDetail",
    "ProjectSummary",
    "ProjectUnavailableInfo",
    "PromoteRequest",
    "PromoteResponse",
    "ResumeRequest",
    "ResumeResponse",
    "RollbackRequest",
    "RollbackResponse",
    "RunArtifactView",
    "RunListItem",
    "StorageStats",
    "StudioHealth",
    "TaskInfo",
    "TaskLaunched",
    "ValidateRequest",
    "ValidationIssue",
    "ValidationResult",
    "VersionsResponse",
    "WriteRequest",
    "WriteResult",
]
