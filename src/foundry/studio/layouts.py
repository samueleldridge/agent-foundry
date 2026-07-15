"""Widget-dashboard layout persistence (docs/72 § Layout persistence).

``GET/PUT /api/layouts`` round-trips ``<FOUNDRY_HOME>/studio/layouts.json``
(``~/.foundry/studio/layouts.json`` by default) — server-side, not
localStorage, so layouts survive browser resets and are trivially
backupable.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from foundry.storage.paths import foundry_home
from foundry.studio.context import StudioContext
from foundry.studio.schemas import LayoutsDocument


def layouts_path() -> Path:
    return foundry_home() / "studio" / "layouts.json"


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/layouts", response_model=LayoutsDocument)
    def get_layouts() -> LayoutsDocument:
        path = layouts_path()
        if not path.is_file():
            return LayoutsDocument()
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return LayoutsDocument()
        if not isinstance(data, dict):
            return LayoutsDocument()
        return LayoutsDocument.model_validate(data)

    @router.put("/layouts", response_model=LayoutsDocument)
    def put_layouts(body: LayoutsDocument) -> LayoutsDocument:
        path = layouts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.model_dump_json(indent=2))
        return body

    return router


__all__ = ["build_router", "layouts_path"]
