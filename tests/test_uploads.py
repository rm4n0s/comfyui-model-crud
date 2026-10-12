import asyncio
import logging
import os
import random
import stat
from pathlib import Path

import pytest
from comfyui_model_crud.services import fsutil, models, uploads
from comfyui_model_crud.services.errors import (
    BadChunk,
    BadRequest,
    ExtensionNotAllowed,
    FileAlreadyExists,
    IncompleteUpload,
    InsufficientStorage,
    InvalidFilename,
    PathOutsideRoot,
    TooLarge,
    TooManyUploads,
    UnknownFolder,
    UnknownUpload,
    UploadInProgress,
    UploadsDisabled,
)
from comfyui_model_crud.services.uploads import UploadManager, missing_ranges, to_ranges
from uploadkit import Body, make_config, payload

CHUNK = 1024


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


@pytest.fixture
def manager(make_manager) -> UploadManager:
    return make_manager()


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


async def send(manager, session, index: int, data: bytes, **kw):
    start = index * session.chunk_size
    piece = data[start : start + session.chunk_size]
    return await manager.write_chunk(session.id, index, Body(piece, **kw), len(piece))


async def create(manager, size: int, name: str = "m.safetensors", **kw):
    return await manager.create_session("loras", name, size, **kw)


def part_files(directory: Path) -> list[Path]:
    return sorted(directory.glob(".model_crud_*.part"))


# --- creation -------------------------------------------------------------------------------


async def test_create_returns_a_session_and_preallocates_a_hidden_temp_file(manager, loras):
    session = await create(manager, 2500)

    assert session.total_chunks == 3 and session.chunk_size == CHUNK
    assert session.relpath == "m.safetensors" and session.folder == "loras"
    assert len(session.id) == 22
    [part] = part_files(loras)
    assert part.name == f".model_crud_{session.id}.part"
    assert part.stat().st_size == 2500
    assert not (loras / "m.safetensors").exists()  # nothing visible until completion


async def test_upload_ids_are_unguessable_and_distinct(manager):
    ids = set()
    for i in range(4):
        session = await create(manager, 10, name=f"m{i}.safetensors")
        ids.add(session.id)
    assert len(ids) == 4
    assert all(uploads.UPLOAD_ID_RE.fullmatch(i) for i in ids)


async def test_filename_is_sanitized_and_subfolder_is_created(manager, loras):
    session = await create(
        manager, 10, name="../../évil dir/Süß 日本.safetensors", subfolder="a/b c"
    )

    assert session.relpath == "a/b c/Süß 日本.safetensors"
    assert (loras / "a" / "b c").is_dir()
    assert part_files(loras / "a" / "b c")


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"size": 0}, BadRequest),
        ({"size": -5}, BadRequest),
        ({"size": True}, BadRequest),
        ({"size": 10.0}, BadRequest),
        ({"size": "10"}, BadRequest),
        ({"size": None}, BadRequest),
        ({"size": 10**9}, TooLarge),
        ({"filename": "m.exe"}, ExtensionNotAllowed),
        ({"filename": "m.safetensors.exe"}, ExtensionNotAllowed),
        ({"filename": ".hidden.safetensors"}, InvalidFilename),
        ({"filename": ""}, InvalidFilename),
        ({"filename": 5}, InvalidFilename),
        ({"folder": "nope"}, UnknownFolder),
        ({"folder": "custom_nodes"}, UnknownFolder),
        ({"subfolder": "../x"}, PathOutsideRoot),
        ({"subfolder": "/etc"}, PathOutsideRoot),
        ({"subfolder": "a/b/c/d/e"}, BadRequest),
        ({"overwrite": "yes"}, BadRequest),
        ({"overwrite": 1}, BadRequest),
        ({"client_fingerprint": "x" * 513}, BadRequest),
        ({"client_fingerprint": "a\nb"}, BadRequest),
        ({"client_fingerprint": 5}, BadRequest),
    ],
)
async def test_create_rejects_bad_input_and_leaves_nothing_behind(
    manager, loras, folder_paths, tmp_path, kwargs, error
):
    (tmp_path / "nodes").mkdir()
    folder_paths.folder_names_and_paths["custom_nodes"] = ([str(tmp_path / "nodes")], set())
    args = {"folder": "loras", "filename": "m.safetensors", "size": 100, **kwargs}

    with pytest.raises(error):
        await manager.create_session(**args)

    assert not manager.sessions and not manager._by_target
    assert list(loras.iterdir()) == []
    assert list((tmp_path / "nodes").iterdir()) == []


