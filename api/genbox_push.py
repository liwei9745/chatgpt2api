from __future__ import annotations

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.support import require_admin, require_cleanup_admin
from services.genbox_push_batch import genbox_push_batch_service
from services.genbox_push_cleanup import genbox_push_cleanup_service
from services.genbox_push_schedule import genbox_push_schedule_service
from services.genbox_push_service import GenBoxPushError, genbox_push_service
from services.genbox_push_transfer import GenBoxPushTransferCoordinator, genbox_push_transfer_coordinator


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


class GenBoxPushBatchRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=200)


class GenBoxPushDateRangeRequest(BaseModel):
    start_date: str = Field(..., min_length=10, max_length=10)
    end_date: str = Field(..., min_length=10, max_length=10)


class GenBoxPushScheduleRequest(BaseModel):
    enabled: bool = False
    weekday: int = Field(default=0, ge=0, le=6)
    time: str = Field(default="09:00", min_length=5, max_length=5)
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)


class GenBoxPushCleanupSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class GenBoxPushCleanupOperationRequest(BaseModel):
    """Intent-only cleanup request; all destructive inputs stay server-side."""

    model_config = ConfigDict(extra="forbid")


def _reject_cleanup_request_authority(request: Request) -> None:
    """Keep cleanup authority out of browser query strings and custom headers."""
    if request.query_params:
        raise HTTPException(status_code=422, detail={"error": "cleanup query parameters are not accepted"})
    if any(name.lower().startswith("x-genbox-") for name in request.headers):
        raise HTTPException(status_code=422, detail={"error": "cleanup custom authority headers are not accepted"})


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

    @router.get("/api/genbox-push/cleanup/settings")
    async def get_cleanup_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"settings": await run_in_threadpool(genbox_push_cleanup_service.settings)}

    @router.post("/api/genbox-push/cleanup/settings")
    async def update_cleanup_settings(
        body: GenBoxPushCleanupSettingsRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        require_cleanup_admin(request, authorization)
        _reject_cleanup_request_authority(request)
        try:
            await run_in_threadpool(
                genbox_push_service.update_settings,
                {"cleanup_enabled": body.enabled},
            )
        except (GenBoxPushError, ValueError) as exc:
            _raise_push_error(exc)
        return {"settings": await run_in_threadpool(genbox_push_cleanup_service.settings)}

    @router.post("/api/genbox-push/cleanup/preview")
    async def preview_cleanup(
        request: Request,
        body: GenBoxPushCleanupOperationRequest | None = None,
        authorization: str | None = Header(default=None),
    ):
        require_cleanup_admin(request, authorization)
        _reject_cleanup_request_authority(request)
        del body
        return {"result": await run_in_threadpool(genbox_push_cleanup_service.preview)}

    @router.post("/api/genbox-push/cleanup/run")
    async def run_cleanup(
        request: Request,
        body: GenBoxPushCleanupOperationRequest | None = None,
        authorization: str | None = Header(default=None),
    ):
        require_cleanup_admin(request, authorization)
        _reject_cleanup_request_authority(request)
        del body
        return {"result": await run_in_threadpool(genbox_push_cleanup_service.execute)}

    @router.post("/api/genbox-push/images")
    async def push_image(body: GenBoxPushImageRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            path = GenBoxPushTransferCoordinator.normalize_relative_path(body.path)
            source_sha256 = await run_in_threadpool(genbox_push_service.source_sha256, path)
            result = await run_in_threadpool(
                genbox_push_transfer_coordinator.push_image,
                genbox_push_service,
                path,
                source_sha256,
                created_at=body.created_at,
                prompt=body.prompt,
                model=body.model,
            )
        except GenBoxPushError as exc:
            _raise_push_error(exc)
        return {"result": result}

    @router.post("/api/genbox-push/batches")
    async def create_batch(body: GenBoxPushBatchRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            batch = await run_in_threadpool(genbox_push_batch_service.create, body.paths)
        except ValueError as exc:
            _raise_push_error(exc)
        return {"batch": batch}

    @router.post("/api/genbox-push/batches/preview-date-range")
    async def preview_batch_date_range(body: GenBoxPushDateRangeRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            preview = await run_in_threadpool(genbox_push_batch_service.preview_date_range, body.start_date, body.end_date)
        except ValueError as exc:
            _raise_push_error(exc)
        return {"preview": preview}

    @router.get("/api/genbox-push/batches/latest-recoverable")
    async def get_latest_recoverable_batch(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"batch": await run_in_threadpool(genbox_push_batch_service.get_latest_recoverable)}

    @router.get("/api/genbox-push/batches/{batch_id}")
    async def get_batch(batch_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        batch = await run_in_threadpool(genbox_push_batch_service.get, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail={"error": "GenBox Push batch not found."})
        return {"batch": batch}

    @router.post("/api/genbox-push/batches/{batch_id}/cancel")
    async def cancel_batch(batch_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        batch = await run_in_threadpool(genbox_push_batch_service.cancel, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail={"error": "GenBox Push batch not found."})
        return {"batch": batch}

    @router.post("/api/genbox-push/batches/{batch_id}/retry-failed")
    async def retry_failed_batch(batch_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        batch = await run_in_threadpool(genbox_push_batch_service.retry_failed, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail={"error": "GenBox Push batch not found."})
        return {"batch": batch}

    @router.get("/api/genbox-push/schedule")
    async def get_schedule(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"schedule": await run_in_threadpool(genbox_push_schedule_service.get_settings)}

    @router.put("/api/genbox-push/schedule")
    async def update_schedule(body: GenBoxPushScheduleRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            schedule = await run_in_threadpool(genbox_push_schedule_service.update_settings, body.model_dump())
        except ValueError as exc:
            _raise_push_error(exc)
        return {"schedule": schedule}

    @router.post("/api/genbox-push/schedule/run-now")
    async def run_schedule_now(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            schedule = await run_in_threadpool(genbox_push_schedule_service.run_now)
        except ValueError as exc:
            _raise_push_error(exc)
        return {"schedule": schedule}

    return router
