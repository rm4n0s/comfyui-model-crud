"""aiohttp handlers. Thin by design: parse the request, call a service, shape the response.

All filesystem work happens in ``services/`` and runs in a worker thread so the event loop never
waits on a directory walk.
"""

import asyncio
import functools
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from ..services import media, models, uploads
from ..services.config import Config, get_config
from ..services.errors import BadChunk, BadRequest, ModelCrudError, TooLarge
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


MAX_JSON_BYTES = 64 * 1024  # control requests are tiny; never buffer an unbounded body


async def read_json_object(request: web.Request) -> dict[str, Any]:
    """Read a small JSON object body without ``request.read()``/``json()``, with a hard cap."""
    if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
        raise TooLarge("Request body is too large.")
    received = bytearray()
    async for block in request.content.iter_chunked(8192):
        received += block
        if len(received) > MAX_JSON_BYTES:
            raise TooLarge("Request body is too large.")
    if not received:
        return {}
    try:
        body = json.loads(received)
    except (ValueError, RecursionError):
        raise BadRequest("Request body must be valid JSON.") from None
    if not isinstance(body, dict):
        raise BadRequest("Request body must be a JSON object.")
    return body


def required_field(body: dict[str, Any], name: str) -> Any:
    if name not in body:
        raise BadRequest(f"'{name}' is required.")
    return body[name]


def chunk_index(raw: str) -> int:
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise BadChunk("Chunk index must be a non-negative whole number.")
    return int(raw)


def area_of(request: web.Request) -> str | None:
    """``input`` / ``output`` on the media routes, None on the model routes."""
    return request.match_info.get("area")


def for_area(body: dict[str, Any], area: str | None) -> dict[str, Any]:
    """Media responses say ``area`` where model responses say ``folder`` (SPEC §6.7)."""
    if area is not None:
        body = {"area": body.pop("folder"), **body}
    return body


def describe_session(session: uploads.UploadSession) -> dict[str, Any]:
    config = uploads.get_manager().config
    return {
        "upload_id": session.id,
        "folder": session.folder,
        "path": session.relpath,
        "size": session.size,
        "chunk_size": session.chunk_size,
        "total_chunks": session.total_chunks,
        "max_parallel": config.max_parallel,
        "expires_at": uploads.iso_utc(session.expires_at),
    }


def describe_status(session: uploads.UploadSession, *, ranges: bool) -> dict[str, Any]:
    """The 6.4.3 / 6.4.4 shape; the list omits `received_ranges`, which can be long."""
    body: dict[str, Any] = {
        "upload_id": session.id,
        "folder": session.folder,
        "path": session.relpath,
        "size": session.size,
        "chunk_size": session.chunk_size,
        "total_chunks": session.total_chunks,
    }
    if ranges:
        body["received_ranges"] = uploads.to_ranges(session.received)
    body.update(
        received_bytes=session.received_bytes,
        client_fingerprint=session.client_fingerprint,
        expires_at=uploads.iso_utc(session.expires_at),
    )
    return body


async def start_uploads(app: web.Application) -> None:
    await uploads.get_manager().start()


async def stop_uploads(app: web.Application) -> None:
    await uploads.get_manager().stop()


def register_lifecycle(app: web.Application) -> None:
    """Resume persisted uploads before the server accepts requests; stop the sweep on exit."""
    app.on_startup.append(start_uploads)
    app.on_cleanup.append(stop_uploads)


