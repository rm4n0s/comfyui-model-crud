import asyncio
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path

import pytest
from comfyui_model_crud.services import uploads
from comfyui_model_crud.services.errors import (
    BadChunk,
    BadRequest,
    ChecksumMismatch,
    IncompleteUpload,
    InsufficientStorage,
    UnknownUpload,
    UploadsDisabled,
)
from comfyui_model_crud.services.uploads import UploadManager, to_ranges
from uploadkit import Body, make_config, payload

CHUNK = 1024
HOUR = 3600


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
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


async def create(manager, size: int, name: str = "m.safetensors", **kw):
    return await manager.create_session("loras", name, size, **kw)


async def send(manager, session, index: int, data: bytes, sha256=None, **kw):
    start = index * session.chunk_size
    piece = data[start : start + session.chunk_size]
    return await manager.write_chunk(session.id, index, Body(piece, **kw), len(piece), sha256)


def sidecar(directory: Path, session) -> dict:
    return json.loads((directory / f".model_crud_{session.id}.json").read_text())


def artifacts(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob(".model_crud_*"))


async def restart(make_manager, **overrides) -> UploadManager:
    """A new process, as far as the upload code can tell: fresh manager, scan the disk."""
    manager = make_manager(**overrides)
    await manager.start()
    return manager


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- sidecar persistence --------------------------------------------------------------------


async def test_sidecar_follows_every_recorded_chunk(make_manager, loras):
    manager = make_manager()
    data = payload(3 * CHUNK)
    session = await create(manager, 3 * CHUNK, client_fingerprint="m.safetensors:3072:1")

    assert sidecar(loras, session)["received"] == []
    for index, expected in [(2, [[2, 2]]), (0, [[0, 0], [2, 2]]), (1, [[0, 2]])]:
        await send(manager, session, index, data)
        assert sidecar(loras, session)["received"] == expected

    saved = sidecar(loras, session)
    assert saved["upload_id"] == session.id and saved["version"] == 1
    assert saved["folder"] == "loras" and saved["path"] == "m.safetensors"
    assert saved["size"] == 3 * CHUNK and saved["chunk_size"] == CHUNK
    assert saved["overwrite"] is False and saved["client_fingerprint"] == "m.safetensors:3072:1"
    assert saved["expires_at"] == pytest.approx(session.expires_at)
    assert not list(loras.glob("*.tmp"))  # the atomic-replace temp never lingers


async def test_sidecar_never_contains_absolute_paths(make_manager, loras, tmp_path):
    manager = make_manager()
    session = await create(manager, CHUNK, subfolder="a/b")

    text = (loras / "a" / "b" / f".model_crud_{session.id}.json").read_text()

    assert str(tmp_path) not in text and "a/b/m.safetensors" in text


async def test_sidecar_is_fsynced_before_it_replaces_the_old_one(make_manager, loras, monkeypatch):
    manager = make_manager()
    session = await create(manager, 2 * CHUNK)
    events = []
    real_replace, real_fsync = os.replace, os.fsync

    def replace(src, dst):
        events.append(("replace", os.path.basename(src), os.path.basename(dst)))
        real_replace(src, dst)

    def fsync(fd):
        events.append(("fsync", os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))))
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)

    await send(manager, session, 0, payload(2 * CHUNK))

    tmp, final = f".model_crud_{session.id}.json.tmp", f".model_crud_{session.id}.json"
    assert events.index(("fsync", tmp)) < events.index(("replace", tmp, final))
    # and the chunk's own data was made durable before the sidecar vouched for it
    assert events.index(("fsync", f".model_crud_{session.id}.part")) < events.index(("fsync", tmp))


async def test_saving_a_closed_session_never_recreates_its_sidecar(make_manager, loras):
    manager = make_manager()
    session = await create(manager, CHUNK)
    manager._forget(session)
    manager._remove_files(session.directory, session.id)
    session.version += 1

    await manager._save(session)

    assert artifacts(loras) == []


async def test_parallel_chunks_end_with_a_sidecar_covering_all_of_them(make_manager, loras):
    manager = make_manager()
    data = payload(20 * CHUNK)
    session = await create(manager, len(data))
    order = list(range(20))
    random.Random(2).shuffle(order)

    await asyncio.gather(*(send(manager, session, i, data) for i in order))

    assert sidecar(loras, session)["received"] == [[0, 19]]
    assert session.saved_version == session.version


