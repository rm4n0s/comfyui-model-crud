import asyncio
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import models
from comfyui_model_crud.services.config import get_config


@pytest.fixture(autouse=True)
def fresh_config():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
async def client(folder_paths, prompt_server, comfy_app):
    api_routes.register_routes(prompt_server.routes)
    async with TestClient(TestServer(comfy_app(prompt_server.routes))) as c:
        yield c


def touch(folder_paths, key: str, rel: str, data: bytes = b"x"):
    from pathlib import Path

    path = Path(folder_paths.get_folder_paths(key)[0]) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("prefix", ["", "/api"])
async def test_every_route_is_reachable_with_and_without_the_api_prefix(
    client, folder_paths, prefix
):
    touch(folder_paths, "loras", "a.safetensors")

    for url in ("/model_crud/config", "/model_crud/folders", "/model_crud/models/loras"):
        resp = await client.get(prefix + url)
        assert resp.status == 200, url
        assert resp.content_type == "application/json"


async def test_both_prefixes_return_identical_bodies(client, folder_paths):
    touch(folder_paths, "loras", "a.safetensors")

    plain = await (await client.get("/model_crud/models/loras")).json()
    prefixed = await (await client.get("/api/model_crud/models/loras")).json()

    assert plain == prefixed


async def test_config_defaults(client):
    body = await (await client.get("/model_crud/config")).json()

    assert body == {
        "version": "0.1.0",
        "upload_enabled": True,
        "delete_enabled": True,
        "max_upload_bytes": 100 * 1024**3,
        "chunk_size": 64 * 1024**2,
        "max_parallel": 4,
        "allowed_extensions": [
            ".safetensors",
            ".ckpt",
            ".pt",
            ".pt2",
            ".pth",
            ".bin",
            ".pkl",
            ".sft",
            ".gguf",
            ".onnx",
        ],
    }


async def test_config_reflects_environment(folder_paths, prompt_server, comfy_app, monkeypatch):
    monkeypatch.setenv("MODEL_CRUD_ALLOW_DELETE", "0")
    monkeypatch.setenv("MODEL_CRUD_CHUNK_MB", "16")
    monkeypatch.setenv("MODEL_CRUD_MAX_PARALLEL", "2")
    monkeypatch.setenv("MODEL_CRUD_MAX_UPLOAD_GB", "2")
    monkeypatch.setenv("MODEL_CRUD_EXTRA_EXTENSIONS", "yaml")
    api_routes.register_routes(prompt_server.routes)

    async with TestClient(TestServer(comfy_app(prompt_server.routes))) as c:
        body = await (await c.get("/model_crud/config")).json()

    assert body["upload_enabled"] is True and body["delete_enabled"] is False
    assert body["chunk_size"] == 16 * 1024**2
    assert body["max_parallel"] == 2
    assert body["max_upload_bytes"] == 2 * 1024**3
    assert body["allowed_extensions"][-1] == ".yaml"


async def test_read_only_disables_both_flags(folder_paths, prompt_server, comfy_app, monkeypatch):
    monkeypatch.setenv("MODEL_CRUD_READ_ONLY", "1")
    api_routes.register_routes(prompt_server.routes)

    async with TestClient(TestServer(comfy_app(prompt_server.routes))) as c:
        body = await (await c.get("/model_crud/config")).json()

    assert body["upload_enabled"] is False and body["delete_enabled"] is False


async def test_folders(client, folder_paths):
    body = await (await client.get("/model_crud/folders")).json()

    assert body == {
        "folders": [
            {"key": "checkpoints", "roots": 1},
            {"key": "loras", "roots": 1},
            {"key": "vae", "roots": 1},
        ]
    }


async def test_models_listing(client, folder_paths):
    touch(folder_paths, "loras", "sdxl/detail.safetensors", b"x" * 10)
    touch(folder_paths, "loras", "readme.txt")

    body = await (await client.get("/model_crud/models/loras")).json()

    assert body["folder"] == "loras"
    [entry] = body["files"]
    assert entry["path"] == "sdxl/detail.safetensors"
    assert entry["name"] == "detail.safetensors"
    assert entry["size"] == 10 and entry["root_index"] == 0
    assert entry["modified"].endswith("Z")


async def test_responses_never_contain_absolute_server_paths(client, folder_paths, tmp_path):
    touch(folder_paths, "loras", "a/b.safetensors")

    for url in ("/model_crud/models/loras", "/model_crud/folders", "/model_crud/models/nope"):
        text = await (await client.get(url)).text()
        assert str(tmp_path) not in text, url


async def test_responses_are_not_cacheable(client):
    for url in ("/model_crud/config", "/model_crud/folders", "/model_crud/models/loras"):
        assert (await client.get(url)).headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "key", ["nope", "unet", "loras%2F..", "LORAS", "a%00b", "x" * 5000, "custom_nodes"]
)
async def test_unknown_folder_uses_the_error_shape(client, folder_paths, tmp_path, key):
    (tmp_path / "nodes").mkdir()
    folder_paths.folder_names_and_paths["custom_nodes"] = ([str(tmp_path / "nodes")], set())
    resp = await client.get(f"/model_crud/models/{key}")

    assert resp.status == 404
    body = await resp.json()
    assert body["error"] == "unknown_folder"
    assert isinstance(body["message"], str) and body["message"]
    assert set(body) == {"error", "message"}


async def test_unexpected_errors_return_a_generic_500_and_log_the_traceback(
    client, monkeypatch, caplog
):
    def boom(*args):
        raise RuntimeError("secret detail at /home/someone/models")

    monkeypatch.setattr(models, "list_models", boom)

    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        resp = await client.get("/model_crud/models/loras")

    assert resp.status == 500
    body = await resp.json()
    assert body == {"error": "internal_error", "message": "Internal error."}
    assert "secret detail" not in await resp.text()
    assert "secret detail" in caplog.text and "Traceback" in caplog.text


async def test_directory_walks_run_off_the_event_loop(client, monkeypatch):
    threads = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        threads.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(api_routes.asyncio, "to_thread", spy)

    await client.get("/model_crud/folders")
    await client.get("/model_crud/models/loras")

    assert threads == ["list_folders", "list_models"]


async def test_listing_does_not_block_other_requests(client, folder_paths, monkeypatch):
    import threading
    import time

    started = threading.Event()

    def slow(folder, allowed):
        started.set()
        time.sleep(0.3)
        return []

    monkeypatch.setattr(models, "list_models", slow)

    slow_request = asyncio.create_task(client.get("/model_crud/models/loras"))
    await asyncio.to_thread(started.wait, 2)
    t0 = time.perf_counter()
    await client.get("/model_crud/config")
    quick = time.perf_counter() - t0
    await slow_request

    assert quick < 0.2


async def test_post_to_a_read_only_route_is_405_not_a_crash(client):
    assert (await client.post("/model_crud/folders")).status == 405