async def test_size_cap_is_inclusive(make_manager, loras):
    manager = make_manager(max_upload_bytes=5000)

    await create(manager, 5000)
    with pytest.raises(TooLarge):
        await create(manager, 5001, name="other.safetensors")


async def test_non_model_folder_is_unknown_for_uploads(manager, folder_paths, tmp_path):
    folder_paths.folder_names_and_paths["configs"] = ([str(tmp_path)], [".yaml"])

    with pytest.raises(UnknownFolder):
        await manager.create_session("configs", "x.safetensors", 10)


async def test_subfolder_blocked_by_an_existing_file_is_a_conflict(manager, loras):
    (loras / "sub").write_bytes(b"i am a file")

    with pytest.raises(FileAlreadyExists):
        await create(manager, 10, subfolder="sub")

    assert not manager.sessions


async def test_subfolder_symlink_escaping_the_root_is_refused(manager, loras, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (loras / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathOutsideRoot):
        await create(manager, 10, subfolder="escape")

    assert list(outside.iterdir()) == []


# --- chunks: happy path ---------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 1023, 1024, 1025, 5 * 1024, 5 * 1024 + 7, 40 * 1024 + 1])
async def test_out_of_order_parallel_chunks_give_a_byte_identical_file(manager, loras, size):
    data = payload(size)
    session = await create(manager, size)
    order = list(range(session.total_chunks))
    random.Random(size).shuffle(order)

    await asyncio.gather(*(send(manager, session, i, data) for i in order))
    result = await manager.complete(session.id)

    assert result == {"folder": "loras", "path": "m.safetensors", "size": size}
    assert (loras / "m.safetensors").read_bytes() == data
    assert part_files(loras) == []


async def test_chunk_responses_count_chunks_and_bytes(manager):
    session = await create(manager, 2500)
    data = payload(2500)

    assert await send(manager, session, 2, data) == {
        "index": 2,
        "received_chunks": 1,
        "received_bytes": 2500 - 2048,
    }
    assert await send(manager, session, 0, data) == {
        "index": 0,
        "received_chunks": 2,
        "received_bytes": 452 + 1024,
    }
    assert (await send(manager, session, 1, data))["received_bytes"] == 2500


async def test_resending_a_chunk_is_harmless(manager, loras):
    data = payload(3000)
    session = await create(manager, 3000)

    for i in (0, 1, 2, 1, 1, 0):
        await send(manager, session, i, data)
    await manager.complete(session.id)

    assert (loras / "m.safetensors").read_bytes() == data


async def test_a_resent_chunk_replaces_the_previous_bytes(manager, loras):
    data = payload(2048)
    other = payload(1024, seed=9)
    session = await create(manager, 2048)

    await send(manager, session, 0, other + other)  # wrong content for chunk 0
    await send(manager, session, 0, data)  # corrected
    await send(manager, session, 1, data)
    await manager.complete(session.id)

    assert (loras / "m.safetensors").read_bytes() == data


async def test_chunk_blocks_larger_than_the_read_limit_are_never_requested(manager):
    session = await create(manager, 100)
    asked = []

    class Spy(Body):
        async def iter_chunked(self, n):
            asked.append(n)
            async for piece in super().iter_chunked(n):
                yield piece

    data = payload(100)
    await manager.write_chunk(session.id, 0, Spy(data), 100)

    assert asked == [uploads.READ_BLOCK] and uploads.READ_BLOCK <= 1024 * 1024


async def test_big_chunk_is_written_in_blocks_and_stays_correct(make_manager, loras):
    manager = make_manager(chunk_size=3 * 1024 * 1024)
    data = payload(5 * 1024 * 1024)
    session = await create(manager, len(data))

    for i in (1, 0):
        piece = data[i * session.chunk_size : (i + 1) * session.chunk_size]
        await manager.write_chunk(session.id, i, Body(piece, block=700_000), len(piece))
    await manager.complete(session.id)

    assert (loras / "m.safetensors").read_bytes() == data


async def test_published_file_has_no_executable_bits_and_no_leftovers(manager, loras):
    session = await create(manager, 10)
    await send(manager, session, 0, payload(10))
    await manager.complete(session.id)

    mode = stat.S_IMODE((loras / "m.safetensors").stat().st_mode)
    assert mode & 0o111 == 0
    assert sorted(p.name for p in loras.iterdir()) == ["m.safetensors"]
    assert not manager.sessions and not manager._by_target


async def test_unicode_and_spaces_upload(manager, loras):
    name = "Süß modell 日本 (v2).safetensors"
    data = payload(1500)
    session = await create(manager, 1500, name=name, subfolder="sdxl v2/été")
    for i in range(2):
        await send(manager, session, i, data)
    await manager.complete(session.id)

    assert (loras / "sdxl v2" / "été" / name).read_bytes() == data


async def test_completed_upload_shows_up_in_the_listing_and_invalidates_the_cache(
    manager, folder_paths, loras
):
    folder_paths.filename_list_cache.update({"loras": "stale", "vae": "keep"})
    session = await create(manager, 10)
    await send(manager, session, 0, payload(10))
    assert models.list_models("loras", manager.config.allowed_extensions) == []  # temp is hidden

    await manager.complete(session.id)

    assert [f["path"] for f in models.list_models("loras", manager.config.allowed_extensions)] == [
        "m.safetensors"
    ]
    assert folder_paths.filename_list_cache == {"vae": "keep"}


async def test_completion_logs_one_info_line_without_absolute_paths(manager, tmp_path, caplog):
    session = await create(manager, 10, subfolder="s")
    await send(manager, session, 0, payload(10))

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        await manager.complete(session.id)

    [record] = caplog.records
    message = record.getMessage()
    assert "folder=loras" in message and "path=s/m.safetensors" in message and "size=10" in message
    assert str(tmp_path) not in message


async def test_chunk_activity_pushes_the_expiry_forward(manager, monkeypatch):
    session = await create(manager, 10)
    before = session.expires_at
    monkeypatch.setattr(uploads.time, "time", lambda: before + 1000)

    await send(manager, session, 0, payload(10))

    assert session.expires_at >= before + 1000


# --- chunks: rejected input -----------------------------------------------------------------


@pytest.mark.parametrize("delta", [-1, 1, -1024, 1024, 0])
async def test_content_length_must_match_exactly(manager, loras, delta):
    session = await create(manager, 3000)
    expected = 1024
    data = payload(expected)

    if delta == 0:
        await manager.write_chunk(session.id, 0, Body(data), expected)
        return
    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, 0, Body(data), expected + delta)

    assert not session.received