async def test_failing_sidecar_write_is_reported_and_retried_by_the_next_chunk(
    make_manager, loras, monkeypatch
):
    import errno

    manager = make_manager()
    data = payload(2 * CHUNK)
    session = await create(manager, 2 * CHUNK)
    real = os.replace
    monkeypatch.setattr(
        os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(errno.ENOSPC, "x"))
    )

    with pytest.raises(InsufficientStorage):
        await send(manager, session, 0, data)

    monkeypatch.setattr(os, "replace", real)
    await send(manager, session, 1, data)
    assert sidecar(loras, session)["received"] == [[0, 1]]  # the later save carried chunk 0 too


# --- surviving a restart --------------------------------------------------------------------


async def test_restart_mid_upload_then_finish(make_manager, loras):
    data = payload(10 * CHUNK + 5)
    first = make_manager()
    session = await create(first, len(data), client_fingerprint="fp")
    for i in (0, 3, 4, 10):
        await send(first, session, i, data)
    first.shutdown()  # the process dies: no cleanup, no goodbye

    second = await restart(make_manager)
    [resumed] = second.list_sessions()

    assert resumed.id == session.id and resumed.client_fingerprint == "fp"
    assert to_ranges(resumed.received) == [[0, 0], [3, 4], [10, 10]]
    assert resumed.chunk_size == CHUNK and resumed.size == len(data)
    assert resumed.created == pytest.approx(session.created)
    assert resumed.expires_at == pytest.approx(session.expires_at)
    with pytest.raises(IncompleteUpload) as exc:
        await second.complete(session.id)
    assert exc.value.details["missing_ranges"] == [[1, 2], [5, 9]]

    for i in (1, 2, 5, 6, 7, 8, 9):  # only the gaps are sent
        await send(second, resumed, i, data)
    result = await second.complete(session.id)

    assert result["size"] == len(data)
    assert (loras / "m.safetensors").read_bytes() == data
    assert artifacts(loras) == []


async def test_a_resumed_session_keeps_its_own_chunk_size_and_flags(make_manager, loras):
    data = payload(5 * CHUNK)
    first = make_manager(chunk_size=CHUNK)
    (loras / "m.safetensors").write_bytes(b"old")
    session = await create(first, len(data), overwrite=True)
    await send(first, session, 1, data)

    second = await restart(make_manager, chunk_size=2 * CHUNK)  # config changed meanwhile
    [resumed] = second.list_sessions()

    assert resumed.chunk_size == CHUNK and resumed.total_chunks == 5 and resumed.overwrite is True
    for i in (0, 2, 3, 4):
        await send(second, resumed, i, data)
    await second.complete(session.id)
    assert (loras / "m.safetensors").read_bytes() == data


async def test_resume_works_for_subfolder_uploads(make_manager, loras):
    data = payload(3 * CHUNK)
    first = make_manager()
    session = await create(first, len(data), subfolder="sdxl/v2", name="Süß 日本.safetensors")
    await send(first, session, 1, data)

    second = await restart(make_manager)
    [resumed] = second.list_sessions()
    await send(second, resumed, 0, data)
    await send(second, resumed, 2, data)
    await second.complete(session.id)

    assert (loras / "sdxl" / "v2" / "Süß 日本.safetensors").read_bytes() == data


async def test_sidecar_that_is_behind_the_data_is_safe(make_manager, loras):
    """Crash after chunk bytes were durable but before the sidecar caught up."""
    data = payload(3 * CHUNK)
    first = make_manager()
    session = await create(first, len(data))
    await send(first, session, 0, data)
    stale = (loras / f".model_crud_{session.id}.json").read_bytes()
    await send(first, session, 1, data)
    (loras / f".model_crud_{session.id}.json").write_bytes(stale)  # roll the sidecar back

    second = await restart(make_manager)
    [resumed] = second.list_sessions()

    assert to_ranges(resumed.received) == [[0, 0]]  # behind is fine: chunk 1 is simply re-sent
    for i in (1, 2):
        await send(second, resumed, i, data)
    await second.complete(session.id)
    assert (loras / "m.safetensors").read_bytes() == data


