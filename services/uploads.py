"""Chunked upload sessions (SPEC §5.2, §6.4): one preallocated temp file, chunks written at
their offset, completion is a rename.

Everything here runs on the event loop except the blocking file work, which goes to a dedicated
thread pool so a slow disk can neither stall ComfyUI's loop nor starve its default executor.
The session object is shaped for what comes next (persisted ranges, hashes, expiry) without
changing its fields.
"""

import asyncio
import contextlib
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar

from . import comfy, fsutil, paths
from .config import Config, get_config
from .errors import (
    BadChunk,
    BadRequest,
    FileAlreadyExists,
    IncompleteUpload,
    InsufficientStorage,
    PathOutsideRoot,
    PermissionDenied,
    TooLarge,
    TooManyUploads,
    UnknownUpload,
    UploadInProgress,
    UploadsDisabled,
)

log = logging.getLogger("comfyui-model-crud")

T = TypeVar("T")

UPLOAD_ID_RE = re.compile(r"[A-Za-z0-9_-]{22,64}")  # matched with fullmatch: `$` allows "\n"
READ_BLOCK = 1024 * 1024  # FR-U14: never hold more than this much of a chunk body per read
BLOCK_TIMEOUT = 120.0  # seconds without a byte before a stalled chunk request is abandoned
MAX_FINGERPRINT_CHARS = 512

OPEN = "open"
COMPLETING = "completing"


class ChunkStream(Protocol):
    """What we need from a request body (aiohttp's ``request.content`` provides it)."""

    def iter_chunked(self, n: int) -> AsyncIterator[bytes]: ...


