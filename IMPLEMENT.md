# IMPLEMENT.md — step-by-step build prompts

Paste these prompts into Claude Code **in order**, one step per session. [SPEC.md](SPEC.md) defines *what* to build and [CLAUDE.md](CLAUDE.md) defines *how*; the prompts only sequence the work.

## How to use

1. Run one step, review the diff, run the step's **Verify** commands yourself, then commit before starting the next step (the prompts tell Claude not to commit).
2. Start each step with a fresh context (`/clear`). Every prompt is self-contained and points at the spec by requirement ID.
   - **Logging rule (from CLAUDE.md):** when a step is finished, Claude appends an entry to [IMPLEMENTED.md](IMPLEMENTED.md) (status, files, checks actually run, deviations, follow-ups). If an entry is missing when you review a step, ask for it before committing.
3. If a step reveals that the spec is wrong or ambiguous, fix [SPEC.md](SPEC.md) first, then continue. The spec stays the source of truth.
4. Steps 0–1 set up the project and a real ComfyUI to test against. Steps 2–7 build the backend. Steps 8–9 build the UI. Steps 10–11 harden and release.

## Decisions assumed by these prompts

The two open questions in SPEC §13 are resolved like this. Change them here and in step 0 if you disagree.

- Writes are **enabled by default**; the server only logs a warning when ComfyUI listens on a non-loopback address (SR-10).
- Uploads go to the **first root** of the folder key; there is no client-selectable root for uploads.

---

## Step 0 — Scaffold the project

**Goal:** a loadable, empty extension with tooling, and the spec updated with the decisions above.

```text
Read CLAUDE.md and SPEC.md fully. Then:

1. In SPEC.md §13, remove the two open questions and record the decisions: writes are enabled by default with only a startup warning on non-loopback listen addresses (SR-10), and uploads always go to the first root of the folder key.
2. Create the layout from SPEC §10: __init__.py, api/, services/, web/, tests/. Do not implement features yet.
3. __init__.py must export NODE_CLASS_MAPPINGS = {} and WEB_DIRECTORY = "./web" via __all__, and register nothing yet except a guarded import: any exception while importing this package's own modules must be logged and must not stop ComfyUI from starting (NFR-5).
4. pyproject.toml: [project] name/version 0.1.0/requires-python >=3.10/GPL-3.0 license, an optional "dev" dependency group (pytest, pytest-asyncio, pytest-aiohttp, aiohttp, ruff), [tool.ruff] with line-length 100, [tool.pytest.ini_options], and a [tool.comfy] table with placeholder PublisherId, DisplayName "Model CRUD", Icon "".
5. Add .gitignore (Python, venv, caches, .pytest_cache, .ruff_cache), an empty requirements.txt (with a comment saying no runtime deps), a short README.md stub, and CHANGELOG.md with an "Unreleased" section.
6. tests/conftest.py: fixtures that fake `folder_paths` (folder keys -> tmp_path roots, supported_pt_extensions, a filename_list_cache dict) and `server.PromptServer` (an object with an aiohttp RouteTableDef at .instance.routes), injected via sys.modules so tests never need ComfyUI.
7. Create a virtualenv at .venv (gitignored), install the dev group, and make `ruff check`, `ruff format --check` and `pytest` pass (pytest may collect zero tests; add one smoke test that imports the package with the fakes).

Do not commit. At the end, list the files you created and the commands to run the checks.
```

**Verify:** `.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .`

---

## Step 1 — Real ComfyUI dev environment and API verification

**Goal:** a local ComfyUI to test against, and confirmation that the ComfyUI internals CLAUDE.md relies on actually exist.

```text
Set up a local ComfyUI for developing this extension. Do not modify ComfyUI's code and do not put it inside this repo.

1. Clone https://github.com/comfyanonymous/ComfyUI next to this repo at ../ComfyUI-dev (ask me first if that path is taken). Create its own venv with Python 3.12 or 3.13 and install its requirements; use the CPU-only PyTorch build to save disk space, since we never run inference.
2. Symlink this repo into ../ComfyUI-dev/custom_nodes/comfyui-model-crud.
3. Add scripts/dev.sh that starts ComfyUI with --cpu --listen 127.0.0.1 on port 8188 using that venv.
4. Start it, confirm our extension is imported without errors in the console, and stop it.
5. Read the actual ComfyUI source (folder_paths.py, server.py) and verify every ComfyUI assumption made in CLAUDE.md and SPEC.md, specifically: PromptServer.instance.routes and the automatic /api prefix; folder_paths.get_folder_paths, folder_names_and_paths, supported_pt_extensions; the name and shape of the filename list cache and how to invalidate it for one folder key; how the aiohttp app's client_max_size is configured; whether the server has any origin/CSRF middleware that affects PUT/POST/DELETE from the frontend; how WEB_DIRECTORY files are served and which import paths (../../scripts/app.js, api.js) the frontend extension API uses; and whether app.extensionManager.registerSidebarTab exists in the bundled frontend.
6. Write the findings to docs/comfyui-notes.md (what is true, with file and line references into ComfyUI, and the ComfyUI commit hash you checked). If anything contradicts CLAUDE.md or SPEC.md, fix those files and list the changes you made.

Do not commit. Summarize contradictions first, then the rest.
```

