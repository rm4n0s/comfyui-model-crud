import asyncio
import logging
from pathlib import Path
from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services.config import get_config


@pytest.fixture(autouse=True)
def fresh_config():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
async def make_client(folder_paths, prompt_server, comfy_app):
    clients = []

    async def make() -> TestClient:
        # Re-register on a fresh table so each env-var variant gets its own app.
        routes_table = type(prompt_server.routes)()
        api_routes.register_routes(routes_table)
        client = TestClient(TestServer(comfy_app(routes_table)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


@pytest.fixture
async def client(make_client):
    return await make_client()


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def url(rel: str, folder: str = "loras", prefix: str = "", extra: str = "") -> str:
    return f"{prefix}/model_crud/models/{folder}?path={quote(rel, safe='')}{extra}"


@pytest.mark.parametrize("prefix", ["", "/api"])
async def test_delete_succeeds_on_both_prefixes(client, loras, prefix):
    touch(loras / "sdxl" / "m.safetensors")

    resp = await client.delete(url("sdxl/m.safetensors", prefix=prefix))

    assert resp.status == 200
    assert await resp.json() == {"folder": "loras", "path": "sdxl/m.safetensors", "deleted": True}
    assert not (loras / "sdxl" / "m.safetensors").exists()


async def test_deleted_file_disappears_from_the_listing_and_the_cache_is_cleared(
    client, folder_paths, loras
):
    touch(loras / "m.safetensors")
    touch(loras / "keep.safetensors")
    folder_paths.filename_list_cache["loras"] = "stale"

    await client.delete(url("m.safetensors"))
    listing = await (await client.get("/model_crud/models/loras")).json()

    assert [f["path"] for f in listing["files"]] == ["keep.safetensors"]
    assert "loras" not in folder_paths.filename_list_cache


async def test_missing_file_is_404_with_the_error_shape(client, loras):
    resp = await client.delete(url("nope.safetensors"))

    assert resp.status == 404
    assert await resp.json() == {"error": "file_not_found", "message": "File not found."}


async def test_unknown_folder_is_404(client):
    resp = await client.delete(url("m.safetensors", folder="nope"))

    assert resp.status == 404 and (await resp.json())["error"] == "unknown_folder"


async def test_directory_is_never_deleted(client, loras):
    touch(loras / "d.safetensors" / "inner.safetensors")
    touch(loras / "plain" / "inner.safetensors")

    for rel in ("d.safetensors", "plain"):
        resp = await client.delete(url(rel))
        assert resp.status in (404, 415)

    assert (loras / "d.safetensors" / "inner.safetensors").exists()
    assert (loras / "plain" / "inner.safetensors").exists()


@pytest.mark.parametrize("name", ["notes.txt", "x.safetensors.exe", "noext"])
async def test_disallowed_extension_is_415(client, loras, name):
    path = touch(loras / name)

    resp = await client.delete(url(name))

    assert resp.status == 415 and (await resp.json())["error"] == "extension_not_allowed"
    assert path.exists()


# What a hostile client can send for the `path` query parameter, as raw (already percent-encoded)
# query strings, so the HTTP layer's own decoding is part of what is tested.
RAW_TRAVERSAL = [
    "../secret.safetensors",
    "%2e%2e/secret.safetensors",
    "%2e%2e%2fsecret.safetensors",
    "..%2fsecret.safetensors",
    "%2E%2E%2Fsecret.safetensors",
    "a/../../secret.safetensors",
    "a%2f..%2f..%2fsecret.safetensors",
    "..%5csecret.safetensors",
    "a%5c..%5csecret.safetensors",
    "/etc/passwd",
    "%2fetc%2fpasswd",
    "%2Fetc%2Fpasswd",
    "//etc/passwd",
    "C:%5Cx.safetensors",
    "C:/x.safetensors",
    "a%00.safetensors",
    "%00",
    "a/./m.safetensors",
    "a//m.safetensors",
    "..",
    "%2e%2e",
    ".",
]


@pytest.mark.parametrize("raw", RAW_TRAVERSAL)
async def test_traversal_attempts_are_400_and_logged(client, folder_paths, tmp_path, raw, caplog):
    secret = touch(tmp_path / "models" / "secret.safetensors")
    outside = touch(tmp_path / "secret.safetensors")

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        resp = await client.delete(f"/model_crud/models/loras?path={raw}")

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "path_outside_root"
    assert secret.exists() and outside.exists()
    assert str(tmp_path) not in await resp.text()
    [record] = [r for r in caplog.records if r.name == "comfyui-model-crud"]
    assert record.levelno == logging.WARNING


@pytest.mark.parametrize("folder", ["..", "%2e%2e", "loras%2f..%2fvae"])
async def test_traversal_in_the_folder_segment_never_reaches_a_file(
    client, loras, tmp_path, folder
):
    secret = touch(tmp_path / "models" / "secret.safetensors")

    resp = await client.delete(f"/model_crud/models/{folder}?path=secret.safetensors")

    assert resp.status in (404, 405)
    assert secret.exists()


async def test_symlink_escape_over_http(client, loras, tmp_path):
    secret = touch(tmp_path / "outside" / "secret.safetensors")
    (loras / "link.safetensors").symlink_to(secret)
    (loras / "escape").symlink_to(secret.parent, target_is_directory=True)

    for rel in ("link.safetensors", "escape/secret.safetensors"):
        resp = await client.delete(url(rel))
        assert resp.status == 400 and (await resp.json())["error"] == "path_outside_root"

    assert secret.exists()


@pytest.mark.parametrize(
    "query",
    ["", "?path=", "?other=1", "?path=a.safetensors&path=b.safetensors"],
)
async def test_missing_empty_or_repeated_path_is_400(client, loras, query):
    touch(loras / "a.safetensors")
    touch(loras / "b.safetensors")

    resp = await client.delete(f"/model_crud/models/loras{query}")

    assert resp.status == 400 and (await resp.json())["error"] == "bad_request"
    assert (loras / "a.safetensors").exists() and (loras / "b.safetensors").exists()


async def test_wildcards_are_literal_not_globs(client, loras):
    keep = [touch(loras / "a.safetensors"), touch(loras / "b.safetensors")]

    for pattern in ("*.safetensors", "?.safetensors", "[ab].safetensors", "*"):
        assert (await client.delete(url(pattern))).status in (404, 415)

    assert all(p.exists() for p in keep)


async def test_root_index_selects_the_duplicate(client, folder_paths, tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    touch(a / "same.safetensors")
    touch(b / "same.safetensors")

    resp = await client.delete(url("same.safetensors", folder="controlnet", extra="&root_index=1"))

    assert resp.status == 200
    assert (a / "same.safetensors").exists() and not (b / "same.safetensors").exists()
    resp = await client.delete(url("same.safetensors", folder="controlnet", extra="&root_index=1"))
    assert resp.status == 404


@pytest.mark.parametrize(
    "value", ["abc", "-1", "1.5", "", "1e1", "0x1", "9999999", "1&root_index=2"]
)
async def test_bad_root_index_is_400(client, folder_paths, tmp_path, value):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    touch(a / "same.safetensors")

    resp = await client.delete(
        url("same.safetensors", folder="controlnet", extra=f"&root_index={value}")
    )

    assert resp.status == 400 and (await resp.json())["error"] == "bad_request"
    assert (a / "same.safetensors").exists()


async def test_root_index_out_of_range_is_400(client, loras):
    touch(loras / "m.safetensors")

    resp = await client.delete(url("m.safetensors", extra="&root_index=5"))

    assert resp.status == 400
    assert (loras / "m.safetensors").exists()


@pytest.mark.parametrize(
    ("env", "config_flags"),
    [
        ({"MODEL_CRUD_READ_ONLY": "1"}, (False, False)),
        ({"MODEL_CRUD_READ_ONLY": "1", "MODEL_CRUD_ALLOW_DELETE": "1"}, (False, False)),
        ({"MODEL_CRUD_ALLOW_DELETE": "0"}, (True, False)),
    ],
)
async def test_disabled_delete_is_403_and_config_agrees(
    make_client, monkeypatch, loras, env, config_flags
):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = await make_client()
    path = touch(loras / "m.safetensors")

    config = await (await client.get("/model_crud/config")).json()
    resp = await client.delete(url("m.safetensors"))
    unknown = await client.delete(url("../x", folder="nope"))

    assert (config["upload_enabled"], config["delete_enabled"]) == config_flags
    assert resp.status == 403 and (await resp.json())["error"] == "delete_disabled"
    assert unknown.status == 403  # nothing about the filesystem is revealed
    assert path.exists()


async def test_allow_upload_off_does_not_block_delete(make_client, monkeypatch, loras):
    monkeypatch.setenv("MODEL_CRUD_ALLOW_UPLOAD", "0")
    client = await make_client()
    touch(loras / "m.safetensors")

    assert (await client.delete(url("m.safetensors"))).status == 200


async def test_unicode_spaces_and_plus_in_names_round_trip(client, loras):
    names = ["sdxl v2/Süß modell 日本.safetensors", "a+b c.safetensors", "100%.safetensors"]
    for rel in names:
        touch(loras / rel)

    for rel in names:
        resp = await client.delete(url(rel))
        assert resp.status == 200, rel
        assert (await resp.json())["path"] == rel
        assert not (loras / rel).exists()


async def test_info_line_is_logged_without_absolute_paths(client, loras, tmp_path, caplog):
    touch(loras / "sdxl" / "m.safetensors", b"x" * 5)

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        await client.delete(url("sdxl/m.safetensors"))

    [record] = [r for r in caplog.records if r.name == "comfyui-model-crud"]
    assert record.levelno == logging.INFO
    assert "path=sdxl/m.safetensors" in record.getMessage() and "size=5" in record.getMessage()
    assert str(tmp_path) not in record.getMessage()


async def test_concurrent_deletes_of_one_file_yield_one_200_and_one_404(client, loras):
    touch(loras / "m.safetensors")

    first, second = await asyncio.gather(
        client.delete(url("m.safetensors")), client.delete(url("m.safetensors"))
    )

    assert sorted([first.status, second.status]) == [200, 404]


async def test_delete_runs_off_the_event_loop(client, loras, monkeypatch):
    touch(loras / "m.safetensors")
    called = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        called.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(api_routes.asyncio, "to_thread", spy)

    await client.delete(url("m.safetensors"))

    assert called == ["delete_model"]


async def test_unexpected_os_error_is_a_generic_500(client, loras, monkeypatch, caplog):
    import os

    touch(loras / "m.safetensors")

    def fail(path):
        raise OSError(5, "disk on fire at /secret/place")

    monkeypatch.setattr(os, "unlink", fail)

    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        resp = await client.delete(url("m.safetensors"))

    assert resp.status == 500
    assert await resp.json() == {"error": "internal_error", "message": "Internal error."}
    assert "/secret/place" in caplog.text


async def test_other_verbs_on_the_delete_route_are_not_allowed(client, loras):
    touch(loras / "m.safetensors")

    assert (await client.put(url("m.safetensors"))).status == 405
    assert (await client.post(url("m.safetensors"))).status == 405
    assert (loras / "m.safetensors").exists()
