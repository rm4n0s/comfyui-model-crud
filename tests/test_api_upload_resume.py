import asyncio
import hashlib
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import uploads
from comfyui_model_crud.services.uploads import UploadManager
from uploadkit import make_config, payload

CHUNK = 4096


def config(**overrides):
    return make_config(chunk_size=CHUNK, max_parallel=8, **overrides)


@pytest.fixture
async def start_server(folder_paths, prompt_server, comfy_app):
    """Boot an app like ComfyUI does: routes plus the lifecycle hooks that resume uploads."""
    clients = []

    async def start(cfg=None) -> TestClient:
        uploads.set_manager(UploadManager(cfg or config()))
        table = type(prompt_server.routes)()
        api_routes.register_routes(table)
        app = comfy_app(table)
        api_routes.register_lifecycle(app)
        client = TestClient(TestServer(app))
        await client.start_server()  # runs on_startup: scans the disk, starts the sweep
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()
    uploads.set_manager(None)


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def create(client, size, name="m.safetensors", **extra):
    resp = await client.post(
        "/model_crud/uploads", json={"folder": "loras", "filename": name, "size": size, **extra}
    )
    assert resp.status == 201, await resp.text()
    return await resp.json()


async def put(client, upload_id, index, data, headers=None):
    return await client.put(
        f"/model_crud/uploads/{upload_id}/chunks/{index}",
        data=data[index * CHUNK : (index + 1) * CHUNK],
        headers=headers,
    )


async def test_status_reports_received_ranges(start_server):
    client = await start_server()
    data = payload(6 * CHUNK + 10)
    session = await create(client, len(data), client_fingerprint="m.safetensors:24586:1")
    for i in (0, 1, 3, 6):
        await put(client, session["upload_id"], i, data)

    resp = await client.get(f"/model_crud/uploads/{session['upload_id']}")

    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {
        "upload_id",
        "folder",
        "path",
        "size",
        "chunk_size",
        "total_chunks",
        "received_ranges",
        "received_bytes",
        "client_fingerprint",
        "expires_at",
    }
    assert body["received_ranges"] == [[0, 1], [3, 3], [6, 6]]
    assert body["received_bytes"] == 3 * CHUNK + 10
    assert body["client_fingerprint"] == "m.safetensors:24586:1" and body["total_chunks"] == 7
    assert body["expires_at"].endswith("Z")


async def test_list_has_the_same_fields_without_ranges(start_server):
    client = await start_server()
    first = await create(client, CHUNK, name="a.safetensors", client_fingerprint="fp-a")
    second = await create(client, 2 * CHUNK, name="b.safetensors")

    resp = await client.get("/model_crud/uploads")

    assert resp.status == 200
    listed = (await resp.json())["uploads"]
    assert [u["upload_id"] for u in listed] == [first["upload_id"], second["upload_id"]]
    assert all("received_ranges" not in u for u in listed)
    assert listed[0]["client_fingerprint"] == "fp-a" and listed[1]["client_fingerprint"] is None


@pytest.mark.parametrize("prefix", ["", "/api"])
async def test_status_routes_on_both_prefixes(start_server, prefix):
    client = await start_server()
    session = await create(client, CHUNK)

    assert (await client.get(f"{prefix}/model_crud/uploads")).status == 200
    assert (await client.get(f"{prefix}/model_crud/uploads/{session['upload_id']}")).status == 200


@pytest.mark.parametrize("upload_id", ["A" * 22, "short", "x" * 100, "a b", "é" * 22])
async def test_status_of_unknown_or_malformed_ids_is_404(start_server, upload_id):
    client = await start_server()

    resp = await client.get(f"/model_crud/uploads/{upload_id}")

    assert resp.status == 404 and (await resp.json())["error"] == "unknown_upload"


@pytest.mark.parametrize("overrides", [{"read_only": True}, {"allow_upload": False}])
async def test_status_and_list_are_403_when_disabled(start_server, overrides):
    client = await start_server(config(**overrides))

    for url in ("/model_crud/uploads", f"/model_crud/uploads/{'A' * 22}"):
        resp = await client.get(url)
        assert resp.status == 403 and (await resp.json())["error"] == "uploads_disabled"