**Verify:** `scripts/dev.sh` starts and prints no tracebacks from `comfyui-model-crud`; read `docs/comfyui-notes.md`.

---

## Step 2 — Core services: errors, config, path safety

**Goal:** the security foundation, test-first.

```text
Implement the foundation in services/ per SPEC §7, §8.1, §8.2 and CLAUDE.md "Security rules". No HTTP code in this step.

1. services/errors.py: a base ModelCrudError carrying (status, code, message) plus subclasses for every error code in SPEC §6.
2. services/config.py: parse all MODEL_CRUD_* environment variables from SPEC §7 into an immutable dataclass with validation (CHUNK_MB 8-256, positive numbers, extension list normalised to lower-case with leading dot). READ_ONLY=1 overrides ALLOW_UPLOAD/ALLOW_DELETE. Invalid values must raise a clear error that is logged and falls back to the default for that variable.
3. services/paths.py: the one shared resolver (SR-1..SR-5): folder key + relative path (+ optional root_index) -> real path, with sanitize_filename and extension allowlist checks, subfolder depth limit 4. A thin adapter module (services/comfy.py) wraps every access to folder_paths so tests can replace it.
4. Write tests FIRST in tests/test_paths.py and tests/test_config.py, then make them pass. Cover every traversal case in SPEC §11: ../x, /etc/passwd, a/../../x, encoded variants, backslashes, NUL bytes, symlink pointing outside the root, symlink inside the root, overlong names, names starting with '.', upper-case and double extensions (x.safetensors.exe), unicode and spaces, unknown folder keys, multi-root folder keys with root_index.

No aiohttp imports in services/. ruff and pytest must pass. Do not commit; summarise what the resolver rejects and how.
```

**Verify:** `pytest tests/test_paths.py tests/test_config.py -q`; skim `services/paths.py` — this is the file to review hardest.

---

## Step 3 — Read-only API (M1)

```text
Implement SPEC milestone M1: the read-only API.

1. services/models.py: list_folders() and list_models(folder) per SPEC 5.1 (FR-L1..L6): recursive, allowed extensions only, skip symlinks that resolve outside every root, entries with path/name/size/modified (ISO-8601 UTC)/root_index. Run the directory walk in a thread (asyncio.to_thread) from the handler, never in the event loop. Do not read file contents.
2. api/routes.py: handlers for GET /model_crud/config, /folders and /models/{folder} (SPEC 6.1-6.3) registered on PromptServer.instance.routes. Handlers are thin: parse, call service, map ModelCrudError to the SPEC error shape. Unexpected exceptions return a generic 500 and log the traceback.
3. Wire registration in __init__.py (inside the guarded import from step 0).
4. Tests with aiohttp.test_utils against an app built from the fake PromptServer: normal listing, nested folders, multi-root with duplicate relative paths, ignored non-model files, symlink escape skipped, unknown folder -> 404, config reflecting env vars, and a performance test listing 10 000 tiny files in under 1 s.

Then manually test against the real ComfyUI from step 1 with curl (both /model_crud/... and /api/model_crud/...) and paste the commands and outputs in your summary. Do not commit.
```

**Verify:** `pytest -q`, then `scripts/dev.sh` and `curl -s localhost:8188/api/model_crud/folders | jq`.

---

## Step 4 — Delete (M2)

```text
Implement SPEC milestone M2: delete (FR-D1..D5, SR-7, SPEC 6.5).

1. services/models.py: delete_model(folder, path, root_index) -> uses the shared resolver, allowed extension only, files only (never directories, no wildcards), os.unlink in a thread, 404 for missing, empty parent dirs left in place.
2. Invalidate the ComfyUI filename cache for that folder key through services/comfy.py (use the mechanism documented in docs/comfyui-notes.md). If upload/delete is disabled by config, return 403 delete_disabled before touching the filesystem.
3. Route DELETE /model_crud/models/{folder}?path=...&root_index=...
4. Tests: successful delete, cache invalidated, missing file 404, directory path rejected, disallowed extension 415, every traversal case again at the HTTP layer, root_index selecting the right duplicate, disabled via READ_ONLY and via ALLOW_DELETE=0, logging of the INFO line (NFR-6) and the WARNING for traversal attempts.
5. Manual test against the real ComfyUI: put a dummy .safetensors file in models/loras, confirm it appears in a LoraLoader dropdown after refresh, delete it through the API, confirm it disappears from the dropdown.

Do not commit.
```

