"""``/model_crud/input`` and ``/model_crud/output`` (SPEC §5.5): ComfyUI's input and output
directories, which sit next to ``models/`` and are not model folders."""

import logging
import random
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import media, models, paths, uploads
from comfyui_model_crud.services.config import (
    DEFAULT_EXTENSIONS,
    DEFAULT_MEDIA_EXTENSIONS,
    Config,
)
from comfyui_model_crud.services.errors import (
    DeleteDisabled,
    ExtensionNotAllowed,
    FileAlreadyExists,
    ModelCrudError,
    ModelFileNotFound,
    PathOutsideRoot,
    UnknownFolder,
    UnknownUpload,
)
from comfyui_model_crud.services.uploads import UploadManager
from uploadkit import Body, make_config, payload

CHUNK = 1024
AREAS = ["input", "output"]


def media_root(folder_paths, area: str) -> Path:
    return Path(getattr(folder_paths, f"{area}_directory"))


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def output_dir(folder_paths) -> Path:
    return media_root(folder_paths, "output")


@pytest.fixture
def make_manager(folder_paths):
    created = []

    def make(**overrides) -> UploadManager:
        manager = UploadManager(make_config(**overrides))
        created.append(manager)
        return manager

    yield make
    for manager in created:
        manager.shutdown()


# --- configuration --------------------------------------------------------------------------


def test_defaults_enable_both_areas_with_their_own_list():
    config = Config()

    assert config.media_folders == ("input", "output")
    assert config.extensions_for_area("input") == config.media_extensions
    assert config.extensions_for_area("output") == config.media_extensions
    assert config.allowed_extensions == tuple(DEFAULT_EXTENSIONS)  # models are unaffected


def test_the_media_list_has_no_models_and_no_active_content():
    assert not set(DEFAULT_MEDIA_EXTENSIONS) & set(DEFAULT_EXTENSIONS)
    for risky in (".svg", ".html", ".htm", ".js", ".py", ".sh", ".exe", ".part", ".yaml", ".zip"):
        assert risky not in DEFAULT_MEDIA_EXTENSIONS
    assert {".png", ".jpg", ".webp", ".mp4", ".wav", ".glb", ".latent"} <= set(
        DEFAULT_MEDIA_EXTENSIONS
    )


def test_a_disabled_or_unknown_area_has_no_extensions():
    config = Config(media_folders=("output",))

    assert config.extensions_for_area("input") == ()
    assert config.extensions_for_area("output") == config.media_extensions
    assert config.extensions_for_area("loras") == ()
    assert config.extensions_for_area(["output"]) == ()
    assert Config(media_folders=()).extensions_for_area("output") == ()


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ("input", "output")),
        ("", ("input", "output")),
        ("input,output", ("input", "output")),
        (" Output , input ", ("output", "input")),
        ("output", ("output",)),
        ("input,input", ("input",)),
        ("none", ()),
        ("NONE", ()),
    ],
)
def test_media_folders_variable(raw, expected):
    env = {} if raw is None else {"MODEL_CRUD_MEDIA_FOLDERS": raw}

    assert Config.from_env(env).media_folders == expected


