"""Chunked, resumable upload sessions (SPEC §5.2, §6.4): one preallocated temp file, chunks
written at their offset, completion is a rename.

Everything here runs on the event loop except the blocking file work, which goes to a dedicated
thread pool so a slow disk can neither stall ComfyUI's loop nor starve its default executor.

Invariant: a chunk index is in ``received`` only while its byte range holds that chunk, complete
and (if a hash was sent) verified. A re-send therefore un-records the chunk *before* the first
byte is overwritten. The sidecar file is written after the data is durable, so after a crash it
can only be behind the data, never ahead of it: the worst case is re-sending a chunk.
"""

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar

from . import comfy, fsutil, paths
from .config import MEDIA_AREAS, Config, get_config
from .errors import (
    BadChunk,
    BadRequest,
    ChecksumMismatch,
    FileAlreadyExists,
    IncompleteUpload,
    InsufficientStorage,
    ModelCrudError,
    PathOutsideRoot,
    PermissionDenied,
    TooLarge,
    TooManyUploads,
    UnknownFolder,
    UnknownUpload,
    UploadInProgress,
    UploadsDisabled,
)

log = logging.getLogger("comfyui-model-crud")

T = TypeVar("T")

UPLOAD_ID_RE = re.compile(r"[A-Za-z0-9_-]{22,64}")  # matched with fullmatch: `$` allows "\n"
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
ARTIFACT_RE = re.compile(r"\.model_crud_([A-Za-z0-9_-]{22,64})\.(part|json|json\.tmp)")
READ_BLOCK = 1024 * 1024  # FR-U14: never hold more than this much of a chunk body per read
HASH_BLOCK = 4 * 1024 * 1024
BLOCK_TIMEOUT = 120.0  # seconds without a byte before a stalled chunk request is abandoned
MAX_FINGERPRINT_CHARS = 512

SIDECAR_VERSION = 1
MAX_SIDECAR_BYTES = 1024 * 1024
MAX_TOTAL_CHUNKS = 1_000_000  # bounds what a hand-edited sidecar can make us loop over
MAX_CHUNK_SIZE = 256 * 1024 * 1024
ORPHAN_MIN_AGE = 3600.0  # never delete a stray temp file younger than this: it may be mid-creation
EXPIRY_INTERVAL = 3600.0

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
    area: str | None = None  # "input" / "output" for media uploads, None for model folders
    received: set[int] = field(default_factory=set)  # indices of fully written chunks
    state: str = OPEN
    active_writers: int = 0
    closed: bool = False
    version: int = 0  # bumped on every change to persisted state
    saved_version: int = 0
    locks: "_KeyedLocks" = field(default_factory=lambda: _KeyedLocks())
    _save_lock: asyncio.Lock | None = None

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

    def save_lock(self) -> asyncio.Lock:
        # Created on first use, on the event loop: restored sessions are built in a worker thread.
        if self._save_lock is None:
            self._save_lock = asyncio.Lock()
        return self._save_lock


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

    def write(self, offset: int, data: bytes, hasher: Any = None) -> None:
        with self._lock:
            if not self._open:
                raise OSError("part file closed")
            if hasher is not None:
                hasher.update(data)  # in the worker: hashing must not run on the event loop
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


class _Discard(Exception):
    """A persisted session that cannot be trusted or resumed; the message is the reason."""


class _Skip(Exception):
    """A persisted session that is valid but not ours to resume now (its area is switched off).
    Its files are left alone."""


class UploadManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: dict[str, UploadSession] = {}
        self._by_target: dict[str, str] = {}
        workers = max(8, min(32, config.max_active_uploads * config.max_parallel + 2))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="model-crud-io")
        self.expiry_interval = EXPIRY_INTERVAL
        self._expiry_task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Resume sessions persisted by an earlier run and begin the hourly expiry sweep."""
        if not self.config.upload_enabled:
            return  # nothing to resume or expire, and no reason to touch the disk
        restored = await self._run(self._scan, time.time())
        for session in restored:
            self.sessions[session.id] = session
            self._by_target[session.final_path] = session.id
        if restored:
            log.info("Resumable uploads found on disk: %d", len(restored))
        self._expiry_task = asyncio.get_running_loop().create_task(self._expiry_loop())

    async def stop(self) -> None:
        task, self._expiry_task = self._expiry_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    async def expire_idle(self, now: float | None = None) -> int:
        """Remove sessions idle past their expiry, with their temp and sidecar files (FR-U12)."""
        now = time.time() if now is None else now
        idle = [
            s
            for s in list(self.sessions.values())
            if s.expires_at <= now and s.state == OPEN and s.active_writers == 0
        ]
        for session in idle:
            self._forget(session)
            async with session.save_lock():
                await self._run(self._remove_files, session.directory, session.id)
            log.info("Expired idle upload: folder=%s path=%s", session.folder, session.relpath)
        return len(idle)

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(self.expiry_interval)
            try:
                await self.expire_idle()
            except Exception:
                log.exception("Expiring idle uploads failed")

    # -- public API --------------------------------------------------------------------------

    async def create_session(
        self,
        folder: object,
        filename: object,
        size: object,
        subfolder: object = None,
        overwrite: object = False,
        client_fingerprint: object = None,
        *,
        area: str | None = None,
    ) -> UploadSession:
        """Start an upload into a model folder, or into ``input``/``output`` if ``area`` is set
        (then ``folder`` is ignored)."""
        self._require_enabled()
        self._require_area(area)
        if not isinstance(overwrite, bool):
            raise BadRequest("'overwrite' must be true or false.")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise BadRequest("'size' must be a positive whole number of bytes.")
        if client_fingerprint is not None and not _valid_fingerprint(client_fingerprint):
            raise BadRequest("'client_fingerprint' must be a short plain string.")

        if area is None:
            target = paths.resolve_upload_target(
                folder, filename, subfolder, self.config.allowed_extensions
            )
        else:
            target = paths.resolve_upload_target(
                area, filename, subfolder, self.config.extensions_for_area(area), media=True
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
            part_path=part_path(target.directory, upload_id),
            size=size,
            chunk_size=self.config.chunk_size,
            overwrite=overwrite,
            client_fingerprint=client_fingerprint,  # type: ignore[arg-type]
            created=now,
            expires_at=now + self.config.upload_ttl_hours * 3600,
            area=area,
        )
        # Reserve the slot before the first await so two racing creates cannot both pass the
        # checks above (single-threaded event loop: no await, no interleaving).
        self.sessions[upload_id] = session
        self._by_target[session.final_path] = upload_id
        try:
            await self._run(self._prepare, session, sidecar_payload(session))
        except BaseException:
            self._forget(session)
            raise
        session.saved_version = session.version
        return session

    def status(self, upload_id: object, area: str | None = None) -> UploadSession:
        self._require_enabled()
        self._require_area(area)
        return self._get(upload_id, area)

    def list_sessions(self, area: str | None = None) -> list[UploadSession]:
        """Sessions of one kind: model uploads for ``area=None``, else that area's uploads."""
        self._require_enabled()
        self._require_area(area)
        found = (s for s in self.sessions.values() if s.area == area)
        return sorted(found, key=lambda s: (s.created, s.id))

    async def write_chunk(
        self,
        upload_id: object,
        index: object,
        stream: ChunkStream,
        content_length: int | None,
        sha256: str | None = None,
        area: str | None = None,
    ) -> dict[str, int]:
        self._require_enabled()
        self._require_area(area)
        session = self._get(upload_id, area)
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
        if sha256 is not None and not SHA256_RE.fullmatch(sha256):
            raise BadRequest("The chunk checksum must be 64 hexadecimal characters.")
        if session.active_writers >= self.config.max_parallel:
            raise TooManyUploads(f"At most {self.config.max_parallel} chunks may be in flight.")

        session.active_writers += 1
        try:
            self._touch(session)  # any chunk request counts as activity, even a failing one
            async with session.locks.hold(index):
                # Serialised per index: two senders of the same chunk cannot interleave blocks.
                self._ensure_writable(session)
                if index in session.received:
                    # About to overwrite a chunk we vouched for; until the new bytes are in and
                    # verified, that vouching must not survive a failure or a crash.
                    session.received.discard(index)
                    session.version += 1
                    await self._save(session)
                await self._receive(session, index, stream, expected, sha256)
                self._ensure_writable(session)
                session.received.add(index)
                session.version += 1
                self._touch(session)
                await self._save(session)
        finally:
            session.active_writers -= 1
        return {
            "index": index,
            "received_chunks": len(session.received),
            "received_bytes": session.received_bytes,
        }

    async def complete(
        self, upload_id: object, sha256: object = None, area: str | None = None
    ) -> dict[str, Any]:
        self._require_enabled()
        self._require_area(area)
        session = self._get(upload_id, area)
        if sha256 is not None and (not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256)):
            raise BadRequest("'sha256' must be 64 hexadecimal characters.")
        if session.state != OPEN:
            raise UploadInProgress("The upload is already being finalised.")
        if session.active_writers:
            raise UploadInProgress("Chunks are still being received.")
        missing = missing_ranges(session.received, session.total_chunks)
        if missing:
            raise IncompleteUpload(missing_ranges=missing)

        session.state = COMPLETING
        try:
            await self._run(self._publish, session, sha256.lower() if sha256 else None)
        except BaseException:
            # Keep the session and its data: the client can fix the cause (remove the conflicting
            # file, re-send a chunk after a checksum mismatch) and call complete again, or cancel.
            session.state = OPEN
            raise
        self._forget(session)
        async with session.save_lock():
            await self._run(self._remove_files, session.directory, session.id)
        if area is None:
            comfy.invalidate_cache(session.folder)  # input/output have no such cache
        log.info(
            "Uploaded %s: %s=%s path=%s size=%d",
            "model" if area is None else "file",
            "folder" if area is None else "area",
            session.folder,
            session.relpath,
            session.size,
        )
        return {"folder": session.folder, "path": session.relpath, "size": session.size}

    async def cancel(self, upload_id: object, area: str | None = None) -> dict[str, Any]:
        self._require_enabled()
        self._require_area(area)
        session = self._get(upload_id, area)
        if session.state != OPEN:
            raise UploadInProgress("The upload is being finalised.")
        self._forget(session)  # in-flight chunk handlers see `closed` and fail with 404
        async with session.save_lock():  # a late sidecar write must not resurrect the session
            await self._run(self._remove_files, session.directory, session.id)
        return {"upload_id": session.id, "cancelled": True}

    # -- internals ---------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.config.upload_enabled:
            raise UploadsDisabled()

    def _require_area(self, area: str | None) -> None:
        if area is not None and not self.config.extensions_for_area(area):
            raise UnknownFolder()

    def _get(self, upload_id: object, area: str | None = None) -> UploadSession:
        # Validate the id before it is used for anything (SR-12): a bad id never reaches the
        # filesystem and cannot select another session. A session is only reachable through the
        # routes of its own kind: a model upload id is unknown under /input and vice versa.
        if not isinstance(upload_id, str) or not UPLOAD_ID_RE.fullmatch(upload_id):
            raise UnknownUpload()
        session = self.sessions.get(upload_id)
        if session is None or session.area != area:
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

    async def _save(self, session: UploadSession) -> None:
        """Persist the session state; serialised, and coalesced when a newer save covered us."""
        wanted = session.version
        async with session.save_lock():
            if session.closed or session.saved_version >= wanted:
                return
            snapshot_version = session.version
            payload = sidecar_payload(session)
            await self._run(write_sidecar, session.directory, session.id, payload)
            session.saved_version = snapshot_version

    async def _receive(
        self,
        session: UploadSession,
        index: int,
        stream: ChunkStream,
        expected: int,
        sha256: str | None,
    ) -> None:
        """Stream the body to its offset in ≤ 1 MiB blocks; return only when fully durable."""
        loop = asyncio.get_running_loop()
        offset = index * session.chunk_size
        hasher = hashlib.sha256() if sha256 is not None else None
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
                pending = loop.run_in_executor(
                    self._pool, part.write, offset + received, block, hasher
                )
                received += len(block)
            if pending is not None:
                await pending
                pending = None
            if received != expected:
                raise BadChunk("Chunk body is shorter than its Content-Length.")
            if hasher is not None and hasher.hexdigest() != sha256.lower():  # type: ignore[union-attr]
                raise ChecksumMismatch("The chunk does not match its checksum; send it again.")
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

    def _prepare(self, session: UploadSession, payload: dict[str, Any]) -> None:
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
                # Sidecar first: a crash while preallocating leaves a size mismatch the startup
                # scan recognises and cleans up, rather than an unexplained temp file.
                write_sidecar(session.directory, session.id, payload)
                fsutil.preallocate(fd, session.size)
            except BaseException:
                os.close(fd)
                self._remove_files(session.directory, session.id)
                raise
            os.close(fd)

    def _publish(self, session: UploadSession, sha256: str | None) -> None:
        with _translate_os_errors():
            try:
                fd = fsutil.open_existing(session.part_path)
            except FileNotFoundError:
                raise UnknownUpload() from None
            try:
                if os.fstat(fd).st_size != session.size:
                    raise OSError("temp file has the wrong size")
                if sha256 is not None and _hash_file(fd, session.size) != sha256:
                    raise ChecksumMismatch(
                        "The uploaded file does not match the checksum. Re-send the damaged "
                        "chunks, or cancel the upload."
                    )
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

    @staticmethod
    def _remove_files(directory: str, upload_id: str) -> None:
        base = os.path.join(directory, f".model_crud_{upload_id}")
        for suffix in (".part", ".json", ".json.tmp"):
            with contextlib.suppress(OSError):  # a plain unlink never follows a symlink
                os.unlink(base + suffix)

    # -- startup scan ------------------------------------------------------------------------

    def _scan(self, now: float) -> list[UploadSession]:
        """Rebuild sessions from sidecars and clean up what cannot be resumed (blocking)."""
        sessions: list[UploadSession] = []
        claimed: set[str] = set()
        scanned: set[str] = set()
        for root in self._scan_roots():
            for directory, names in _artifact_directories(root.path):
                # Roots can nest (``output/`` contains ComfyUI's ``output/checkpoints`` model
                # root), so a directory can be reached through two roots. Look at it once.
                if directory in scanned:
                    continue
                scanned.add(directory)
                by_id: dict[str, dict[str, str]] = {}
                for name in names:
                    match = ARTIFACT_RE.fullmatch(name)
                    if match:
                        by_id.setdefault(match.group(1), {})[match.group(2)] = name
                for upload_id, kinds in sorted(by_id.items()):
                    try:
                        if "json.tmp" in kinds:  # a sidecar write that never finished
                            self._sweep_orphan(directory, kinds["json.tmp"], now)
                        if "json" not in kinds:
                            for kind in ("part", "json.tmp"):
                                if kind in kinds:
                                    self._sweep_orphan(directory, kinds[kind], now)
                            continue
                        session = self._restore(directory, upload_id, now)
                        if session.final_path in claimed:
                            raise _Discard("another upload already targets this file")
                        claimed.add(session.final_path)
                        sessions.append(session)
                    except _Skip:
                        continue
                    except _Discard as why:
                        log.warning("Discarding unusable upload %s: %s", upload_id, why)
                        self._remove_files(directory, upload_id)
                    except Exception:
                        # A bug or an odd filesystem state in one upload must not stop the rest.
                        log.exception(
                            "Could not examine upload %s; leaving its files alone", upload_id
                        )
        return sessions

    def _scan_roots(self) -> list[paths.Root]:
        seen: set[str] = set()
        roots: list[paths.Root] = []
        allowed = self.config.allowed_extensions
        candidates: list[Callable[[], list[paths.Root]]] = [
            functools.partial(paths.get_roots, key, allowed) for key in sorted(comfy.folder_keys())
        ]
        candidates += [
            functools.partial(paths.get_media_roots, area, self.config.extensions_for_area(area))
            for area in MEDIA_AREAS
        ]
        for get in candidates:
            try:
                key_roots = get()
            except ModelCrudError:
                continue
            for root in key_roots:
                if root.path not in seen:
                    seen.add(root.path)
                    roots.append(root)
        return roots

    @staticmethod
    def _sweep_orphan(directory: str, name: str, now: float) -> None:
        # A temp file nobody can resume. Young ones may belong to an upload that is still being
        # created (by this or another process), so only old ones are removed.
        try:
            info = os.lstat(os.path.join(directory, name))
        except OSError:
            return
        if now - info.st_mtime >= ORPHAN_MIN_AGE:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(directory, name))
            log.info("Removed orphaned upload file %s", name)

    def _restore(self, directory: str, upload_id: str, now: float) -> UploadSession:
        """Validate one sidecar. Nothing in the file is trusted: the target is re-resolved."""
        try:
            data = read_sidecar(os.path.join(directory, f".model_crud_{upload_id}.json"))
        except (OSError, ValueError) as exc:
            raise _Discard(f"sidecar unreadable ({type(exc).__name__})") from None
        if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
            raise _Discard("unknown sidecar format")
        if data.get("upload_id") != upload_id:
            raise _Discard("sidecar does not match its file name")

        size = _int_field(data, "size", 1, self.config.max_upload_bytes)
        chunk_size = _int_field(data, "chunk_size", 1, MAX_CHUNK_SIZE)
        total = -(-size // chunk_size)
        if total > MAX_TOTAL_CHUNKS:
            raise _Discard("implausible chunk count")
        overwrite = data.get("overwrite")
        fingerprint = data.get("client_fingerprint")
        if not isinstance(overwrite, bool) or (
            fingerprint is not None and not _valid_fingerprint(fingerprint)
        ):
            raise _Discard("invalid field")
        created = _time_field(data, "created")
        expires_at = _time_field(data, "expires_at")
        if expires_at <= now:
            raise _Discard("expired while the server was down")
        received = _ranges_field(data, total)

        folder, relpath = data.get("folder"), data.get("path")
        area = data.get("area")
        if not isinstance(folder, str) or not isinstance(relpath, str):
            raise _Discard("invalid target")
        if area is not None and (area not in MEDIA_AREAS or folder != area):
            raise _Discard("invalid area")
        if area is not None and not self.config.extensions_for_area(area):
            raise _Skip()
        subfolder, _, filename = relpath.rpartition("/")
        try:
            if area is None:
                target = paths.resolve_upload_target(
                    folder, filename, subfolder or None, self.config.allowed_extensions
                )
            else:
                target = paths.resolve_upload_target(
                    area,
                    filename,
                    subfolder or None,
                    self.config.extensions_for_area(area),
                    media=True,
                )
        except ModelCrudError:
            raise _Discard("target no longer valid") from None
        # The sidecar must live exactly where the resolver says this upload belongs. That also
        # rules out sidecars in secondary roots, which uploads never use; comparing against the
        # scanned root instead would discard valid sessions when roots nest.
        if target.relpath != relpath or target.directory != os.path.realpath(directory):
            raise _Discard("sidecar is not in its upload directory")

        part = part_path(target.directory, upload_id)
        try:
            info = os.lstat(part)
        except OSError:
            raise _Discard("temp file is missing") from None
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise _Discard("temp file does not match the sidecar")

        session = UploadSession(
            id=upload_id,
            folder=target.folder,
            relpath=target.relpath,
            root=target.root,
            directory=target.directory,
            final_path=target.entry,
            part_path=part,
            size=size,
            chunk_size=chunk_size,
            overwrite=overwrite,
            client_fingerprint=fingerprint,
            created=created,
            expires_at=expires_at,
            area=area,
            received=received,
        )
        session.saved_version = session.version
        return session


# -- sidecar files ---------------------------------------------------------------------------


def part_path(directory: str, upload_id: str) -> str:
    return os.path.join(directory, f".model_crud_{upload_id}.part")


def sidecar_payload(session: UploadSession) -> dict[str, Any]:
    return {
        "version": SIDECAR_VERSION,
        "upload_id": session.id,
        "folder": session.folder,
        "area": session.area,
        "path": session.relpath,
        "size": session.size,
        "chunk_size": session.chunk_size,
        "overwrite": session.overwrite,
        "client_fingerprint": session.client_fingerprint,
        "created": session.created,
        "expires_at": session.expires_at,
        "received": to_ranges(session.received),
    }


def write_sidecar(directory: str, upload_id: str, payload: dict[str, Any]) -> None:
    """Replace the sidecar atomically: a reader sees the old or the new file, never half of one."""
    final = os.path.join(directory, f".model_crud_{upload_id}.json")
    tmp = final + ".tmp"
    data = json.dumps(payload, separators=(",", ":")).encode()
    with _translate_os_errors():
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
        try:
            fsutil.write_at(fd, 0, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)


def read_sidecar(path: str) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SIDECAR_BYTES:
            raise ValueError("not a plain, small file")
        data = os.read(fd, MAX_SIDECAR_BYTES + 1)
    finally:
        os.close(fd)
    return json.loads(data)


def _artifact_directories(root: str) -> Iterator[tuple[str, list[str]]]:
    """Directories an upload can live in (the root and up to 4 levels of visible subfolders)."""
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        yield directory, [e.name for e in entries]
        if depth < paths.MAX_SUBFOLDER_DEPTH:
            for entry in entries:
                with contextlib.suppress(OSError):
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        stack.append((entry.path, depth + 1))


def _int_field(data: dict[str, Any], name: str, low: int, high: int) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _Discard(f"invalid {name}")
    return value


def _time_field(data: dict[str, Any], name: str) -> float:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _Discard(f"invalid {name}")
    return float(value)


def _ranges_field(data: dict[str, Any], total: int) -> set[int]:
    ranges = data.get("received")
    if not isinstance(ranges, list) or len(ranges) > total:
        raise _Discard("invalid received ranges")
    received: set[int] = set()
    for item in ranges:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(isinstance(n, bool) or not isinstance(n, int) for n in item)
            or not 0 <= item[0] <= item[1] < total
        ):
            raise _Discard("invalid received ranges")
        received.update(range(item[0], item[1] + 1))
    return received


def _hash_file(fd: int, size: int) -> str:
    """SHA-256 of the whole file, dropping pages from the cache as we go (FR-U14)."""
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        block = os.pread(fd, min(HASH_BLOCK, size - offset), offset)
        if not block:
            break
        digest.update(block)
        offset += len(block)
        with contextlib.suppress(OSError):
            os.posix_fadvise(fd, offset - len(block), len(block), os.POSIX_FADV_DONTNEED)
    return digest.hexdigest()


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
