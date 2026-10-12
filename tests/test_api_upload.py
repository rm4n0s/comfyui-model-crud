import ast
import asyncio
import json
import logging
import random
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import uploads
from comfyui_model_crud.services.config import Config
from comfyui_model_crud.services.uploads import UploadManager

CHUNK = 4096
ROOT = Path(__file__).resolve().parent.parent


def config(**overrides) -> Config:
    values = dict(chunk_size=CHUNK, max_upload_bytes=50 * 1024**2, max_parallel=8)
    values.update(overrides)
    return Config(**values)


@pytest.fixture
async def make_client(folder_paths, prompt_server, comfy_app):
    clients = []

    async def make(cfg: Config | None = None, **app_kwargs) -> TestClient:
        uploads.set_manager(UploadManager(cfg or config()))
        table = type(prompt_server.routes)()
        api_routes.register_routes(table)

        @table.put("/_buffered")  # what the chunk route must NOT do, to prove the cap is real
        async def buffered(request: web.Request) -> web.Response:
            return web.Response(text=str(len(await request.read())))

        client = TestClient(TestServer(comfy_app(table, **app_kwargs)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for created in clients:
        await created.close()
    uploads.set_manager(None)


@pytest.fixture
async def client(make_client):
    return await make_client()


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


def payload(size: int, seed: int = 3) -> bytes:
    return random.Random(seed).randbytes(size)


async def create(client, size, name="m.safetensors", prefix="", **extra):
    body = {"folder": "loras", "filename": name, "size": size, **extra}
    return await client.post(f"{prefix}/model_crud/uploads", json=body)


async def put_chunk(client, upload_id, index, data, prefix=""):
    start = index * CHUNK
    return await client.put(
        f"{prefix}/model_crud/uploads/{upload_id}/chunks/{index}", data=data[start : start + CHUNK]
    )


# --- full flow ------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["", "/api"])
async def test_full_flow_with_parallel_out_of_order_chunks(client, loras, prefix):
    data = payload(10 * CHUNK + 123)

    resp = await create(
        client, len(data), prefix=prefix, subfolder="sdxl", client_fingerprint="f:1:2"
    )
    assert resp.status == 201
    session = await resp.json()
    assert set(session) == {
        "upload_id",
        "folder",
        "path",
        "size",
        "chunk_size",
        "total_chunks",
        "max_parallel",
        "expires_at",
    }
    assert session["path"] == "sdxl/m.safetensors" and session["total_chunks"] == 11
    assert session["chunk_size"] == CHUNK and session["max_parallel"] == 8
    assert session["expires_at"].endswith("Z")

    order = list(range(11))
    random.Random(1).shuffle(order)
    gate = asyncio.Semaphore(session["max_parallel"])  # the server enforces this cap

    async def send(i):
        async with gate:
            return await put_chunk(client, session["upload_id"], i, data, prefix)

    responses = await asyncio.gather(*(send(i) for i in order))
    assert [r.status for r in responses] == [200] * 11
    body = await responses[0].json()
    assert set(body) == {"index", "received_chunks", "received_bytes"}

    done = await client.post(f"{prefix}/model_crud/uploads/{session['upload_id']}/complete")
    assert done.status == 201
    assert await done.json() == {"folder": "loras", "path": "sdxl/m.safetensors", "size": len(data)}
    assert (loras / "sdxl" / "m.safetensors").read_bytes() == data
    assert [p.name for p in (loras / "sdxl").iterdir()] == ["m.safetensors"]

    listing = await (await client.get("/model_crud/models/loras")).json()
    assert [f["path"] for f in listing["files"]] == ["sdxl/m.safetensors"]


async def test_a_chunk_larger_than_client_max_size_is_streamed_not_buffered(make_client, loras):
    # ComfyUI caps buffered bodies at --max-upload-size (100 MB by default). Chunks may be bigger
    # (CHUNK_MB goes up to 256), so they must be read from request.content.
    chunk = 3 * 1024 * 1024
    client = await make_client(config(chunk_size=chunk), client_max_size=1024 * 1024)
    data = payload(2 * chunk)

    refused = await client.put("/_buffered", data=data[:chunk])  # sanity: the cap is enforced
    assert refused.status == 413

    session = await (await create(client, len(data))).json()
    for i in range(2):
        resp = await client.put(
            f"/model_crud/uploads/{session['upload_id']}/chunks/{i}",
            data=data[i * chunk : (i + 1) * chunk],
        )
        assert resp.status == 200, await resp.text()
    done = await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")

    assert done.status == 201
    assert (loras / "m.safetensors").read_bytes() == data


async def test_cancel_removes_everything(client, loras):
    session = await (await create(client, 3 * CHUNK)).json()
    await put_chunk(client, session["upload_id"], 0, payload(3 * CHUNK))

    resp = await client.delete(f"/model_crud/uploads/{session['upload_id']}")

    assert resp.status == 200
    assert await resp.json() == {"upload_id": session["upload_id"], "cancelled": True}
    assert list(loras.iterdir()) == []
    assert (await client.delete(f"/model_crud/uploads/{session['upload_id']}")).status == 404
    assert (await put_chunk(client, session["upload_id"], 1, payload(3 * CHUNK))).status == 404


async def test_incomplete_complete_lists_the_missing_ranges(client, loras):
    data = payload(6 * CHUNK)
    session = await (await create(client, len(data))).json()
    for i in (0, 3, 4):
        await put_chunk(client, session["upload_id"], i, data)

    resp = await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")

    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "incomplete_upload" and body["missing_ranges"] == [[1, 2], [5, 5]]
    assert not (loras / "m.safetensors").exists()


# --- error shapes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"filename": "m.safetensors", "size": 10}, 400, "bad_request"),
        ({"folder": "loras", "size": 10}, 400, "bad_request"),
        ({"folder": "loras", "filename": "m.safetensors"}, 400, "bad_request"),
        ({"folder": "loras", "filename": "m.safetensors", "size": 0}, 400, "bad_request"),
        ({"folder": "loras", "filename": "m.safetensors", "size": "10"}, 400, "bad_request"),
        (
            {"folder": "loras", "filename": "m.safetensors", "size": 10, "overwrite": "y"},
            400,
            "bad_request",
        ),
        ({"folder": "loras", "filename": "", "size": 10}, 400, "invalid_filename"),
        (
            {"folder": "loras", "filename": "m.safetensors", "size": 10, "subfolder": "../x"},
            400,
            "path_outside_root",
        ),
        ({"folder": "nope", "filename": "m.safetensors", "size": 10}, 404, "unknown_folder"),
        ({"folder": "loras", "filename": "m.exe", "size": 10}, 415, "extension_not_allowed"),
        ({"folder": "loras", "filename": "m.safetensors", "size": 10**12}, 413, "too_large"),
    ],
)
async def test_create_errors_use_the_error_shape(client, loras, body, status, code):
    resp = await client.post("/model_crud/uploads", json=body)

    assert resp.status == status
    payload_ = await resp.json()
    assert payload_["error"] == code and isinstance(payload_["message"], str)
    assert list(loras.iterdir()) == []