**Verify:** `pytest -q`; the manual dropdown check above.

---

## Step 5 — Upload core (M3)

```text
Implement SPEC milestone M3: the core of the chunked upload protocol (SPEC 5.2 FR-U1..U5, U8..U11, U13..U15, 6.4.1, 6.4.2, 6.4.5, 6.4.6). Resume persistence, hashes and expiry come in the next step, but design the session object so they fit without rewrites.

1. services/fsutil.py: preallocate(fd, size) using os.posix_fallocate with the EOPNOTSUPP fallback to ftruncate; free-space check (size + 1 GiB margin) via os.statvfs; write_at(fd, offset, data) via os.pwrite; flush_range(fd, offset, length) = fsync + posix_fadvise(DONTNEED); publish_no_replace(tmp, dst) via os.link + os.unlink (409 if dst exists); publish_replace via os.replace.
2. services/uploads.py: in-memory session registry. create_session validates everything once (shared resolver, extension, size cap, overwrite vs existing file, one session per target, MAX_ACTIVE_UPLOADS, free space), creates the target subfolder, creates and preallocates ".model_crud_<id>.part" in the target directory, and returns the session. The upload_id is secrets.token_urlsafe(16) and every endpoint validates it against ^[A-Za-z0-9_-]{22,64}$ before use. write_chunk(id, index, stream, content_length) checks the expected length exactly, reads the stream in blocks of at most 1 MiB, writes via a dedicated ThreadPoolExecutor with os.pwrite, flushes the range, and only then marks the chunk received. A truncated body (client disconnect) must NOT mark the chunk received. complete(id) verifies all chunks are present (409 incomplete_upload with missing_ranges), fsyncs, publishes atomically, invalidates the filename cache, deletes the session. cancel(id) removes the temp file.
3. Routes: POST /model_crud/uploads, PUT /model_crud/uploads/{id}/chunks/{index}, POST /model_crud/uploads/{id}/complete, DELETE /model_crud/uploads/{id}. Chunk bodies must be read from request.content, never request.read()/post()/multipart(). Respect READ_ONLY / ALLOW_UPLOAD (403 uploads_disabled).
4. Tests per SPEC §11 "Upload protocol", "Limits" and "Publish": out-of-order and parallel chunks yield a byte-identical file; re-sent chunk is harmless; wrong Content-Length, bad index and malformed ids are rejected; oversize declared size 413; session limit 429; same-target 409; overwrite false/true; completion race cannot clobber an existing file; a test that uploads a body larger than aiohttp's client_max_size to prove we stream.

Do not commit. In the summary, state what is still missing from the SPEC for uploads (it should be exactly: sidecar persistence/resume, GET status/list, hashes, expiry).
```

**Verify:** `pytest -q`; try the flow once by hand with curl against the real ComfyUI using a small file.

---

## Step 6 — Upload resilience (M4)

```text
Implement SPEC milestone M4: persistence, resume, integrity and cleanup (FR-U5, U6, U7, U12, SPEC 6.4.3, 6.4.4, 6.4.5 sha256 option).

1. Sidecar state: services/uploads.py persists each session to ".model_crud_<id>.json" next to the .part file (target, size, chunk_size, received ranges as inclusive chunk-index ranges, overwrite, client_fingerprint, created, expires_at), written atomically (tmp + os.replace) after each recorded chunk, under a per-session asyncio.Lock. At startup, scan the model roots for sidecars and rebuild sessions; discard sessions whose .part file is missing or whose size mismatches.
2. GET /model_crud/uploads/{id} (status with received_ranges and received_bytes) and GET /model_crud/uploads (list).
3. Integrity: per-chunk SHA-256 via the Upload-Chunk-SHA256 header, computed incrementally in the executor while receiving; mismatch -> 422 checksum_mismatch and the chunk is not recorded. Optional whole-file sha256 on complete, computed in a worker thread; mismatch -> 422 and the temp file is kept.
4. Expiry: sessions idle longer than MODEL_CRUD_UPLOAD_TTL_HOURS are deleted with their .part and .json files, at startup and then hourly (an asyncio background task that is cancelled cleanly on shutdown). Every chunk request refreshes expires_at.
5. Tests: rebuild the registry from sidecars mid-upload and finish the upload (simulated restart); truncated body not recorded; chunk hash mismatch; whole-file hash match and mismatch; expiry removes both files; startup scan ignores orphans safely; received_ranges merging is correct (property-style test with random chunk orders).
6. Add the opt-in slow test from SPEC §11 (5 GB sparse file, 8 MiB chunks, bounded RSS) behind a pytest marker "slow".

Do not commit.
```

