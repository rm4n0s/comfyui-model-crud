import errno
import os
from pathlib import Path

import pytest
from comfyui_model_crud.services import fsutil
from comfyui_model_crud.services.errors import FileAlreadyExists, InsufficientStorage, TooLarge


@pytest.fixture
def fd(tmp_path: Path):
    descriptor = os.open(tmp_path / "f", os.O_RDWR | os.O_CREAT, 0o644)
    yield descriptor
    os.close(descriptor)


def test_preallocate_reserves_the_full_size(tmp_path: Path, fd):
    assert fsutil.preallocate(fd, 3 * 1024 * 1024) is True

    stat = os.fstat(fd)
    assert stat.st_size == 3 * 1024 * 1024
    assert stat.st_blocks * 512 >= 3 * 1024 * 1024  # really allocated, not sparse


@pytest.mark.parametrize("code", [errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL])
def test_preallocate_falls_back_to_a_sparse_file(fd, monkeypatch, code):
    def refuse(*args):
        raise OSError(code, "no fallocate here")

    monkeypatch.setattr(os, "posix_fallocate", refuse)

    assert fsutil.preallocate(fd, 12345) is False
    assert os.fstat(fd).st_size == 12345


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_preallocate_maps_a_full_disk(fd, monkeypatch, code):
    monkeypatch.setattr(os, "posix_fallocate", lambda *a: (_ for _ in ()).throw(OSError(code, "x")))

    with pytest.raises(InsufficientStorage):
        fsutil.preallocate(fd, 10)


def test_preallocate_maps_a_file_that_is_too_big_for_the_filesystem(fd, monkeypatch):
    monkeypatch.setattr(
        os, "posix_fallocate", lambda *a: (_ for _ in ()).throw(OSError(errno.EFBIG, "x"))
    )

    with pytest.raises(TooLarge):
        fsutil.preallocate(fd, 10)


def test_preallocate_propagates_unexpected_errors(fd, monkeypatch):
    monkeypatch.setattr(
        os, "posix_fallocate", lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "x"))
    )

    with pytest.raises(OSError) as exc:
        fsutil.preallocate(fd, 10)
    assert exc.value.errno == errno.EIO


def test_write_at_writes_at_the_offset_and_survives_short_writes(fd, monkeypatch):
    real = os.pwrite
    calls = []

    def one_byte_at_a_time(descriptor, data, offset):
        calls.append(offset)
        return real(descriptor, bytes(data[:1]), offset)

    os.ftruncate(fd, 20)
    monkeypatch.setattr(os, "pwrite", one_byte_at_a_time)

    fsutil.write_at(fd, 5, b"hello")

    monkeypatch.setattr(os, "pwrite", real)
    assert calls == [5, 6, 7, 8, 9]
    assert os.pread(fd, 20, 0) == b"\0" * 5 + b"hello" + b"\0" * 10


def test_flush_range_ignores_a_missing_fadvise(fd, monkeypatch):
    os.write(fd, b"data")
    monkeypatch.setattr(
        os, "posix_fadvise", lambda *a: (_ for _ in ()).throw(OSError(errno.EINVAL, "x"))
    )

    fsutil.flush_range(fd, 0, 4)  # must not raise


def test_flush_range_asks_to_drop_the_pages_and_fsyncs(fd, monkeypatch):
    seen = []
    monkeypatch.setattr(os, "fsync", lambda f: seen.append(("fsync", f)))
    monkeypatch.setattr(os, "posix_fadvise", lambda *a: seen.append(("fadvise", a)))

    fsutil.flush_range(fd, 100, 50)

    assert seen == [("fsync", fd), ("fadvise", (fd, 100, 50, os.POSIX_FADV_DONTNEED))]


def test_publish_no_replace_moves_the_file(tmp_path: Path):
    (tmp_path / "t.part").write_bytes(b"new")

    fsutil.publish_no_replace(str(tmp_path / "t.part"), str(tmp_path / "m.safetensors"))

    assert (tmp_path / "m.safetensors").read_bytes() == b"new"
    assert not (tmp_path / "t.part").exists()


