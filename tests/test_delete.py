import errno
import logging
import os
from pathlib import Path

import pytest
from comfyui_model_crud.services import models
from comfyui_model_crud.services.config import Config
from comfyui_model_crud.services.errors import (
    BadRequest,
    DeleteDisabled,
    ExtensionNotAllowed,
    InvalidFilename,
    ModelFileNotFound,
    PathOutsideRoot,
    PermissionDenied,
    UnknownFolder,
)

CONFIG = Config()


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def delete(folder="loras", rel="m.safetensors", root_index=None, config=CONFIG):
    return models.delete_model(folder, rel, root_index, config)


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


def test_deletes_the_file_and_reports_it(loras):
    touch(loras / "sdxl" / "m.safetensors", b"x" * 42)

    result = delete(rel="sdxl/m.safetensors")

    assert result == {"folder": "loras", "path": "sdxl/m.safetensors", "deleted": True}
    assert not (loras / "sdxl" / "m.safetensors").exists()


def test_only_that_file_goes(loras):
    touch(loras / "m.safetensors")
    keep = [
        touch(loras / "other.safetensors"),
        touch(loras / "m.safetensors.bak"),
        touch(loras / "sub" / "m.safetensors"),
    ]

    delete()

    assert all(p.exists() for p in keep)


def test_empty_parent_folders_are_left_in_place(loras):
    touch(loras / "a" / "b" / "m.safetensors")

    delete(rel="a/b/m.safetensors")

    assert (loras / "a" / "b").is_dir()


def test_filename_cache_is_invalidated_for_that_key_only(folder_paths, loras):
    touch(loras / "m.safetensors")
    folder_paths.filename_list_cache.update({"loras": "stale", "vae": "keep"})

    delete()

    assert folder_paths.filename_list_cache == {"vae": "keep"}


def test_missing_file_is_404_and_leaves_the_cache_alone(folder_paths, loras):
    folder_paths.filename_list_cache["loras"] = "keep"

    with pytest.raises(ModelFileNotFound):
        delete(rel="nope.safetensors")

    assert folder_paths.filename_list_cache == {"loras": "keep"}


def test_second_delete_of_the_same_file_is_404(loras):
    touch(loras / "m.safetensors")

    delete()
    with pytest.raises(ModelFileNotFound):
        delete()


def test_directories_are_never_deleted(loras):
    touch(loras / "dir.safetensors" / "inner.safetensors")
    touch(loras / "plain" / "inner.safetensors")

    for rel in ("dir.safetensors", "plain"):
        with pytest.raises((ModelFileNotFound, ExtensionNotAllowed)):
            delete(rel=rel)

    assert (loras / "dir.safetensors" / "inner.safetensors").exists()
    assert (loras / "plain" / "inner.safetensors").exists()


@pytest.mark.parametrize("name", ["notes.txt", "x.safetensors.exe", "noext", "x.py", "x.json"])
def test_disallowed_extensions_are_refused_and_the_file_survives(loras, name):
    path = touch(loras / name)

    with pytest.raises(ExtensionNotAllowed):
        delete(rel=name)

    assert path.exists()


@pytest.mark.parametrize("name", ["m.SAFETENSORS", "g.gguf", "o.onnx", "a.exe.safetensors"])
def test_allowed_extensions_can_be_deleted(loras, name):
    touch(loras / name)

    delete(rel=name)

    assert not (loras / name).exists()


def test_extra_extension_from_config_is_deletable(loras):
    touch(loras / "c.yaml")

    with pytest.raises(ExtensionNotAllowed):
        delete(rel="c.yaml")
    delete(rel="c.yaml", config=Config(extra_extensions=(".yaml",)))

    assert not (loras / "c.yaml").exists()


@pytest.mark.parametrize(
    "rel",
    [
        "../secret.safetensors",
        "/etc/passwd",
        "a/../../secret.safetensors",
        "..\\secret.safetensors",
        "x\x00.safetensors",
        ".",
        "..",
        "a//m.safetensors",
    ],
)
def test_traversal_never_touches_files_outside_the_root(folder_paths, loras, tmp_path, rel):
    secret = touch(tmp_path / "models" / "secret.safetensors")

    with pytest.raises(PathOutsideRoot):
        delete(rel=rel)

    assert secret.exists()


def test_hidden_names_are_refused(loras):
    path = touch(loras / ".hidden.safetensors")

    with pytest.raises(InvalidFilename):
        delete(rel=".hidden.safetensors")
    with pytest.raises(BadRequest):
        delete(rel=".git/x.safetensors")

    assert path.exists()


def test_symlink_inside_the_root_removes_the_link_not_the_target(loras):
    real = touch(loras / "real" / "m.safetensors", b"precious")
    (loras / "alias.safetensors").symlink_to(real)

    delete(rel="alias.safetensors")

    assert not (loras / "alias.safetensors").is_symlink()
    assert real.read_bytes() == b"precious"


