"""The one resolver for client-supplied paths (SR-1..SR-5).

Every operation that turns a (folder key, relative path) from a request into a filesystem path
goes through ``resolve_existing`` or ``resolve_upload_target``. Nothing else in the project may
join client strings onto a root.

What gets rejected, and with which error:

* traversal-shaped input (absolute, drive letter, ``..``/``.``/empty segments, backslash, NUL,
  overlong) -> ``PathOutsideRoot``;
* a path that is well-formed but whose real location (after resolving symlinks) is outside the
  root -> ``PathOutsideRoot``;
* bad file names (empty, leading dot, control characters, > 255 bytes) -> ``InvalidFilename``;
* the same problems in a directory segment -> ``BadRequest``;
* extensions outside the allowlist -> ``ExtensionNotAllowed``;
* folder keys ComfyUI does not know -> ``UnknownFolder``.

The check is done on a path string, so it is not atomic with the later open/unlink: a local
attacker who can swap directories for symlinks between check and use is out of scope here and
is addressed with ``O_NOFOLLOW``/``dir_fd`` where it matters (IMPLEMENT.md step 10).
"""

import logging
import os
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

from . import comfy
from .errors import (
    BadRequest,
    ExtensionNotAllowed,
    InvalidFilename,
    ModelFileNotFound,
    PathOutsideRoot,
    UnknownFolder,
)

log = logging.getLogger("comfyui-model-crud")

MAX_SUBFOLDER_DEPTH = 4
MAX_NAME_BYTES = 255
MAX_PATH_CHARS = 4096

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Bidirectional overrides can make "evil.exe.safetensors" render as something else.
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


@dataclass(frozen=True)
class Root:
    index: int  # position in ``get_folder_paths`` order; this is the API's ``root_index``
    path: str  # realpath of the configured directory (it need not exist yet)


@dataclass(frozen=True)
class ResolvedPath:
    folder: str
    root_index: int
    root: str
    relpath: str  # "/"-separated, relative to the root; the only form that goes into responses
    entry: str  # realpath(parent) + name. If the name is a symlink this is the link itself,
    # so unlink/replace act on the link and never on what it points to.
    real: str  # realpath(entry): where the bytes actually live; always inside ``root``

    @property
    def name(self) -> str:
        return os.path.basename(self.entry)

    @property
    def directory(self) -> str:
        return os.path.dirname(self.entry)


def is_inside(root: str, path: str) -> bool:
    """True if ``path`` equals or is below ``root``. Both must be absolute and normalised."""
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def is_managed(folder_key: str, allowed: Collection[str]) -> bool:
    """Whether ``folder_key`` holds files of a type we manage (FR-L1).

    ComfyUI registers non-model keys too (``custom_nodes``, ``configs``, ...). Those must be
    unknown to every endpoint, not just hidden from the folder list, or a client could write
    into the directory ComfyUI executes code from.
    """
    return bool(comfy.folder_extensions(folder_key) & set(allowed))


def get_roots(folder_key: object, allowed: Collection[str]) -> list[Root]:
    configured = comfy.get_folder_paths(folder_key)
    if not configured or not is_managed(str(folder_key), allowed):
        raise UnknownFolder()
    return [Root(i, os.path.realpath(p)) for i, p in enumerate(configured)]


def sanitize_filename(raw: object) -> str:
    """Reduce a client-supplied file name to a safe basename (SR-5).

    Both ``/`` and ``\\`` count as separators, because browsers on Windows and some scripts send
    paths. Control characters are stripped; a NUL is rejected outright.
    """
    if not isinstance(raw, str) or "\x00" in raw:
        raise InvalidFilename()
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not _is_control(c)).strip()
    _check_component(name, final=True)
    return name


def check_extension(name: str, allowed: Collection[str]) -> None:
    """Case-insensitive allowlist check on the last extension, so ``x.safetensors.exe`` fails."""
    if os.path.splitext(name)[1].lower() not in allowed:
        raise ExtensionNotAllowed()


