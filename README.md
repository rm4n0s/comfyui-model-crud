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

## License

GPL-3.0, see [LICENSE](LICENSE).