async def test_last_chunk_must_be_the_remainder(manager):
    session = await create(manager, 2500)

    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, 2, Body(payload(1024)), 1024)
    await manager.write_chunk(session.id, 2, Body(payload(452)), 452)


async def test_missing_content_length_is_rejected(manager):
    session = await create(manager, 100)

    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, 0, Body(payload(100)), None)


@pytest.mark.parametrize("index", [-1, 3, 10**9, "1", 1.0, None, True])
async def test_bad_chunk_index(manager, index):
    session = await create(manager, 2500)

    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, index, Body(b"x"), 1)


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "short",
        "a" * 21,
        "a" * 65,
        "../../etc/passwd",
        "a" * 21 + "\n",
        "!" * 22,
        "é" * 22,
        "a" * 22 + "/..",
        None,
        5,
        b"a" * 22,
    ],
)
async def test_malformed_upload_ids_are_unknown_and_touch_nothing(manager, monkeypatch, bad_id):
    def explode(*args, **kwargs):
        raise AssertionError("the filesystem was touched")

    for name in ("open_existing", "create_exclusive"):
        monkeypatch.setattr(fsutil, name, explode)
    monkeypatch.setattr(os, "unlink", explode)

    with pytest.raises(UnknownUpload):
        await manager.write_chunk(bad_id, 0, Body(b"x"), 1)
    with pytest.raises(UnknownUpload):
        await manager.complete(bad_id)
    with pytest.raises(UnknownUpload):
        await manager.cancel(bad_id)


