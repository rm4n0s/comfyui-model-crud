# IMPLEMENTED.md

Log of completed steps from [IMPLEMENT.md](IMPLEMENT.md), newest last. Append an entry as the final action of each step, before reporting it done. Never edit past entries except to correct a factual error (note the correction).

Entry template:

```markdown
## Step N — Title

- Date: YYYY-MM-DD
- Status: done | done with deviations | partial
- Summary: what was built, in 2–4 lines.
- Files: key files added or changed.
- Verification: commands actually run and their result (e.g. `pytest -q`: 42 passed). State what was NOT verified, e.g. no real ComfyUI or browser run.
- Deviations from SPEC.md / IMPLEMENT.md: what differed and why, and which spec sections were updated.
- Follow-ups: known gaps, open questions, things the next step must know.
```

---

<!-- Entries start below. -->

## Step 0 — Scaffold the project

- Date: 2026-10-12
- Status: done
- Summary: Empty but loadable extension with tooling. `__init__.py` exports `NODE_CLASS_MAPPINGS = {}` and `WEB_DIRECTORY = "./web"` via `__all__` and imports `api` inside a try/except that logs and continues (NFR-5). `tests/conftest.py` fakes `folder_paths` and `server.PromptServer` through `sys.modules`. SPEC §13 now records the two decisions.
- Files: `__init__.py`, `api/__init__.py`, `services/__init__.py`, `web/.gitkeep`, `pyproject.toml`, `.gitignore`, `requirements.txt`, `README.md`, `CHANGELOG.md`, `tests/conftest.py`, `tests/test_package.py`, `SPEC.md` (§13).
- Verification: `.venv/bin/pytest -q`: 3 passed (exports, import failure is logged and not raised, fake ComfyUI environment). `.venv/bin/ruff check .`: all checks passed. `.venv/bin/ruff format --check .`: 11 files already formatted. Venv built with system Python 3.14.8 (pytest 9.1.1, pytest-asyncio 1.4.0, pytest-aiohttp 1.1.1, aiohttp 3.14.5, ruff 0.17.0). NOT verified: loading inside a real ComfyUI (that is Step 1); Python 3.10 compatibility (only 3.14 was available).
- Deviations from SPEC.md / IMPLEMENT.md: SPEC §13 retitled from "Open questions" to "Decisions". The "dev dependency group" is `[project.optional-dependencies] dev`; there is no `[build-system]`, so `pip install ".[dev]"` does not work and the venv was created with an explicit `pip install` of the same packages (command is in README). pytest uses `--import-mode=importlib` because the repo root is a package. Tests import project modules through the alias `comfyui_model_crud` (set up in conftest), not as top-level `services`/`api`.
- Follow-ups: `api/__init__.py` must register the routes in Step 3 (the guarded import in `__init__.py` already triggers it). The fake `PromptServer` has an `address` attribute as a guess for SR-10; Step 1 must check how the real server exposes its listen address. CLAUDE.md "Project" layout still matches the tree, except that `web/.gitkeep` and `docs/` / `scripts/` (Steps 1, 7) are new or pending.

## Step 1 — Real ComfyUI dev environment and API verification

- Date: 2026-10-12
- Status: done
- Summary: Cloned ComfyUI (commit `e8665ca8`, shallow) to `../ComfyUI-dev` with its own Python 3.13.16 venv (CPU torch 2.14.1+cpu), symlinked this repo into `custom_nodes/`, added `scripts/dev.sh`, and checked every ComfyUI assumption in CLAUDE.md/SPEC.md against the source and a running server. Findings are in `docs/comfyui-notes.md`.
- Files: `scripts/dev.sh`, `docs/comfyui-notes.md`, `SPEC.md` (FR-L1, §4, SR-10, 6.4.5), `CLAUDE.md` (cache key, folder filtering, listen address, frontend conventions, layout), `README.md`, `tests/conftest.py` (removed guessed `address` attribute).
- Verification: `scripts/dev.sh` started ComfyUI; console listed `custom_nodes/comfyui-model-crud` under "Import times for custom nodes" with no traceback or error from the extension (the only WARNING in the log came from my deliberate origin-mismatch probe). curl against the live server: `/system_stats` and `/api/system_stats` both 200; `/scripts/app.js` 200 `no-store`; `Sec-Fetch-Site: cross-site` gives 403; POST with a mismatched `Origin` gives 403. Standalone aiohttp 3.14.5 test: `client_max_size` rejects `request.read()` with 413 but does not limit streaming `request.content`. `pytest -q`: 3 passed; `ruff check` and `ruff format --check` clean after the edits. Server stopped afterwards. NOT verified: any browser behaviour (sidebar tab fields beyond `type`/`render`/`destroy`, themes), `--enable-manager`.
- Deviations from SPEC.md / IMPLEMENT.md: The clone is shallow (`--depth 1`). Python 3.13 was obtained through `uv` because the system only has 3.14. Contradictions found and fixed: (1) `PromptServer.address` is unset at import time, so SR-10 uses `comfy.cli_args.args.listen`, plus a warning for `--enable-cors-header`; (2) `/folders` must filter non-model keys (`custom_nodes`, `datasets`, `configs`, `diffusers`, `classifiers`) by extension intersection (FR-L1); (3) legacy folder aliases (`unet`, `clip`) are rejected as `unknown_folder`; (4) `api.fetchApi` has a 60 s default timeout; (5) every `.js` under `web/` is loaded as an extension.
- Follow-ups: The dev machine has only about 16 GB free on `/home/rmanos`, so the 50 GB validation in Step 7 cannot run here as specified; decide then (smaller sparse-file test, other disk, or ask the user). `folder_paths` extension values may be `list` or `set` and may contain `""`; the fake in `conftest.py` should gain `map_legacy` and a couple of non-model keys when Step 2/3 need them.