def test_symlink_pointing_outside_the_root_is_refused_and_nothing_is_removed(loras, tmp_path):
    secret = touch(tmp_path / "outside" / "secret.safetensors", b"secret")
    link = loras / "link.safetensors"
    link.symlink_to(secret)

    with pytest.raises(PathOutsideRoot):
        delete(rel="link.safetensors")

    assert link.is_symlink() and secret.read_bytes() == b"secret"


def test_directory_symlink_leaving_the_root_is_refused(loras, tmp_path):
    secret = touch(tmp_path / "outside" / "secret.safetensors")
    (loras / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(PathOutsideRoot):
        delete(rel="escape/secret.safetensors")

    assert secret.exists()


def test_root_index_selects_the_duplicate_and_default_is_the_first_root(folder_paths, tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    touch(a / "same.safetensors", b"A")
    touch(b / "same.safetensors", b"B")

    delete("controlnet", "same.safetensors", root_index=1)
    assert (a / "same.safetensors").exists() and not (b / "same.safetensors").exists()

    touch(b / "same.safetensors", b"B")
    delete("controlnet", "same.safetensors")
    assert not (a / "same.safetensors").exists() and (b / "same.safetensors").exists()


def test_root_index_does_not_fall_through_to_another_root(folder_paths, tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    only_b = touch(b / "only_b.safetensors")

    with pytest.raises(ModelFileNotFound):
        delete("controlnet", "only_b.safetensors", root_index=0)

    assert only_b.exists()


@pytest.mark.parametrize("key", ["nope", "unet", "", None, "custom_nodes"])
def test_unknown_folder(folder_paths, tmp_path, key):
    (tmp_path / "nodes").mkdir()
    folder_paths.folder_names_and_paths["custom_nodes"] = ([str(tmp_path / "nodes")], set())

    with pytest.raises(UnknownFolder):
        delete(folder=key)


@pytest.mark.parametrize("config", [Config(read_only=True), Config(allow_delete=False)])
def test_disabled_delete_is_refused_before_anything_else(folder_paths, loras, config):
    path = touch(loras / "m.safetensors")

    with pytest.raises(DeleteDisabled):
        delete(config=config)
    with pytest.raises(DeleteDisabled):  # not even "unknown folder" or "bad path" leaks out
        delete(folder="nope", rel="../x", config=config)

    assert path.exists()


def test_upload_flag_alone_does_not_block_delete(loras):
    touch(loras / "m.safetensors")

    delete(config=Config(allow_upload=False))

    assert not (loras / "m.safetensors").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_directory_maps_to_permission_denied(folder_paths, loras):
    path = touch(loras / "locked" / "m.safetensors")
    folder_paths.filename_list_cache["loras"] = "keep"
    path.parent.chmod(0o555)
    try:
        with pytest.raises(PermissionDenied) as exc:
            delete(rel="locked/m.safetensors")
    finally:
        path.parent.chmod(0o755)

    assert exc.value.status == 403
    assert path.exists() and folder_paths.filename_list_cache == {"loras": "keep"}


def test_other_os_errors_propagate_unchanged_for_the_handler_to_log(loras, monkeypatch):
    touch(loras / "m.safetensors")

    def fail(path):
        raise OSError(errno.EIO, "disk on fire at /secret/place")

    monkeypatch.setattr(os, "unlink", fail)

    with pytest.raises(OSError) as exc:
        delete()
    assert exc.value.errno == errno.EIO


def test_losing_a_race_to_another_delete_is_a_404(loras, monkeypatch):
    touch(loras / "m.safetensors")
    real_unlink = os.unlink

    def racing(path):
        real_unlink(path)  # someone else got there first...
        raise FileNotFoundError(path)  # ...so ours finds nothing

    monkeypatch.setattr(os, "unlink", racing)

    with pytest.raises(ModelFileNotFound):
        delete()


def test_unicode_and_spaces(loras):
    name = "Süß modell 日本 (v2).safetensors"
    touch(loras / "sdxl v2" / "été" / name)

    result = delete(rel=f"sdxl v2/été/{name}")

    assert result["path"] == f"sdxl v2/été/{name}"
    assert not (loras / "sdxl v2" / "été" / name).exists()


def test_info_log_has_folder_path_and_size_but_no_absolute_path(loras, tmp_path, caplog):
    touch(loras / "sdxl" / "m.safetensors", b"x" * 123)

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        delete(rel="sdxl/m.safetensors")

    [record] = caplog.records
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert (
        "folder=loras" in message and "path=sdxl/m.safetensors" in message and "size=123" in message
    )
    assert str(tmp_path) not in message