async def test_well_formed_but_unknown_id(manager):
    for call in (
        lambda: manager.write_chunk("A" * 22, 0, Body(b"x"), 1),
        lambda: manager.complete("A" * 22),
        lambda: manager.cancel("A" * 22),
    ):
        with pytest.raises(UnknownUpload):
            await call()


async def test_body_cut_short_is_not_recorded_and_can_be_resent(manager, loras):
    data = payload(2048)
    session = await create(manager, 2048)

    with pytest.raises(BadChunk):
        await send(manager, session, 0, data, fail_after=600)  # connection drops mid-body
    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, 1, Body(data[:700]), 1024)  # ends early, no error

    assert not session.received
    with pytest.raises(IncompleteUpload) as exc:
        await manager.complete(session.id)
    assert exc.value.details["missing_ranges"] == [[0, 1]]

    for i in range(2):
        await send(manager, session, i, data)
    await manager.complete(session.id)
    assert (loras / "m.safetensors").read_bytes() == data


async def test_body_longer_than_declared_is_rejected(manager):
    session = await create(manager, 2048)

    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, 0, Body(payload(1500)), 1024)

    assert not session.received


async def test_stalled_body_times_out(manager, monkeypatch):
    monkeypatch.setattr(uploads, "BLOCK_TIMEOUT", 0.05)
    session = await create(manager, 100)

    class Stalled:
        async def iter_chunked(self, n):
            yield b"x" * 10
            await asyncio.sleep(5)
            yield b"y"

    with pytest.raises(BadChunk, match="Timed out"):
        await manager.write_chunk(session.id, 0, Stalled(), 100)

    assert not session.received and session.active_writers == 0


async def test_chunk_writes_never_leak_file_descriptors(manager):
    data = payload(4096)
    session = await create(manager, 4096)
    before = len(os.listdir("/proc/self/fd"))

    for i in range(4):
        await send(manager, session, i, data)
    with pytest.raises(BadChunk):
        await send(manager, session, 0, data, fail_after=10)
    await send(manager, session, 0, data)  # a failed re-send un-records the chunk
    await manager.complete(session.id)

    assert len(os.listdir("/proc/self/fd")) <= before


# --- limits and conflicts -------------------------------------------------------------------


async def test_session_limit_is_enforced_and_freed_by_cancel(make_manager, loras):
    manager = make_manager(max_active_uploads=2)
    first = await create(manager, 10, name="a.safetensors")
    await create(manager, 10, name="b.safetensors")

    with pytest.raises(TooManyUploads):
        await create(manager, 10, name="c.safetensors")
    assert len(part_files(loras)) == 2

    await manager.cancel(first.id)
    await create(manager, 10, name="c.safetensors")


async def test_second_session_for_the_same_target_is_a_conflict(manager):
    await create(manager, 10)

    with pytest.raises(UploadInProgress):
        await create(manager, 10)
    with pytest.raises(UploadInProgress):  # different spelling, same file
        await manager.create_session("loras", "dir/../m.safetensors", 10)


async def test_same_name_in_different_folders_is_fine(manager):
    await create(manager, 10)
    await create(manager, 10, subfolder="other")
    await manager.create_session("vae", "m.safetensors", 10)


async def test_racing_creates_for_one_target_only_one_wins(manager, loras):
    results = await asyncio.gather(*(create(manager, 10) for _ in range(5)), return_exceptions=True)

    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(isinstance(r, UploadInProgress) for r in results if isinstance(r, Exception))
    assert len(part_files(loras)) == 1


async def test_existing_file_is_a_conflict_unless_overwrite(manager, loras):
    (loras / "m.safetensors").write_bytes(b"old")

    with pytest.raises(FileAlreadyExists):
        await create(manager, 10)
    assert (loras / "m.safetensors").read_bytes() == b"old"
    assert part_files(loras) == [] and not manager.sessions

    session = await create(manager, 10, overwrite=True)
    assert session.overwrite


async def test_existing_directory_with_the_target_name_is_always_a_conflict(manager, loras):
    (loras / "m.safetensors").mkdir()

    for overwrite in (False, True):
        with pytest.raises(FileAlreadyExists):
            await create(manager, 10, overwrite=overwrite)