**Verify:** `pytest -q` and `pytest -m slow -q`; kill ComfyUI mid-upload by hand and confirm status survives a restart.

---

## Step 7 — Reference CLI client and large-file validation

**Goal:** prove the 50 GB case end to end before touching the UI, and give scripts a client (use case U4).

```text
Write scripts/model_crud_upload.py: a dependency-free (stdlib + urllib/http.client only, or aiohttp if you justify it) command-line client for our upload protocol.

Usage: model_crud_upload.py --server http://127.0.0.1:8188 --folder checkpoints [--subfolder X] [--overwrite] [--parallel N] [--chunk-hash] [--sha256] FILE...
It must: create the session, read the file lazily in chunks (never load it fully), send chunks in parallel with retries and exponential backoff, resume an existing session when the same fingerprint (name:size:mtime) is found via GET /uploads, show progress/speed/ETA, call complete (with the whole-file sha256 if --sha256), and exit non-zero on failure.

Then validate against the real ComfyUI from step 1:
1. Upload a 2 GB random file; compare sha256 of source and result.
2. Upload a ~50 GB file (use a sparse file plus a few random regions, or ask me before generating real data; check free disk space first and tell me the exact space needed). Record wall time, average MB/s, server RSS before/during/after (ps), and page-cache behaviour if you can observe it.
3. Interrupt the client with Ctrl-C and also kill -9 ComfyUI mid-upload; resume each time and confirm only missing chunks are re-sent and the final hash matches.
4. Try --parallel 1, 2, 4, 8 on the 2 GB file and report which setting is fastest on this machine; update the defaults in SPEC §7 if the data justifies it.

Put results in docs/benchmarks.md. Fix any bugs the validation exposes in the services (with regression tests). Do not commit.
```

**Verify:** read `docs/benchmarks.md`; `sha256sum` source vs. uploaded file yourself.

---

## Step 8 — UI: sidebar tab, list and delete (M5, part 1)

```text
Implement the first half of SPEC milestone M5 in web/ (FR-UI1, UI2, UI4, UI5, UI6, UI7, SR-11). Follow CLAUDE.md "Frontend conventions" and docs/comfyui-notes.md for the exact import paths and extension API of the bundled frontend.

1. web/model_crud.js registers the extension "ModelCrud.Sidebar" and a sidebar tab "Models" (use the official sidebar API verified in step 1; if it is unavailable in the bundled frontend, tell me before choosing a fallback).
2. The tab shows: folder picker (from /folders), file table (name, size in human units, modified), client-side sort by column and text filter, refresh button, total count and total size. Fetch via api.fetchApi only. Render all server strings with textContent. All CSS classes prefixed "model-crud-", colors from ComfyUI CSS variables so light and dark themes work; usable on a narrow viewport.
3. Delete: per-row delete button -> confirmation dialog naming the file and folder -> DELETE call -> list refresh -> toast. Error messages from the server are shown.
4. Read /config on load; when delete_enabled is false, hide or disable the buttons with an explanatory tooltip (the upload area will be added in the next step).
5. Keep modules small: separate api client (web/api.js), state, and view code. No build step, no dependencies.

Then actually open ComfyUI in a browser (use the browser tools available to you, or give me a precise checklist and wait for my results) and verify: tab appears, listing works, sorting and filtering work, delete works and the file disappears from a loader dropdown, READ_ONLY mode disables delete, dark and light themes look right. Report what you verified and what you could not. Do not commit.
```

**Verify:** open ComfyUI, click through the checklist yourself.

---

## Step 9 — UI: chunked uploader (M5, part 2)

