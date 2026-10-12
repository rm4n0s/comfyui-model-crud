import asyncio
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import uploads
from comfyui_model_crud.services.uploads import UploadManager
from uploadkit import make_config, payload

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 4096

spec = importlib.util.spec_from_file_location(
    "model_crud_upload", ROOT / "scripts" / "model_crud_upload.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class Recorder:
    """Server-side view of what the CLI did, plus hooks to break things on purpose."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.inflight = 0
        self.max_inflight = 0
        self.fail_chunk: dict[int, list[int]] = {}  # chunk index -> statuses to answer, in order
        self.drop_chunk: dict[int, int] = {}  # chunk index -> times to cut the connection
        self.lose_complete_reply = False
        self.delay = 0.0

    def puts(self) -> list[int]:
        return sorted(
            int(re.search(r"/chunks/(\d+)", path).group(1))
            for method, path, _ in self.requests
            if method == "PUT" and "/chunks/" in path
        )


@pytest.fixture
async def env(folder_paths, prompt_server, comfy_app, monkeypatch):
    rec = Recorder()

    @web.middleware
    async def middleware(request, handler):
        rec.requests.append((request.method, request.path, dict(request.headers)))
        match = re.search(r"/chunks/(\d+)$", request.path)
        if request.method == "PUT" and match:
            index = int(match.group(1))
            if rec.drop_chunk.get(index, 0) > 0:
                rec.drop_chunk[index] -= 1
                request.transport.close()
                raise web.HTTPInternalServerError()
            if rec.fail_chunk.get(index):
                status = rec.fail_chunk[index].pop(0)
                return web.json_response(
                    {"error": "injected", "message": "injected failure"}, status=status
                )
            rec.inflight += 1
            rec.max_inflight = max(rec.max_inflight, rec.inflight)
            try:
                if rec.delay:
                    await asyncio.sleep(rec.delay)
                return await handler(request)
            finally:
                rec.inflight -= 1
        if (
            request.method == "POST"
            and request.path.endswith("/complete")
            and rec.lose_complete_reply
        ):
            rec.lose_complete_reply = False
            response = await handler(request)
            request.transport.close()  # the work is done, the answer never arrives
            return response
        return await handler(request)

    state = {}

    async def start(cfg=None):
        cfg = cfg or make_config(chunk_size=CHUNK, max_parallel=8)
        uploads.set_manager(UploadManager(cfg))
        monkeypatch.setattr(api_routes, "get_config", lambda: cfg)  # /config agrees with uploads
        table = type(prompt_server.routes)()
        api_routes.register_routes(table)
        app = comfy_app(table, middlewares=[middleware])
        api_routes.register_lifecycle(app)
        server = TestServer(app)
        await server.start_server()
        state["server"] = server
        return str(server.make_url("")).rstrip("/")

    state["start"] = start
    state["rec"] = rec
    yield state
    if "server" in state:
        await state["server"].close()
    uploads.set_manager(None)


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(cli, "backoff", lambda attempt: 0.0)


async def run(*argv: str) -> int:
    return await asyncio.to_thread(cli.main, [*argv])


def make_file(tmp_path: Path, size: int, name="m.safetensors", seed=1) -> tuple[Path, bytes]:
    data = payload(size, seed)
    path = tmp_path / name
    path.write_bytes(data)
    return path, data


# --- the happy path -------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, CHUNK, CHUNK + 1, 7 * CHUNK + 123])
async def test_uploads_a_file_byte_for_byte(env, loras, tmp_path, capsys, size):
    server = await env["start"]()
    path, data = make_file(tmp_path, size)

    code = await run("--server", server, "--folder", "loras", str(path))

    assert code == 0
    assert (loras / "m.safetensors").read_bytes() == data
    assert not list(loras.glob(".model_crud_*"))
    out = capsys.readouterr()
    assert "done" in out.out and "loras/m.safetensors" in out.out


async def test_subfolder_unicode_name_and_overwrite(env, loras, tmp_path):
    server = await env["start"]()
    (loras / "sdxl").mkdir()
    (loras / "sdxl" / "Süß 日本.safetensors").write_bytes(b"old")
    path, data = make_file(tmp_path, 3 * CHUNK, "Süß 日本.safetensors")

    refused = await run("--server", server, "--folder", "loras", "--subfolder", "sdxl", str(path))
    assert refused == 1 and (loras / "sdxl" / "Süß 日本.safetensors").read_bytes() == b"old"
    code = await run(
        "--server", server, "--folder", "loras", "--subfolder", "sdxl", "--overwrite", str(path)
    )

    assert code == 0 and (loras / "sdxl" / "Süß 日本.safetensors").read_bytes() == data


async def test_conflict_message_suggests_overwrite(env, loras, tmp_path, capsys):
    server = await env["start"]()
    (loras / "m.safetensors").write_bytes(b"old")
    path, _ = make_file(tmp_path, CHUNK)

    code = await run("--server", server, "--folder", "loras", str(path))

    assert code == 1
    assert "--overwrite" in capsys.readouterr().err


@pytest.mark.parametrize("requested", [1, 4, 16])
async def test_parallelism_is_capped_by_the_server(env, loras, tmp_path, requested):
    server = await env["start"](make_config(chunk_size=CHUNK, max_parallel=3))
    env["rec"].delay = 0.03
    path, data = make_file(tmp_path, 24 * CHUNK)

    code = await run(
        "--server", server, "--folder", "loras", "--parallel", str(requested), str(path)
    )

    assert code == 0 and (loras / "m.safetensors").read_bytes() == data
    assert env["rec"].max_inflight == min(requested, 3)


# --- resume ---------------------------------------------------------------------------------


async def test_a_second_run_sends_only_the_missing_chunks(env, loras, tmp_path):
    server = await env["start"]()
    path, data = make_file(tmp_path, 8 * CHUNK + 10)
    manager = uploads.get_manager()
    session = await manager.create_session(
        "loras",
        "m.safetensors",
        len(data),
        client_fingerprint=cli.fingerprint(str(path), path.stat()),
    )
    for i in (0, 1, 4, 8):
        piece = data[i * CHUNK : (i + 1) * CHUNK]
        await manager.write_chunk(session.id, i, _body(piece), len(piece))
    env["rec"].requests.clear()

    code = await run("--server", server, "--folder", "loras", "--parallel", "1", str(path))

    assert code == 0 and (loras / "m.safetensors").read_bytes() == data
    assert env["rec"].puts() == [2, 3, 5, 6, 7]  # exactly the gaps
    assert not any(m == "POST" and p.endswith("/uploads") for m, p, _ in env["rec"].requests)


def _body(piece: bytes):
    from uploadkit import Body

    return Body(piece)


async def test_resumes_after_a_run_that_failed_part_way(env, loras, tmp_path, capsys):
    server = await env["start"]()
    path, data = make_file(tmp_path, 6 * CHUNK)
    env["rec"].fail_chunk = {3: [500] * 20}  # this chunk never gets through

    first = await run(
        "--server", server, "--folder", "loras", "--parallel", "1", "--retries", "1", str(path)
    )

    assert first == 1 and not (loras / "m.safetensors").exists()
    assert "Chunk 3" in capsys.readouterr().err
    done_before = {i for i in env["rec"].puts()}
    env["rec"].fail_chunk = {}
    env["rec"].requests.clear()

    second = await run("--server", server, "--folder", "loras", "--parallel", "1", str(path))

    assert second == 0 and (loras / "m.safetensors").read_bytes() == data
    assert env["rec"].puts() == [3, 4, 5]  # 0-2 were kept from the first run
    assert done_before >= {0, 1, 2, 3}


async def test_a_changed_file_does_not_resume_the_old_session(env, loras, tmp_path, capsys):
    server = await env["start"]()
    path, _ = make_file(tmp_path, 4 * CHUNK)
    env["rec"].fail_chunk = {2: [500] * 20}
    await run(
        "--server", server, "--folder", "loras", "--parallel", "1", "--retries", "0", str(path)
    )
    env["rec"].fail_chunk = {}
    path.write_bytes(payload(4 * CHUNK, seed=99))  # same size, different content and mtime
    os.utime(path, (1_000_000_000, 1_000_000_000))

    code = await run("--server", server, "--folder", "loras", str(path))

    assert code == 1  # the old session still owns the target; we must not splice two versions
    assert "already in progress" in capsys.readouterr().err
    leftover = (await _list(server))[0]["upload_id"]
    assert await run("--server", server, "--cancel", leftover) == 0
    assert await run("--server", server, "--folder", "loras", str(path)) == 0
    assert (loras / "m.safetensors").read_bytes() == payload(4 * CHUNK, seed=99)


async def _list(server: str) -> list[dict]:
    def get():
        status, body = cli.Client(server, [], 10).request("GET", cli.API + "/uploads")
        return body["uploads"]

    return await asyncio.to_thread(get)


# --- failures and retries -------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 503, 429, 422])
async def test_transient_answers_are_retried(env, loras, tmp_path, status):
    server = await env["start"]()
    env["rec"].fail_chunk = {1: [status, status]}
    path, data = make_file(tmp_path, 3 * CHUNK)

    code = await run("--server", server, "--folder", "loras", "--retries", "3", str(path))

    assert code == 0 and (loras / "m.safetensors").read_bytes() == data
    assert env["rec"].puts().count(1) == 3  # two failures, one success


async def test_dropped_connections_are_retried(env, loras, tmp_path):
    server = await env["start"]()
    env["rec"].drop_chunk = {0: 2, 2: 1}
    path, data = make_file(tmp_path, 4 * CHUNK)

    code = await run("--server", server, "--folder", "loras", "--retries", "4", str(path))

    assert code == 0 and (loras / "m.safetensors").read_bytes() == data


async def test_failed_attempts_are_not_counted_as_progress(env, loras, tmp_path, monkeypatch):
    server = await env["start"]()
    env["rec"].fail_chunk = {1: [503, 503]}
    env["rec"].drop_chunk = {2: 1}
    path, _ = make_file(tmp_path, 4 * CHUNK + 9)
    created = []
    real = cli.Progress
    monkeypatch.setattr(
        cli, "Progress", lambda *a, **k: created.append(real(*a, **k)) or created[-1]
    )

    code = await run(
        "--server", server, "--folder", "loras", "--retries", "4", "--quiet", str(path)
    )

    assert code == 0 and created[0].sent == created[0].total == 4 * CHUNK + 9


async def test_gives_up_after_the_configured_retries(env, loras, tmp_path, capsys):
    server = await env["start"]()
    env["rec"].fail_chunk = {0: [503] * 50}
    path, _ = make_file(tmp_path, CHUNK)

    code = await run("--server", server, "--folder", "loras", "--retries", "2", str(path))

    assert code == 1
    assert env["rec"].puts().count(0) == 3  # the first try plus two retries
    err = capsys.readouterr().err
    assert "Chunk 0 failed after 3 attempts" in err and "injected failure" in err


@pytest.mark.parametrize("status", [400, 404, 409, 413, 415, 507])
async def test_refusals_are_not_retried(env, loras, tmp_path, status):
    server = await env["start"]()
    env["rec"].fail_chunk = {0: [status] * 10}
    path, _ = make_file(tmp_path, CHUNK)

    code = await run("--server", server, "--folder", "loras", "--retries", "5", str(path))

    assert code == 1 and env["rec"].puts().count(0) == 1


async def test_server_side_refusals_at_creation(env, loras, tmp_path, capsys):
    server = await env["start"](make_config(chunk_size=CHUNK, max_upload_bytes=5 * CHUNK))
    too_big, _ = make_file(tmp_path, 6 * CHUNK, "big.safetensors")
    bad_type, _ = make_file(tmp_path, 10, "x.exe")

    assert await run("--server", server, "--folder", "loras", str(too_big)) == 1
    assert "upload limit" in capsys.readouterr().err
    assert await run("--server", server, "--folder", "loras", str(bad_type)) == 1
    assert "extension" in capsys.readouterr().err
    assert await run("--server", server, "--folder", "nope", str(too_big)) == 1
    assert "Unknown folder" in capsys.readouterr().err
    assert list(loras.iterdir()) == []


async def test_a_lost_reply_to_complete_is_recognised_as_success(env, loras, tmp_path):
    server = await env["start"]()
    env["rec"].lose_complete_reply = True
    path, data = make_file(tmp_path, 2 * CHUNK)

    code = await run("--server", server, "--folder", "loras", str(path))

    assert code == 0 and (loras / "m.safetensors").read_bytes() == data


async def test_uploads_disabled_is_reported(env, tmp_path, capsys):
    server = await env["start"](make_config(read_only=True))
    path, _ = make_file(tmp_path, 10)

    assert await run("--server", server, "--folder", "loras", str(path)) == 1
    assert "disabled" in capsys.readouterr().err


async def test_unreachable_server_fails_cleanly(tmp_path, capsys):
    path, _ = make_file(tmp_path, 10)

    code = await run(
        "--server", "http://127.0.0.1:9", "--folder", "loras", "--timeout", "2", str(path)
    )

    assert code == 1 and "cannot talk to the server" in capsys.readouterr().err


async def test_bad_files_do_not_stop_the_good_ones(env, loras, tmp_path, capsys):
    server = await env["start"]()
    good, data = make_file(tmp_path, CHUNK + 1)
    empty = tmp_path / "empty.safetensors"
    empty.write_bytes(b"")

    code = await run(
        "--server",
        server,
        "--folder",
        "loras",
        str(tmp_path / "missing.safetensors"),
        str(empty),
        str(tmp_path),
        str(good),
    )

    assert code == 1 and (loras / "m.safetensors").read_bytes() == data
    assert capsys.readouterr().err.count("not a non-empty regular file") == 2


async def test_file_that_shrinks_while_uploading_fails(env, loras, tmp_path, capsys):
    server = await env["start"]()
    path, _ = make_file(tmp_path, 4 * CHUNK)
    original = cli.iter_range

    def shrinking(p, offset, length, *a, **k):
        if offset == 2 * CHUNK:
            os.truncate(p, CHUNK)
        return original(p, offset, length, *a, **k)

    cli.iter_range = shrinking
    try:
        code = await run("--server", server, "--folder", "loras", "--parallel", "1", str(path))
    finally:
        cli.iter_range = original

    assert code == 1 and "got shorter" in capsys.readouterr().err
    assert not (loras / "m.safetensors").exists()


# --- integrity options ----------------------------------------------------------------------


async def test_whole_file_hash_is_sent_and_verified(env, loras, tmp_path, capsys):
    server = await env["start"]()
    path, data = make_file(tmp_path, 5 * CHUNK + 7)

    code = await run("--server", server, "--folder", "loras", "--sha256", str(path))

    assert code == 0
    assert hashlib.sha256(data).hexdigest() in capsys.readouterr().out
    assert (loras / "m.safetensors").read_bytes() == data


async def test_whole_file_hash_mismatch_fails_and_keeps_the_session(
    env, loras, tmp_path, capsys, monkeypatch
):
    server = await env["start"]()
    path, _ = make_file(tmp_path, 3 * CHUNK)
    monkeypatch.setattr(cli, "sha256_file", lambda p: "0" * 64)

    code = await run("--server", server, "--folder", "loras", "--sha256", str(path))

    assert code == 1 and "checksum" in capsys.readouterr().err.lower()
    assert not (loras / "m.safetensors").exists()
    assert len(await _list(server)) == 1  # kept, so a fixed run can resume it


async def test_chunk_hashes_are_sent_and_correct(env, loras, tmp_path):
    server = await env["start"]()
    path, data = make_file(tmp_path, 3 * CHUNK + 5)

    code = await run(
        "--server", server, "--folder", "loras", "--chunk-hash", "--parallel", "1", str(path)
    )

    assert code == 0
    sent = {
        int(re.search(r"/chunks/(\d+)", p).group(1)): h.get("Upload-Chunk-SHA256")
        for m, p, h in env["rec"].requests
        if m == "PUT"
    }
    for index, header in sent.items():
        assert header == hashlib.sha256(data[index * CHUNK : (index + 1) * CHUNK]).hexdigest()
    assert len(sent) == 4


async def test_without_chunk_hash_no_header_is_sent(env, loras, tmp_path):
    server = await env["start"]()
    path, _ = make_file(tmp_path, 2 * CHUNK)

    await run("--server", server, "--folder", "loras", str(path))

    assert all("Upload-Chunk-SHA256" not in h for m, _, h in env["rec"].requests if m == "PUT")


async def test_custom_headers_reach_the_server(env, loras, tmp_path):
    server = await env["start"]()
    path, _ = make_file(tmp_path, CHUNK)

    await run(
        "--server", server, "--folder", "loras", "--header", "X-Proxy-Token: abc123", str(path)
    )

    assert all(h.get("X-Proxy-Token") == "abc123" for _, _, h in env["rec"].requests)


# --- interruption and cancel ----------------------------------------------------------------


async def test_ctrl_c_exits_130_and_leaves_the_upload_resumable(
    env, loras, tmp_path, capsys, monkeypatch
):
    server = await env["start"]()
    path, data = make_file(tmp_path, 6 * CHUNK)
    real_wait = cli.wait

    def interrupted(futures, **kwargs):
        real_wait(futures[:2], return_when=cli.FIRST_EXCEPTION)  # let some chunks go
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "wait", interrupted)
    code = await run("--server", server, "--folder", "loras", "--parallel", "1", str(path))

    assert code == 130 and "run the same command again" in capsys.readouterr().err
    assert len(await _list(server)) == 1
    monkeypatch.setattr(cli, "wait", real_wait)
    assert await run("--server", server, "--folder", "loras", str(path)) == 0
    assert (loras / "m.safetensors").read_bytes() == data


async def test_cancel_removes_the_session(env, loras, tmp_path):
    server = await env["start"]()
    path, _ = make_file(tmp_path, 3 * CHUNK)
    env["rec"].fail_chunk = {1: [500] * 20}
    await run(
        "--server", server, "--folder", "loras", "--retries", "0", "--parallel", "1", str(path)
    )
    [session] = await _list(server)

    assert await run("--server", server, "--cancel", session["upload_id"]) == 0
    assert await _list(server) == []
    assert await run("--server", server, "--cancel", session["upload_id"]) == 1  # already gone
    assert not list(loras.glob(".model_crud_*"))


# --- arguments ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--server", "http://x", "f"],
        ["--server", "http://x", "--folder", "loras"],
        ["--server", "http://x", "--folder", "loras", "--parallel", "0", "f"],
        ["--server", "http://x", "--folder", "loras", "--retries", "-1", "f"],
        ["--server", "http://x", "--folder", "loras", "--max-rate", "0", "f"],
        ["--folder", "loras", "f"],
    ],
)
def test_bad_command_lines_exit_with_status_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("server", ["ftp://host", "http://", "::::"])
def test_bad_server_urls(server, capsys):
    assert cli.main(["--server", server, "--folder", "loras", "f"]) == 1
    assert "Cannot understand --server" in capsys.readouterr().err


def test_bad_header_option(capsys):
    assert cli.main(["--server", "http://x", "--folder", "l", "--header", "nocolon", "f"]) == 1
    assert "--header" in capsys.readouterr().err


# --- helpers --------------------------------------------------------------------------------


def test_the_file_is_read_in_blocks_of_at_most_one_mebibyte(tmp_path):
    path = tmp_path / "f"
    data = payload(3 * 1024 * 1024 + 17)
    path.write_bytes(data)
    sizes = []

    blocks = list(cli.iter_range(str(path), 100, 2 * 1024 * 1024 + 5, sizes.append))

    assert b"".join(blocks) == data[100 : 100 + 2 * 1024 * 1024 + 5]
    assert max(len(b) for b in blocks) <= cli.BLOCK == 1024 * 1024
    assert sum(sizes) == 2 * 1024 * 1024 + 5


def test_iter_range_stops_when_asked(tmp_path):
    import threading

    path = tmp_path / "f"
    path.write_bytes(payload(3 * 1024 * 1024))
    stop = threading.Event()
    blocks = cli.iter_range(str(path), 0, 3 * 1024 * 1024, stop=stop)
    next(blocks)
    stop.set()

    with pytest.raises(cli.UploadError):
        next(blocks)


def test_fingerprint_matches_the_browser_format(tmp_path):
    path = tmp_path / "Süß.safetensors"
    path.write_bytes(b"x" * 10)
    os.utime(path, (1_760_000_000.123, 1_760_000_000.123))

    assert cli.fingerprint(str(path), path.stat()) == "Süß.safetensors:10:1760000000123"


def test_missing_indices():
    assert cli.missing_indices([], 3) == [0, 1, 2]
    assert cli.missing_indices([[0, 1], [3, 3]], 5) == [2, 4]
    assert cli.missing_indices([[0, 4]], 5) == []


@pytest.mark.parametrize("attempt", [1, 2, 3, 6, 10, 50])
def test_backoff_grows_exponentially_with_jitter_and_a_cap(attempt):
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)  # the autouse fixture patched `cli`; use a pristine copy

    values = [real.backoff(attempt) for _ in range(200)]

    ceiling = min(2 ** (attempt - 1), 30)
    assert all(ceiling * 0.5 <= v <= ceiling for v in values)
    assert len(set(values)) > 1


def test_rate_limiter_paces_senders(monkeypatch):
    slept = []
    monkeypatch.setattr(cli.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    limiter = cli.RateLimiter(1000)  # bytes per second

    for _ in range(6):
        limiter.wait(1000)

    assert slept and slept[-1] == pytest.approx(5.0)  # six seconds of data, a second of burst


def test_formatting():
    assert cli.format_size(0) == "0 B" and cli.format_size(1536) == "1.5 KiB"
    assert cli.format_size(5 * 1024**3) == "5.0 GiB"
    assert cli.format_duration(59) == "0:59" and cli.format_duration(3725) == "1:02:05"


def test_progress_reports_speed_and_stays_quiet_when_asked(capsys, monkeypatch):
    clock = iter(range(1000))
    monkeypatch.setattr(cli.time, "monotonic", lambda: float(next(clock)))
    quiet = cli.Progress("f", 1000, 0, quiet=True)
    for _ in range(10):
        quiet.add(100)
    assert capsys.readouterr().err == ""
    loud = cli.Progress("f", 1000, 0, quiet=False)
    for _ in range(10):
        loud.add(100)
    assert "ETA" in capsys.readouterr().err and loud.sent == 1000
    assert json.dumps(loud.sent)  # plain int


def test_the_script_uses_only_the_standard_library():
    import ast

    imported = set()
    for node in ast.walk(ast.parse((ROOT / "scripts" / "model_crud_upload.py").read_text())):
        if isinstance(node, ast.Import):
            imported.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    import sys

    assert imported <= set(sys.stdlib_module_names)