async def test_chunk_checksum_over_http(start_server, loras):
    client = await start_server()
    data = payload(2 * CHUNK)
    session = await create(client, len(data))
    uid = session["upload_id"]

    bad = await put(client, uid, 0, data, {"Upload-Chunk-SHA256": sha(b"nope")})
    assert bad.status == 422
    assert await bad.json() == {
        "error": "checksum_mismatch",
        "message": "The chunk does not match its checksum; send it again.",
    }
    malformed = await put(client, uid, 0, data, {"Upload-Chunk-SHA256": "zzz"})
    assert malformed.status == 400 and (await malformed.json())["error"] == "bad_request"
    status = await (await client.get(f"/model_crud/uploads/{uid}")).json()
    assert status["received_ranges"] == []

    ok = await put(client, uid, 0, data, {"Upload-Chunk-SHA256": sha(data[:CHUNK])})
    assert ok.status == 200
    ok = await put(client, uid, 1, data, {"Upload-Chunk-SHA256": sha(data[CHUNK:]).upper()})
    assert ok.status == 200
    assert (await client.post(f"/model_crud/uploads/{uid}/complete")).status == 201
    assert (loras / "m.safetensors").read_bytes() == data


async def test_whole_file_checksum_over_http(start_server, loras):
    client = await start_server()
    data = payload(3 * CHUNK)
    session = await create(client, len(data))
    uid = session["upload_id"]
    for i in range(3):
        await put(client, uid, i, data)

    mismatch = await client.post(f"/model_crud/uploads/{uid}/complete", json={"sha256": "0" * 64})
    assert mismatch.status == 422 and (await mismatch.json())["error"] == "checksum_mismatch"
    assert not (loras / "m.safetensors").exists()
    assert (await client.get(f"/model_crud/uploads/{uid}")).status == 200  # session kept

    malformed = await client.post(f"/model_crud/uploads/{uid}/complete", json={"sha256": "abc"})
    assert malformed.status == 400

    ok = await client.post(f"/model_crud/uploads/{uid}/complete", json={"sha256": sha(data)})
    assert ok.status == 201
    assert (loras / "m.safetensors").read_bytes() == data


async def test_restarting_the_server_keeps_the_upload_resumable(start_server, loras):
    data = payload(8 * CHUNK + 100)
    client = await start_server()
    session = await create(client, len(data), client_fingerprint="m.safetensors:1:2", subfolder="s")
    uid = session["upload_id"]
    for i in (0, 1, 2, 5):
        assert (await put(client, uid, i, data)).status == 200
    await client.close()  # the ComfyUI process goes away

    client = await start_server()  # ...and comes back
    listed = (await (await client.get("/model_crud/uploads")).json())["uploads"]
    assert [u["upload_id"] for u in listed] == [uid]
    assert listed[0]["client_fingerprint"] == "m.safetensors:1:2"
    status = await (await client.get(f"/model_crud/uploads/{uid}")).json()
    assert status["received_ranges"] == [[0, 2], [5, 5]]
    assert status["path"] == "s/m.safetensors" and status["chunk_size"] == CHUNK

    early = await client.post(f"/model_crud/uploads/{uid}/complete")
    assert early.status == 409
    assert (await early.json())["missing_ranges"] == [[3, 4], [6, 8]]

    sent = []
    for first, last in (await early.json())["missing_ranges"]:
        for i in range(first, last + 1):
            assert (await put(client, uid, i, data)).status == 200
            sent.append(i)
    assert sent == [3, 4, 6, 7, 8]  # only the gaps were sent again
    assert (await client.post(f"/model_crud/uploads/{uid}/complete")).status == 201
    assert (loras / "s" / "m.safetensors").read_bytes() == data
    assert not list((loras / "s").glob(".model_crud_*"))


async def test_cleanup_stops_the_sweep(start_server):
    client = await start_server()
    manager = uploads.get_manager()
    assert manager._expiry_task is not None and not manager._expiry_task.done()
    task = manager._expiry_task

    await client.close()

    assert manager._expiry_task is None and task.done()


async def test_expired_uploads_disappear_from_the_api(start_server, loras):
    client = await start_server(config(upload_ttl_hours=0.00001))
    session = await create(client, CHUNK)
    manager = uploads.get_manager()

    await asyncio.sleep(0.2)
    assert await manager.expire_idle() == 1

    assert (await client.get(f"/model_crud/uploads/{session['upload_id']}")).status == 404
    assert (await (await client.get("/model_crud/uploads")).json())["uploads"] == []
    assert not list(loras.glob(".model_crud_*"))