async def test_random_crash_points_never_lose_or_invent_chunks(make_manager, loras, tmp_path):
    rng = random.Random(11)
    for round_no in range(12):
        total = rng.randint(2, 9)
        data = payload(total * CHUNK - rng.randint(0, CHUNK - 1), seed=round_no)
        name = f"r{round_no}.safetensors"
        manager = make_manager()
        session = await create(manager, len(data), name=name)
        acknowledged = set()
        for index in rng.sample(range(total), rng.randint(0, total)):
            await send(manager, session, index, data)
            acknowledged.add(index)
        manager.shutdown()

        resumed_manager = await restart(make_manager)
        resumed = resumed_manager.sessions[session.id]
        assert resumed.received == acknowledged

        for index in sorted(set(range(total)) - acknowledged):
            await send(resumed_manager, resumed, index, data)
        await resumed_manager.complete(session.id)
        assert (loras / name).read_bytes() == data
        for stale in list(resumed_manager.sessions):
            await resumed_manager.cancel(stale)


async def test_restart_with_nothing_to_resume_is_a_no_op(make_manager, loras):
    (loras / "keep.safetensors").write_bytes(b"x")

    manager = await restart(make_manager)

    assert manager.list_sessions() == []
    assert (loras / "keep.safetensors").read_bytes() == b"x"


async def test_start_does_nothing_when_uploads_are_disabled(make_manager, loras):
    enabled = make_manager()
    session = await create(enabled, CHUNK)
    old = time.time() - 10 * HOUR
    for name in (f".model_crud_{session.id}.part", f".model_crud_{session.id}.json"):
        os.utime(loras / name, (old, old))

    disabled = await restart(make_manager, read_only=True)

    assert disabled.sessions == {} and len(artifacts(loras)) == 2
    with pytest.raises(UploadsDisabled):
        disabled.list_sessions()
    with pytest.raises(UploadsDisabled):
        disabled.status(session.id)


async def test_start_runs_twice_without_duplicating(make_manager, loras):
    first = make_manager()
    await create(first, CHUNK)

    second = make_manager()
    await second.start()
    await second.stop()

    assert len(second.sessions) == 1


# --- sidecars that cannot be trusted --------------------------------------------------------


def tamper(directory: Path, session, mutate):
    path = directory / f".model_crud_{session.id}.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


# Not listed: renaming the target within the same directory. Whoever can edit a sidecar can already
# write to that directory, and the file name is not recorded anywhere else to check against.
TAMPERINGS = {
    "path_traversal": lambda d: d.update(path="../../escape.safetensors"),
    "absolute_path": lambda d: d.update(path="/etc/escape.safetensors"),
    "backslash": lambda d: d.update(path="..\\escape.safetensors"),
    "other_subfolder": lambda d: d.update(path="elsewhere/m.safetensors"),
    "disallowed_extension": lambda d: d.update(path="m.exe"),
    "other_folder": lambda d: d.update(folder="vae"),
    "unknown_folder": lambda d: d.update(folder="nope"),
    "non_model_folder": lambda d: d.update(folder="custom_nodes"),
    "wrong_version": lambda d: d.update(version=2),
    "wrong_id": lambda d: d.update(upload_id="A" * 22),
    "size_mismatch": lambda d: d.update(size=d["size"] + 1),
    "size_zero": lambda d: d.update(size=0),
    "size_not_int": lambda d: d.update(size="3072"),
    "size_over_cap": lambda d: d.update(size=10**15),
    "chunk_size_zero": lambda d: d.update(chunk_size=0),
    "chunk_size_huge": lambda d: d.update(chunk_size=10**12),
    "implausible_chunk_count": lambda d: d.update(chunk_size=1, size=2 * 10**6),
    "overwrite_not_bool": lambda d: d.update(overwrite="yes"),
    "fingerprint_bad": lambda d: d.update(client_fingerprint="a\nb"),
    "expired": lambda d: d.update(expires_at=1.0),
    "expires_nan": lambda d: d.update(expires_at="soon"),
    "created_missing": lambda d: d.pop("created"),
    "ranges_out_of_bounds": lambda d: d.update(received=[[0, 99]]),
    "ranges_negative": lambda d: d.update(received=[[-1, 0]]),
    "ranges_reversed": lambda d: d.update(received=[[2, 1]]),
    "ranges_not_ints": lambda d: d.update(received=[["a", "b"]]),
    "ranges_wrong_shape": lambda d: d.update(received=[[1, 2, 3]]),
    "ranges_not_list": lambda d: d.update(received="all"),
    "ranges_too_many": lambda d: d.update(received=[[0, 0]] * 50),
}


