# comfyui-model-crud — Specification

Status: draft v0.1 · License: GPL-3.0

## 1. Summary

A ComfyUI custom-node extension that adds model-file management to a running ComfyUI instance:

- a **REST API** to list, upload and delete model files, and
- a **UI** in the ComfyUI frontend that uses that API.

It runs inside the ComfyUI process (aiohttp server) and adds no workflow nodes.

## 2. Goals and non-goals

### Goals
- Manage model files without shell/SFTP access to the host (useful for remote, containerized and cloud ComfyUI).
- Work with whatever model folders ComfyUI knows about, including those from `extra_model_paths.yaml`.
- Be safe to run on a machine the user does not fully trust the network of: strict path handling, no accidental data loss.
- Keep listings in ComfyUI's loader dropdowns in sync after changes.
- Scriptable: everything the UI does is available via the API.

### Non-goals (v1)
- Downloading from URLs / Hugging Face / CivitAI (candidate for v2).
- Renaming or moving files, creating or deleting directories.
- Editing model contents, converting formats, reading safetensors metadata or previews.
- Authentication or user management (ComfyUI has none; see §8).
- Trash, undo or soft delete: delete permanently removes the file (`os.unlink`), which is why the UI confirmation (FR-UI4) is mandatory.
- Managing non-model files (workflows, outputs, custom nodes).

## 3. Users and use cases

| # | Use case |
|---|----------|
| U1 | Local user browses what is installed in `loras/` and sees sizes to decide what to clean up. |
| U2 | Remote/cloud user uploads a `.safetensors` checkpoint from their browser to the server. |
| U3 | User deletes an unused model and sees it disappear from loader dropdowns. |
| U4 | Developer/automation script lists, uploads and deletes models via HTTP. |

## 4. Terminology

- **Folder key**: ComfyUI's name for a model category (`checkpoints`, `loras`, `vae`, `controlnet`, ...), as in `folder_paths.folder_names_and_paths`. Clients only ever send folder keys, never filesystem paths.
- **Root**: one of the directories registered for a folder key (`folder_paths.get_folder_paths(key)`). A key can have several roots.
- **Relative path**: a file's path under its root, `/`-separated, e.g. `sdxl/my_lora.safetensors`.

## 5. Functional requirements

### 5.1 List
- **FR-L1** List all folder keys that have at least one existing root.
- **FR-L2** List the model files under a folder key, across all its roots, recursively.
- **FR-L3** Only files with an allowed extension (§8.2) are returned. Other files are ignored.
- **FR-L4** Each entry has: relative `path`, `name`, `size` (bytes), `modified` (ISO-8601 UTC), and `root_index` (index of the root it lives in, so that duplicate relative paths in different roots are distinguishable).
- **FR-L5** Listing must not hash or open file contents.
- **FR-L6** Symlinks that resolve outside every root are skipped.

### 5.2 Upload

There is one upload protocol: **chunked, resumable, parallel**. A small file is simply a one-chunk upload. It is designed for single files of tens of GB (the reference case is a 50 GB checkpoint) over links that can drop or sit behind a proxy with request-size limits.

