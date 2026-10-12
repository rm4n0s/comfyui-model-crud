# Changelog

## Unreleased

- Project scaffold.
- API for ComfyUI's `input` and `output` directories under `/model_crud/input` and
  `/model_crud/output`: list, chunked resumable upload, delete, with their own media extension list.
  `MODEL_CRUD_MEDIA_FOLDERS` and `MODEL_CRUD_EXTRA_MEDIA_EXTENSIONS` configure them; `GET
  /model_crud/config` gains `media_folders` / `media_extensions`. No UI yet. Error messages no
  longer say "model folder".
- `openapi.yaml`: OpenAPI 3.1 description of every endpoint, checked against the code by a test; README rewritten (install, API overview, examples, configuration, security model).
- Fixed: the startup scan for unfinished uploads visited a directory once per root that contains it
  and could discard a valid session when roots nest.