@dataclass
class UploadSession:
    id: str
    folder: str
    relpath: str
    root: str
    directory: str  # realpath of the target directory, fixed at creation
    final_path: str
    part_path: str
    size: int
    chunk_size: int
    overwrite: bool
    client_fingerprint: str | None
    created: float
    expires_at: float
    received: set[int] = field(default_factory=set)  # indices of fully written chunks
    state: str = OPEN
    active_writers: int = 0
    closed: bool = False
    locks: "_KeyedLocks" = field(default_factory=lambda: _KeyedLocks())

    @property
    def total_chunks(self) -> int:
        return -(-self.size // self.chunk_size)

    def expected_length(self, index: int) -> int:
        return min(self.chunk_size, self.size - index * self.chunk_size)

    @property
    def received_bytes(self) -> int:
        full = len(self.received)
        last = self.total_chunks - 1
        if last in self.received:
            return (full - 1) * self.chunk_size + self.expected_length(last)
        return full * self.chunk_size


class _KeyedLocks:
    """One asyncio lock per key, dropped again when nobody holds or awaits it."""

    def __init__(self) -> None:
        self._entries: dict[int, list[Any]] = {}  # key -> [lock, users]

    @contextlib.asynccontextmanager
    async def hold(self, key: int) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self._entries[key]


class _PartFile:
    """An open handle on the temp file whose close waits for any write still in a worker.

    Closing an fd while a thread is inside ``pwrite`` on it could hit a recycled descriptor, and
    cancelling an awaiting coroutine does not stop its executor job.
    """

    def __init__(self, path: str) -> None:
        self._fd = fsutil.open_existing(path)
        self._lock = threading.Lock()
        self._open = True

    def write(self, offset: int, data: bytes) -> None:
        with self._lock:
            if not self._open:
                raise OSError("part file closed")
            fsutil.write_at(self._fd, offset, data)

    def flush(self, offset: int, length: int) -> None:
        with self._lock:
            if not self._open:
                raise OSError("part file closed")
            fsutil.flush_range(self._fd, offset, length)

    def close(self) -> None:
        with self._lock:
            if self._open:
                self._open = False
                os.close(self._fd)


class UploadManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: dict[str, UploadSession] = {}
        self._by_target: dict[str, str] = {}
        workers = max(8, min(32, config.max_active_uploads * config.max_parallel + 2))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="model-crud-io")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    # -- public API --------------------------------------------------------------------------

    async def create_session(
        self,
        folder: object,
        filename: object,
        size: object,
        subfolder: object = None,
        overwrite: object = False,
        client_fingerprint: object = None,
    ) -> UploadSession:
        self._require_enabled()
        if not isinstance(overwrite, bool):
            raise BadRequest("'overwrite' must be true or false.")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise BadRequest("'size' must be a positive whole number of bytes.")
        if client_fingerprint is not None and not _valid_fingerprint(client_fingerprint):
            raise BadRequest("'client_fingerprint' must be a short plain string.")

        target = paths.resolve_upload_target(
            folder, filename, subfolder, self.config.allowed_extensions
        )
        if size > self.config.max_upload_bytes:
            raise TooLarge(
                f"The upload limit is {self.config.max_upload_bytes} bytes on this server."
            )
        if target.entry in self._by_target:
            raise UploadInProgress()
        if len(self.sessions) >= self.config.max_active_uploads:
            raise TooManyUploads()
        if os.path.lexists(target.entry) and (not overwrite or not os.path.isfile(target.entry)):
            raise FileAlreadyExists()

        upload_id = secrets.token_urlsafe(16)
        now = time.time()
        session = UploadSession(
            id=upload_id,
            folder=target.folder,
            relpath=target.relpath,
            root=target.root,
            directory=target.directory,
            final_path=target.entry,
            part_path=os.path.join(target.directory, f".model_crud_{upload_id}.part"),
            size=size,
            chunk_size=self.config.chunk_size,
            overwrite=overwrite,
            client_fingerprint=client_fingerprint,  # type: ignore[arg-type]
            created=now,
            expires_at=now + self.config.upload_ttl_hours * 3600,
        )
        # Reserve the slot before the first await so two racing creates cannot both pass the
        # checks above (single-threaded event loop: no await, no interleaving).
        self.sessions[upload_id] = session
        self._by_target[session.final_path] = upload_id
        try:
            await self._run(self._prepare, session)
        except BaseException:
            self._forget(session)
            raise
        return session

    async def write_chunk(
        self, upload_id: object, index: object, stream: ChunkStream, content_length: int | None
    ) -> dict[str, int]:
        self._require_enabled()
        session = self._get(upload_id)
        if session.state != OPEN:
            raise UploadInProgress("The upload is being finalised.")
        if isinstance(index, bool) or not isinstance(index, int):
            raise BadChunk()
        if not 0 <= index < session.total_chunks:
            only = session.total_chunks == 1
            raise BadChunk(
                "The only valid chunk index is 0."
                if only
                else f"Chunk index must be between 0 and {session.total_chunks - 1}."
            )
        expected = session.expected_length(index)
        if content_length != expected:
            raise BadChunk(f"Chunk {index} must be sent with Content-Length: {expected}.")
        if session.active_writers >= self.config.max_parallel:
            raise TooManyUploads(f"At most {self.config.max_parallel} chunks may be in flight.")

        session.active_writers += 1
        try:
            async with session.locks.hold(index):
                # Serialised per index: two senders of the same chunk cannot interleave blocks.
                self._ensure_writable(session)
                await self._receive(session, index, stream, expected)
                self._ensure_writable(session)
                session.received.add(index)
                self._touch(session)
        finally:
            session.active_writers -= 1
        return {
            "index": index,
            "received_chunks": len(session.received),
            "received_bytes": session.received_bytes,
        }

    async def complete(self, upload_id: object, sha256: object = None) -> dict[str, Any]:
        self._require_enabled()
        session = self._get(upload_id)
        if sha256 is not None:
            raise BadRequest("Whole-file checksums are not supported yet.")
        if session.state != OPEN:
            raise UploadInProgress("The upload is already being finalised.")
        if session.active_writers:
            raise UploadInProgress("Chunks are still being received.")
        missing = missing_ranges(session.received, session.total_chunks)
        if missing:
            raise IncompleteUpload(missing_ranges=missing)

        session.state = COMPLETING
        try:
            await self._run(self._publish, session)
        except BaseException:
            # Keep the session and its data: the client can fix the cause (for example remove
            # the conflicting file) and call complete again, or cancel.
            session.state = OPEN
            raise
        self._forget(session)
        comfy.invalidate_cache(session.folder)
        log.info(
            "Uploaded model: folder=%s path=%s size=%d",
            session.folder,
            session.relpath,
            session.size,
        )
        return {"folder": session.folder, "path": session.relpath, "size": session.size}

    async def cancel(self, upload_id: object) -> dict[str, Any]:
        self._require_enabled()
        session = self._get(upload_id)
        if session.state != OPEN:
            raise UploadInProgress("The upload is being finalised.")
        self._forget(session)  # in-flight chunk handlers see `closed` and fail with 404
        await self._run(self._remove_part, session)
        return {"upload_id": session.id, "cancelled": True}

    # -- internals ---------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.config.upload_enabled:
            raise UploadsDisabled()

    def _get(self, upload_id: object) -> UploadSession:
        # Validate the id before it is used for anything (SR-12): a bad id never reaches the
        # filesystem and cannot select another session.
        if not isinstance(upload_id, str) or not UPLOAD_ID_RE.fullmatch(upload_id):
            raise UnknownUpload()
        session = self.sessions.get(upload_id)
        if session is None:
            raise UnknownUpload()
        return session

    def _ensure_writable(self, session: UploadSession) -> None:
        if session.closed:
            raise UnknownUpload()
        if session.state != OPEN:
            raise UploadInProgress("The upload is being finalised.")

    def _touch(self, session: UploadSession) -> None:
        session.expires_at = time.time() + self.config.upload_ttl_hours * 3600

    def _forget(self, session: UploadSession) -> None:
        session.closed = True
        self.sessions.pop(session.id, None)
        if self._by_target.get(session.final_path) == session.id:
            del self._by_target[session.final_path]

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def _receive(
        self, session: UploadSession, index: int, stream: ChunkStream, expected: int
    ) -> None:
        """Stream the body to its offset in ≤ 1 MiB blocks; return only when fully durable."""
        loop = asyncio.get_running_loop()
        offset = index * session.chunk_size
        try:
            part = await self._run(_PartFile, session.part_path)
        except FileNotFoundError:
            raise UnknownUpload() from None
        except PermissionError:
            raise PermissionDenied() from None
        pending: asyncio.Future[None] | None = None
        try:
            received = 0
            blocks = stream.iter_chunked(READ_BLOCK).__aiter__()
            while True:
                try:
                    block = await asyncio.wait_for(blocks.__anext__(), BLOCK_TIMEOUT)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise BadChunk("Timed out waiting for chunk data.") from None
                except Exception:  # client went away mid-body
                    raise BadChunk("The chunk upload was interrupted.") from None
                if received + len(block) > expected:  # Content-Length alone is not trusted
                    raise BadChunk("Chunk body is longer than its Content-Length.")
                if pending is not None:  # overlap the next read with the previous write
                    await pending
                pending = loop.run_in_executor(self._pool, part.write, offset + received, block)
                received += len(block)
            if pending is not None:
                await pending
                pending = None
            if received != expected:
                raise BadChunk("Chunk body is shorter than its Content-Length.")
            await self._run(part.flush, offset, expected)
        except OSError as exc:
            if fsutil.disk_full(exc):
                raise InsufficientStorage() from None
            raise
        finally:
            if pending is not None:
                pending.add_done_callback(_consume_exception)
            part.close()

    # Everything below runs in the worker pool.

    def _prepare(self, session: UploadSession) -> None:
        with _translate_os_errors():
            try:
                os.makedirs(session.directory, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                raise FileAlreadyExists("A file exists where the subfolder should be.") from None
            # The directory was vetted as a string; make sure nothing turned it into a link.
            real = os.path.realpath(session.directory)
            if real != session.directory or not paths.is_inside(session.root, real):
                raise PathOutsideRoot()
            fsutil.check_free_space(session.directory, session.size)
            fd = fsutil.create_exclusive(session.part_path)
            try:
                fsutil.preallocate(fd, session.size)
            except BaseException:
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(session.part_path)
                raise
            os.close(fd)

    def _publish(self, session: UploadSession) -> None:
        with _translate_os_errors():
            try:
                fd = fsutil.open_existing(session.part_path)
            except FileNotFoundError:
                raise UnknownUpload() from None
            try:
                if os.fstat(fd).st_size != session.size:
                    raise OSError("temp file has the wrong size")
                os.fsync(fd)
            finally:
                os.close(fd)
            real = os.path.realpath(session.directory)
            if real != session.directory or not paths.is_inside(session.root, real):
                raise PathOutsideRoot()
            if session.overwrite:
                fsutil.publish_replace(session.part_path, session.final_path)
            else:
                fsutil.publish_no_replace(session.part_path, session.final_path)

    def _remove_part(self, session: UploadSession) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(session.part_path)


@contextlib.contextmanager
def _translate_os_errors() -> Iterator[None]:
    try:
        yield
    except PermissionError:
        raise PermissionDenied() from None
    except OSError as exc:
        if fsutil.disk_full(exc):
            raise InsufficientStorage() from None
        raise


def _consume_exception(future: "asyncio.Future[None]") -> None:
    if not future.cancelled():
        future.exception()  # mark retrieved; the original error is already being handled


def _valid_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_FINGERPRINT_CHARS
        and not any(unicodedata.category(c) == "Cc" for c in value)
    )


def to_ranges(indices: Iterable[int]) -> list[list[int]]:
    """Sorted, merged inclusive ``[first, last]`` ranges of chunk indices."""
    ranges: list[list[int]] = []
    for i in sorted(set(indices)):
        if ranges and i == ranges[-1][1] + 1:
            ranges[-1][1] = i
        else:
            ranges.append([i, i])
    return ranges


def missing_ranges(received: Iterable[int], total: int) -> list[list[int]]:
    have = set(received)
    return to_ranges(i for i in range(total) if i not in have)


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_manager: UploadManager | None = None


def get_manager() -> UploadManager:
    """The process-wide manager, built from the config on first use."""
    global _manager
    if _manager is None:
        _manager = UploadManager(get_config())
    return _manager


def set_manager(manager: UploadManager | None) -> None:
    """Replace the process-wide manager (tests; ``None`` makes the next call rebuild it)."""
    global _manager
    if _manager is not None and _manager is not manager:
        _manager.shutdown()
    _manager = manager