@pytest.mark.parametrize("name", sorted(TAMPERINGS))
async def test_tampered_sidecars_are_discarded_and_cleaned_up(
    make_manager, loras, folder_paths, tmp_path, name
):
    data = payload(3 * CHUNK)
    first = make_manager()
    session = await create(first, len(data))
    await send(first, session, 0, data)
    first.shutdown()
    canaries = [loras / "keep.safetensors", tmp_path / "escape.safetensors"]
    for canary in canaries:
        canary.write_bytes(b"precious")
    (tmp_path / "models" / "escape.safetensors").write_bytes(b"precious")
    tamper(loras, session, TAMPERINGS[name])

    manager = await restart(make_manager)

    assert manager.sessions == {} and manager._by_target == {}
    assert artifacts(loras) == []  # our own temp and sidecar are removed
    assert all(c.read_bytes() == b"precious" for c in canaries)
    assert (tmp_path / "models" / "escape.safetensors").read_bytes() == b"precious"
    assert not list(tmp_path.rglob("m.safetensors"))  # and nothing was published


@pytest.mark.parametrize(
    "damage",
    [
        lambda part, js: part.unlink(),
        lambda part, js: part.write_bytes(b"short"),
        lambda part, js: (part.unlink(), part.symlink_to(js)),
        lambda part, js: js.write_bytes(b"{not json"),
        lambda part, js: js.write_bytes(b""),
        lambda part, js: js.write_bytes(b"[1, 2, 3]"),
        lambda part, js: js.write_bytes(b"null"),
        lambda part, js: js.write_bytes(b" " * (uploads.MAX_SIDECAR_BYTES + 10)),
        lambda part, js: js.write_bytes(b"\xff\xfe\x00"),
        lambda part, js: (js.unlink(), js.symlink_to(part)),
    ],
    ids=[
        "no_part",
        "short_part",
        "part_is_symlink",
        "bad_json",
        "empty",
        "list",
        "null",
        "huge",
        "binary",
        "sidecar_is_symlink",
    ],
)
async def test_damaged_session_files_are_discarded(make_manager, loras, damage):
    first = make_manager()
    session = await create(first, 3 * CHUNK)
    first.shutdown()
    part = loras / f".model_crud_{session.id}.part"
    js = loras / f".model_crud_{session.id}.json"
    damage(part, js)

    manager = await restart(make_manager)

    assert manager.sessions == {}
    assert artifacts(loras) == []


@pytest.mark.parametrize(("original", "copy"), [("a", "b"), ("b", "a")])  # either scan order
async def test_a_sidecar_copied_to_another_directory_is_not_adopted(
    make_manager, loras, original, copy
):
    for name in ("a", "b"):  # fixed creation order, so the scan visits them in a fixed order
        (loras / name).mkdir()
    first = make_manager()
    session = await create(first, 2 * CHUNK, subfolder=original)
    first.shutdown()
    for suffix in ("part", "json"):  # a copy, so the claimed upload really exists there as well
        (loras / copy / f".model_crud_{session.id}.{suffix}").write_bytes(
            (loras / original / f".model_crud_{session.id}.{suffix}").read_bytes()
        )

    manager = await restart(make_manager)

    [resumed] = manager.sessions.values()
    assert resumed.directory == str(loras / original)  # the one that is where it says it is
    assert len(artifacts(loras / original)) == 2  # its files were not swept away by the copy
    assert artifacts(loras / copy) == []