@pytest.mark.parametrize("raw", ["temp", "input,temp", "all", "none,input", "models", "loras"])
def test_bad_media_folders_fall_back_to_the_default_and_are_logged(raw, caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        config = Config.from_env({"MODEL_CRUD_MEDIA_FOLDERS": raw})

    assert config.media_folders == ("input", "output")
    assert "MODEL_CRUD_MEDIA_FOLDERS" in caplog.text


def test_extra_media_extensions_are_separate_from_the_model_extras():
    config = Config.from_env(
        {
            "MODEL_CRUD_EXTRA_MEDIA_EXTENSIONS": "heic, .TGA, .png",
            "MODEL_CRUD_EXTRA_EXTENSIONS": "yaml",
        }
    )

    assert config.media_extensions == (*DEFAULT_MEDIA_EXTENSIONS, ".heic", ".tga")
    assert ".yaml" in config.allowed_extensions and ".yaml" not in config.media_extensions


def test_the_part_suffix_cannot_be_made_a_media_extension(caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        config = Config.from_env({"MODEL_CRUD_EXTRA_MEDIA_EXTENSIONS": ".part"})

    assert ".part" not in config.media_extensions
    assert "MODEL_CRUD_EXTRA_MEDIA_EXTENSIONS" in caplog.text


# --- listing --------------------------------------------------------------------------------


def test_listing_returns_media_files_recursively_and_skips_the_rest(folder_paths, output_dir):
    touch(output_dir / "ComfyUI_00001_.png", b"1234")
    touch(output_dir / "video" / "clip.MP4")
    touch(output_dir / "2026" / "10" / "x.webp")
    for skipped in ("weights.safetensors", "run.py", "page.svg", "page.html", "noext"):
        touch(output_dir / skipped)
    touch(output_dir / ".hidden.png")
    touch(output_dir / ".model_crud_abc.part")

    result = media.list_files("output", Config())

    assert [f["path"] for f in result] == ["2026/10/x.webp", "ComfyUI_00001_.png", "video/clip.MP4"]
    png = next(f for f in result if f["name"] == "ComfyUI_00001_.png")
    assert png["size"] == 4 and png["modified"].endswith("Z")
    assert set(png) == {"path", "name", "size", "modified"}  # one root: no root_index


def test_the_two_areas_are_independent(folder_paths):
    touch(media_root(folder_paths, "input") / "in.png")
    touch(media_root(folder_paths, "output") / "out.png")

    assert [f["path"] for f in media.list_files("input", Config())] == ["in.png"]
    assert [f["path"] for f in media.list_files("output", Config())] == ["out.png"]


def test_a_missing_output_directory_lists_as_empty(folder_paths, output_dir):
    output_dir.rmdir()  # ComfyUI creates it lazily

    assert media.list_files("output", Config()) == []


def test_directories_are_read_from_comfyui_on_every_call(folder_paths, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    touch(elsewhere / "a.png")
    folder_paths.output_directory = str(elsewhere)  # what --output-directory / set_* do

    assert [f["path"] for f in media.list_files("output", Config())] == ["a.png"]


def test_models_are_not_listed_in_media_and_media_not_in_model_folders(folder_paths):
    touch(media_root(folder_paths, "input") / "m.safetensors")
    touch(Path(folder_paths.get_folder_paths("loras")[0]) / "photo.png")

    assert media.list_files("input", Config()) == []
    assert models.list_models("loras", Config().allowed_extensions) == []


def test_symlinks_leaving_the_directory_are_not_listed(folder_paths, output_dir, tmp_path):
    secret = touch(tmp_path / "private" / "secret.png")
    (output_dir / "leak.png").symlink_to(secret)
    (output_dir / "dirleak").symlink_to(tmp_path / "private")
    touch(output_dir / "ok.png")
    (output_dir / "alias.png").symlink_to(output_dir / "ok.png")

    assert [f["path"] for f in media.list_files("output", Config())] == ["alias.png", "ok.png"]


@pytest.mark.parametrize("area", ["temp", "models", "loras", "Input", "", None, 5])
def test_only_input_and_output_are_areas(folder_paths, area):
    with pytest.raises(UnknownFolder):
        media.list_files(area, Config())


@pytest.mark.parametrize("area", ["temp", "loras", "models", "Input", "", None])
def test_the_resolver_itself_refuses_other_names(folder_paths, area):
    with pytest.raises(UnknownFolder):  # even with a non-empty allowlist
        paths.get_media_roots(area, (".png",))


def test_a_disabled_area_is_unknown(folder_paths):
    with pytest.raises(UnknownFolder):
        media.list_files("output", Config(media_folders=("input",)))


def test_a_model_key_with_the_same_name_is_a_different_thing(folder_paths, tmp_path):
    [models_input] = folder_paths.add_folder("input", tmp_path / "models" / "input")
    touch(models_input / "weights.safetensors")
    touch(media_root(folder_paths, "input") / "cat.png")

    assert [f["path"] for f in media.list_files("input", Config())] == ["cat.png"]
    assert [f["path"] for f in models.list_models("input", Config().allowed_extensions)] == [
        "weights.safetensors"
    ]


# --- path and extension rules ---------------------------------------------------------------

TRAVERSAL = [
    "../x.png",
    "a/../../x.png",
    "/etc/passwd.png",
    "..\\x.png",
    "C:/x.png",
    "a\x00.png",
    "a//b.png",
    "./x.png",
]


@pytest.mark.parametrize("area", AREAS)
@pytest.mark.parametrize("raw", TRAVERSAL)
def test_traversal_is_rejected(folder_paths, area, raw):
    allowed = Config().extensions_for_area(area)

    with pytest.raises(PathOutsideRoot):
        paths.resolve_existing(area, raw, allowed, media=True)
    with pytest.raises(ModelCrudError) as exc:  # as a subfolder: 400 either way
        paths.resolve_upload_target(area, "x.png", raw, allowed, media=True)
    assert exc.value.status == 400


def test_a_symlink_escaping_the_directory_is_rejected(folder_paths, output_dir, tmp_path):
    secret = touch(tmp_path / "private" / "secret.png")
    (output_dir / "leak.png").symlink_to(secret)
    (output_dir / "dirleak").symlink_to(tmp_path / "private")
    allowed = Config().media_extensions

    with pytest.raises(PathOutsideRoot):
        paths.resolve_existing("output", "leak.png", allowed, media=True)
    with pytest.raises(PathOutsideRoot):
        paths.resolve_existing("output", "dirleak/secret.png", allowed, media=True)
    with pytest.raises(PathOutsideRoot):
        paths.resolve_upload_target("output", "new.png", "dirleak", allowed, media=True)


@pytest.mark.parametrize("name", ["weights.safetensors", "run.py", "a.png.exe", "x.svg", "x"])
def test_only_media_extensions_resolve(folder_paths, output_dir, name):
    touch(output_dir / name)
    allowed = Config().media_extensions

    with pytest.raises(ExtensionNotAllowed):
        paths.resolve_existing("output", name, allowed, media=True)
    with pytest.raises(ExtensionNotAllowed):
        paths.resolve_upload_target("output", name, None, allowed, media=True)


def test_extensions_are_case_insensitive_and_names_may_have_spaces_and_unicode(
    folder_paths, output_dir
):
    touch(output_dir / "Foto Ωmega 1.PNG")

    resolved = paths.resolve_existing(
        "output", "Foto Ωmega 1.PNG", Config().media_extensions, media=True
    )

    assert resolved.relpath == "Foto Ωmega 1.PNG"


def test_an_image_is_not_a_model(folder_paths):
    with pytest.raises(ExtensionNotAllowed):
        paths.resolve_upload_target("loras", "cat.png", None, Config().allowed_extensions)


# --- delete ---------------------------------------------------------------------------------


@pytest.mark.parametrize("area", AREAS)
def test_delete_removes_exactly_one_file(folder_paths, area):
    root = media_root(folder_paths, area)
    touch(root / "sub" / "a.png")
    touch(root / "sub" / "b.png")
    folder_paths.filename_list_cache["loras"] = ("untouched",)

    result = media.delete_file(area, "sub/a.png", Config())

    assert result == {"area": area, "path": "sub/a.png", "deleted": True}
    assert not (root / "sub" / "a.png").exists() and (root / "sub" / "b.png").exists()
    assert folder_paths.filename_list_cache == {"loras": ("untouched",)}


def test_delete_refuses_models_and_other_files(folder_paths, output_dir):
    for name in ("weights.safetensors", "notes.py", "page.html"):
        touch(output_dir / name)
        with pytest.raises(ExtensionNotAllowed):
            media.delete_file("output", name, Config())
        assert (output_dir / name).exists()


def test_delete_missing_and_directory(folder_paths, output_dir):
    (output_dir / "dir.png").mkdir()

    with pytest.raises(ModelFileNotFound):
        media.delete_file("output", "nope.png", Config())
    with pytest.raises(ModelFileNotFound):
        media.delete_file("output", "dir.png", Config())
    assert (output_dir / "dir.png").is_dir()


def test_delete_through_a_symlink_removes_the_link_only(folder_paths, output_dir):
    real = touch(output_dir / "real.png")
    (output_dir / "link.png").symlink_to(real)

    media.delete_file("output", "link.png", Config())

    assert real.exists() and not (output_dir / "link.png").is_symlink()


def test_delete_respects_the_switches(folder_paths, output_dir):
    touch(output_dir / "a.png")

    with pytest.raises(DeleteDisabled):
        media.delete_file("output", "a.png", Config(allow_delete=False))
    with pytest.raises(DeleteDisabled):
        media.delete_file("output", "a.png", Config(read_only=True))
    with pytest.raises(UnknownFolder):
        media.delete_file("output", "a.png", Config(media_folders=("input",)))
    assert (output_dir / "a.png").exists()


def test_delete_logs_without_absolute_paths(folder_paths, output_dir, caplog):
    touch(output_dir / "a.png")

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        media.delete_file("output", "a.png", Config())

    assert "area=output path=a.png" in caplog.text and str(output_dir) not in caplog.text


# --- uploads --------------------------------------------------------------------------------


async def send_all(manager, session, data, area=None):
    for i in range(session.total_chunks):
        piece = data[i * session.chunk_size : (i + 1) * session.chunk_size]
        await manager.write_chunk(session.id, i, Body(piece), len(piece), None, area)


@pytest.mark.parametrize("area", AREAS)
async def test_upload_creates_subfolders_and_publishes(make_manager, folder_paths, area):
    root = media_root(folder_paths, area)
    data = payload(3 * CHUNK + 7)
    manager = make_manager()

    session = await manager.create_session(
        None, "Result 1.png", len(data), subfolder="2026/10", area=area
    )
    assert session.area == area and session.folder == area and (root / "2026" / "10").is_dir()
    assert media.list_files(area, manager.config) == []  # the temp file is hidden
    await send_all(manager, session, data, area)
    result = await manager.complete(session.id, None, area)

    assert result == {"folder": area, "path": "2026/10/Result 1.png", "size": len(data)}
    assert (root / "2026" / "10" / "Result 1.png").read_bytes() == data
    assert list(root.rglob(".model_crud_*")) == []


async def test_upload_creates_a_missing_output_directory(make_manager, folder_paths, output_dir):
    output_dir.rmdir()
    manager = make_manager()

    session = await manager.create_session(None, "a.png", CHUNK, area="output")
    await send_all(manager, session, payload(CHUNK), "output")
    await manager.complete(session.id, None, "output")

    assert (output_dir / "a.png").is_file()


async def test_upload_refuses_models_and_other_types_in_a_media_area(make_manager, folder_paths):
    manager = make_manager()
    for name in ("m.safetensors", "run.py", "x.svg", "a.png.exe"):
        with pytest.raises(ExtensionNotAllowed):
            await manager.create_session(None, name, 10, area="output")
    with pytest.raises(ExtensionNotAllowed):
        await manager.create_session("loras", "cat.png", 10)
    assert list(media_root(folder_paths, "output").iterdir()) == []


async def test_a_disabled_area_cannot_be_uploaded_to(make_manager, folder_paths):
    manager = make_manager(media_folders=("input",))

    with pytest.raises(UnknownFolder):
        await manager.create_session(None, "a.png", 10, area="output")
    with pytest.raises(UnknownFolder):
        manager.list_sessions("output")
    assert list(media_root(folder_paths, "output").iterdir()) == []


async def test_conflict_unless_overwrite(make_manager, output_dir):
    touch(output_dir / "a.png", b"old")
    manager = make_manager()

    with pytest.raises(FileAlreadyExists):
        await manager.create_session(None, "a.png", 3, area="output")
    session = await manager.create_session(None, "a.png", 3, overwrite=True, area="output")
    await send_all(manager, session, b"new", "output")
    await manager.complete(session.id, None, "output")
    assert (output_dir / "a.png").read_bytes() == b"new"


async def test_bad_subfolders_are_400_and_create_nothing(make_manager, folder_paths, tmp_path):
    manager = make_manager()
    for sub in ("..", "../x", "/tmp", "a/../..", "a\\b"):
        with pytest.raises(ModelCrudError) as exc:
            await manager.create_session(None, "a.png", 3, subfolder=sub, area="output")
        assert exc.value.status == 400, sub
    assert not (tmp_path / "x").exists()


async def test_sessions_are_only_reachable_through_their_own_kind_of_route(
    make_manager, folder_paths
):
    manager = make_manager()
    model = await manager.create_session("loras", "m.safetensors", CHUNK)
    out = await manager.create_session(None, "a.png", CHUNK, area="output")
    data = payload(CHUNK)

    for upload_id, wrong in ((model.id, "output"), (out.id, None), (out.id, "input")):
        with pytest.raises(UnknownUpload):
            manager.status(upload_id, wrong)
        with pytest.raises(UnknownUpload):
            await manager.write_chunk(upload_id, 0, Body(data), len(data), None, wrong)
        with pytest.raises(UnknownUpload):
            await manager.complete(upload_id, None, wrong)
        with pytest.raises(UnknownUpload):
            await manager.cancel(upload_id, wrong)
    assert [s.id for s in manager.list_sessions()] == [model.id]
    assert [s.id for s in manager.list_sessions("output")] == [out.id]
    assert manager.list_sessions("input") == []


async def test_the_same_target_cannot_be_uploaded_twice_across_areas_and_models(
    make_manager, folder_paths, tmp_path
):
    # Point the output directory at a model root: one file, two ways to address it.
    folder_paths.output_directory = folder_paths.get_folder_paths("loras")[0]
    manager = make_manager()
    await manager.create_session(None, "a.txt", 5, area="output")

    from comfyui_model_crud.services.errors import UploadInProgress

    with pytest.raises(UploadInProgress):
        await manager.create_session(None, "a.txt", 5, area="output")


async def test_resume_after_restart(make_manager, output_dir):
    data = payload(5 * CHUNK)
    first = make_manager()
    session = await first.create_session(None, "big.mp4", len(data), subfolder="v", area="output")
    for i in (0, 2):
        piece = data[i * CHUNK : (i + 1) * CHUNK]
        await first.write_chunk(session.id, i, Body(piece), len(piece), None, "output")
    first.shutdown()

    second = make_manager()
    await second.start()

    assert second.list_sessions() == []  # not a model upload
    [resumed] = second.list_sessions("output")
    assert resumed.area == "output" and resumed.relpath == "v/big.mp4"
    assert sorted(resumed.received) == [0, 2]
    for i in (1, 3, 4):
        piece = data[i * CHUNK : (i + 1) * CHUNK]
        await second.write_chunk(resumed.id, i, Body(piece), len(piece), None, "output")
    await second.complete(resumed.id, None, "output")
    assert (output_dir / "v" / "big.mp4").read_bytes() == data


async def test_a_session_survives_restarts_when_roots_nest(make_manager, folder_paths, output_dir):
    """ComfyUI adds output/checkpoints as a model root: scanning it must not eat the session."""
    inner = output_dir / "checkpoints"
    inner.mkdir()
    folder_paths.folder_names_and_paths["checkpoints"][0].append(str(inner))
    data = payload(3 * CHUNK)
    first = make_manager()
    session = await first.create_session(
        None, "p.png", len(data), subfolder="checkpoints", area="output"
    )
    await first.write_chunk(session.id, 0, Body(data[:CHUNK]), CHUNK, None, "output")
    first.shutdown()

    for _ in range(2):  # and again: the first scan damaged nothing
        again = make_manager()
        await again.start()
        assert [s.id for s in again.list_sessions("output")] == [session.id]
        again.shutdown()
    assert len(list(inner.glob(".model_crud_*"))) == 2


async def test_sessions_of_a_disabled_area_are_left_alone_and_resume_when_enabled(
    make_manager, folder_paths
):
    root = media_root(folder_paths, "input")
    first = make_manager()
    session = await first.create_session(None, "a.png", 2 * CHUNK, area="input")
    first.shutdown()

    off = make_manager(media_folders=("output",))
    await off.start()
    assert off.sessions == {}
    assert len(list(root.glob(".model_crud_*"))) == 2  # not adopted, not deleted

    on = make_manager()
    await on.start()
    assert [s.id for s in on.list_sessions("input")] == [session.id]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(area="temp"),
        lambda d: d.update(area="output", folder="loras"),
        lambda d: d.update(area=5),
        lambda d: d.update(path="../x.png"),
        lambda d: d.update(path="a.safetensors"),
    ],
)
async def test_tampered_media_sidecars_are_discarded(make_manager, output_dir, mutate):
    import json

    first = make_manager()
    session = await first.create_session(None, "a.png", CHUNK, area="output")
    first.shutdown()
    sidecar = output_dir / f".model_crud_{session.id}.json"
    data = json.loads(sidecar.read_text())
    mutate(data)
    sidecar.write_text(json.dumps(data))

    second = make_manager()
    await second.start()

    assert second.sessions == {}
    assert list(output_dir.glob(".model_crud_*")) == []


# --- HTTP -----------------------------------------------------------------------------------


@pytest.fixture
async def make_client(folder_paths, prompt_server, comfy_app, monkeypatch):
    clients = []

    async def make(config: Config | None = None) -> TestClient:
        config = config or make_config()
        monkeypatch.setattr(api_routes, "get_config", lambda: config)
        uploads.set_manager(UploadManager(config))
        table = type(prompt_server.routes)()
        api_routes.register_routes(table)
        client = TestClient(TestServer(comfy_app(table)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()
    uploads.set_manager(None)


async def upload_over_http(client, base, name, data, **extra):
    resp = await client.post(f"{base}/uploads", json={"filename": name, "size": len(data), **extra})
    assert resp.status == 201, await resp.text()
    session = await resp.json()
    for i in range(session["total_chunks"]):
        part = data[i * CHUNK : (i + 1) * CHUNK]
        put = await client.put(f"{base}/uploads/{session['upload_id']}/chunks/{i}", data=part)
        assert put.status == 200
    return session, await client.post(f"{base}/uploads/{session['upload_id']}/complete", json={})


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("area", AREAS)
async def test_list_upload_and_delete_over_http(make_client, folder_paths, prefix, area):
    client = await make_client()
    root = media_root(folder_paths, area)
    touch(root / "sub" / "a.png", b"abc")
    touch(root / "m.safetensors")
    base = f"{prefix}/model_crud/{area}"

    listing = await (await client.get(base)).json()
    assert listing["area"] == area and "folder" not in listing
    assert [f["path"] for f in listing["files"]] == ["sub/a.png"]
    assert str(root) not in str(listing)  # relative paths only (SR-3)

    data = random.Random(5).randbytes(2 * CHUNK + 1)
    session, done = await upload_over_http(client, base, "up.png", data, subfolder="new")
    expected = "area upload_id path size chunk_size total_chunks max_parallel expires_at"
    assert set(session) == set(expected.split())
    assert session["area"] == area and session["path"] == "new/up.png"
    assert done.status == 201
    assert await done.json() == {"area": area, "path": "new/up.png", "size": len(data)}
    assert (root / "new" / "up.png").read_bytes() == data

    deleted = await client.delete(f"{base}?path=sub%2Fa.png")
    assert deleted.status == 200
    assert await deleted.json() == {"area": area, "path": "sub/a.png", "deleted": True}
    assert not (root / "sub" / "a.png").exists() and (root / "m.safetensors").exists()


async def test_status_list_and_cancel_over_http(make_client, output_dir):
    client = await make_client()
    resp = await client.post(
        "/model_crud/output/uploads",
        json={"filename": "a.png", "size": 3 * CHUNK, "client_fingerprint": "fp"},
    )
    session = await resp.json()
    base = "/model_crud/output/uploads"

    status = await (await client.get(f"{base}/{session['upload_id']}")).json()
    listing = await (await client.get(base)).json()

    assert status["area"] == "output" and status["received_ranges"] == []
    assert status["client_fingerprint"] == "fp" and "folder" not in status
    assert [u["upload_id"] for u in listing["uploads"]] == [session["upload_id"]]
    assert (
        listing["uploads"][0]["area"] == "output" and "received_ranges" not in listing["uploads"][0]
    )
    assert (await (await client.get("/model_crud/uploads")).json()) == {"uploads": []}
    assert (await client.delete(f"{base}/{session['upload_id']}")).status == 200
    assert list(output_dir.glob(".model_crud_*")) == []


async def test_model_routes_are_unchanged_and_do_not_know_the_areas(make_client, folder_paths):
    client = await make_client()
    touch(media_root(folder_paths, "output") / "a.png")

    for url in ("/model_crud/models/output", "/model_crud/models/input"):
        resp = await client.get(url)
        assert resp.status == 404 and (await resp.json())["error"] == "unknown_folder"
    resp = await client.post(
        "/model_crud/uploads", json={"folder": "output", "filename": "b.png", "size": 5}
    )
    assert resp.status == 404 and (await resp.json())["error"] == "unknown_folder"
    folders = await (await client.get("/model_crud/folders")).json()
    assert {"input", "output"}.isdisjoint(f["key"] for f in folders["folders"])
    assert [p.name for p in media_root(folder_paths, "output").iterdir()] == ["a.png"]


async def test_upload_ids_do_not_cross_between_the_kinds_of_route(make_client, folder_paths):
    client = await make_client()
    model = await (
        await client.post(
            "/model_crud/uploads",
            json={"folder": "loras", "filename": "m.safetensors", "size": CHUNK},
        )
    ).json()
    out = await (
        await client.post("/model_crud/output/uploads", json={"filename": "a.png", "size": CHUNK})
    ).json()

    attempts = [
        ("put", f"/model_crud/output/uploads/{model['upload_id']}/chunks/0"),
        ("put", f"/model_crud/input/uploads/{out['upload_id']}/chunks/0"),
        ("put", f"/model_crud/uploads/{out['upload_id']}/chunks/0"),
        ("get", f"/model_crud/uploads/{out['upload_id']}"),
        ("get", f"/model_crud/output/uploads/{model['upload_id']}"),
        ("post", f"/model_crud/uploads/{out['upload_id']}/complete"),
        ("delete", f"/model_crud/uploads/{out['upload_id']}"),
        ("delete", f"/model_crud/input/uploads/{out['upload_id']}"),
    ]
    for method, url in attempts:
        kwargs = {"data": b"x" * CHUNK} if method == "put" else {}
        resp = await getattr(client, method)(url, **kwargs)
        assert resp.status == 404 and (await resp.json())["error"] == "unknown_upload", url


@pytest.mark.parametrize(
    "raw",
    ["../x.png", "%2e%2e%2fx.png", "%2e%2e/x.png", "..%5cx.png", "/etc/passwd", "a%00.png"],
)
async def test_traversal_over_http(make_client, folder_paths, tmp_path, raw):
    client = await make_client()
    outside = touch(tmp_path / "x.png")

    resp = await client.delete(f"/model_crud/input?path={raw}")

    assert resp.status in (400, 415)
    assert (await resp.json())["error"] in {
        "path_outside_root",
        "invalid_filename",
        "bad_request",
        "extension_not_allowed",
    }
    assert outside.exists()


async def test_delete_query_is_validated(make_client, output_dir):
    client = await make_client()
    touch(output_dir / "a.png")

    for query in ("", "?path=", "?path=a.png&path=b.png"):
        resp = await client.delete(f"/model_crud/output{query}")
        assert resp.status == 400 and (await resp.json())["error"] == "bad_request", query
    assert (await client.delete("/model_crud/output?path=nope.png")).status == 404
    assert (await client.delete("/model_crud/output?path=m.safetensors")).status == 415
    assert (output_dir / "a.png").exists()


async def test_other_names_are_not_areas(make_client):
    client = await make_client()

    for name in ("temp", "models", "Input", "inputs", "user"):
        assert (await client.get(f"/model_crud/{name}")).status == 404
        assert (await client.delete(f"/model_crud/{name}?path=a.png")).status == 404


async def test_disabled_area_is_404_on_every_route(make_client, output_dir):
    client = await make_client(make_config(media_folders=("input",)))
    touch(output_dir / "a.png")

    calls = [
        client.get("/model_crud/output"),
        client.delete("/model_crud/output?path=a.png"),
        client.post("/model_crud/output/uploads", json={"filename": "b.png", "size": 5}),
        client.get("/model_crud/output/uploads"),
        client.get("/model_crud/output/uploads/" + "A" * 22),
    ]
    for call in calls:
        resp = await call
        assert resp.status == 404 and (await resp.json())["error"] == "unknown_folder"
    assert [p.name for p in output_dir.iterdir()] == ["a.png"]
    config = await (await client.get("/model_crud/config")).json()
    assert config["media_folders"] == ["input"]


async def test_read_only_blocks_writes_but_not_listing(make_client, output_dir):
    client = await make_client(make_config(read_only=True))
    touch(output_dir / "a.png")

    assert (await client.get("/model_crud/output")).status == 200
    delete = await client.delete("/model_crud/output?path=a.png")
    create = await client.post("/model_crud/output/uploads", json={"filename": "b.png", "size": 5})
    assert delete.status == 403 and (await delete.json())["error"] == "delete_disabled"
    assert create.status == 403 and (await create.json())["error"] == "uploads_disabled"
    assert (output_dir / "a.png").exists()


async def test_conflict_and_overwrite_over_http(make_client, output_dir):
    client = await make_client()
    touch(output_dir / "a.png", b"old")

    resp = await client.post("/model_crud/output/uploads", json={"filename": "a.png", "size": 3})
    assert resp.status == 409 and (await resp.json())["error"] == "file_exists"
    _, done = await upload_over_http(client, "/model_crud/output", "a.png", b"new", overwrite=True)
    assert done.status == 201 and (output_dir / "a.png").read_bytes() == b"new"


async def test_unexpected_errors_are_generic_500s(make_client, output_dir, monkeypatch, caplog):
    client = await make_client()

    def boom(*args):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(media, "list_files", boom)
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        resp = await client.get("/model_crud/output")

    assert resp.status == 500 and "secret detail" not in await resp.text()
    assert "secret detail" in caplog.text


async def test_listing_runs_off_the_event_loop(make_client, output_dir, monkeypatch):
    import threading

    client = await make_client()
    seen = []
    real = media.list_files
    monkeypatch.setattr(
        media, "list_files", lambda *a: (seen.append(threading.current_thread().name), real(*a))[1]
    )

    await client.get("/model_crud/output")

    assert seen and seen[0] != threading.current_thread().name


def test_listing_ten_thousand_files_is_fast(folder_paths, output_dir):
    for i in range(10_000):
        touch(output_dir / f"d{i % 50}" / f"f{i}.png", b"")

    start = time.perf_counter()
    result = media.list_files("output", Config())
    elapsed = time.perf_counter() - start

    assert len(result) == 10_000 and elapsed < 1.0, f"{elapsed:.2f}s"


async def test_a_disabled_areas_session_is_skipped_not_discarded_when_reached_via_a_model_root(
    make_manager, folder_paths, output_dir
):
    inner = output_dir / "checkpoints"
    inner.mkdir()
    folder_paths.folder_names_and_paths["checkpoints"][0].append(str(inner))
    first = make_manager()
    session = await first.create_session(
        None, "p.png", CHUNK, subfolder="checkpoints", area="output"
    )
    first.shutdown()

    off = make_manager(media_folders=("input",))
    await off.start()  # scans output/checkpoints as a model root, but the area is off
    assert off.sessions == {} and len(list(inner.glob(".model_crud_*"))) == 2

    on = make_manager()
    await on.start()
    assert [s.id for s in on.list_sessions("output")] == [session.id]


async def test_completing_a_media_upload_leaves_the_filename_cache_alone(
    make_manager, folder_paths
):
    folder_paths.filename_list_cache.update({"loras": 1, "output": 2, "input": 3})
    manager = make_manager()

    session = await manager.create_session(None, "a.png", CHUNK, area="output")
    await send_all(manager, session, payload(CHUNK), "output")
    await manager.complete(session.id, None, "output")

    assert folder_paths.filename_list_cache == {"loras": 1, "output": 2, "input": 3}
