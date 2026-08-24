"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.deps import HealthServiceDep
from app.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(response: Response, health_service: HealthServiceDep) -> HealthResponse:
    result = await health_service.check()
    if not result.database:
        response.status_code = 503
    return HealthResponse(
        status=result.status,
        database=result.database,
        models_catalogue_synced_at=result.models_catalogue_synced_at,
    )
