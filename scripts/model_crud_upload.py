#!/usr/bin/env python3
"""Upload model files to a ComfyUI that runs comfyui-model-crud.

Chunked, parallel and resumable (see SPEC.md 5.2 and 6.4). Uses only the standard library.

    model_crud_upload.py --server http://127.0.0.1:8188 --folder checkpoints big.safetensors

Run the same command again after an interruption (Ctrl-C, dropped connection, server restart)
and only the chunks the server does not have yet are sent.
"""

import argparse
import collections
import hashlib
import http.client
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

API = "/api/model_crud"
BLOCK = 1024 * 1024  # request bodies are streamed in blocks this size, never held in memory
RETRYABLE_STATUS = {408, 422, 429, 500, 502, 503, 504}  # 422: checksum mismatch, send again
EXIT_FAILED, EXIT_INTERRUPTED = 1, 130


class UploadError(Exception):
    """A failure that retrying will not fix; the message is meant for the user."""


class RetryableError(Exception):
    """A failure worth another attempt (network trouble or a transient server answer)."""


class Progress:
    """Thread-safe byte counter that renders progress, speed and ETA."""

    def __init__(self, label: str, total: int, already: int, quiet: bool) -> None:
        self.label, self.total, self.quiet = label, total, quiet
        self.sent = already
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._samples: collections.deque[tuple[float, int]] = collections.deque(maxlen=64)
        self._last_render = 0.0
        self._tty = sys.stderr.isatty()

    def add(self, nbytes: int) -> None:
        with self._lock:
            self.sent += nbytes
            now = time.monotonic()
            self._samples.append((now, self.sent))
            if not self.quiet and now - self._last_render >= (0.25 if self._tty else 5.0):
                self._last_render = now
                self._render(now, end="\r" if self._tty else "\n")

    def speed(self, now: float) -> float:
        """Bytes per second over roughly the last five seconds."""
        recent = [s for s in self._samples if now - s[0] <= 5.0]
        if len(recent) < 2 or recent[-1][0] == recent[0][0]:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])

    def finish(self) -> None:
        if not self.quiet and self._tty:
            sys.stderr.write("\n")

    def _render(self, now: float, end: str) -> None:
        speed = self.speed(now)
        remaining = (self.total - self.sent) / speed if speed > 0 else None
        percent = 100.0 * self.sent / self.total if self.total else 100.0
        eta = format_duration(remaining) if remaining is not None else "--:--"
        sys.stderr.write(
            f"{self.label}: {percent:5.1f}%  {format_size(self.sent)} / {format_size(self.total)}"
            f"  {format_size(speed)}/s  ETA {eta}   {end}"
        )
        sys.stderr.flush()


class RateLimiter:
    """Shared by all sender threads; keeps the average rate at or below ``rate`` bytes/s."""

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self._next = time.monotonic()
        self._lock = threading.Lock()

    def wait(self, nbytes: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now) + nbytes / self.rate
            delay = self._next - now - 1.0  # allow up to a second of burst
        if delay > 0:
            time.sleep(delay)