- **FR-U1** Upload one file into a folder key, optionally into a subfolder (created if missing, depth ≤ 4) of the first root. The target is resolved and validated **once**, when the upload session is created; later requests carry only the session id.
- **FR-U2** An upload is a *session*: the client creates it (declaring filename and total size), sends fixed-size chunks, then completes it. The server dictates the chunk size.
- **FR-U3** **No assembly step.** At creation the server preallocates one temp file of the full size in the target directory. Each chunk is written directly at `index × chunk_size` in that file, so completion is a rename, not a 50 GB copy.
- **FR-U4** **Parallel and out-of-order.** Chunks may be sent concurrently (up to `max_parallel`, advertised by the server) and in any order. A single TCP stream rarely saturates a fast or high-latency link; several do.
- **FR-U5** **Idempotent chunks.** Re-sending a chunk rewrites the same byte range and is always safe. A chunk is recorded as received only after its full body was written, flushed (`fsync`) and, if a hash was supplied, verified.
- **FR-U6** **Resumable.** Session state (target, size, chunk size, received chunk ranges, expiry) is persisted in a sidecar file next to the temp file, updated atomically after each chunk. The client can ask which chunks are missing after a dropped connection, a page reload, or a ComfyUI restart, and send only those.
- **FR-U7** **Integrity.** Optional per-chunk SHA-256 (`Upload-Chunk-SHA256` header) is verified while receiving, so a corrupt chunk is rejected and re-sent without re-reading the whole file. Optional whole-file SHA-256 at completion is for scripts that already know the hash.
- **FR-U8** **Fail early on space.** At creation the server requires free space ≥ declared size + 1 GiB margin and preallocates with `os.posix_fallocate`. On filesystems that don't support it (`EOPNOTSUPP`, e.g. some network filesystems) it falls back to a sparse `truncate`; the free-space check still applies, but a disk filled by something else mid-transfer surfaces as `507` on the affected chunk. Out-of-space is normally reported at creation, not at 90 % of the transfer.
- **FR-U9** **Atomic publish.** The file appears at its final path only on completion, after all chunks are present, the file is `fsync`ed and (optionally) the whole-file hash matches. Publishing is a rename inside the same directory. With `overwrite=false` it must not replace an existing file (hard-link-then-unlink, or the platform's no-replace rename; `409 file_exists`).
- **FR-U10** One active session per target path (`409 upload_in_progress`). At most `MODEL_CRUD_MAX_ACTIVE_UPLOADS` sessions exist at once (`429`), because each reserves disk space.
- **FR-U11** Size is enforced at creation (declared size ≤ cap, `413`) and per chunk (a chunk may not exceed `chunk_size` or write past `size`; its `Content-Length` must equal the expected length exactly).
- **FR-U12** Sessions idle longer than `MODEL_CRUD_UPLOAD_TTL_HOURS` are deleted with their temp and sidecar files, at startup and then hourly. The client can cancel explicitly.
- **FR-U13** After completion, invalidate the ComfyUI filename cache for the folder key.
- **FR-U14** **Host friendliness.** Chunk bodies are read from the request stream in ≤ 1 MiB blocks (never `request.read()`/`post()`/`multipart()`) and written by a dedicated thread pool; after each `fsync` the server calls `posix_fadvise(DONTNEED)` on the written range (best effort) so a 50 GB upload does not evict the page cache ComfyUI relies on for model loading.
- **FR-U15** No compression anywhere in the upload path (model weights are incompressible).

### 5.3 Delete
- **FR-D1** Delete exactly one file, identified by folder key + relative path (+ optional `root_index`).
- **FR-D2** Only files with an allowed extension can be deleted. Directories are never deleted.
- **FR-D3** Missing file → `404`.
- **FR-D4** After success, invalidate the filename cache for the folder key.
- **FR-D5** Empty parent subfolders are left in place.

### 5.4 UI
- **FR-UI1** A sidebar tab "Models" in the ComfyUI frontend.
- **FR-UI2** Folder picker; file table with name, size, modified; client-side sort and text filter.
- **FR-UI3** Upload via button and drag-and-drop. Multiple files are queued and sent one at a time; within a file, chunks go in parallel (default 3, capped by the server's `max_parallel`).
  - The file is read lazily with `Blob.slice`; the whole file is never loaded into memory.
  - Each chunk is sent with `XMLHttpRequest` for byte-level progress, with up to 5 retries and exponential backoff; only failed chunks are re-sent.
  - Shows aggregate progress, speed and ETA, and offers pause/resume and cancel.
  - Optionally sends a per-chunk SHA-256 (WebCrypto), toggled in settings.
  - Unfinished server sessions are listed after a reload; resuming requires re-selecting a file whose `client_fingerprint` (name + size + lastModified) matches.
  - The server's error `message` is shown on failure. Requests a screen wake lock while uploading (best effort).
- **FR-UI4** Delete with a confirmation dialog naming the file and folder.
- **FR-UI5** Refresh button; list auto-refreshes after upload/delete.
- **FR-UI6** When the server reports write operations disabled (§7), upload/delete controls are hidden or disabled with an explanation.
- **FR-UI7** Follows ComfyUI light/dark theme; usable on narrow screens.

## 6. REST API

Base: `/model_crud` (also reachable as `/api/model_crud`; clients use `api.fetchApi`). Requests and responses are JSON, except upload chunk bodies (raw bytes). Errors use one shape:

```json
{ "error": "path_outside_root", "message": "Path escapes the model folder." }
```

### 6.1 `GET /model_crud/config`
Returns what the server permits, so the UI can adapt.
```json
{
  "version": "0.1.0",
  "upload_enabled": true,
  "delete_enabled": true,
  "max_upload_bytes": 107374182400,
  "chunk_size": 67108864,
  "max_parallel": 4,
  "allowed_extensions": [".safetensors", ".ckpt", ".pt", ".pt2", ".pth", ".bin", ".pkl", ".sft", ".gguf", ".onnx"]
}
```

### 6.2 `GET /model_crud/folders`
```json
{ "folders": [ { "key": "loras", "roots": 2 }, { "key": "checkpoints", "roots": 1 } ] }
```

### 6.3 `GET /model_crud/models/{folder}`
`200`:
```json
{
  "folder": "loras",
  "files": [
    { "path": "sdxl/detail.safetensors", "name": "detail.safetensors",
      "size": 171966520, "modified": "2026-10-01T12:30:00Z", "root_index": 0 }
  ]
}
```
Errors: `404 unknown_folder`.

### 6.4 Uploads

Chunk bodies are raw `application/octet-stream` (no multipart framing to parse). `upload_id` is a 128-bit random token; it is validated against `^[A-Za-z0-9_-]{22,64}$` before it is used for anything.

Typical flow for a 50 GB file with 64 MiB chunks (800 chunks):
`POST /uploads` → many concurrent `PUT /uploads/{id}/chunks/{i}` → `POST /uploads/{id}/complete`.

#### 6.4.1 `POST /model_crud/uploads` — create a session
Request (JSON):

| Field | Required | Description |
|-------|----------|-------------|
| `folder` | yes | Folder key. |
| `filename` | yes | Sanitized to a basename (SR-5); extension checked against the allowlist. |
| `size` | yes | Total size in bytes (> 0). |
| `subfolder` | no | Relative subfolder, validated like any path. |
| `overwrite` | no | `true` to replace an existing file at completion. Default `false`. |
| `client_fingerprint` | no | Opaque string stored with the session (the UI sends name + size + lastModified) to guard resumption. |

`201`:
```json
{
  "upload_id": "Zk3v…",
  "folder": "checkpoints",
  "path": "sdxl/big.safetensors",
  "size": 53687091200,
  "chunk_size": 67108864,
  "total_chunks": 800,
  "max_parallel": 4,
  "expires_at": "2026-10-13T12:00:00Z"
}
```
Errors: `400 bad_request`, `400 invalid_filename`, `400 path_outside_root`, `403 uploads_disabled`, `404 unknown_folder`, `409 file_exists`, `409 upload_in_progress`, `413 too_large`, `415 extension_not_allowed`, `429 too_many_uploads`, `507 insufficient_storage`.

#### 6.4.2 `PUT /model_crud/uploads/{upload_id}/chunks/{index}` — send one chunk
Body: the raw bytes of chunk `index` (0-based). `Content-Length` is required and must equal `chunk_size` (or the remainder for the last chunk). Optional header `Upload-Chunk-SHA256: <hex>`.

`200`: `{ "index": 17, "received_chunks": 18, "received_bytes": 1207959552 }`
Errors: `400 bad_chunk` (index out of range, wrong length), `404 unknown_upload`, `422 checksum_mismatch` (chunk not recorded; re-send), `507 insufficient_storage`.

#### 6.4.3 `GET /model_crud/uploads/{upload_id}` — status / resume
`200`:
```json
{
  "upload_id": "Zk3v…", "folder": "checkpoints", "path": "sdxl/big.safetensors",
  "size": 53687091200, "chunk_size": 67108864, "total_chunks": 800,
  "received_ranges": [[0, 311], [313, 520]],
  "received_bytes": 35021144064,
  "client_fingerprint": "big.safetensors:53687091200:1760000000000",
  "expires_at": "2026-10-13T12:00:00Z"
}
```
`received_ranges` are inclusive chunk-index ranges; the client sends only the gaps. Errors: `404 unknown_upload`.

#### 6.4.4 `GET /model_crud/uploads` — list active sessions
`200`: `{ "uploads": [ <same fields as 6.4.3 without received_ranges> ] }`. Used by the UI to offer resumption.

#### 6.4.5 `POST /model_crud/uploads/{upload_id}/complete` — publish
Optional JSON body: `{ "sha256": "<hex of the whole file>" }`. If given, the server hashes the assembled file in a worker thread (a full read; roughly a minute for 50 GB on NVMe) and rejects on mismatch.

`201`: `{ "folder": "checkpoints", "path": "sdxl/big.safetensors", "size": 53687091200 }`
Errors: `404 unknown_upload`, `409 incomplete_upload` (body lists `missing_ranges`), `409 file_exists`, `422 checksum_mismatch` (temp file kept so the client can investigate or cancel).

#### 6.4.6 `DELETE /model_crud/uploads/{upload_id}` — cancel
Removes the temp and sidecar files and the session. `200`: `{ "upload_id": "Zk3v…", "cancelled": true }`. Errors: `404 unknown_upload`.

### 6.5 `DELETE /model_crud/models/{folder}?path=<relative path>[&root_index=<n>]`
`path` is a query parameter (not a URL segment) to avoid slash/encoding ambiguity in routing.

`200`: `{ "folder": "loras", "path": "sdxl/detail.safetensors", "deleted": true }`
Errors: `400 path_outside_root`, `403 delete_disabled`, `404 unknown_folder`, `404 file_not_found`, `415 extension_not_allowed`.

### 6.6 Status code summary
`400` malformed input · `403` operation disabled by config · `404` unknown folder / file / upload · `409` exists, upload in progress, incomplete · `413` too large · `415` extension not allowed · `422` checksum mismatch · `429` too many active uploads · `500` unexpected (generic message, traceback logged) · `507` disk full.

## 7. Configuration

Environment variables (read once at startup); no config file in v1.

| Variable | Default | Meaning |
|----------|---------|---------|
| `MODEL_CRUD_READ_ONLY` | `0` | `1` disables upload and delete (list still works). |
| `MODEL_CRUD_ALLOW_UPLOAD` | `1` | `0` disables upload only. |
| `MODEL_CRUD_ALLOW_DELETE` | `1` | `0` disables delete only. |
| `MODEL_CRUD_MAX_UPLOAD_GB` | `100` | Per-file upload size cap, in GiB. |
| `MODEL_CRUD_CHUNK_MB` | `64` | Chunk size in MiB (valid 8–256). 64 stays under the 100 MB request-body limit of common proxies/tunnels; lower it if yours is stricter. |
| `MODEL_CRUD_MAX_PARALLEL` | `4` | Max concurrent chunk requests per upload advertised to clients. |
| `MODEL_CRUD_MAX_ACTIVE_UPLOADS` | `4` | Max simultaneous upload sessions (each reserves disk space). |
| `MODEL_CRUD_UPLOAD_TTL_HOURS` | `24` | Idle time after which an unfinished session and its temp files are removed. |
| `MODEL_CRUD_EXTRA_EXTENSIONS` | empty | Comma-separated extra extensions to allow (e.g. `.yaml`), added to the defaults in SR-4. |

## 8. Security requirements

ComfyUI has no authentication and is commonly exposed with `--listen`. This extension can destroy data, so:

### 8.1 Path handling
- **SR-1** One shared resolver in `services/` turns (folder key, relative path) into a real path and is the only code that touches user-supplied paths.
- **SR-2** It rejects absolute paths, drive letters, `..` segments, NUL bytes and backslashes, then verifies `realpath(target)` is inside `realpath(root)` using `os.path.commonpath`. Symlink escapes are rejected.
- **SR-3** API responses contain only relative paths, never absolute server paths.

### 8.2 File types
- **SR-4** Allowed extensions: `.safetensors .ckpt .pt .pt2 .pth .bin .pkl .sft .gguf .onnx` plus `MODEL_CRUD_EXTRA_EXTENSIONS`. Checked case-insensitively on upload and delete.
- **SR-5** Filenames: basename only, control characters stripped, must be non-empty, must not start with `.`, ≤ 255 bytes.

### 8.3 Write safety
- **SR-6** No silent overwrite; temp-file + atomic rename. Temp files are named `.model_crud_<id>.part` (+ `.json` sidecar), so they never match the extension allowlist or appear in listings, and are removed on cancel, expiry or failed completion.
- **SR-7** Delete is single-file, no wildcards, no recursion.
- **SR-8** Size limits are enforced at session creation and per chunk (FR-U11). Chunk writes cannot exceed the declared size.
- **SR-12** Upload sessions are bound to the target resolved at creation; chunk, status, complete and cancel requests accept only the `upload_id` and never a path. Unknown or malformed ids return `404` without touching the filesystem.
- **SR-13** Session count, per-session size and preallocation are bounded (FR-U8, FR-U10) so unauthenticated clients cannot exhaust disk by opening many sessions.

### 8.4 Exposure
- **SR-9** No CORS wildcard; ComfyUI's origin checks are left intact.
- **SR-10** On startup, log one warning if ComfyUI is listening on a non-loopback address while writes are enabled, naming `MODEL_CRUD_READ_ONLY=1`.
- **SR-11** The UI renders server-provided strings with `textContent` only.

## 9. Non-functional requirements

- **NFR-1 Compatibility**: Python ≥ 3.10; current ComfyUI release and its bundled frontend (exact minimum version recorded in the README once tested). Linux only. macOS and Windows are not supported (not tested, no compatibility code).
- **NFR-2 Performance**: listing 10 000 files completes in < 1 s on local SSD; the event loop is never blocked > 50 ms by file operations (run in threads).
- **NFR-3 Large files**: a 50 GB upload works with < 200 MB extra server RSS and < 500 MB extra browser memory, and survives a mid-transfer network drop and a ComfyUI restart without re-sending completed chunks.
- **NFR-3a Throughput**: server-side processing (read, optional hash, write) must sustain ≥ 500 MB/s per session on local NVMe, so network or disk is the bottleneck, not Python. Completion (rename, no hash) takes < 1 s regardless of file size.
- **NFR-4 Dependencies**: none beyond ComfyUI's own runtime (aiohttp, stdlib).
- **NFR-5 Failure isolation**: an exception in this extension must not prevent ComfyUI from starting; import errors are logged and the extension disables itself.
- **NFR-6 Observability**: INFO log line per upload/delete (folder key, relative path, size), WARNING for rejected traversal attempts.

## 10. Architecture

```
__init__.py          registers routes, sets WEB_DIRECTORY
api/routes.py        aiohttp handlers: parse → call service → map exceptions to responses
services/paths.py    folder-key + relative-path resolution and validation (SR-1..5)
services/models.py   list / delete using paths.py
services/uploads.py  sessions, chunk writes, sidecar state, completion, expiry cleanup
services/fsutil.py   preallocate, fsync + fadvise, no-replace publish (`os.link` + `os.unlink`); POSIX only
services/config.py   env-var parsing (§7)
services/errors.py   typed exceptions carrying (status, code, message)
web/model_crud.js    extension registration, sidebar tab
web/uploader.js      chunk scheduler: slicing, parallelism, retry/backoff, pause/resume
web/…                UI components, CSS (prefix `model-crud-`)
tests/               pytest; stubs for folder_paths and server
```

Rules: `services/` imports no aiohttp and no ComfyUI modules except through a thin adapter (`folder_paths` accessor) that tests replace. Handlers contain no filesystem logic.

## 11. Testing and acceptance

Unit/integration tests (pytest + `aiohttp.test_utils`, no ComfyUI required):
- Traversal: `../x`, `/etc/passwd`, `a/../../x`, `%2e%2e`, backslashes, NUL, symlink pointing outside root.
- Extension allowlist on upload and delete, including upper-case and double extensions (`x.safetensors.exe`).
- Upload protocol: create → chunks out of order and in parallel → complete yields a byte-identical file; re-sent chunk is harmless; wrong `Content-Length`, out-of-range index and bad `upload_id` formats are rejected; per-chunk hash mismatch returns `422` and is not recorded; completing with missing chunks returns `409` with `missing_ranges`; whole-file hash mismatch returns `422`.
- Resume: kill the server object mid-upload, rebuild it from the sidecar, and finish the upload; truncated chunk body (client disconnect) is not recorded.
- Limits: declared size over cap (`413`), insufficient free space (`507` at creation), session limit (`429`), second session for the same target (`409`), expiry cleanup removes temp and sidecar files.
- Publish: existing target with `overwrite=false` → `409` and no change; with `overwrite=true` replaces atomically; concurrent completion race cannot clobber an existing file.
- No upload code path calls `request.read()`, `post()` or `multipart()` (checked by a test that sends a body larger than aiohttp's `client_max_size`).
- Scale check (marked slow, opt-in): 5 GB sparse file uploaded with 8 MiB chunks; assert RSS growth stays bounded.
- Multi-root folder keys; duplicate relative path in two roots; `root_index` delete.
- Disabled operations return `403`; `/config` reflects them.
- Unicode and space-containing filenames.

Manual acceptance (in a real ComfyUI):
1. Extension loads with no console errors; sidebar tab appears.
2. Upload a `.safetensors` file; it shows in the list and in a loader node dropdown after refresh.
3. Upload the same name again → conflict message; retry with overwrite succeeds.
3a. Upload a ~50 GB file: progress, speed and ETA update; disable the network mid-transfer, re-enable, and it continues without restarting; reload the page and resume; restart ComfyUI mid-transfer and resume; the final file's hash matches the source.
4. Delete it; it disappears from list and dropdown.
5. With `MODEL_CRUD_READ_ONLY=1`, controls are disabled and direct API calls return `403`.

## 12. Milestones

1. **M1 – Read-only**: scaffold, `paths.py`, `GET config/folders/models`, tests.
2. **M2 – Delete**: delete endpoint, cache invalidation, tests.
3. **M3 – Upload core**: `fsutil.py`, sessions, chunk write at offset, complete with atomic publish, size/space limits, tests.
4. **M4 – Upload resilience**: sidecar persistence and resume, per-chunk and whole-file hashes, expiry cleanup, session limits, tests.
5. **M5 – UI**: sidebar tab with list, delete with confirm, chunked uploader (parallelism, retry, pause/resume, progress/ETA, resume after reload).
6. **M6 – Release**: README with API docs and screenshots, `pyproject.toml` Comfy Registry metadata, CHANGELOG, v0.1.0.

## 13. Decisions

1. **Writes are enabled by default.** The server does not auto-disable upload/delete when ComfyUI listens on a non-loopback address; it only logs a startup warning (SR-10). Remote and cloud users are a primary audience, and operators who expose ComfyUI opt out with `MODEL_CRUD_READ_ONLY=1`.
2. **Uploads always go to the first root** of the folder key (FR-U1). There is no client-selectable root for uploads. `root_index` exists only to identify existing files in listings and deletes.