def resolve_existing(
    folder_key: object,
    relpath: object,
    allowed: Collection[str],
    root_index: int | None = None,
) -> ResolvedPath:
    """Locate an existing regular file. Used by delete (and anything else that reads a path).

    Without ``root_index`` the first root containing the file wins.
    """
    roots = get_roots(folder_key, allowed)
    segments = _parse_relative(relpath, folder_key)
    check_extension(segments[-1], allowed)

    for root in _select_roots(roots, root_index):
        resolved = _locate(str(folder_key), root, segments[:-1], segments[-1], relpath)
        if os.path.isfile(resolved.real):
            return resolved
    raise ModelFileNotFound()


def resolve_upload_target(
    folder_key: object,
    filename: object,
    subfolder: object,
    allowed: Collection[str],
) -> ResolvedPath:
    """Resolve where an upload will be published. Always the first root (SPEC §13).

    Touches nothing on disk: the subfolder may not exist yet, and the caller creates it.
    """
    roots = get_roots(folder_key, allowed)
    name = sanitize_filename(filename)
    check_extension(name, allowed)
    segments = (
        []
        if subfolder is None or subfolder == ""
        else _parse_relative(subfolder, folder_key, max_depth=MAX_SUBFOLDER_DEPTH, dirs_only=True)
    )
    return _locate(str(folder_key), roots[0], segments, name, subfolder)


def _select_roots(roots: list[Root], root_index: int | None) -> list[Root]:
    if root_index is None:
        return roots
    if isinstance(root_index, bool) or not isinstance(root_index, int):
        raise BadRequest("root_index must be a whole number.")
    if not 0 <= root_index < len(roots):
        raise BadRequest("root_index is out of range.")
    return [roots[root_index]]


def _parse_relative(
    raw: object, folder_key: object, *, max_depth: int | None = None, dirs_only: bool = False
) -> list[str]:
    """Split and vet a client-supplied relative path without touching the filesystem."""
    if not isinstance(raw, str) or raw == "":
        raise BadRequest("A path is required.")
    if (
        len(raw) > MAX_PATH_CHARS
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
        or _DRIVE_RE.match(raw)
    ):
        raise _outside(folder_key, raw)
    segments = raw.split("/")
    if any(s in ("", ".", "..") for s in segments):
        raise _outside(folder_key, raw)
    if max_depth is not None and len(segments) > max_depth:
        raise BadRequest(f"Subfolders can be nested at most {max_depth} levels deep.")
    for i, segment in enumerate(segments):
        _check_component(segment, final=not dirs_only and i == len(segments) - 1)
    return segments


def _locate(
    folder_key: str, root: Root, dir_parts: list[str], name: str, raw_for_log: object
) -> ResolvedPath:
    parent = os.path.realpath(os.path.join(root.path, *dir_parts))
    entry = os.path.join(parent, name)
    real = os.path.realpath(entry)  # follows a symlink at the final component too
    if not is_inside(root.path, parent) or not is_inside(root.path, real) or real == root.path:
        raise _outside(folder_key, raw_for_log)
    return ResolvedPath(
        folder=folder_key,
        root_index=root.index,
        root=root.path,
        relpath="/".join([*dir_parts, name]),
        entry=entry,
        real=real,
    )


def is_valid_component(part: str) -> bool:
    """One path segment: non-empty, no leading dot, no controls, valid UTF-8, <= 255 bytes.

    Listing uses this too, so nothing is shown that the resolver would refuse to address.
    """
    try:
        size = len(part.encode("utf-8"))
    except UnicodeEncodeError:  # lone surrogates would become arbitrary raw bytes on disk
        return False
    if not part or part.startswith(".") or size > MAX_NAME_BYTES:
        return False
    return not any(_is_control(c) for c in part)


def _check_component(part: str, *, final: bool) -> None:
    if not is_valid_component(part):
        raise (InvalidFilename if final else BadRequest)()


def _is_control(char: str) -> bool:
    return unicodedata.category(char) == "Cc" or char in _BIDI_CONTROLS


def _outside(folder_key: object, raw: object) -> PathOutsideRoot:
    # %r escapes control characters, so a hostile name cannot forge log lines (NFR-6).
    log.warning("Rejected path outside model folder: folder=%.100r path=%.200r", folder_key, raw)
    return PathOutsideRoot()