class Client:
    """A tiny JSON-over-HTTP client with one keep-alive connection per thread."""

    def __init__(self, server: str, headers: list[str], timeout: float) -> None:
        url = urllib.parse.urlsplit(server if "://" in server else "http://" + server)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise UploadError(f"Cannot understand --server {server!r}")
        self.scheme, self.host, self.port = url.scheme, url.hostname, url.port
        self.prefix = url.path.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        for header in headers:
            name, sep, value = header.partition(":")
            if not sep or not name.strip():
                raise UploadError(f"--header must look like 'Name: value', got {header!r}")
            self.headers[name.strip()] = value.strip()
        self._local = threading.local()

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            factory = (
                http.client.HTTPSConnection
                if self.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = self._local.conn = factory(self.host, self.port, timeout=self.timeout)
        return conn

    def _drop_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | Iterator[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        """Returns (status, parsed JSON body). Network errors propagate as OSError."""
        merged = {**self.headers, **(headers or {})}
        if isinstance(body, bytes):
            merged.setdefault("Content-Length", str(len(body)))
        try:
            conn = self._connection()
            conn.request(method, self.prefix + path, body=body, headers=merged)
            response = conn.getresponse()
            raw = response.read()
        except BaseException:
            self._drop_connection()  # never reuse a connection in an unknown state
            raise
        if response.getheader("Connection", "").lower() == "close":
            self._drop_connection()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"error": "bad_response", "message": raw[:200].decode("utf-8", "replace")}
        return response.status, data if isinstance(data, dict) else {}


def format_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def describe_error(status: int, body: dict) -> str:
    message = body.get("message") or "no details"
    return f"{message} (HTTP {status}, {body.get('error', 'unknown_error')})"


def fingerprint(path: str, st: os.stat_result) -> str:
    """Matches what the browser UI sends (name, size, lastModified in ms) so either can resume."""
    return f"{os.path.basename(path)}:{st.st_size}:{int(st.st_mtime * 1000)}"


def iter_range(
    path: str, offset: int, length: int, on_bytes=None, limiter=None, stop=None
) -> Iterator[bytes]:
    """Yield ``length`` bytes of the file from ``offset`` in blocks of at most 1 MiB."""
    remaining = length
    with open(path, "rb") as f:
        f.seek(offset)
        while remaining:
            if stop is not None and stop.is_set():
                raise UploadError(
                    "stopped"
                )  # Ctrl-C or another chunk failed: do not finish this one
            block = f.read(min(BLOCK, remaining))
            if not block:
                raise UploadError(f"{path} changed while it was being uploaded (it got shorter).")
            remaining -= len(block)
            if limiter is not None:
                limiter.wait(len(block))
            yield block
            if on_bytes is not None:
                on_bytes(len(block))


def sha256_range(path: str, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    for block in iter_range(path, offset, length):
        digest.update(block)
    return digest.hexdigest()


def backoff(attempt: int) -> float:
    """1 s, 2 s, 4 s ... capped at 30 s, with jitter so parallel senders do not retry in step."""
    return min(2 ** (attempt - 1), 30) * random.uniform(0.5, 1.0)


def send_chunk(
    client: Client,
    upload_id: str,
    index: int,
    path: str,
    offset: int,
    length: int,
    *,
    chunk_hash: bool,
    progress: Progress,
    limiter: RateLimiter | None,
    retries: int,
    cancelled: threading.Event,
) -> None:
    url = f"{API}/uploads/{upload_id}/chunks/{index}"
    for attempt in range(1, retries + 2):
        counted = 0

        def count(n: int) -> None:
            nonlocal counted
            counted += n
            progress.add(n)

        try:
            headers = {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
            if chunk_hash:
                headers["Upload-Chunk-SHA256"] = sha256_range(path, offset, length)
            body = iter_range(path, offset, length, count, limiter, cancelled)
            status, data = client.request("PUT", url, body, headers)
            if status == 200:
                return
            problem = describe_error(status, data)
            if status not in RETRYABLE_STATUS:
                raise UploadError(f"Chunk {index} was refused: {problem}")
            failure: Exception = RetryableError(problem)
        except (OSError, http.client.HTTPException) as exc:  # includes timeouts and resets
            failure = RetryableError(f"{type(exc).__name__}: {exc}")
        progress.add(-counted)  # these bytes were not accepted; do not count them twice
        if attempt > retries or cancelled.is_set():
            raise UploadError(f"Chunk {index} failed after {attempt} attempts: {failure}")
        delay = backoff(attempt)
        if not progress.quiet:
            sys.stderr.write(f"\nchunk {index}: {failure}; retrying in {delay:.1f}s\n")
        if cancelled.wait(delay):
            raise UploadError(f"Chunk {index} was abandoned.")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(4 * BLOCK):
            digest.update(block)
    return digest.hexdigest()


def missing_indices(ranges: list[list[int]], total: int) -> list[int]:
    have = set()
    for first, last in ranges:
        have.update(range(first, last + 1))
    return [i for i in range(total) if i not in have]


def find_resumable(client: Client, folder: str, size: int, finger: str) -> str | None:
    status, body = client.request("GET", f"{API}/uploads")
    if status != 200:
        raise UploadError(f"Cannot list unfinished uploads: {describe_error(status, body)}")
    for item in body.get("uploads", []):
        if (
            item.get("client_fingerprint") == finger
            and item.get("folder") == folder
            and item.get("size") == size
        ):
            return item["upload_id"]
    return None


def is_published(client: Client, folder: str, relpath: str, size: int) -> bool:
    status, body = client.request("GET", f"{API}/models/{urllib.parse.quote(folder, safe='')}")
    return status == 200 and any(
        f.get("path") == relpath and f.get("size") == size for f in body.get("files", [])
    )


def upload_file(client: Client, args: argparse.Namespace, path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError as exc:
        print(f"{path}: {exc.strerror}", file=sys.stderr)
        return False
    if not os.path.isfile(path) or st.st_size == 0:
        print(f"{path}: not a non-empty regular file", file=sys.stderr)
        return False
    label, size = os.path.basename(path), st.st_size
    finger = fingerprint(path, st)
    started = time.monotonic()

    status, config = client.request("GET", f"{API}/config")
    if status != 200 or not config.get("upload_enabled"):
        reason = describe_error(status, config) if status != 200 else "uploads are disabled"
        print(f"{label}: cannot upload: {reason}", file=sys.stderr)
        return False

    upload_id = find_resumable(client, args.folder, size, finger)
    if upload_id:
        status, session = client.request("GET", f"{API}/uploads/{upload_id}")
        if status != 200:
            raise UploadError(f"Cannot read upload {upload_id}: {describe_error(status, session)}")
        received = session["received_ranges"]
    else:
        request = {
            "folder": args.folder,
            "filename": os.path.basename(path),
            "size": size,
            "overwrite": args.overwrite,
            "client_fingerprint": finger,
        }
        if args.subfolder:
            request["subfolder"] = args.subfolder
        status, session = client.request(
            "POST",
            f"{API}/uploads",
            json.dumps(request).encode(),
            {"Content-Type": "application/json"},
        )
        if status != 201:
            hint = (
                " (use --overwrite to replace it)" if session.get("error") == "file_exists" else ""
            )
            raise UploadError(f"Cannot start the upload: {describe_error(status, session)}{hint}")
        upload_id, received = session["upload_id"], []

    chunk_size, total = session["chunk_size"], session["total_chunks"]
    todo = missing_indices(received, total)
    already = size - sum(min(chunk_size, size - i * chunk_size) for i in todo)
    advertised = session.get("max_parallel", config.get("max_parallel", args.parallel))
    parallel = max(1, min(args.parallel, advertised))
    if not args.quiet:
        state = "resuming" if upload_id and received else "uploading"
        print(
            f"{label}: {state} to {args.folder}/{session['path']} as {upload_id}: "
            f"{len(todo)} of {total} chunks to send ({format_size(chunk_size)} each, "
            f"{parallel} in parallel)",
            file=sys.stderr,
        )

    whole: dict[str, str] = {}
    hasher = None
    if args.sha256:

        def hash_whole_file() -> None:
            try:
                whole["digest"] = sha256_file(path)
            except OSError as exc:
                whole["error"] = exc.strerror or str(exc)

        hasher = threading.Thread(target=hash_whole_file, name="hash", daemon=True)
        hasher.start()

    progress = Progress(label, size, already, args.quiet)
    limiter = RateLimiter(args.max_rate * 1024 * 1024) if args.max_rate else None
    cancelled = threading.Event()
    pool = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="send")
    try:
        futures = [
            pool.submit(
                send_chunk,
                client,
                upload_id,
                i,
                path,
                i * chunk_size,
                min(chunk_size, size - i * chunk_size),
                chunk_hash=args.chunk_hash,
                progress=progress,
                limiter=limiter,
                retries=args.retries,
                cancelled=cancelled,
            )
            for i in todo
        ]
        done, pending = wait(futures, return_when=FIRST_EXCEPTION)
        for future in done:
            future.result()  # re-raise the first failure
        assert not pending
    except BaseException:
        cancelled.set()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True)  # senders notice `cancelled` and stop at their next attempt
        raise
    pool.shutdown()
    progress.finish()

    complete_body = {}
    if hasher is not None:
        hasher.join()
        if "error" in whole:
            raise UploadError(f"Cannot hash the file: {whole['error']}")
        complete_body["sha256"] = whole["digest"]
    status, result = complete(client, upload_id, complete_body, args, session["path"], size)
    if status != 201:
        raise UploadError(f"Cannot finish the upload: {describe_error(status, result)}")

    elapsed = max(time.monotonic() - started, 1e-9)
    sent = size - already
    print(
        f"{label}: done, {args.folder}/{result.get('path', session['path'])} "
        f"({format_size(size)}) in {format_duration(elapsed)}, "
        f"{format_size(sent / elapsed)}/s; sent {len(todo)} chunks, "
        f"{total - len(todo)} were already on the server"
        + (f"; sha256 {whole['digest']}" if whole else "")
    )
    return True


def complete(client, upload_id, body, args, relpath, size) -> tuple[int, dict]:
    """Finish the upload. If the answer is lost the upload may still have been published."""
    payload, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
    url = f"{API}/uploads/{upload_id}/complete"
    last: Exception | None = None
    for attempt in range(1, args.retries + 2):
        try:
            status, data = client.request("POST", url, payload, headers)
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            time.sleep(backoff(attempt))
            continue
        if status == 404 and last is not None and is_published(client, args.folder, relpath, size):
            return 201, {"path": relpath}  # an earlier attempt succeeded; only its reply got lost
        if status in RETRYABLE_STATUS - {422}:
            time.sleep(backoff(attempt))
            continue
        return status, data
    raise UploadError(f"Cannot reach the server to finish the upload: {last}")


def cancel_upload(client: Client, upload_id: str) -> int:
    status, body = client.request(
        "DELETE", f"{API}/uploads/{urllib.parse.quote(upload_id, safe='')}"
    )
    if status == 200:
        print(f"cancelled {upload_id}")
        return 0
    print(f"cannot cancel {upload_id}: {describe_error(status, body)}", file=sys.stderr)
    return EXIT_FAILED


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload model files to ComfyUI: chunked, parallel and resumable.",
        epilog="Exit status: 0 all uploaded, 1 a failure, 130 interrupted (run again to resume).",
    )
    parser.add_argument(
        "--server", required=True, help="ComfyUI address, e.g. http://127.0.0.1:8188"
    )
    parser.add_argument("--folder", help="model folder key, e.g. checkpoints, loras, vae")
    parser.add_argument(
        "--subfolder", help="subfolder inside the model folder (created if missing)"
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing file")
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        metavar="N",
        help="chunks in flight (default 4, capped by the server's max_parallel)",
    )
    parser.add_argument(
        "--chunk-hash",
        action="store_true",
        help="send a SHA-256 with every chunk so the server can verify it",
    )
    parser.add_argument(
        "--sha256",
        action="store_true",
        help="hash the whole file and have the server verify it at the end",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        metavar="N",
        help="retries per chunk with exponential backoff (default 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="network timeout per request (default 120)",
    )
    parser.add_argument("--max-rate", type=float, metavar="MiB/S", help="limit the upload speed")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="'Name: value'",
        help="extra request header, e.g. for a reverse proxy; repeatable",
    )
    parser.add_argument("--quiet", action="store_true", help="no progress output")
    parser.add_argument(
        "--cancel", metavar="UPLOAD_ID", help="cancel an unfinished upload on the server and exit"
    )
    parser.add_argument("files", nargs="*", metavar="FILE")
    args = parser.parse_args(argv)
    if args.parallel < 1 or args.retries < 0 or args.timeout <= 0:
        parser.error("--parallel must be at least 1, --retries at least 0, --timeout positive")
    if args.max_rate is not None and args.max_rate <= 0:
        parser.error("--max-rate must be positive")
    if not args.cancel and not (args.folder and args.files):
        parser.error("--folder and at least one FILE are required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        client = Client(args.server, args.header, args.timeout)
        if args.cancel:
            return cancel_upload(client, args.cancel)
        ok = True
        for path in args.files:
            try:
                ok = upload_file(client, args, path) and ok
            except UploadError as exc:
                print(f"{os.path.basename(path)}: {exc}", file=sys.stderr)
                ok = False
        return 0 if ok else EXIT_FAILED
    except UploadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted; run the same command again to resume", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (OSError, http.client.HTTPException) as exc:  # TimeoutError is an OSError
        print(f"error: cannot talk to the server: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