```text
Implement the uploader in the UI (SPEC FR-UI3, SPEC 6.4, NFR-3). Create web/uploader.js as a framework-free module with no DOM access (so it is testable), and the upload panel in the Models tab.

uploader.js: given a File and options, create the session, then run a scheduler that sends chunks with Blob.slice using XMLHttpRequest (for byte-level upload progress), N parallel (default 3, capped by max_parallel from /config), each chunk retried up to 5 times with exponential backoff and jitter, failed chunks only; optional per-chunk SHA-256 with crypto.subtle (setting, default off, because hashing costs CPU); pause/resume/cancel (cancel calls DELETE /uploads/{id}); aggregate progress, rolling-average speed and ETA; on network loss, wait for the 'online' event; request a screen wake lock while active (best effort, ignore failures); resume support by listing GET /uploads and matching client_fingerprint (name:size:lastModified), using received_ranges so only gaps are sent. Never hold more than parallel x chunk_size bytes of the file in memory.

UI: a button plus drag-and-drop onto the tab; files queue and are processed one at a time; each row shows progress bar, speed, ETA, pause/resume and cancel; unfinished server sessions are listed after a reload with a "Resume (select file)" action; completed uploads refresh the list; server error messages are displayed; the upload area is hidden or disabled with an explanation when upload_enabled is false; an overwrite checkbox that is off by default and a 409 file_exists message that offers "retry with overwrite".

Add unit tests for the scheduler logic if you can run them with node (no framework needed: node's built-in test runner against a mocked XHR) — at minimum range-gap computation, retry/backoff, and pause/resume.

Verify in a real browser against the real ComfyUI: small file, 2 GB file, pause/resume, network drop (toggle offline in devtools), page reload and resume, restart ComfyUI mid-upload and resume, 409 conflict path, READ_ONLY mode. Report browser memory use during the 2 GB upload. Do not commit.
```

**Verify:** upload a real 50 GB file from the browser yourself; compare sha256.

---

## Step 10 — Review and hardening

```text
Harden the project before release. Do not add features.

1. Run /code-review at high effort on the whole codebase (git diff against the initial commit) and fix every confirmed finding with a regression test.
2. Run /security-review. Then do a manual adversarial pass against SPEC §8 and CLAUDE.md "Security rules": for each SR-* item, point to the code that enforces it and the test that proves it; add missing tests. Specifically attack: session-id handling, path inputs in every endpoint (query, JSON, URL segment), symlink races (TOCTOU between check and open: use O_NOFOLLOW / dir_fd where practical), concurrent complete() calls, concurrent chunk writes to the same index, chunk writes after cancel, temp-file leakage on every error path, sidecar tampering (a hand-edited .json must not allow writing outside the target directory), log injection via filenames, and resource exhaustion (many sessions, tiny chunks, huge JSON bodies — set sane limits on JSON body size).
3. Confirm that no module in services/ imports aiohttp, that no handler contains filesystem logic, and that no upload path uses request.read()/post()/multipart().
4. Run ruff, pytest (including -m slow), and a final manual pass through SPEC §11 "Manual acceptance" in a real ComfyUI and browser.
5. Produce docs/security-review.md: SR-* item -> enforcing code -> test. List any residual risks honestly, including that ComfyUI itself has no authentication.

Do not commit. Summarise findings by severity.
```

**Verify:** read `docs/security-review.md`; skim the diff of every fix.

---

## Step 11 — Documentation and release (M6)

```text
Prepare release 0.1.0. Do not publish, push, tag, or run `comfy node publish` — I will do that myself.

1. README.md: what it is, screenshots placeholders (list the screenshots I need to take), install (ComfyUI Manager and manual git clone), tested ComfyUI version and Python versions, quick start, full REST API reference with curl examples generated from SPEC §6, the reference upload client usage, all MODEL_CRUD_* settings, the security model (no auth in ComfyUI, what to set when exposing it, READ_ONLY), reverse-proxy notes for large uploads (request-size limits, buffering, timeouts for nginx and Cloudflare), troubleshooting, license.
2. pyproject.toml: final metadata (description, URLs placeholders for me to fill, classifiers, keywords), [tool.comfy] with the fields I must fill clearly marked, version 0.1.0. CHANGELOG.md entry for 0.1.0.
3. CI: .github/workflows/ci.yml running ruff and pytest on Python 3.10, 3.12 and 3.13 (Linux only), plus the node scheduler tests; no secrets, no publish job.
4. Make sure SPEC.md status says v0.1 implemented, every FR/SR/NFR is either implemented or explicitly listed as deferred, and CLAUDE.md "Project" layout matches the final tree.
5. Verify a clean-clone install: clone the repo to a temp directory, symlink it into ../ComfyUI-dev/custom_nodes, start ComfyUI, and run the manual acceptance list. Report anything that depended on files not committed.

Finish with a release checklist for me (what to fill in, which screenshots to take, how to publish to the Comfy Registry and what to tag).
```

**Verify:** fresh clone works; CI config passes locally via `act` or by running the same commands.

---

## After 0.1.0

Candidates, deliberately out of scope for v1 (SPEC §2): download from URL / Hugging Face / CivitAI, rename/move, safetensors metadata and previews. Write a new SPEC section before starting any of them.
