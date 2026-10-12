"""``MODEL_CRUD_*`` environment variables (SPEC §7), parsed once into an immutable object.

A bad value never stops ComfyUI from starting: it is logged as an error and that variable falls
back to its default (NFR-5).
"""

import functools
import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

log = logging.getLogger("comfyui-model-crud")

# SR-4. A deliberate fixed list rather than ``folder_paths.supported_pt_extensions``, so a
# ComfyUI update cannot silently widen what remote clients may write or delete.
DEFAULT_EXTENSIONS: tuple[str, ...] = (
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pt2",
    ".pth",
    ".bin",
    ".pkl",
    ".sft",
    ".gguf",
    ".onnx",
)

# ComfyUI's ``input/`` and ``output/`` directories (siblings of ``models/``, not model folders).
# They are managed through their own routes, ``/model_crud/input`` and ``/model_crud/output``.
MEDIA_AREAS: tuple[str, ...] = ("input", "output")

# SR-4a. What ComfyUI nodes load from ``input/`` and save to ``output/``. Deliberately without
# model files, ``.svg``/``.html``/``.js`` (active content) and anything executable.
DEFAULT_MEDIA_EXTENSIONS: tuple[str, ...] = (
    # images
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".exr",
    # video
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    # audio
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    # 3D
    ".glb",
    ".gltf",
    ".obj",
    ".ply",
    ".stl",
    # data
    ".latent",
    ".txt",
    ".json",
    ".csv",
)

# Temp files are named ``.model_crud_<id>.part`` (SR-6); allowing the suffix would make an
# in-progress upload look like a model.
_FORBIDDEN_EXTRA_EXTENSIONS = {".part"}

_EXTENSION_RE = re.compile(r"^\.[a-z0-9][a-z0-9_+-]*$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_GIB = 1024**3
_MIB = 1024**2

T = TypeVar("T")


class ConfigError(ValueError):
    """An invalid value for one variable."""


@dataclass(frozen=True)
class Config:
    read_only: bool = False
    allow_upload: bool = True
    allow_delete: bool = True
    max_upload_bytes: int = 100 * _GIB
    chunk_size: int = 64 * _MIB
    max_parallel: int = 4
    max_active_uploads: int = 4
    upload_ttl_hours: float = 24.0
    extra_extensions: tuple[str, ...] = field(default=())
    media_folders: tuple[str, ...] = MEDIA_AREAS  # the enabled ones
    extra_media_extensions: tuple[str, ...] = field(default=())

    @property
    def upload_enabled(self) -> bool:
        return self.allow_upload and not self.read_only

    @property
    def delete_enabled(self) -> bool:
        return self.allow_delete and not self.read_only

    @property
    def allowed_extensions(self) -> tuple[str, ...]:
        """Defaults first, then extras, without duplicates (order is shown to API clients)."""
        return tuple(dict.fromkeys((*DEFAULT_EXTENSIONS, *self.extra_extensions)))

    @property
    def media_extensions(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*DEFAULT_MEDIA_EXTENSIONS, *self.extra_media_extensions)))

    def extensions_for_area(self, area: object) -> tuple[str, ...]:
        """The allowlist for ``input``/``output``; empty if that area is switched off or unknown,
        which makes every endpoint answer ``unknown_folder`` for it."""
        return self.media_extensions if area in self.media_folders else ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        defaults = cls()

        def get(name: str, parse: Callable[[str], T], default: T) -> T:
            raw = env.get(f"MODEL_CRUD_{name}")
            if raw is None or raw.strip() == "":
                return default
            try:
                return parse(raw.strip())
            except ConfigError as exc:
                log.error("Ignoring MODEL_CRUD_%s=%r: %s. Using the default.", name, raw[:100], exc)
                return default

        max_gb = get("MAX_UPLOAD_GB", _positive_number, defaults.max_upload_bytes / _GIB)
        ttl = get("UPLOAD_TTL_HOURS", _positive_number, defaults.upload_ttl_hours)
        return cls(
            read_only=get("READ_ONLY", _boolean, defaults.read_only),
            allow_upload=get("ALLOW_UPLOAD", _boolean, defaults.allow_upload),
            allow_delete=get("ALLOW_DELETE", _boolean, defaults.allow_delete),
            max_upload_bytes=int(max_gb * _GIB),
            chunk_size=get("CHUNK_MB", _chunk_mb, defaults.chunk_size // _MIB) * _MIB,
            max_parallel=get("MAX_PARALLEL", _positive_int, defaults.max_parallel),
            max_active_uploads=get(
                "MAX_ACTIVE_UPLOADS", _positive_int, defaults.max_active_uploads
            ),
            upload_ttl_hours=ttl,
            extra_extensions=get("EXTRA_EXTENSIONS", _extensions, defaults.extra_extensions),
            media_folders=get("MEDIA_FOLDERS", _media_folders, defaults.media_folders),
            extra_media_extensions=get(
                "EXTRA_MEDIA_EXTENSIONS", _extensions, defaults.extra_media_extensions
            ),
        )


def _boolean(raw: str) -> bool:
    value = raw.lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError("expected one of 1/0, true/false, yes/no, on/off")


def _positive_number(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("expected a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError("expected a positive, finite number")
    return value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("expected a whole number") from None
    if value <= 0:
        raise ConfigError("expected a positive whole number")
    return value


def _chunk_mb(raw: str) -> int:
    value = _positive_int(raw)
    if not 8 <= value <= 256:
        raise ConfigError("expected a whole number between 8 and 256")
    return value


def _extensions(raw: str) -> tuple[str, ...]:
    result: list[str] = []
    for item in raw.split(","):
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if not _EXTENSION_RE.match(ext) or ext in _FORBIDDEN_EXTRA_EXTENSIONS:
            raise ConfigError(f"{item.strip()!r} is not a usable file extension")
        result.append(ext)
    return tuple(dict.fromkeys(result))


def _media_folders(raw: str) -> tuple[str, ...]:
    """``input,output`` (default), one of them, or ``none``."""
    items = [i.strip().lower() for i in raw.split(",") if i.strip()]
    if items == ["none"]:
        return ()
    if any(i not in MEDIA_AREAS for i in items):
        raise ConfigError("expected input, output, input,output or none")
    return tuple(dict.fromkeys(items))


@functools.cache
def get_config() -> Config:
    """The process-wide config, read once. Tests call ``get_config.cache_clear()``."""
    return Config.from_env()
