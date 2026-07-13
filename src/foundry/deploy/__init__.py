"""foundry.deploy — the deployment surface (docs/84).

The foundry is platform-agnostic: it provides the content-hashed image tag
(``<project>:<system_version>``), the pre-deploy eval gate, thin platform-CLI
wrappers, and the audit record. The actual rollout mechanism belongs to the
operator's platform (kubectl/aws/gcloud/fly/nomad — or ``noop`` + their own
scripts).
"""

from foundry.deploy.compute_version import compute_system_version
from foundry.deploy.deploy_recorder import DeploymentStatus, record_deployment
from foundry.deploy.platforms import (
    DeployTarget,
    PlatformHelper,
    PlatformResult,
    get_platform,
)
from foundry.deploy.pre_deploy_eval import GateResult, run_pre_deploy_gate

__all__ = [
    "DeployTarget",
    "DeploymentStatus",
    "GateResult",
    "PlatformHelper",
    "PlatformResult",
    "compute_system_version",
    "get_platform",
    "record_deployment",
    "run_pre_deploy_gate",
]
