# ComfyUI integration notes

Verified against ComfyUI source, not from memory. Re-check when raising the minimum supported version.

- ComfyUI commit: `e8665ca8e4089ab917567cdb491308e1397663d4` (2026-10-11, "Remove some outdated code. (#16960)"), shallow clone at `../ComfyUI-dev`.
- Bundled frontend: `comfyui-frontend-package==1.55.14` (from ComfyUI's `requirements.txt`).
- Dev environment: Python 3.13.16 (via `uv`), `torch 2.14.1+cpu`, `aiohttp 3.14.5`.
- Line numbers below refer to that commit.

## Contradictions with CLAUDE.md / SPEC.md (all fixed, see "Changes made")

1. **Listen address (SR-10).** `PromptServer.address` is not available when custom nodes are imported: it is first set in `start_multi_address` after the sites start, and is marked `#TODO: remove this` (`server.py:1531`). Read `comfy.cli_args.args.listen` instead (`comfy/cli_args.py:63`; a comma-separated string, default `127.0.0.1`, bare `--listen` gives `0.0.0.0,::`).
2. **`/folders` must filter keys.** `folder_paths.folder_names_and_paths` also holds non-model keys: `custom_nodes`, `datasets` (empty extension sets), `configs` (`[".yaml"]`), `diffusers` (`["folder"]`), `classifiers` (`{""}`) (`folder_paths.py:26-67`). Exposing `custom_nodes` would let a remote client write into the directory ComfyUI executes code from. A key is now listed only if its ComfyUI extension set intersects our allowlist.
3. **Folder keys have legacy aliases.** `map_legacy` maps `unet` to `diffusion_models` and `clip` to `text_encoders` (`folder_paths.py:111-114`); `get_folder_paths` and the cache use the mapped name. We accept only canonical keys (those in `folder_names_and_paths`), otherwise the cache invalidation would miss.
4. **`api.fetchApi` has a 60 s default timeout** (`timeoutMs`, default `6e4`, in the bundled `api-*.js`). Pass `{ timeoutMs: null }` for `/complete` with a whole-file hash, which can take minutes.
5. **Every `.js` under `WEB_DIRECTORY` is loaded as an extension**, recursively (`server.py:417-427`), so helper modules (`uploader.js`, `api.js`, ...) are imported by the frontend as well. They must have no side effects on import. Test files must not live under `web/`.

## Verified as stated

### Routes
- `PromptServer.instance` exists before custom nodes are imported (`main.py:589` creates it, `main.py:595` loads nodes), and `prompt_server.add_routes()` runs afterwards (`main.py:611`). Registering on `PromptServer.instance.routes` at import time is therefore correct. Routes added after `add_routes()` get neither the app registration nor the `/api` copy.
- `add_routes()` copies every plain route to `"/api" + path` and also registers the unprefixed path (`server.py:1306-1313`). Checked live: `/system_stats` and `/api/system_stats` both return 200.
- Route handlers can read `PromptServer.instance.routes` as an aiohttp `RouteTableDef` (`server.py:322-323`), which matches the test fake.

### folder_paths
- `supported_pt_extensions: set[str]` = `.ckpt .pt .pt2 .bin .pth .safetensors .pkl .sft` (`folder_paths.py:10`). `.gguf` and `.onnx` are not included, as CLAUDE.md says.
- `folder_names_and_paths: dict[str, tuple[list[str], set[str]]]` (`folder_paths.py:12`). The extension collection is sometimes a `list` or contains `""`, so treat it as an opaque iterable.
- `get_folder_paths(name) -> list[str]` returns a copy and raises `KeyError` for unknown keys (`folder_paths.py:391-393`). Roots are not guaranteed to exist on disk.
- `models_dir` exists but must not be used for paths: roots come from `get_folder_paths`.

### Filename list cache
- `folder_paths.filename_list_cache: dict[str, tuple[list[str], dict[str, float], float]]` (`folder_paths.py:74`), keyed by the `map_legacy`-mapped name.
- It is self-validating: `cached_filename_list_` compares the mtime of every cached directory and re-walks on change (`folder_paths.py:484-507`). We still `filename_list_cache.pop(key, None)` after each write/delete, because directory mtime resolution can hide a quick change and a pop is cheap.
- A second layer, `folder_paths.cache_helper`, is only active inside one `/object_info` request and is cleared on exit (`folder_paths.py:76-104`, `server.py:869`). It needs no invalidation.

### Request size and streaming
- `web.Application(client_max_size=round(args.max_upload_size * 1024 * 1024))`, default `--max-upload-size 100` (MB) (`server.py:288-289`, `comfy/cli_args.py:68`).
- Tested with ComfyUI's aiohttp 3.14.5 (script not kept): with `client_max_size=1024`, `PUT` of 5 MiB handled with `async for block in request.content.iter_chunked(1 << 20)` returns 200 and reads all 5 MiB; the same body through `await request.read()` returns `413 Maximum request body size 1024 exceeded`. Streaming from `request.content` is not bounded by `client_max_size`, so the limit has to be enforced by us (SR-8).

### Origin / CSRF
- By default `create_origin_only_middleware` is installed (`server.py:199-237`, selected at `server.py:277-280`). It applies to **all methods**: it returns 403 when `Sec-Fetch-Site: cross-site`, and when `Host` is a loopback name and `Origin`'s host differs. Checked live: cross-site header gives 403; `Origin: http://evil.example` on a POST gives 403 (logged as "request with non matching host and origin").
- Same-origin `fetch`/XHR from the ComfyUI page (PUT/DELETE included) passes. No CSRF token exists.
- The check is skipped when `Host` is not loopback (LAN or tunnel access), so it gives no protection there.
- With `--enable-cors-header`, this middleware is **replaced** by a CORS one (`server.py:277-280`), which removes the origin check entirely. Reverse proxies must preserve the original `Host` header, otherwise a loopback upstream `Host` with an external `Origin` is rejected.
- We add no middleware and no CORS headers (SR-9).

### Web directory and frontend API
- `WEB_DIRECTORY` is resolved relative to the module and, if it is a directory, served as static files at `/extensions/<custom_nodes folder name>/` (`nodes.py:2299-2302`, `server.py:1316-1317`). The folder name is the directory name inside `custom_nodes/`, so a symlink named `comfyui-model-crud` gives `/extensions/comfyui-model-crud/`.
- `[tool.comfy] web` in `pyproject.toml` would register an additional directory under `project.name` (`nodes.py:2281-2293`); we do not set it. ComfyUI parsed our `pyproject.toml` at load without warnings.
- `.js` responses carry `Cache-Control: no-store` (`middleware/cache_middleware.py`), so frontend edits show on reload.
- The frontend ships shims: `/scripts/app.js` exports `app`, `/scripts/api.js` exports `api` (checked live, 200 text/javascript). From `/extensions/<name>/x.js`, `../../scripts/app.js` resolves to `/scripts/app.js`.
- `api.apiURL(path)` prefixes `/api`; `api.fetchApi(path, options)` calls it and adds the `Comfy-User` header. Use `api.apiURL` for XHR upload URLs.
- `app.extensionManager` is the workspace store and exposes `registerSidebarTab`, `unregisterSidebarTab`, `toast`, `dialog`, `setting`, `command` (bundled `settingStore-*.js`, `workspaceStoreSetup`). A custom tab object is `{ id, icon, title, tooltip, type: "custom", render(el), destroy? }`: `ExtensionSlot` calls `render(el)` for `type === "custom"` and `destroy()` on unmount (bundled `GraphView-*.js`). The exact tab fields beyond `type`/`render`/`destroy` still need checking in a browser in Step 8.

### Load check
- With the repo symlinked into `custom_nodes/comfyui-model-crud`, ComfyUI started with `--cpu --listen 127.0.0.1` and printed `0.0 seconds: .../custom_nodes/comfyui-model-crud` under "Import times for custom nodes", with no traceback or error from the extension.

## Not verified

- A real browser session (sidebar tab rendering, theme variables): Step 8.
- Behaviour of `--enable-manager` (ComfyUI-Manager middleware) with our routes.
- Windows/macOS paths: out of scope.

## Changes made because of these findings

- `SPEC.md`: FR-L1 (folder key filtering), §4 (canonical keys only), SR-10 (read `comfy.cli_args.args.listen`; also warn on `--enable-cors-header`), 6.4.5 (timeout note).
- `CLAUDE.md`: cache key note, frontend conventions (`timeoutMs`, every `.js` in `web/` is loaded, `api.apiURL` for XHR), listen-address source.
- `tests/conftest.py`: removed the guessed `address` attribute from the fake `PromptServer`.

## Observed while building the read-only API (Step 3, real server)

- ComfyUI appends `output/<key>` roots for `checkpoints`, `clip`, `vae`, `diffusion_models` and `loras` (`main.py:156-161`), so those keys report 2-3 roots out of the box. Models live in the first root (`models/<key>`) unless `extra_model_paths.yaml` sets `is_default: true`.
- A key such as `custom_nodes` exists in `folder_names_and_paths` with an empty extension set. Hiding it from `/folders` is not enough, the resolver itself refuses it (`paths.is_managed`).
- ComfyUI refuses to start a second instance on the same database file ("Close the other ComfyUI ..."), so parallel test instances need `--database-url` or `--disable-assets`.
- `extra_model_paths.yaml` entries are honoured (second `loras` root listed with `root_index: 1`).
- `--listen 0.0.0.0` is visible through `comfy.cli_args.args.listen` at import time, and our SR-10 warning fires; `MODEL_CRUD_READ_ONLY=1` silences it.

## Observed while building delete (Step 4, real server)

- `/object_info/LoraLoader` (`input.required.lora_name[0]`) is what the frontend reads when a loader dropdown refreshes. A file dropped into `models/loras` appeared there, and after `DELETE /api/model_crud/models/loras?path=...` it was gone. `filename_list_cache` also revalidates by directory mtime, so this check does not by itself prove our `pop` was necessary; the pop is covered by unit tests.
- ComfyUI's own dropdown lists model files that are symlinks pointing outside the models folder (`escape.safetensors` showed up). Our listing omits them and delete refuses them (SR-2), so such a file is visible in the dropdown but not manageable here.
- The origin middleware applies to `DELETE` like any other method: a request with `Origin: http://evil.example` got 403 and the file survived.

## Observed while building upload (Step 5, real server)

- ComfyUI's own buffered endpoint refuses a 128 MiB body (`POST /upload/image` gives 413 with the default `--max-upload-size 100`), while our `PUT .../chunks/{i}` with `MODEL_CRUD_CHUNK_MB=128` accepted a 128 MiB body and the SHA-256 of the result matched. Server RSS stayed at about 725 MB before, during and after.
- When a client drops mid-body, aiohttp 3.14 cancels the handler task (`CancelledError` at the next read). The upload code therefore must release session state in `finally` blocks, which it does (writer count, per-index lock, file handle); tested with a real aborted request.
- Recent aiohttp versions send the request headers together with the first body bytes, so a test client that stalls before yielding anything never reaches the server.
- `os.posix_fallocate` is available and preallocation of the temp file is immediate on this machine's ext4; the sparse-file fallback is only covered by unit tests (no real filesystem here lacks fallocate).

## Observed while building upload resilience (Step 6, real server)

- Custom nodes are imported inside `nodes.init_extra_nodes()` (a coroutine on the server's own event loop), and `PromptServer.instance.app` already exists at that point, so `app.on_startup` / `app.on_cleanup` can be appended at import time. `on_startup` runs in `AppRunner.setup()` before the sockets open, which is where unfinished uploads are rediscovered, so no request can arrive before they are restored.
- `kill -9` of the ComfyUI process during an upload leaves `.model_crud_<id>.part` and `.json` on disk. After a restart `GET /api/model_crud/uploads` listed the session with the same ranges, only the missing chunks were sent, and the final SHA-256 matched the source.
- ComfyUI refuses to start a second instance while another one holds its SQLite assets database, so restart tests must wait for the old process to exit.

## Dev environment: which filesystems are real (found in Step 7)

In the sandbox this project was developed in, `df` is misleading. `stat -f -c %T <path>` shows that everything under the home
directory except the project directory is `tmpfs` (RAM-backed, 16 GB), including `../ComfyUI-dev`. Anything that needs a real
disk (large uploads, page-cache behaviour, fsync timing) must put its model folder inside the project directory, in a
gitignored location such as `.bench/` or `.slow-test-tmp/`. Check with `stat -f` before trusting any I/O measurement.

## Frontend extension API, checked in a real browser (Step 8, frontend 1.55.14, Firefox 157)

- A tab registered with `app.extensionManager.registerSidebarTab({ id, icon: "pi pi-folder-open", title, tooltip, label, type: "custom", render(el), destroy() })` appears in the sidebar and `render` is called each time the tab opens. Plain strings work for `title`/`tooltip`/`label` (core tabs pass i18n keys). `getSidebarTabs()` lists it.
- `app.extensionManager.dialog.confirm({ title, message, type: "delete" })` resolves truthy on confirm and renders `message` as **text**: a file named `<img src=x onerror=...>"'.safetensors` showed up literally and nothing was injected. `app.extensionManager.toast.add({ severity, summary, detail, life })` also renders text.
- The theme switches with the setting `Comfy.ColorPalette` (`"dark"` / `"light"`); `--fg-color`, `--comfy-menu-bg`, `--comfy-input-bg`, `--border-color`, `--descrip-text` and `--error-text` are defined in both and the panel is readable in both.
- `app.refreshComboInNodes()` re-reads node definitions, which is what refreshes loader-node dropdowns; after a UI delete the file was gone from a `LoraLoader` node's combo values. The frontend shows its own "Node definitions updated" toast when that runs.
- The frontend lists and loads every `.js` under `web/` (`/extensions` showed `api.js`, `state.js`, `controller.js`, `view.js` too). They are inert on import, enforced by `tests/js/modules.test.mjs`.
- `container-type: inline-size` with `@container` works in the sidebar, so the date column hides when the panel is narrow (the sidebar is about 300 px by default).