async def test_a_sidecar_in_a_secondary_root_is_not_adopted(make_manager, folder_paths, tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    first = make_manager()
    session = await first.create_session("controlnet", "m.safetensors", 2 * CHUNK)
    first.shutdown()
    for suffix in ("part", "json"):  # uploads belong in the first root; this one is elsewhere
        os.replace(
            a / f".model_crud_{session.id}.{suffix}", b / f".model_crud_{session.id}.{suffix}"
        )

    manager = await restart(make_manager)

    assert manager.sessions == {} and artifacts(b) == []


async def test_two_sidecars_for_one_target_resume_only_one(make_manager, loras):
    first = make_manager(max_active_uploads=4)
    session = await create(first, 2 * CHUNK)
    first.shutdown()
    twin = "T" * 22
    for suffix in ("part", "json"):
        (loras / f".model_crud_{twin}.{suffix}").write_bytes(
            (loras / f".model_crud_{session.id}.{suffix}").read_bytes()
        )
    tamper(loras, type("S", (), {"id": twin}), lambda d: d.update(upload_id=twin))

    manager = await restart(make_manager)

    assert len(manager.sessions) == 1
    assert len(artifacts(loras)) == 2


async def test_scan_survives_an_unexpected_error_on_one_upload(
    make_manager, loras, monkeypatch, caplog
):
    first = make_manager()
    good = await create(first, CHUNK, name="good.safetensors")
    bad = await create(first, CHUNK, name="bad.safetensors")
    first.shutdown()
    real = UploadManager._restore

    def flaky(self, directory, upload_id, now):
        if upload_id == bad.id:
            raise RuntimeError("boom")
        return real(self, directory, upload_id, now)

    monkeypatch.setattr(UploadManager, "_restore", flaky)

    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        manager = await restart(make_manager)

    assert set(manager.sessions) == {good.id}
    assert (loras / f".model_crud_{bad.id}.part").exists()  # unknown state: left alone
    assert "boom" in caplog.text


# --- orphans --------------------------------------------------------------------------------


def age(path: Path, seconds: float):
    old = time.time() - seconds
    os.utime(path, (old, old), follow_symlinks=False)


async def test_orphans_are_removed_only_when_old(make_manager, loras):
    young_part = loras / ("Y" * 22).join([".model_crud_", ".part"])
    old_part = loras / ("O" * 22).join([".model_crud_", ".part"])
    old_tmp = loras / ("P" * 22).join([".model_crud_", ".json.tmp"])
    for p in (young_part, old_part, old_tmp):
        p.write_bytes(b"x")
    age(old_part, 2 * HOUR)
    age(old_tmp, 2 * HOUR)

    await restart(make_manager)

    assert young_part.exists() and not old_part.exists() and not old_tmp.exists()


async def test_unrelated_files_are_never_touched(make_manager, loras, tmp_path):
    keep = [
        loras / ".model_crud_short.part",  # not a valid id
        loras / ".model_crud_" + "x" * 22 if False else loras / ("." + "model_crud_" + "x" * 22),
        loras / f".model_crud_{'Z' * 22}.txt",
        loras / "model_crud_" + "Z" * 22 if False else loras / ("model_crud_" + "Z" * 22 + ".part"),
        loras / ".other_dotfile",
        loras / "m.safetensors",
    ]
    for p in keep:
        p.write_bytes(b"x")
        age(p, 5 * HOUR)

    await restart(make_manager)

    assert all(p.exists() for p in keep)


async def test_scan_ignores_hidden_deep_symlinked_and_non_model_directories(
    make_manager, loras, folder_paths, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    victims = {
        "hidden": loras / ".hidden",
        "deep": loras / "1" / "2" / "3" / "4" / "5",
        "linked": outside,
        "nodes": tmp_path / "nodes",
    }
    (loras / "linkdir").symlink_to(outside, target_is_directory=True)
    folder_paths.folder_names_and_paths["custom_nodes"] = ([str(victims["nodes"])], set())
    for directory in victims.values():
        directory.mkdir(parents=True, exist_ok=True)
        orphan = directory / f".model_crud_{'Q' * 22}.part"
        orphan.write_bytes(b"x")
        age(orphan, 5 * HOUR)

    await restart(make_manager)

    for name, directory in victims.items():
        assert (directory / f".model_crud_{'Q' * 22}.part").exists(), name


async def test_deepest_allowed_subfolder_is_scanned(make_manager, loras):
    first = make_manager()
    session = await create(first, CHUNK, subfolder="a/b/c/d")
    first.shutdown()

    manager = await restart(make_manager)

    assert session.id in manager.sessions


# --- expiry ---------------------------------------------------------------------------------


async def test_expire_idle_removes_session_and_both_files(make_manager, loras):
    manager = make_manager(upload_ttl_hours=1)
    session = await create(manager, CHUNK)
    await send(manager, session, 0, payload(CHUNK))

    assert await manager.expire_idle(now=time.time() + 30 * 60) == 0
    assert await manager.expire_idle(now=time.time() + 2 * HOUR) == 1

    assert manager.sessions == {} and manager._by_target == {} and artifacts(loras) == []
    await create(manager, CHUNK)  # the target is free again


async def test_chunk_requests_refresh_the_expiry(make_manager):
    manager = make_manager(upload_ttl_hours=1)
    session = await create(manager, 3 * CHUNK)
    data = payload(3 * CHUNK)
    soon = time.time() + 50 * 60

    original = session.expires_at
    real_time = time.time
    uploads.time.time = lambda: soon
    try:
        await send(manager, session, 0, data)
    finally:
        uploads.time.time = real_time

    assert session.expires_at == pytest.approx(soon + HOUR)
    assert session.expires_at > original
    assert await manager.expire_idle(now=real_time() + 90 * 60) == 0  # would have expired before


async def test_failing_chunk_requests_also_count_as_activity(make_manager, monkeypatch):
    manager = make_manager(upload_ttl_hours=1)
    session = await create(manager, 2 * CHUNK)
    original = session.expires_at
    soon = time.time() + 40 * 60
    monkeypatch.setattr(uploads.time, "time", lambda: soon)

    with pytest.raises(BadChunk):
        await manager.write_chunk(session.id, 0, Body(b"x"), 1)  # wrong length is refused first
    assert session.expires_at == original  # validation failures happen before the touch

    with pytest.raises(BadChunk):
        await send(manager, session, 0, payload(2 * CHUNK), fail_after=10)  # reached the body
    assert session.expires_at == pytest.approx(soon + HOUR)


async def test_busy_and_finalising_sessions_are_not_expired(make_manager):
    manager = make_manager(upload_ttl_hours=1)
    busy = await create(manager, CHUNK, name="busy.safetensors")
    finishing = await create(manager, CHUNK, name="finishing.safetensors")
    busy.active_writers = 1
    finishing.state = uploads.COMPLETING

    assert await manager.expire_idle(now=time.time() + 5 * HOUR) == 0
    assert set(manager.sessions) == {busy.id, finishing.id}


async def test_expiry_at_startup_removes_what_expired_while_down(make_manager, loras):
    first = make_manager(upload_ttl_hours=1)
    session = await create(first, CHUNK)
    tamper(loras, session, lambda d: d.update(expires_at=time.time() - 5))
    first.shutdown()

    manager = await restart(make_manager)

    assert manager.sessions == {} and artifacts(loras) == []


async def test_the_background_sweep_runs_and_stops_cleanly(make_manager, loras):
    manager = make_manager(upload_ttl_hours=0.00001)  # ~36 ms
    manager.expiry_interval = 0.05
    session = await create(manager, CHUNK)
    await manager.start()
    task = manager._expiry_task

    for _ in range(100):
        if session.id not in manager.sessions:
            break
        await asyncio.sleep(0.05)
    await manager.stop()

    assert session.id not in manager.sessions and artifacts(loras) == []
    assert task.done() and manager._expiry_task is None
    await manager.stop()  # idempotent


async def test_the_sweep_survives_an_error(make_manager, monkeypatch, caplog):
    manager = make_manager()
    manager.expiry_interval = 0.02
    calls = []

    async def flaky(now=None):
        calls.append(1)
        raise RuntimeError("disk gone")

    monkeypatch.setattr(manager, "expire_idle", flaky)
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        await manager.start()
        await asyncio.sleep(0.15)
        await manager.stop()

    assert len(calls) >= 2 and "Expiring idle uploads failed" in caplog.text


async def test_cancel_and_completion_remove_the_sidecar_too(make_manager, loras):
    manager = make_manager()
    a = await create(manager, CHUNK, name="a.safetensors")
    b = await create(manager, CHUNK, name="b.safetensors")
    await send(manager, b, 0, payload(CHUNK))

    await manager.cancel(a.id)
    assert artifacts(loras) == [f".model_crud_{b.id}.json", f".model_crud_{b.id}.part"]
    await manager.complete(b.id)

    assert artifacts(loras) == [] and sorted(p.name for p in loras.iterdir()) == ["b.safetensors"]


async def test_a_failed_creation_leaves_no_sidecar(make_manager, loras, monkeypatch):
    import errno

    manager = make_manager()
    monkeypatch.setattr(
        os, "posix_fallocate", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "x"))
    )

    with pytest.raises(InsufficientStorage):
        await create(manager, CHUNK)

    assert artifacts(loras) == []