@pytest.mark.parametrize(
    "raw", [b"", b"not json", b"[1, 2]", b'"str"', b"null", b"\xff\xfe", b"{" * 10000]
)
async def test_create_with_a_bad_body(client, raw):
    resp = await client.post("/model_crud/uploads", data=raw)

    assert resp.status in (400, 413) and (await resp.json())["error"] in (
        "bad_request",
        "too_large",
    )


async def test_oversized_json_bodies_are_refused_without_buffering(client):
    big = b'{"folder": "loras", "pad": "' + b"x" * (api_routes.MAX_JSON_BYTES + 1) + b'"}'

    assert (await client.post("/model_crud/uploads", data=big)).status == 413

    async def endless():  # no Content-Length: chunked transfer
        for _ in range(100):
            yield b"x" * 4096

    assert (await client.post("/model_crud/uploads", data=endless())).status == 413


async def test_more_chunks_in_flight_than_max_parallel_get_429(make_client):
    client = await make_client(config(max_parallel=2))
    session = await (await create(client, 4 * CHUNK)).json()
    data = payload(4 * CHUNK)
    release = asyncio.Event()

    async def held(i):
        piece = data[i * CHUNK : (i + 1) * CHUNK]
        yield piece[:100]  # aiohttp sends the headers together with the first body bytes
        await release.wait()
        yield piece[100:]

    url = f"/model_crud/uploads/{session['upload_id']}/chunks/"
    slow = [
        asyncio.create_task(
            client.put(url + str(i), data=held(i), headers={"Content-Length": str(CHUNK)})
        )
        for i in (0, 1)
    ]
    await asyncio.sleep(0.2)

    refused = await client.put(url + "2", data=data[2 * CHUNK : 3 * CHUNK])
    release.set()
    results = await asyncio.gather(*slow)

    assert refused.status == 429 and (await refused.json())["error"] == "too_many_uploads"
    assert [r.status for r in results] == [200, 200]


