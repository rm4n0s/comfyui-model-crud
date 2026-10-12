"""The only module that touches ComfyUI's ``folder_paths``.

Everything else goes through here so tests can swap in a fake via ``sys.modules`` and so a
ComfyUI API change has one place to fix. ``folder_paths`` is imported lazily for that reason.
See docs/comfyui-notes.md for what was verified against ComfyUI's source.
"""

from collections.abc import Iterable
from types import ModuleType

from .errors import UnknownFolder


def _folder_paths() -> ModuleType:
    import folder_paths

    return folder_paths


def media_directory(area: str) -> str:
    """ComfyUI's ``input`` or ``output`` directory. Read on every call: ``--input-directory`` and
    ``--output-directory`` are applied through ``set_*_directory`` and can change."""
    fp = _folder_paths()
    return fp.get_input_directory() if area == "input" else fp.get_output_directory()


def folder_keys() -> list[str]:
    return list(_folder_paths().folder_names_and_paths)


def get_folder_paths(folder_key: object) -> list[str]:
    """Roots registered for ``folder_key``, in ComfyUI's order.

    Only canonical keys are accepted. ComfyUI also resolves legacy aliases (``unet``, ``clip``)
    in ``get_folder_paths``, but its filename cache is keyed by the canonical name, so accepting
    an alias would make cache invalidation miss.
    """
    fp = _folder_paths()
    if not isinstance(folder_key, str) or folder_key not in fp.folder_names_and_paths:
        raise UnknownFolder()
    return list(fp.get_folder_paths(folder_key))


def folder_extensions(folder_key: str) -> set[str]:
    """ComfyUI's extension set for a key, lower-cased. May hold ``""`` or non-extensions."""
    exts: Iterable[str] = _folder_paths().folder_names_and_paths[folder_key][1]
    return {e.lower() for e in exts}


def invalidate_cache(folder_key: str) -> None:
    """Make loader-node dropdowns see added/removed files (FR-U13, FR-D4)."""
    cache = getattr(_folder_paths(), "filename_list_cache", None)
    if cache is not None:
        cache.pop(folder_key, None)


def listen_addresses() -> list[str] | None:
    """Addresses ComfyUI was told to listen on, or None if that cannot be determined.

    ``PromptServer.address`` is not set yet while custom nodes load (docs/comfyui-notes.md), so
    the parsed command line is the only source available at import time.
    """
    try:
        from comfy.cli_args import args
    except Exception:
        return None
    listen = getattr(args, "listen", None)
    if not isinstance(listen, str):
        return None
    return [a.strip() for a in listen.split(",") if a.strip()]


def cors_enabled() -> bool:
    """True if ``--enable-cors-header`` replaced ComfyUI's origin check."""
    try:
        from comfy.cli_args import args
    except Exception:
        return False
    return bool(getattr(args, "enable_cors_header", None))
