"""Opt-in scale check (SPEC §11): run with ``pytest -m slow``.

Uploads a 5 GB sparse file in 8 MiB chunks through the real HTTP stack and checks that the
process does not grow with the file. The data lands on a real disk, so the working directory is
the repo's own (ext4) filesystem rather than /tmp, which is often RAM-backed.
"""

import asyncio
import os
import resource
import shutil
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import uploads
from comfyui_model_crud.services.config import Config
from comfyui_model_crud.services.uploads import UploadManager

SIZE = 5 * 1024**3
CHUNK = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # kilobytes on Linux


@pytest.mark.slow
async def test_five_gigabyte_upload_keeps_memory_bounded(
    folder_paths, prompt_server, comfy_app, request
):
    workdir = ROOT / ".slow-test-tmp"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(workdir, ignore_errors=True))
    if shutil.disk_usage(workdir).free < SIZE + 3 * 1024**3:
        pytest.skip("needs about 8 GB of free disk space")

    models = workdir / "models"
    folder_paths.add_folder("loras", models)
    source = workdir / "source.bin"
    with open(source, "wb") as f:  # sparse: no disk space, reads back as zeros
        f.truncate(SIZE)
        f.seek(SIZE - 4096)
        f.write(os.urandom(4096))

    uploads.set_manager(
        UploadManager(Config(chunk_size=CHUNK, max_upload_bytes=SIZE * 2, max_parallel=4))
    )
    table = type(prompt_server.routes)()
    api_routes.register_routes(table)
    sem = asyncio.Semaphore(4)
    started = time.perf_counter()

    async with TestClient(TestServer(comfy_app(table))) as client:
        resp = await client.post(
            "/model_crud/uploads",
            json={"folder": "loras", "filename": "big.safetensors", "size": SIZE},
        )
        session = await resp.json()
        assert resp.status == 201 and session["total_chunks"] == SIZE // CHUNK
        baseline = peak_rss_mb()

        async def send(index: int) -> None:
            async with sem:
                with open(source, "rb") as f:
                    f.seek(index * CHUNK)
                    body = f.read(CHUNK)  # one chunk in memory per in-flight request
                reply = await client.put(
                    f"/model_crud/uploads/{session['upload_id']}/chunks/{index}", data=body
                )
                assert reply.status == 200, await reply.text()

        await asyncio.gather(*(send(i) for i in range(session["total_chunks"])))
        done = await client.post(f"/model_crud/uploads/{session['upload_id']}/complete")
        assert done.status == 201

    elapsed = time.perf_counter() - started
    final = models / "big.safetensors"
    assert final.stat().st_size == SIZE
    with open(final, "rb") as a, open(source, "rb") as b:  # the tail with real data matches
        a.seek(SIZE - 4096)
        b.seek(SIZE - 4096)
        assert a.read(4096) == b.read(4096)
    growth = peak_rss_mb() - baseline
    rate = SIZE / elapsed / 1e6
    print(f"\n5 GB in {elapsed:.0f}s ({rate:.0f} MB/s), peak RSS growth {growth:.0f} MB")
    assert growth < 200  # NFR-3: well under 200 MB extra, whatever the file size
    assert not list(models.glob(".model_crud_*"))
