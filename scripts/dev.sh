#!/usr/bin/env bash
# Start a local CPU-only ComfyUI (../ComfyUI-dev) with this repo symlinked into custom_nodes.
# Extra arguments are passed through to ComfyUI, e.g. scripts/dev.sh --port 8199
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
comfy="${COMFYUI_DEV_DIR:-$repo/../ComfyUI-dev}"

if [ ! -x "$comfy/venv/bin/python" ]; then
  echo "ComfyUI dev environment not found at $comfy (see docs/comfyui-notes.md)" >&2
  exit 1
fi

cd "$comfy"
exec venv/bin/python main.py --cpu --listen 127.0.0.1 --port 8188 "$@"
