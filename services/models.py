"""Listing of model folders and files (SPEC §5.1). Blocking: call from a worker thread.

Listing never opens or hashes file contents (FR-L5).
"""

import logging
import os
from collections.abc import Collection, Iterator
from datetime import datetime, timezone
from typing import Any

from . import comfy, paths
from .errors import UnknownFolder

log = logging.getLogger("comfyui-model-crud")


def list_folders(allowed: Collection[str]) -> list[dict[str, Any]]:
    """Folder keys that can hold models we manage and have at least one existing root (FR-L1).

    ComfyUI also registers keys such as ``custom_nodes`` or ``configs``. A key is offered only if
    ComfyUI's own extension set for it overlaps our allowlist, so those are left out.
    """
    folders = []
    for key in sorted(comfy.folder_keys()):
        try:
            roots = paths.get_roots(key, allowed)
        except UnknownFolder:  # not a managed key, or no roots registered
            continue
        if any(os.path.isdir(r.path) for r in roots):
            folders.append({"key": key, "roots": len(roots)})
    return folders


def list_models(folder_key: object, allowed: Collection[str]) -> list[dict[str, Any]]:
    """Every allowed model file under every root of ``folder_key``, recursively (FR-L2..L6)."""
    allowed = frozenset(allowed)
    files = [f for root in paths.get_roots(folder_key, allowed) for f in _walk(root, allowed)]
    files.sort(key=lambda f: (f["path"], f["root_index"]))
    return files


def _walk(root: paths.Root, allowed: frozenset[str]) -> Iterator[dict[str, Any]]:
    # Directory symlinks are not followed: that avoids loops and duplicates, and a link that
    # leaves the root could not be operated on anyway (FR-L6, SR-2).
    stack = [("", root.path)]
    while stack:
        prefix, directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:  # missing root, permission denied, directory removed mid-walk
            log.debug("Cannot list a model directory", exc_info=True)
            continue
        for entry in entries:
            name = entry.name
            relpath = prefix + name
            if not paths.is_valid_component(name) or len(relpath) > paths.MAX_PATH_CHARS:
                continue  # hidden (includes upload temp files) or not addressable by the API
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((relpath + "/", entry.path))
                    continue
                if os.path.splitext(name)[1].lower() not in allowed:
                    continue
                if entry.is_symlink():
                    real = os.path.realpath(entry.path)
                    if not paths.is_inside(root.path, real) or not os.path.isfile(real):
                        continue
                    info = os.stat(real)
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                else:
                    continue
            except OSError:  # vanished or unreadable between scandir and stat
                continue
            yield {
                "path": relpath,
                "name": name,
                "size": info.st_size,
                "modified": _iso_utc(info.st_mtime),
                "root_index": root.index,
            }


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