async def test_a_sidecar_write_racing_a_cancel_cannot_resurrect_the_session(make_manager, loras):
    manager = make_manager()
    data = payload(2 * CHUNK)
    session = await create(manager, 2 * CHUNK)
    release = asyncio.Event()

    class Slow:
        async def iter_chunked(self, n):
            yield data[:CHUNK]
            await release.wait()

    writer = asyncio.create_task(manager.write_chunk(session.id, 0, Slow(), CHUNK))
    await asyncio.sleep(0.05)
    await manager.cancel(session.id)
    release.set()
    with pytest.raises(UnknownUpload):
        await writer

    assert artifacts(loras) == []


# --- integrity ------------------------------------------------------------------------------


async def test_matching_chunk_hash_is_accepted_in_any_case(make_manager):
    manager = make_manager()
    data = payload(2 * CHUNK)
    session = await create(manager, 2 * CHUNK)

    await send(manager, session, 0, data, sha256=digest(data[:CHUNK]))
    await send(manager, session, 1, data, sha256=digest(data[CHUNK:]).upper())

    assert session.received == {0, 1}


async def test_hash_is_computed_over_a_multi_block_body(make_manager):
    manager = make_manager(chunk_size=3 * 1024 * 1024)
    data = payload(3 * 1024 * 1024)
    session = await create(manager, len(data))

    await manager.write_chunk(session.id, 0, Body(data, block=700_000), len(data), digest(data))

    assert session.received == {0}