async def test_overwrite_replaces_the_file_atomically(manager, loras):
    (loras / "m.safetensors").write_bytes(b"old")
    data = payload(3000)
    session = await create(manager, 3000, overwrite=True)
    for i in range(3):
        await send(manager, session, i, data)

    assert (loras / "m.safetensors").read_bytes() == b"old"  # untouched until the end
    await manager.complete(session.id)

    assert (loras / "m.safetensors").read_bytes() == data
    assert part_files(loras) == []


async def test_a_file_created_during_the_upload_is_never_clobbered(manager, loras):
    session = await create(manager, 10)
    await send(manager, session, 0, payload(10))
    (loras / "m.safetensors").write_bytes(b"appeared meanwhile")  # the completion race

    with pytest.raises(FileAlreadyExists):
        await manager.complete(session.id)

    assert (loras / "m.safetensors").read_bytes() == b"appeared meanwhile"
    assert session.id in manager.sessions and part_files(loras)  # kept, state restored
    assert session.state == uploads.OPEN

    (loras / "m.safetensors").unlink()  # the user resolves the conflict, then retries
    await manager.complete(session.id)
    assert (loras / "m.safetensors").read_bytes() == payload(10)


async def test_concurrent_completions_publish_once(manager, loras):
    session = await create(manager, 10)
    await send(manager, session, 0, payload(10))

    results = await asyncio.gather(
        manager.complete(session.id), manager.complete(session.id), return_exceptions=True
    )

    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, UploadInProgress | UnknownUpload) for r in results) == 1
    assert (loras / "m.safetensors").read_bytes() == payload(10)


async def test_incomplete_upload_reports_missing_ranges(manager, loras):
    data = payload(10 * CHUNK)
    session = await create(manager, 10 * CHUNK)
    for i in (0, 1, 2, 5, 9):
        await send(manager, session, i, data)

    with pytest.raises(IncompleteUpload) as exc:
        await manager.complete(session.id)

    assert exc.value.status == 409
    assert exc.value.to_body()["missing_ranges"] == [[3, 4], [6, 8]]
    assert not (loras / "m.safetensors").exists() and session.state == uploads.OPEN


async def test_nothing_received_means_everything_is_missing(manager):
    session = await create(manager, 3 * CHUNK)

    with pytest.raises(IncompleteUpload) as exc:
        await manager.complete(session.id)
    assert exc.value.details["missing_ranges"] == [[0, 2]]


async def test_complete_is_refused_while_a_chunk_is_in_flight_and_vice_versa(manager):
    data = payload(2048)
    session = await create(manager, 2048)
    await send(manager, session, 0, data)
    release = asyncio.Event()

    class Slow:
        async def iter_chunked(self, n):
            yield data[1024:1500]
            await release.wait()
            yield data[1500:]

    writer = asyncio.create_task(manager.write_chunk(session.id, 1, Slow(), 1024))
    await asyncio.sleep(0.05)
    with pytest.raises(UploadInProgress):
        await manager.complete(session.id)
    release.set()
    await writer

    await manager.complete(session.id)
    with pytest.raises(UnknownUpload):  # the session is gone once published
        await send(manager, session, 0, data)


async def test_chunks_during_finalisation_are_refused(manager, monkeypatch):
    data = payload(1024)
    session = await create(manager, 1024)
    await send(manager, session, 0, data)
    started, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    real = manager._publish

    def slow_publish(*args):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(5)
        real(*args)

    monkeypatch.setattr(manager, "_publish", slow_publish)
    completing = asyncio.create_task(manager.complete(session.id))
    await started.wait()

    with pytest.raises(UploadInProgress):
        await send(manager, session, 0, data)  # a re-send must not touch the file being published
    with pytest.raises(UploadInProgress):
        await manager.cancel(session.id)
    release.set()
    await completing


