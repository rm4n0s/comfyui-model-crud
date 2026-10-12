"""aiohttp handlers. Thin by design: parse the request, call a service, shape the response.

All filesystem work happens in ``services/`` and runs in a worker thread so the event loop never
waits on a directory walk.
"""

import asyncio
import functools
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from ..services import models
from ..services.config import Config, get_config
from ..services.errors import BadRequest, ModelCrudError
from ..services.version import VERSION

log = logging.getLogger("comfyui-model-crud")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def json_ok(body: dict[str, Any], status: int = 200) -> web.Response:
    # Listings must reflect the disk right now; never let a browser or proxy reuse them.
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


def error_response(exc: ModelCrudError) -> web.Response:
    return json_ok(exc.to_body(), status=exc.status)


def api_handler(handler: Handler) -> Handler:
    """Map service errors to the SPEC §6 error shape; hide everything unexpected."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        try:
            return await handler(request)
        except ModelCrudError as exc:
            return error_response(exc)
        except web.HTTPException:
            raise
        except Exception:
            # Full traceback goes to the log only; the client gets no internals (CLAUDE.md).
            log.exception("Unexpected error in %s %s", request.method, request.path)
            return error_response(ModelCrudError())

    return wrapper


def query_param(request: web.Request, name: str, *, required: bool = False) -> str | None:
    """A query parameter that must appear at most once, so there is no ambiguity about which
    value the server acted on."""
    values = request.query.getall(name, [])
    if len(values) > 1:
        raise BadRequest(f"Query parameter '{name}' must not be repeated.")
    if required and (not values or values[0] == ""):
        raise BadRequest(f"Query parameter '{name}' is required.")
    return values[0] if values else None


def root_index_param(request: web.Request) -> int | None:
    raw = query_param(request, "root_index")
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]{1,6}", raw):
        raise BadRequest("root_index must be a non-negative whole number.")
    return int(raw)


def describe_config(config: Config) -> dict[str, Any]:
    return {
        "version": VERSION,
        "upload_enabled": config.upload_enabled,
        "delete_enabled": config.delete_enabled,
        "max_upload_bytes": config.max_upload_bytes,
        "chunk_size": config.chunk_size,
        "max_parallel": config.max_parallel,
        "allowed_extensions": list(config.allowed_extensions),
    }


def register_routes(routes: web.RouteTableDef) -> None:
    """Add our handlers to ComfyUI's route table (must run before ``add_routes()``)."""

    @routes.get("/model_crud/config")
    @api_handler
    async def get_config_(request: web.Request) -> web.StreamResponse:
        return json_ok(describe_config(get_config()))

    @routes.get("/model_crud/folders")
    @api_handler
    async def get_folders(request: web.Request) -> web.StreamResponse:
        allowed = get_config().allowed_extensions
        folders = await asyncio.to_thread(models.list_folders, allowed)
        return json_ok({"folders": folders})

    @routes.get("/model_crud/models/{folder}")
    @api_handler
    async def get_models(request: web.Request) -> web.StreamResponse:
        folder = request.match_info["folder"]
        allowed = get_config().allowed_extensions
        files = await asyncio.to_thread(models.list_models, folder, allowed)
        return json_ok({"folder": folder, "files": files})

    @routes.delete("/model_crud/models/{folder}")
    @api_handler
    async def delete_model(request: web.Request) -> web.StreamResponse:
        folder = request.match_info["folder"]
        path = query_param(request, "path", required=True)
        root_index = root_index_param(request)
        result = await asyncio.to_thread(
            models.delete_model, folder, path, root_index, get_config()
        )
        return json_ok(result)
