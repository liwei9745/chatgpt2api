from __future__ import annotations

from fastapi import APIRouter, HTTPException, Header
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.genbox_push_service import GenBoxPushError, genbox_push_service


class GenBoxPushSettingsRequest(BaseModel):
    enabled: bool = False
    base_url: str = ""
    source_id: str = ""
    push_key: str = ""
    clear_push_key: bool = False
    timeout_secs: int = Field(default=20, ge=5, le=120)


class GenBoxPushImageRequest(BaseModel):
    path: str = Field(..., min_length=1)
    created_at: str = ""
    prompt: str = ""
    model: str = ""


def _raise_push_error(exc: Exception) -> None:
    message = str(exc) or "GenBox 推送失败"
    raise HTTPException(status_code=400, detail={"error": message}) from exc


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/genbox-push/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"settings": await run_in_threadpool(genbox_push_service.get_settings)}

    @router.post("/api/genbox-push/settings")
    async def update_settings(body: GenBoxPushSettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            settings = await run_in_threadpool(genbox_push_service.update_settings, body.model_dump())
        except (GenBoxPushError, ValueError) as exc:
            _raise_push_error(exc)
        return {"settings": settings}

    @router.post("/api/genbox-push/probe")
    async def probe(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(genbox_push_service.probe)
        except GenBoxPushError as exc:
            _raise_push_error(exc)
        return {"result": result}

    @router.post("/api/genbox-push/images")
    async def push_image(body: GenBoxPushImageRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(
                genbox_push_service.push_image,
                body.path,
                created_at=body.created_at,
                prompt=body.prompt,
                model=body.model,
            )
        except GenBoxPushError as exc:
            _raise_push_error(exc)
        return {"result": result}

    return router
