"""Helpers shared by the upload test modules."""

import asyncio
import random

from comfyui_model_crud.services.config import Config


class Body:
    """A fake request body: ``data`` in small blocks, optionally cut short or failing."""

    def __init__(self, data: bytes, *, block: int = 300, fail_after: int | None = None):
        self.data, self.block, self.fail_after = data, block, fail_after

    async def iter_chunked(self, n: int):
        sent = 0
        for i in range(0, len(self.data), min(n, self.block)):
            piece = self.data[i : i + min(n, self.block)]
            if self.fail_after is not None and sent + len(piece) > self.fail_after:
                yield piece[: self.fail_after - sent]
                raise ConnectionResetError("client went away")
            sent += len(piece)
            yield piece
            await asyncio.sleep(0)


def make_config(**overrides) -> Config:
    values = dict(
        chunk_size=1024, max_upload_bytes=10 * 1024**2, max_parallel=64, max_active_uploads=4
    )
    values.update(overrides)
    return Config(**values)


def payload(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)
