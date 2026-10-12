# CLAUDE.md

## Project

`comfyui-model-crud` is a ComfyUI custom-node extension (GPL-3.0). It lets users **list, upload and delete model files** through:

1. a REST API served by ComfyUI's own aiohttp server, and
2. a UI inside the ComfyUI frontend that calls that API.

It is installed by cloning/copying into `ComfyUI/custom_nodes/comfyui-model-crud/`. There is no standalone server and no separate build of ComfyUI; the extension runs inside the ComfyUI process.

The repo is new. The layout below is the intended structure; update this file when it changes.

```
__init__.py          # ComfyUI entry point: registers routes, exports WEB_DIRECTORY
pyproject.toml       # Comfy Registry metadata ([tool.comfy]) + dev tooling config
api/                 # aiohttp route handlers (thin: parse request, call service, shape response)
services/            # Pure-ish logic: path resolution, validation, file ops. No aiohttp imports.
web/                 # Frontend JS served by ComfyUI (WEB_DIRECTORY = "./web"); web/package.json makes Node treat it as ESM
tests/               # pytest; runnable without a ComfyUI install (see Testing)
scripts/              # dev.sh (local ComfyUI), later model_crud_upload.py (reference CLI client)
docs/                 # comfyui-notes.md (verified ComfyUI internals), later benchmarks, security review
```

## ComfyUI integration rules

