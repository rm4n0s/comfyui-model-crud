# comfyui-model-crud

A ComfyUI custom-node extension that lets you **list, upload and delete files on the ComfyUI server**
without shell access, through a REST API and a sidebar tab in the ComfyUI frontend:

| What | Where | API | UI |
|------|-------|-----|----|
| **Model files** | the model folders (`checkpoints`, `loras`, `vae`, ... including `extra_model_paths.yaml`) | `/model_crud/models/{folder}`, `/model_crud/uploads` | sidebar tab "Model files" |
| **Generated images, videos, audio** | ComfyUI's `output/` directory | `/model_crud/output` | not yet |
| **Input files** (what `LoadImage` reads) | ComfyUI's `input/` directory | `/model_crud/input` | not yet |

`input/` and `output/` sit next to `models/`; they are **not** model folders and have their own routes.

Uploads are chunked, parallel and resumable, designed for single files of tens of GB: a dropped
connection, a closed tab or a ComfyUI restart costs only the chunks in flight.

**Status:** pre-release (0.1.0 is not published). Developed and tested against ComfyUI commit `e8665ca8`
with frontend 1.55.14, on Linux only (macOS and Windows are not supported). Older or newer ComfyUI
versions are untested.

## Install

```bash
cd ComfyUI/custom_nodes
git clone <this repository> comfyui-model-crud
```

Restart ComfyUI. There are no extra Python dependencies. The console should list
`custom_nodes/comfyui-model-crud` under "Import times for custom nodes" without a traceback.

## API

The full contract, with every parameter, response schema and error code, is
**[openapi.yaml](openapi.yaml)** (OpenAPI 3.1). Import it into Swagger UI, Postman or a client
generator. `SPEC.md` is the design document behind it. A test checks that `openapi.yaml` matches the
routes and responses of the code.

Every route is available at `/model_crud/...` and at `/api/model_crud/...`. Errors always look like
`{"error": "<code>", "message": "<text>"}`; branch on `error`.

| Operation | Model folders | `input` / `output` |
|-----------|---------------|--------------------|
| Server limits and switches | `GET /model_crud/config` | same |
| Folders | `GET /model_crud/folders` | not listed |
| List files | `GET /model_crud/models/{folder}` | `GET /model_crud/{area}` |
| Delete one file | `DELETE /model_crud/models/{folder}?path=...` | `DELETE /model_crud/{area}?path=...` |
| Start an upload | `POST /model_crud/uploads` | `POST /model_crud/{area}/uploads` |
| Send a chunk | `PUT /model_crud/uploads/{id}/chunks/{i}` | `PUT /model_crud/{area}/uploads/{id}/chunks/{i}` |
| Status / list / cancel | `GET`, `GET`, `DELETE` on `/model_crud/uploads[/{id}]` | the same under `/model_crud/{area}/uploads` |
| Publish | `POST /model_crud/uploads/{id}/complete` | `POST /model_crud/{area}/uploads/{id}/complete` |

`{area}` is `input` or `output`.

### Examples

```bash
BASE=http://127.0.0.1:8188/model_crud

curl -s $BASE/config                       # limits, enabled features, allowed extensions
curl -s $BASE/folders                      # model folders
curl -s $BASE/models/loras                 # model files in loras/
curl -s $BASE/output                       # generated files in output/ (recursive, relative paths)

# Delete one file. Permanent: no trash. URL-encode slashes in the path.
curl -s -X DELETE "$BASE/output?path=2026%2F10%2FComfyUI_00001_.png"
curl -s -X DELETE "$BASE/models/loras?path=old.safetensors"
```

Upload a file into `input/` (the same steps work for `output` and, with `"folder": "loras"` in the
body and `/uploads` instead of `/input/uploads`, for model folders):

```bash
FILE=ref.png
SIZE=$(stat -c %s "$FILE")

# 1. Start. The server answers with upload_id, chunk_size and total_chunks.
SESSION=$(curl -s -X POST $BASE/input/uploads -H 'content-type: application/json' \
  -d "{\"filename\": \"$FILE\", \"size\": $SIZE, \"subfolder\": \"refs\"}")
ID=$(echo "$SESSION" | jq -r .upload_id)
CHUNK=$(echo "$SESSION" | jq -r .chunk_size)
TOTAL=$(echo "$SESSION" | jq -r .total_chunks)

# 2. Send every chunk (any order, several at a time is allowed; shown sequentially here).
for ((i = 0; i < TOTAL; i++)); do
  dd if="$FILE" bs="$CHUNK" skip="$i" count=1 iflag=fullblock status=none |
    curl -s -X PUT "$BASE/input/uploads/$ID/chunks/$i" \
      -H 'content-type: application/octet-stream' --data-binary @-
done

# 3. Publish. The file appears at refs/ref.png only now.
curl -s -X POST $BASE/input/uploads/$ID/complete
```

An existing file is a `409 file_exists` unless the start request has `"overwrite": true`. If a
transfer is interrupted, `GET $BASE/input/uploads/$ID` returns `received_ranges`; send only the
missing chunks, then complete. Retry a chunk that failed with `429`, `5xx` or a network error. The
details (parallelism, per-chunk and whole-file SHA-256, expiry) are in `openapi.yaml`.

