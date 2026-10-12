"""Low-level file operations for uploads. Linux only; blocking, so call from a worker thread.

Filesystem-level fallbacks (no ``fallocate``, no hard links) are kept because they depend on the
filesystem the models live on, which is often a network mount, not on the OS.
"""

import contextlib
import errno
import os

from .errors import FileAlreadyExists, InsufficientStorage, TooLarge

FREE_SPACE_MARGIN = 1024**3  # FR-U8: keep 1 GiB free beyond the file being uploaded

_NO_FALLOCATE = {errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}
_NO_HARDLINKS = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK}
_DISK_FULL = {errno.ENOSPC, errno.EDQUOT}


def disk_full(exc: OSError) -> bool:
    return exc.errno in _DISK_FULL


def free_bytes(directory: str) -> int:
    stat = os.statvfs(directory)
    return stat.f_bavail * stat.f_frsize


def check_free_space(directory: str, size: int) -> None:
    if free_bytes(directory) < size + FREE_SPACE_MARGIN:
        raise InsufficientStorage()


def create_exclusive(path: str) -> int:
    """Create a new file, failing if the name exists or is a symlink (never write through one)."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)


def open_existing(path: str) -> int:
    return os.open(path, os.O_RDWR | os.O_NOFOLLOW)


def preallocate(fd: int, size: int) -> bool:
    """Reserve ``size`` bytes so a full disk shows up now, not at 90 % (FR-U8).

    Returns False if the filesystem cannot reserve space and the file is only sparse.
    """
    try:
        os.posix_fallocate(fd, 0, size)
        return True
    except OSError as exc:
        if disk_full(exc):
            raise InsufficientStorage() from None
        if exc.errno == errno.EFBIG:
            raise TooLarge("The filesystem cannot hold a file this large.") from None
        if exc.errno not in _NO_FALLOCATE:
            raise
    try:
        os.ftruncate(fd, size)
    except OSError as exc:
        if exc.errno == errno.EFBIG:
            raise TooLarge("The filesystem cannot hold a file this large.") from None
        raise
    return False


def write_at(fd: int, offset: int, data: bytes) -> None:
    """``pwrite`` all of ``data``; a single call may write less than asked."""
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        offset += written
        view = view[written:]


def flush_range(fd: int, offset: int, length: int) -> None:
    """Make the range durable, then drop it from the page cache (FR-U5, FR-U14).

    Without the ``DONTNEED`` hint a 50 GB upload would push the models ComfyUI has cached out of
    RAM. The hint is best effort and its failure is ignored.
    """
    os.fsync(fd)
    with contextlib.suppress(OSError):
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)


def fsync_directory(path: str) -> None:
    """Persist a rename/link in ``path``. Not every filesystem supports it, so best effort."""
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_replace(tmp: str, dst: str) -> None:
    """Atomically put ``tmp`` at ``dst``, replacing what is there (``overwrite=true``)."""
    try:
        os.replace(tmp, dst)
    except IsADirectoryError:
        raise FileAlreadyExists("A folder with this name already exists.") from None
    fsync_directory(os.path.dirname(dst))


def publish_no_replace(tmp: str, dst: str) -> None:
    """Put ``tmp`` at ``dst`` unless ``dst`` exists (FR-U9).

    ``os.rename`` would silently replace a file created after our earlier check. A hard link
    fails with ``EEXIST`` instead, and the second name is then removed.
    """
    try:
        os.link(tmp, dst, follow_symlinks=False)
    except FileExistsError:
        raise FileAlreadyExists() from None
    except OSError as exc:
        if exc.errno not in _NO_HARDLINKS:
            raise
        _rename_if_absent(tmp, dst)
        fsync_directory(os.path.dirname(dst))
        return
    os.unlink(tmp)
    fsync_directory(os.path.dirname(dst))


def _rename_if_absent(tmp: str, dst: str) -> None:
    # Filesystems without hard links (some network mounts, FAT): not atomic against a file
    # appearing between the check and the rename, but there is no better primitive here.
    if os.path.lexists(dst):
        raise FileAlreadyExists()
    os.rename(tmp, dst)
