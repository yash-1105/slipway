"""Domain errors to HTTP status codes.

The mapping lives here, once, rather than as a try/except in every handler.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.errors import (
    AlreadyDecidedError,
    IllegalTransitionError,
    NotFoundError,
    ResourceExhaustedError,
)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(IllegalTransitionError)
    async def _illegal(request: Request, exc: IllegalTransitionError) -> JSONResponse:
        # 409: the request was well-formed, the run is just not there any more.
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(AlreadyDecidedError)
    async def _decided(request: Request, exc: AlreadyDecidedError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(ResourceExhaustedError)
    async def _exhausted(request: Request, exc: ResourceExhaustedError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": str(exc)})
