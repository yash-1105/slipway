"""FastAPI dependencies.

These reach into `request.app.state`, where main.py put the container. The
router modules therefore import services and domain types only, which is what
keeps api/ a thin adapter (ADR 0002).
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.services.health import HealthService
from app.services.runs import RunService


def get_run_service(request: Request) -> RunService:
    return cast(RunService, request.app.state.container.runs)


def get_health_service(request: Request) -> HealthService:
    return cast(HealthService, request.app.state.container.health)


RunServiceDep = Annotated[RunService, Depends(get_run_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