async def test_mismatching_chunk_hash_is_422_and_not_recorded(make_manager, loras):
    manager = make_manager()
    data = payload(2 * CHUNK)
    session = await create(manager, 2 * CHUNK)

    with pytest.raises(ChecksumMismatch) as exc:
        await send(manager, session, 0, data, sha256=digest(b"something else"))

    assert exc.value.status == 422 and exc.value.code == "checksum_mismatch"
    assert session.received == set() and sidecar(loras, session)["received"] == []
    await send(manager, session, 0, data, sha256=digest(data[:CHUNK]))  # the re-send succeeds
    assert session.received == {0}


async def test_a_corrupt_resend_of_a_recorded_chunk_makes_it_missing_again(make_manager, loras):
    manager = make_manager()
    data = payload(2 * CHUNK)
    session = await create(manager, 2 * CHUNK)
    for i in range(2):
        await send(manager, session, i, data)
    corrupt = bytes(reversed(data[:CHUNK]))

    with pytest.raises(ChecksumMismatch):
        await manager.write_chunk(session.id, 0, Body(corrupt), CHUNK, digest(data[:CHUNK]))

    assert session.received == {1} and sidecar(loras, session)["received"] == [[1, 1]]
    with pytest.raises(IncompleteUpload) as exc:
        await manager.complete(session.id)
    assert exc.value.details["missing_ranges"] == [[0, 0]]
    restarted = await restart(make_manager)
    assert restarted.sessions[session.id].received == {1}


async def test_an_aborted_resend_of_a_recorded_chunk_makes_it_missing_again(make_manager, loras):
    manager = make_manager()
    data = payload(2 * CHUNK)
    session = await create(manager, 2 * CHUNK)
    for i in range(2):
        await send(manager, session, i, data)

    with pytest.raises(BadChunk):
        await send(manager, session, 0, payload(2 * CHUNK, seed=77), fail_after=300)

    assert session.received == {1} and sidecar(loras, session)["received"] == [[1, 1]]
    await send(manager, session, 0, data)
    await manager.complete(session.id)
    assert (loras / "m.safetensors").read_bytes() == data


