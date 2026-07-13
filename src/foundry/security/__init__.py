"""Security surface: path sandbox, prompt-injection boundaries,
content validators (docs/83)."""

from foundry.security.injection import (
    BOUNDARY_TAG,
    TOOL_RESULT_BOUNDARY_NOTE,
    unwrap_tool_output,
    wrap_tool_output,
)
from foundry.security.sandbox import PathSandbox
from foundry.security.validators import (
    ensure_no_secret_leak,
    find_secret_shaped_content,
    validated_json,
)

__all__ = [
    "BOUNDARY_TAG",
    "TOOL_RESULT_BOUNDARY_NOTE",
    "PathSandbox",
    "ensure_no_secret_leak",
    "find_secret_shaped_content",
    "unwrap_tool_output",
    "validated_json",
    "wrap_tool_output",
]