- **Entry point**: `__init__.py` must define `NODE_CLASS_MAPPINGS = {}` (empty is fine, this extension adds no nodes) and `WEB_DIRECTORY = "./web"`, and export both via `__all__`. Importing the package must register the routes as a side effect.
- **Routes**: register on `PromptServer.instance.routes` (`from server import PromptServer`). ComfyUI exposes every route under both `/path` and `/api/path`; the frontend should call `api.fetchApi("/model_crud/...")` and never hardcode the `/api` prefix or a host.
- **Namespace all routes** under `/model_crud/` to avoid colliding with core or other extensions (e.g. `GET /model_crud/folders`, `GET /model_crud/models/{folder}`, `POST /model_crud/uploads`, `DELETE /model_crud/models/{folder}?path=...`). [SPEC.md](SPEC.md) is the source of truth for endpoints, error codes and requirements; keep it and the README in sync with the code.
- **Model directories come from ComfyUI, never from hardcoded paths.** Use `folder_paths` (`folder_names_and_paths`, `get_folder_paths(name)`, `models_dir`, `supported_pt_extensions`). This is what makes `extra_model_paths.yaml` and custom model locations work. Do not assume `ComfyUI/models/...`.
- A model "folder" in the API is a **ComfyUI folder key** (`checkpoints`, `loras`, `vae`, ...), not a filesystem path. Clients send the key; the server resolves it.
- After an upload or delete, invalidate ComfyUI's cached listing so node dropdowns update: `folder_paths.filename_list_cache.pop(folder_key, None)` (verified to exist; the key is the canonical folder key, which is why legacy aliases like `unet` are rejected). See [docs/comfyui-notes.md](docs/comfyui-notes.md) for everything verified against ComfyUI's source.
- Only expose folder keys whose ComfyUI extension set intersects our allowlist (FR-L1); `folder_names_and_paths` also contains `custom_nodes`, `datasets`, `configs`, ...
- The listen address for SR-10 is `comfy.cli_args.args.listen` (guarded import), not `PromptServer.address`, which is unset while custom nodes load.
- Import only what ComfyUI guarantees at runtime (`server`, `folder_paths`, `aiohttp`, stdlib). Do not add runtime Python dependencies unless unavoidable; if you do, list them in `requirements.txt` (ComfyUI Manager installs from it) and justify it in the PR.
- Never block the event loop: file hashing, large copies, `os.walk` over big trees, and deletes of large files go through `asyncio.to_thread` / `run_in_executor`. Uploads follow the chunked/resumable protocol in SPEC.md §5.2/§6.4 (target of 50 GB files): one preallocated temp file, chunks written at their offset, no assembly step, `fsync` + atomic rename on completion. Read chunk bodies from `request.content` in ≤ 1 MiB blocks and write them in a dedicated thread pool; never use `request.read()`, `request.post()` or `request.multipart()` for uploads (they buffer, and are bounded by aiohttp's `client_max_size`).

## Security rules (non-negotiable)

This extension writes and deletes files on the host. ComfyUI has no built-in auth, and users often expose it on a LAN or tunnel. Treat every request as hostile.

- **Path traversal**: resolve the target with `os.path.realpath` and verify it is inside one of `get_folder_paths(folder_key)` using `os.path.commonpath`. Reject `..`, absolute paths, drive letters, NUL bytes, and symlinks that escape the root. Do this check in one shared function in `services/` and use it for every operation; never build paths with raw string concatenation.
- **Allowlist file extensions** (start from `folder_paths.supported_pt_extensions`: `.ckpt .pt .pt2 .bin .pth .safetensors .pkl .sft`, plus `.gguf` and `.onnx`; adding any other type needs a deliberate decision recorded in SPEC.md SR-4). Reject everything else on upload, and refuse to delete files that are not models.
- **Uploads never overwrite silently.** Default to `409 Conflict` if the file exists; overwriting requires an explicit `overwrite=true`. Write to a temp file in the same directory and `os.replace` on success so a failed/aborted upload never leaves a half-written model that ComfyUI would list. Clean up the temp file on any error.
- **Enforce the max upload size** at session creation and on every chunk (never trust `Content-Length` alone), and bound session count and preallocated space so anonymous clients can't fill the disk.
- Upload endpoints after session creation accept only the `upload_id` (validated against a strict regex), never a path.
- **Deletes**: files only (no recursive directory deletion), one file per request, no wildcards. Return `404` for missing files rather than succeeding silently.
- **Sanitize filenames**: take `os.path.basename` of the client-supplied name (subfolders, if supported, are a separate validated field), strip control characters, reject empty names and names starting with `.`.
- Don't log full filesystem paths of users' machines at INFO level, and never return absolute server paths in API responses — return paths relative to the folder root.
- Don't add CORS wildcard headers or disable ComfyUI's origin checks.

## REST API conventions

- JSON in, JSON out (`web.json_response`), except upload chunk bodies, which are raw `application/octet-stream`.
- Consistent error shape: `{"error": "<machine_code>", "message": "<human text>"}` with accurate status codes (`400` bad input, `404` missing, `409` conflict, `413` too large, `415` bad extension, `500` unexpected).
- Listing returns, per file: relative `path`, `name`, `size` (bytes), `modified` (ISO-8601 UTC). Keep it cheap — no hashing or safetensors header parsing in the list call (make that a separate opt-in endpoint if ever needed).
- Handlers stay thin. Validation and filesystem logic live in `services/` so they can be unit-tested without aiohttp or ComfyUI.
- Catch expected errors (`FileNotFoundError`, `PermissionError`, `OSError` with `ENOSPC`) and map them to proper responses; let truly unexpected ones log a traceback and return `500` without leaking internals.

## Frontend conventions

- Plain ES modules in `web/`, no build step unless it becomes unavoidable (ComfyUI serves `WEB_DIRECTORY` files as-is, and **every** `.js` file under it, recursively, is loaded as an extension, so helper modules must have no import-time side effects and tests must live outside `web/`). If a bundler is introduced, commit the built output or document the build clearly, since users install by `git clone`.
- Register via `app.registerExtension({ name: "ModelCrud.<Feature>", ... })` using `import { app } from "../../scripts/app.js"` and `import { api } from "../../scripts/api.js"`. The `../../scripts/` imports are relative to `/extensions/<folder-name>/`, so do not rename or relocate files in `web/` casually.
- Prefer the frontend's official extension APIs (sidebar tab via `app.extensionManager.registerSidebarTab`, settings via the `settings` field, toasts via `app.extensionManager.toast`) over patching DOM or core prototypes. Don't monkey-patch ComfyUI internals.
- The upload queue lives for the page's lifetime, outside the sidebar tab's `render`/`destroy`, so switching panels never orphans a running transfer.
- Use `api.fetchApi` for all requests (default timeout is 60 s: pass `{ timeoutMs: null }` for long calls such as `/complete` with a hash; use `api.apiURL(path)` to build XHR URLs). For uploads, use the chunk scheduler in `web/uploader.js`: `Blob.slice` (never load the whole file), parallel chunk `XMLHttpRequest`s for byte progress, retry with backoff, resume from the server's `received_ranges`; surface server `message` text on failure.
- **Always confirm before delete** in the UI, showing the filename and folder.
- Escape/`textContent` everything that comes from the server (filenames are untrusted input). Never use `innerHTML` with them.
- Style with ComfyUI's CSS variables (`--comfy-menu-bg`, `--fg-color`, ...) so it follows light/dark themes. Namespace all CSS classes with `model-crud-`.

## Code style

- Linux only (macOS and Windows are unsupported): Linux-specific calls (`os.posix_fallocate`, `os.posix_fadvise`, `os.link`) are fine without platform guards. Don't write cross-platform fallbacks or Windows/macOS-specific code. Filesystem-level fallbacks (e.g. `EOPNOTSUPP` from `posix_fallocate`) are still required.
- Python ≥ 3.10 (match what current ComfyUI supports). Type hints on public functions, `pathlib.Path` over string paths where it doesn't fight `folder_paths`, `logging.getLogger("comfyui-model-crud")` instead of `print`.
- Format/lint with `ruff` (`ruff format`, `ruff check`); configuration lives in `pyproject.toml`. Keep line length at 100.
- JS: 2-space indent, `const`/`let`, no unused globals, semicolons.
- Keep comments for the *why* (especially security decisions and ComfyUI quirks). Don't narrate what the code does.
- Every new file starts without a license header; the project license is in `LICENSE` (GPL-3.0). Don't copy code from incompatibly licensed projects.

## Testing

- `pytest` for everything under `services/` and for handlers via `aiohttp.test_utils`. Tests must not require a running ComfyUI: stub `folder_paths` (and `server.PromptServer`) in `tests/conftest.py` using `tmp_path` as the model roots.
- Required test cases for any change touching file operations: traversal attempts (`../`, absolute paths, encoded variants, symlink escape), disallowed extension, existing-file conflict, aborted upload leaves no partial file, delete of missing file, and filenames with unicode/spaces.
- JS: `node --test tests/js/*.test.mjs` (Node's built-in runner, no dependencies). UI changes are also checked in a real browser with `scripts/ui_check/` (see its README); do not claim a UI works without that or an explicit note that it was not run.
- Manual check before declaring a feature done: start ComfyUI with this extension in `custom_nodes/`, confirm it loads without errors in the console, exercise list/upload/delete from the UI, and confirm that a new file appears in a loader node's dropdown after refresh. If you couldn't run ComfyUI, say so instead of claiming it works.

## Packaging & release

- `pyproject.toml` carries Comfy Registry metadata under `[tool.comfy]` (`PublisherId`, `DisplayName`, `Icon`) and a `[project]` version. Bump the version on every release and keep a `CHANGELOG.md`.
- Publishing is via `comfy node publish`; do not run it, or push tags/releases, without the user asking.
- Keep the repo free of model files, caches, and `__pycache__`; maintain `.gitignore` accordingly.

## Working agreements

- Check the targeted ComfyUI API (`folder_paths`, `PromptServer`) against its actual source before relying on a function; these internals change between versions. Document the minimum ComfyUI version you tested against in the README.
- **Log every implemented step.** Work is sequenced by the prompts in [IMPLEMENT.md](IMPLEMENT.md). When a step is finished, append an entry to [IMPLEMENTED.md](IMPLEMENTED.md) using the template at the top of that file, as the last action of the step and before reporting it done. Record only what actually happened: the checks you ran with their results, what you could not verify (no real ComfyUI or browser run, etc.), and any deviation from SPEC.md. Do not log a step that is partial as "done". Read IMPLEMENTED.md at the start of a step to see what already exists.
- Don't commit or push unless asked.
- Prefer small, reviewable changes. Security-relevant code (path validation, upload, delete) gets tests in the same change.