@pytest.mark.parametrize(
    "bad", ["", "abc", "g" * 64, "0" * 63, "0" * 65, " " + "0" * 63, "0" * 64 + "\n"]
)
async def test_malformed_chunk_hash_is_a_bad_request(make_manager, bad):
    manager = make_manager()
    session = await create(manager, CHUNK)

    with pytest.raises(BadRequest):
        await send(manager, session, 0, payload(CHUNK), sha256=bad)

    assert session.received == set()


async def test_whole_file_hash_match_publishes(make_manager, loras):
    manager = make_manager()
    data = payload(4 * CHUNK + 9)
    session = await create(manager, len(data))
    for i in range(session.total_chunks):
        await send(manager, session, i, data)

    await manager.complete(session.id, digest(data).upper())

    assert (loras / "m.safetensors").read_bytes() == data


async def test_whole_file_hash_mismatch_is_422_and_keeps_everything(make_manager, loras):
    manager = make_manager()
    data = payload(3 * CHUNK)
    wrong = data[:CHUNK] + bytes(reversed(data[CHUNK : 2 * CHUNK])) + data[2 * CHUNK :]
    session = await create(manager, len(data))
    for i in range(3):
        await send(manager, session, i, wrong)

    with pytest.raises(ChecksumMismatch):
        await manager.complete(session.id, digest(data))

    assert not (loras / "m.safetensors").exists()
    assert session.id in manager.sessions and session.state == uploads.OPEN
    assert artifacts(loras) == [f".model_crud_{session.id}.json", f".model_crud_{session.id}.part"]
    await send(manager, session, 1, data)  # fix the damaged chunk and try again
    await manager.complete(session.id, digest(data))
    assert (loras / "m.safetensors").read_bytes() == data


@pytest.mark.parametrize("bad", ["", "xyz", "0" * 63, 5, ["0" * 64], "0" * 64 + "\n"])
async def test_malformed_whole_file_hash_is_a_bad_request(make_manager, bad):
    manager = make_manager()
    session = await create(manager, CHUNK)
    await send(manager, session, 0, payload(CHUNK))

    with pytest.raises(BadRequest):
        await manager.complete(session.id, bad)

    assert session.state == uploads.OPEN


async def test_the_whole_file_hash_does_not_block_the_event_loop(make_manager):
    manager = make_manager(chunk_size=4 * 1024 * 1024, max_upload_bytes=64 * 1024**2)
    data = payload(24 * 1024 * 1024)
    session = await create(manager, len(data))
    for i in range(session.total_chunks):
        await manager.write_chunk(
            session.id,
            i,
            Body(data[i * session.chunk_size : (i + 1) * session.chunk_size], block=1 << 20),
            session.expected_length(i),
        )
    ticks = []

    async def heartbeat():
        while True:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.005)

    beat = asyncio.create_task(heartbeat())
    await manager.complete(session.id, digest(data))
    beat.cancel()

    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert gaps and max(gaps) < 0.25


# --- status and listing ---------------------------------------------------------------------


async def test_status_and_list(make_manager):
    manager = make_manager()
    first = await create(manager, 3 * CHUNK, name="a.safetensors")
    second = await create(manager, CHUNK, name="b.safetensors")

    assert manager.status(first.id) is first
    assert [s.id for s in manager.list_sessions()] == [first.id, second.id]
    with pytest.raises(UnknownUpload):
        manager.status("A" * 22)
    with pytest.raises(UnknownUpload):
        manager.status("../etc")


# --- property: restart preserves the acknowledged set ---------------------------------------


async def test_ranges_survive_the_sidecar_round_trip_for_random_sets(make_manager, loras):
    rng = random.Random(5)
    manager = make_manager()
    session = await create(manager, 60 * CHUNK)
    for _ in range(25):
        have = {i for i in range(60) if rng.random() < 0.4}
        session.received = set(have)
        session.version += 1
        await manager._save(session)
        saved = sidecar(loras, session)["received"]

        assert saved == to_ranges(have)
        assert all(a <= b for a, b in saved)
        assert all(x[1] + 1 < y[0] for x, y in zip(saved, saved[1:], strict=False))
