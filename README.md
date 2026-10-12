# comfyui-model-crud

A ComfyUI custom-node extension to list, upload and delete model files, through a REST API and a
sidebar tab in the ComfyUI frontend.

**Status:** under construction. See [SPEC.md](SPEC.md) for the design and
[IMPLEMENTED.md](IMPLEMENTED.md) for progress.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio pytest-aiohttp aiohttp ruff
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

A real ComfyUI for manual testing is expected at `../ComfyUI-dev` with this repo symlinked into its
`custom_nodes/`; `scripts/dev.sh` starts it (CPU only, `127.0.0.1:8188`). Setup details and the
ComfyUI internals this project relies on are in [docs/comfyui-notes.md](docs/comfyui-notes.md).

## License

GPL-3.0, see [LICENSE](LICENSE).
