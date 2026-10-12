"""List and delete files in ComfyUI's ``input`` and ``output`` directories (SPEC §5.5).

These are not model folders: they sit next to ``models/`` and are reached through their own
routes. Everything security-relevant is shared with the model code (the resolver in ``paths`` and
``models.walk`` / ``models.unlink_file``); what differs is the root, which comes from ComfyUI's
``get_input_directory()`` / ``get_output_directory()``, and the extension allowlist (SR-4a).
Blocking: call from a worker thread.
"""

import logging
from typing import Any

from . import models, paths
from .config import Config
from .errors import DeleteDisabled

log = logging.getLogger("comfyui-model-crud")


def list_files(area: object, config: Config) -> list[dict[str, Any]]:
    """Every allowed file below the area's directory, recursively (FR-M1)."""
    allowed = frozenset(config.extensions_for_area(area))
    [root] = paths.get_media_roots(area, allowed)
    files = list(models.walk(root, allowed))
    for f in files:
        del f["root_index"]  # there is exactly one root
    files.sort(key=lambda f: f["path"])
    return files


def delete_file(area: object, relpath: object, config: Config) -> dict[str, Any]:
    """Delete exactly one file (FR-D1..D3, SR-7). The enabled check comes first."""
    if not config.delete_enabled:
        raise DeleteDisabled()
    resolved = paths.resolve_existing(area, relpath, config.extensions_for_area(area), media=True)
    size = models.unlink_file(resolved)
    # No filename cache to invalidate: ComfyUI lists these directories on every request.
    log.info("Deleted file: area=%s path=%s size=%d", resolved.folder, resolved.relpath, size)
    return {"area": resolved.folder, "path": resolved.relpath, "deleted": True}