def describe_config(config: Config) -> dict[str, Any]:
    return {
        "version": VERSION,
        "upload_enabled": config.upload_enabled,
        "delete_enabled": config.delete_enabled,
        "max_upload_bytes": config.max_upload_bytes,
        "chunk_size": config.chunk_size,
        "max_parallel": config.max_parallel,
        "allowed_extensions": list(config.allowed_extensions),
        "media_folders": list(config.media_folders),
        "media_extensions": list(config.media_extensions),
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

    # The upload handlers serve both kinds of target: /model_crud/uploads... (model folders, the
    # folder is in the body) and /model_crud/{input|output}/uploads... (the area is in the URL).
    # A session is only reachable through the routes of its own kind.

    @routes.get("/model_crud/{area:input|output}")
    @api_handler
    async def get_media(request: web.Request) -> web.StreamResponse:
        area = request.match_info["area"]
        files = await asyncio.to_thread(media.list_files, area, get_config())
        return json_ok({"area": area, "files": files})

    @routes.delete("/model_crud/{area:input|output}")
    @api_handler
    async def delete_media(request: web.Request) -> web.StreamResponse:
        path = query_param(request, "path", required=True)
        result = await asyncio.to_thread(
            media.delete_file, request.match_info["area"], path, get_config()
        )
        return json_ok(result)

    @routes.post("/model_crud/uploads")
    @routes.post("/model_crud/{area:input|output}/uploads")
    @api_handler
    async def create_upload(request: web.Request) -> web.StreamResponse:
        body = await read_json_object(request)
        area = area_of(request)
        session = await uploads.get_manager().create_session(
            folder=None if area else required_field(body, "folder"),
            filename=required_field(body, "filename"),
            size=required_field(body, "size"),
            subfolder=body.get("subfolder"),
            overwrite=body.get("overwrite", False),
            client_fingerprint=body.get("client_fingerprint"),
            area=area,
        )
        return json_ok(for_area(describe_session(session), area), status=201)

    @routes.put("/model_crud/uploads/{upload_id}/chunks/{index}")
    @routes.put("/model_crud/{area:input|output}/uploads/{upload_id}/chunks/{index}")
    @api_handler
    async def put_chunk(request: web.Request) -> web.StreamResponse:
        # Streamed from request.content: request.read()/post()/multipart() would buffer the
        # whole chunk and are capped by aiohttp's client_max_size (docs/comfyui-notes.md).
        index = chunk_index(request.match_info["index"])
        result = await uploads.get_manager().write_chunk(
            request.match_info["upload_id"],
            index,
            request.content,
            request.content_length,
            request.headers.get("Upload-Chunk-SHA256"),
            area_of(request),
        )
        return json_ok(result)

    @routes.post("/model_crud/uploads/{upload_id}/complete")
    @routes.post("/model_crud/{area:input|output}/uploads/{upload_id}/complete")
    @api_handler
    async def complete_upload(request: web.Request) -> web.StreamResponse:
        body = await read_json_object(request)
        area = area_of(request)
        result = await uploads.get_manager().complete(
            request.match_info["upload_id"], body.get("sha256"), area
        )
        return json_ok(for_area(result, area), status=201)

    @routes.delete("/model_crud/uploads/{upload_id}")
    @routes.delete("/model_crud/{area:input|output}/uploads/{upload_id}")
    @api_handler
    async def cancel_upload(request: web.Request) -> web.StreamResponse:
        result = await uploads.get_manager().cancel(
            request.match_info["upload_id"], area_of(request)
        )
        return json_ok(result)

    @routes.get("/model_crud/uploads")
    @routes.get("/model_crud/{area:input|output}/uploads")
    @api_handler
    async def list_uploads(request: web.Request) -> web.StreamResponse:
        area = area_of(request)
        found = uploads.get_manager().list_sessions(area)
        listing = [for_area(describe_status(s, ranges=False), area) for s in found]
        return json_ok({"uploads": listing})

    @routes.get("/model_crud/uploads/{upload_id}")
    @routes.get("/model_crud/{area:input|output}/uploads/{upload_id}")
    @api_handler
    async def get_upload(request: web.Request) -> web.StreamResponse:
        area = area_of(request)
        session = uploads.get_manager().status(request.match_info["upload_id"], area)
        return json_ok(for_area(describe_status(session, ranges=True), area))
