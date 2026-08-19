#!/usr/bin/env bash
# Bundle the built Studio frontend into the wheel's packaged assets.
#
# Release step (docs/72 § Packaging): builds the sibling
# agent-foundry-studio checkout (or the path given as $1) with
# `npm run build` and copies its dist/ into src/foundry/studio/_assets/.
# That directory is gitignored; the wheel force-includes it via
# pyproject [tool.hatch.build.targets.wheel] artifacts, so a release
# wheel serves the SPA with zero Node on the install host.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend="${1:-$repo_root/../agent-foundry-studio}"
assets="$repo_root/src/foundry/studio/_assets"

if [[ ! -d "$frontend" ]]; then
    echo "error: frontend checkout not found at $frontend" >&2
    echo "usage: scripts/bundle_studio_assets.sh [path-to-agent-foundry-studio]" >&2
    exit 1
fi

(cd "$frontend" && npm run build)

if [[ ! -f "$frontend/dist/index.html" ]]; then
    echo "error: $frontend/dist holds no build (no index.html)" >&2
    exit 1
fi

rm -rf "$assets"
mkdir -p "$assets"
cp -R "$frontend/dist/." "$assets/"
echo "bundled $(find "$assets" -type f | wc -l | tr -d ' ') files into src/foundry/studio/_assets/"
echo "next: uv build   # the wheel picks _assets/** up automatically"