For model files from the command line there is also a reference client,
`scripts/model_crud_upload.py` (standard library only: parallel chunks, retries, resume, progress).
It does not support `input` / `output` yet.

### What is allowed

* **Model folders:** `.safetensors .ckpt .pt .pt2 .pth .bin .pkl .sft .gguf .onnx`, plus
  `MODEL_CRUD_EXTRA_EXTENSIONS`.
* **`input` / `output`:** images (`.png .jpg .jpeg .webp .gif .bmp .tif .tiff .avif .exr`), video
  (`.mp4 .webm .mov .mkv .avi`), audio (`.wav .mp3 .flac .ogg .opus .m4a .aac`), 3D
  (`.glb .gltf .obj .ply .stl`) and `.latent .txt .json .csv`, plus `MODEL_CRUD_EXTRA_MEDIA_EXTENSIONS`.
  Model files, `.svg`, `.html`, `.js` and executables are never accepted there.
* Everything else is `415 extension_not_allowed`, for upload, listing and delete alike. The
  extension check is case-insensitive and looks at the last extension only (`x.safetensors.exe`
  fails).
* A listing shows only allowed files; hidden files (leading `.`), upload temp files and symlinks
  that leave their folder are never listed.

## Configuration

Environment variables, read once when ComfyUI starts. An invalid value is logged and its default
is used; it never stops ComfyUI from starting.

| Variable | Default | Meaning |
|----------|---------|---------|
| `MODEL_CRUD_READ_ONLY` | `0` | `1` disables upload and delete everywhere (listing still works). |
| `MODEL_CRUD_ALLOW_UPLOAD` | `1` | `0` disables uploads only. |
| `MODEL_CRUD_ALLOW_DELETE` | `1` | `0` disables deletes only. |
| `MODEL_CRUD_MEDIA_FOLDERS` | `input,output` | Which of `input` / `output` are managed: `input`, `output`, both, or `none`. A folder that is off answers `404 unknown_folder` on all its routes. |
| `MODEL_CRUD_EXTRA_EXTENSIONS` | empty | Extra extensions allowed in model folders (e.g. `.yaml`). |
| `MODEL_CRUD_EXTRA_MEDIA_EXTENSIONS` | empty | Extra extensions allowed in `input` / `output`. `.part` is refused. |
| `MODEL_CRUD_MAX_UPLOAD_GB` | `100` | Largest accepted file, in GiB. |
| `MODEL_CRUD_CHUNK_MB` | `64` | Chunk size in MiB (8-256). 64 stays under the 100 MB request limit of common proxies and tunnels; lower it if yours is stricter. |
| `MODEL_CRUD_MAX_PARALLEL` | `4` | Concurrent chunk requests per upload the server accepts. |
| `MODEL_CRUD_MAX_ACTIVE_UPLOADS` | `4` | Upload sessions at once (each reserves disk space). |
| `MODEL_CRUD_UPLOAD_TTL_HOURS` | `24` | Idle time after which an unfinished upload and its temp files are removed. |

## Security model

**ComfyUI has no authentication, and neither does this extension.** Anyone who can reach the port
can list, upload and delete. ComfyUI listens on `127.0.0.1` by default, which is safe for a local
machine; with `--listen` (or a tunnel or reverse proxy) put authentication in front of it, or set
`MODEL_CRUD_READ_ONLY=1`. The extension logs a warning at startup when ComfyUI listens on a
non-loopback address with writes enabled, and when `--enable-cors-header` replaces ComfyUI's
browser origin check.

What the extension itself enforces on every request:

* Clients send folder keys, never filesystem paths. Every path is resolved with `realpath` and must
  stay inside its folder root; `..`, absolute paths, backslashes, NUL and symlinks that leave the
  root are rejected (`400 path_outside_root`). Responses contain only relative paths.
* Only allowlisted extensions (above); only regular files can be deleted, one per request, no
  wildcards, no directories.
* Uploads never overwrite silently (`409`), are written to a hidden temp file and published by an
  atomic rename, and are bounded in size, in concurrent sessions and in reserved disk space.
* `custom_nodes`, `configs`, `temp`, `user` and every other directory stay out of reach.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio pytest-aiohttp aiohttp ruff \
  pyyaml jsonschema openapi-spec-validator
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .
node --test tests/js/*.test.mjs        # browser modules, no dependencies
```

Tests run without ComfyUI (it is replaced by stubs in `tests/conftest.py`). The three packages on the
second line are only needed by `tests/test_openapi.py`, which skips itself without them. When you
change an endpoint, update `openapi.yaml`, `SPEC.md` and this README; that test fails if
`openapi.yaml` drifts from the code.

A real ComfyUI for manual testing is expected at `../ComfyUI-dev` with this repo symlinked into its
`custom_nodes/`; `scripts/dev.sh` starts it (CPU only, `127.0.0.1:8188`). Browser checks of the UI
are in `scripts/ui_check/` (see its README). ComfyUI internals this project relies on are in
[docs/comfyui-notes.md](docs/comfyui-notes.md); [IMPLEMENTED.md](IMPLEMENTED.md) logs what was built
and verified.

## License

GPL-3.0, see [LICENSE](LICENSE).