async def test_two_senders_of_the_same_chunk_cannot_interleave(make_manager, loras):
    manager = make_manager(chunk_size=3 * 1024 * 1024)
    size = 3 * 1024 * 1024
    a, b = b"A" * size, b"B" * size
    session = await create(manager, size)
    a_paused = asyncio.Event()
    resume_a = asyncio.Event()

    class PausingA:
        async def iter_chunked(self, n):
            yield a[: size // 2]
            a_paused.set()
            await resume_a.wait()  # B gets its chance to write while A is half way
            yield a[size // 2 :]

    first = asyncio.create_task(manager.write_chunk(session.id, 0, PausingA(), size))
    await a_paused.wait()
    second = asyncio.create_task(manager.write_chunk(session.id, 0, Body(b, block=400_000), size))
    await asyncio.sleep(0.1)  # without the per-index lock B would write all of itself now
    resume_a.set()
    await asyncio.gather(first, second)
    await manager.complete(session.id)

    result = (loras / "m.safetensors").read_bytes()
    assert result in (a, b)  # one whole chunk, never a mix


async def test_a_directory_swapped_for_a_symlink_during_creation_is_refused(
    manager, loras, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()

    def hostile_makedirs(path, exist_ok=False):  # the race: a link appears where we make a dir
        os.symlink(outside, path)

    monkeypatch.setattr(os, "makedirs", hostile_makedirs)

    with pytest.raises(PathOutsideRoot):
        await create(manager, 10, subfolder="sub")

    assert list(outside.iterdir()) == []  # nothing was created through the link
    assert not manager.sessions


async def test_a_directory_swapped_for_a_symlink_before_completion_is_refused(
    manager, loras, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    session = await create(manager, 10, subfolder="sub")
    await send(manager, session, 0, payload(10))
    for suffix in ("part", "json"):  # the whole upload "moves" behind a symlinked folder
        (loras / "sub" / f".model_crud_{session.id}.{suffix}").rename(
            outside / f".model_crud_{session.id}.{suffix}"
        )
    (loras / "sub").rmdir()
    (loras / "sub").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathOutsideRoot):
        await manager.complete(session.id)

    assert not (outside / "m.safetensors").exists()


async def test_parallel_chunk_requests_per_session_are_capped(make_manager):
    manager = make_manager(max_parallel=2)
    session = await create(manager, 5 * CHUNK)
    release = asyncio.Event()

    class Held:
        def __init__(self, n):
            self.n = n

        async def iter_chunked(self, n):
            await release.wait()
            yield b"z" * self.n

    tasks = [
        asyncio.create_task(manager.write_chunk(session.id, i, Held(CHUNK), CHUNK)) for i in (0, 1)
    ]
    await asyncio.sleep(0.05)
    with pytest.raises(TooManyUploads):
        await manager.write_chunk(session.id, 2, Held(CHUNK), CHUNK)
    release.set()
    await asyncio.gather(*tasks)
    await manager.write_chunk(session.id, 2, Body(b"z" * CHUNK), CHUNK)  # capacity is back


# --- cancel ---------------------------------------------------------------------------------


async def test_cancel_removes_the_temp_file_and_the_session(manager, loras):
    session = await create(manager, 2048)
    await send(manager, session, 0, payload(2048))

    result = await manager.cancel(session.id)

    assert result == {"upload_id": session.id, "cancelled": True}
    assert list(loras.iterdir()) == []
    assert not manager.sessions and not manager._by_target
    with pytest.raises(UnknownUpload):
        await manager.cancel(session.id)
    await create(manager, 10)  # the target is free again


async def test_chunk_that_arrives_after_cancel_fails_and_leaves_no_file(manager, loras):
    session = await create(manager, 2048)
    release = asyncio.Event()
    data = payload(2048)

    class Slow:
        async def iter_chunked(self, n):
            yield data[:500]
            await release.wait()
            yield data[500:1024]

    writer = asyncio.create_task(manager.write_chunk(session.id, 0, Slow(), 1024))
    await asyncio.sleep(0.05)
    await manager.cancel(session.id)
    release.set()

    with pytest.raises(UnknownUpload):
        await writer
    assert list(loras.iterdir()) == []
    with pytest.raises(UnknownUpload):
        await send(manager, session, 1, data)


# --- disabled, disk full --------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [{"read_only": True}, {"allow_upload": False}])
async def test_disabled_uploads_refuse_every_operation(make_manager, loras, overrides):
    enabled = make_manager()
    session = await create(enabled, 10)
    manager = make_manager(**overrides)
    manager.sessions[session.id] = session  # even a known id is refused

    with pytest.raises(UploadsDisabled):
        await create(manager, 10, name="other.safetensors")
    with pytest.raises(UploadsDisabled):
        await send(manager, session, 0, payload(10))
    with pytest.raises(UploadsDisabled):
        await manager.complete(session.id)
    with pytest.raises(UploadsDisabled):
        await manager.cancel(session.id)

    assert sorted(p.name for p in loras.iterdir()) == [
        f".model_crud_{session.id}.json",
        f".model_crud_{session.id}.part",
    ]


async def test_delete_flag_does_not_disable_uploads(make_manager):
    await create(make_manager(allow_delete=False), 10)


async def test_not_enough_free_space_fails_at_creation_and_leaves_nothing(
    manager, loras, monkeypatch
):
    monkeypatch.setattr(fsutil, "free_bytes", lambda d: 500)

    with pytest.raises(InsufficientStorage) as exc:
        await create(manager, 100)

    assert exc.value.status == 507
    assert list(loras.iterdir()) == [] and not manager.sessions and not manager._by_target


async def test_disk_filling_up_during_preallocation_cleans_up(manager, loras, monkeypatch):
    def full(fd, offset, size):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "posix_fallocate", full)

    with pytest.raises(InsufficientStorage):
        await create(manager, 100)

    assert list(loras.iterdir()) == [] and not manager.sessions


async def test_filesystem_without_fallocate_still_works_with_a_sparse_file(
    manager, loras, monkeypatch
):
    import errno

    def unsupported(fd, offset, size):
        raise OSError(errno.EOPNOTSUPP, "nope")

    monkeypatch.setattr(os, "posix_fallocate", unsupported)
    data = payload(2048)

    session = await create(manager, 2048)
    assert part_files(loras)[0].stat().st_size == 2048
    for i in range(2):
        await send(manager, session, i, data)
    await manager.complete(session.id)

    assert (loras / "m.safetensors").read_bytes() == data


async def test_disk_full_while_writing_a_chunk_is_507_and_not_recorded(manager, monkeypatch):
    session = await create(manager, 1024)

    def full(fd, data, offset):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "pwrite", full)

    with pytest.raises(InsufficientStorage):
        await send(manager, session, 0, payload(1024))

    assert not session.received


async def test_unwritable_directory_is_permission_denied(manager, loras):
    from comfyui_model_crud.services.errors import PermissionDenied

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    loras.chmod(0o555)
    try:
        with pytest.raises(PermissionDenied):
            await create(manager, 10)
    finally:
        loras.chmod(0o755)

    assert not manager.sessions


async def test_failed_creation_frees_the_slot(make_manager, loras, monkeypatch):
    manager = make_manager(max_active_uploads=1)
    with monkeypatch.context() as scoped:
        scoped.setattr(fsutil, "free_bytes", lambda d: 0)
        with pytest.raises(InsufficientStorage):
            await create(manager, 10)

    await create(manager, 10)


# --- helpers --------------------------------------------------------------------------------


def test_to_ranges_merges_and_sorts():
    assert to_ranges([]) == []
    assert to_ranges([5]) == [[5, 5]]
    assert to_ranges([3, 1, 2, 7, 5, 6, 2]) == [[1, 3], [5, 7]]


def test_missing_ranges_is_the_complement():
    assert missing_ranges([], 3) == [[0, 2]]
    assert missing_ranges([0, 1, 2], 3) == []
    assert missing_ranges([1], 4) == [[0, 0], [2, 3]]


def test_ranges_property_with_random_orders():
    rng = random.Random(7)
    for _ in range(300):
        total = rng.randint(1, 60)
        have = {i for i in range(total) if rng.random() < 0.5}
        got, gaps = to_ranges(have), missing_ranges(have, total)

        covered = {i for a, b in got for i in range(a, b + 1)}
        holes = {i for a, b in gaps for i in range(a, b + 1)}
        assert covered == have and holes == set(range(total)) - have
        for ranges in (got, gaps):
            assert all(a <= b for a, b in ranges)
            assert all(
                prev[1] + 1 < nxt[0] for prev, nxt in zip(ranges, ranges[1:], strict=False)
            )  # no touching


async def test_received_bytes_accounting(manager):
    session = await create(manager, 2 * CHUNK + 10)
    data = payload(2 * CHUNK + 10)

    await send(manager, session, 2, data)
    assert session.received_bytes == 10
    await send(manager, session, 0, data)
    assert session.received_bytes == CHUNK + 10
    await send(manager, session, 1, data)
    assert session.received_bytes == session.size