async def test_conflicts(client, loras):
    (loras / "exists.safetensors").write_bytes(b"old")
    first = await create(client, 10, name="a.safetensors")
    assert first.status == 201

    again = await create(client, 10, name="a.safetensors")
    existing = await create(client, 10, name="exists.safetensors")

    assert (again.status, (await again.json())["error"]) == (409, "upload_in_progress")
    assert (existing.status, (await existing.json())["error"]) == (409, "file_exists")
    assert (loras / "exists.safetensors").read_bytes() == b"old"


async def test_session_limit_is_429(make_client):
    client = await make_client(config(max_active_uploads=1))
    await create(client, 10, name="a.safetensors")

    resp = await create(client, 10, name="b.safetensors")

    assert resp.status == 429 and (await resp.json())["error"] == "too_many_uploads"


async def test_overwrite_flag_over_http(client, loras):
    (loras / "m.safetensors").write_bytes(b"old")
    data = payload(CHUNK)
    session = await (await create(client, CHUNK, overwrite=True)).json()
    await put_chunk(client, session["upload_id"], 0, data)

    assert (await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")).status == 201
    assert (loras / "m.safetensors").read_bytes() == data


async def test_completion_race_returns_409_and_keeps_the_other_file(client, loras):
    session = await (await create(client, 10)).json()
    await put_chunk(client, session["upload_id"], 0, payload(10))
    (loras / "m.safetensors").write_bytes(b"appeared meanwhile")

    resp = await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")

    assert resp.status == 409 and (await resp.json())["error"] == "file_exists"
    assert (loras / "m.safetensors").read_bytes() == b"appeared meanwhile"


async def test_chunk_checksum_header_is_not_silently_ignored(client):
    session = await (await create(client, 10)).json()

    resp = await client.put(
        f"/model_crud/uploads/{session['upload_id']}/chunks/0",
        data=payload(10),
        headers={"Upload-Chunk-SHA256": "0" * 64},
    )

    assert resp.status == 400 and (await resp.json())["error"] == "bad_request"
    assert not uploads.get_manager().sessions[session["upload_id"]].received


async def test_whole_file_checksum_is_not_silently_ignored(client):
    session = await (await create(client, 10)).json()
    await put_chunk(client, session["upload_id"], 0, payload(10))

    resp = await client.post(
        f"/model_crud/uploads/{session['upload_id']}/complete", json={"sha256": "0" * 64}
    )

    assert resp.status == 400


# --- chunk validation -----------------------------------------------------------------------


async def test_unknown_and_malformed_ids_are_404(client):
    for upload_id in ("A" * 22, "short", "x" * 100, "a b", "é" * 22, "A" * 21 + "%0A"):
        for call in (
            lambda u=upload_id: client.put(f"/model_crud/uploads/{u}/chunks/0", data=b"x"),
            lambda u=upload_id: client.post(f"/model_crud/uploads/{u}/complete"),
            lambda u=upload_id: client.delete(f"/model_crud/uploads/{u}"),
        ):
            resp = await call()
            assert resp.status == 404 and (await resp.json())["error"] == "unknown_upload"


@pytest.mark.parametrize("index", ["abc", "-1", "1.5", "0x1", "1234567890", "99", "%20", "1e2"])
async def test_bad_chunk_index(client, index):
    session = await (await create(client, 3 * CHUNK)).json()

    resp = await client.put(
        f"/model_crud/uploads/{session['upload_id']}/chunks/{index}", data=payload(CHUNK)
    )

    assert resp.status == 400 and (await resp.json())["error"] == "bad_chunk"


@pytest.mark.parametrize("length", [CHUNK - 1, CHUNK + 1, 0, 1])
async def test_wrong_body_length_is_bad_chunk(client, loras, length):
    session = await (await create(client, 3 * CHUNK)).json()

    resp = await client.put(
        f"/model_crud/uploads/{session['upload_id']}/chunks/0", data=payload(length)
    )

    assert resp.status == 400 and (await resp.json())["error"] == "bad_chunk"
    resp = await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")
    assert (await resp.json())["missing_ranges"] == [[0, 2]]


async def test_chunked_transfer_without_content_length_is_refused(client):
    session = await (await create(client, CHUNK)).json()

    async def body():
        yield payload(CHUNK)

    resp = await client.put(f"/model_crud/uploads/{session['upload_id']}/chunks/0", data=body())

    assert resp.status == 400 and (await resp.json())["error"] == "bad_chunk"


async def test_client_that_drops_mid_body_leaves_the_chunk_unrecorded(client, loras):
    data = payload(CHUNK)
    session = await (await create(client, CHUNK)).json()
    manager = uploads.get_manager()

    async def dropping():
        yield data[:1000]
        raise ConnectionResetError("simulated network drop")

    with pytest.raises(Exception):  # noqa: B017, PT011 - the client side of the broken request
        await client.put(
            f"/model_crud/uploads/{session['upload_id']}/chunks/0",
            data=dropping(),
            headers={"Content-Length": str(CHUNK)},
        )
    state = manager.sessions[session["upload_id"]]
    for _ in range(100):  # the handler is cancelled when aiohttp notices the closed socket
        if state.active_writers == 0:
            break
        await asyncio.sleep(0.05)

    assert not state.received
    assert state.active_writers == 0
    assert (await put_chunk(client, session["upload_id"], 0, data)).status == 200


# --- disabled -------------------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [{"read_only": True}, {"allow_upload": False}])
async def test_disabled_uploads_are_403_everywhere(make_client, loras, overrides):
    client = await make_client(config(**overrides))
    some_id = "A" * 22

    responses = [
        await create(client, 10),
        await client.put(f"/model_crud/uploads/{some_id}/chunks/0", data=b"x"),
        await client.post(f"/model_crud/uploads/{some_id}/complete"),
        await client.delete(f"/model_crud/uploads/{some_id}"),
    ]

    for resp in responses:
        assert resp.status == 403 and (await resp.json())["error"] == "uploads_disabled"
    assert list(loras.iterdir()) == []


# --- logging and structure ------------------------------------------------------------------


async def test_completion_is_logged_once_at_info(client, tmp_path, caplog):
    session = await (await create(client, 10)).json()
    await put_chunk(client, session["upload_id"], 0, payload(10))

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")

    [record] = [r for r in caplog.records if r.name == "comfyui-model-crud"]
    assert record.levelno == logging.INFO and "size=10" in record.getMessage()
    assert str(tmp_path) not in record.getMessage()


async def test_unexpected_errors_in_upload_routes_are_generic_500s(client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail at /home/someone")

    monkeypatch.setattr(UploadManager, "create_session", boom)

    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        resp = await create(client, 10)

    assert resp.status == 500
    assert await resp.json() == {"error": "internal_error", "message": "Internal error."}
    assert "secret detail" in caplog.text


def test_no_upload_path_buffers_a_request_body():
    """request.read()/post()/multipart()/json()/text() buffer, and are capped by client_max_size."""
    forbidden = {"read", "post", "multipart", "json", "text"}
    offenders = []
    for source in [*(ROOT / "api").glob("*.py"), *(ROOT / "services").glob("*.py")]:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                root = node.func.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if (
                    node.func.attr in forbidden
                    and isinstance(root, ast.Name)
                    and root.id == "request"
                ):
                    offenders.append(f"{source.name}:{node.lineno} request...{node.func.attr}()")
    assert offenders == []


def test_json_helper_reads_through_the_stream_only():
    source = (ROOT / "api" / "routes.py").read_text()
    assert "iter_chunked" in source and json.__name__ == "json"
