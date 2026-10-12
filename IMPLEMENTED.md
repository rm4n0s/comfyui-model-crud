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