def test_publish_no_replace_never_replaces(tmp_path: Path):
    (tmp_path / "t.part").write_bytes(b"new")
    (tmp_path / "m.safetensors").write_bytes(b"old")

    with pytest.raises(FileAlreadyExists):
        fsutil.publish_no_replace(str(tmp_path / "t.part"), str(tmp_path / "m.safetensors"))

    assert (tmp_path / "m.safetensors").read_bytes() == b"old"
    assert (tmp_path / "t.part").read_bytes() == b"new"  # kept so the client can retry


def test_publish_no_replace_does_not_follow_a_symlink_at_the_destination(tmp_path: Path):
    (tmp_path / "t.part").write_bytes(b"new")
    (tmp_path / "victim").write_bytes(b"old")
    (tmp_path / "m.safetensors").symlink_to(tmp_path / "victim")

    with pytest.raises(FileAlreadyExists):
        fsutil.publish_no_replace(str(tmp_path / "t.part"), str(tmp_path / "m.safetensors"))

    assert (tmp_path / "victim").read_bytes() == b"old"


def test_publish_no_replace_falls_back_without_hard_links(tmp_path: Path, monkeypatch):
    (tmp_path / "t.part").write_bytes(b"new")
    monkeypatch.setattr(
        os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "x"))
    )

    fsutil.publish_no_replace(str(tmp_path / "t.part"), str(tmp_path / "m.safetensors"))
    assert (tmp_path / "m.safetensors").read_bytes() == b"new"

    (tmp_path / "t2.part").write_bytes(b"newer")
    with pytest.raises(FileAlreadyExists):  # the fallback still refuses to replace
        fsutil.publish_no_replace(str(tmp_path / "t2.part"), str(tmp_path / "m.safetensors"))
    assert (tmp_path / "m.safetensors").read_bytes() == b"new"


def test_publish_no_replace_propagates_other_link_errors(tmp_path: Path, monkeypatch):
    (tmp_path / "t.part").write_bytes(b"new")
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "x")))

    with pytest.raises(OSError) as exc:
        fsutil.publish_no_replace(str(tmp_path / "t.part"), str(tmp_path / "m.safetensors"))
    assert exc.value.errno == errno.EIO


def test_publish_replace_swaps_atomically_and_refuses_directories(tmp_path: Path):
    (tmp_path / "t.part").write_bytes(b"new")
    (tmp_path / "m.safetensors").write_bytes(b"old")

    fsutil.publish_replace(str(tmp_path / "t.part"), str(tmp_path / "m.safetensors"))
    assert (tmp_path / "m.safetensors").read_bytes() == b"new"

    (tmp_path / "t2.part").write_bytes(b"x")
    (tmp_path / "dir.safetensors").mkdir()
    with pytest.raises(FileAlreadyExists):
        fsutil.publish_replace(str(tmp_path / "t2.part"), str(tmp_path / "dir.safetensors"))


def test_create_exclusive_refuses_existing_names_and_symlinks(tmp_path: Path):
    (tmp_path / "taken").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "taken")

    for name in ("taken", "link"):
        with pytest.raises(FileExistsError):
            fsutil.create_exclusive(str(tmp_path / name))
    assert (tmp_path / "taken").read_bytes() == b"x"


def test_open_existing_refuses_symlinks(tmp_path: Path):
    (tmp_path / "real").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "real")

    with pytest.raises(OSError):
        fsutil.open_existing(str(tmp_path / "link"))


def test_free_space_check_keeps_a_margin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fsutil, "free_bytes", lambda d: 10 * 1024**3)

    fsutil.check_free_space(str(tmp_path), 9 * 1024**3 - 1)
    with pytest.raises(InsufficientStorage):
        fsutil.check_free_space(str(tmp_path), 9 * 1024**3 + 1)
