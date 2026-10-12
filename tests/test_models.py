import os
import re
import time
from pathlib import Path

import pytest
from comfyui_model_crud.services import models
from comfyui_model_crud.services.config import DEFAULT_EXTENSIONS
from comfyui_model_crud.services.errors import UnknownFolder

ALLOWED = frozenset(DEFAULT_EXTENSIONS)


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def listing(folder: str = "loras", allowed=ALLOWED) -> dict[tuple[str, int], dict]:
    return {(f["path"], f["root_index"]): f for f in models.list_models(folder, allowed)}


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


# --- list_models ----------------------------------------------------------------------------


def test_entry_shape_size_and_utc_timestamp(loras):
    f = touch(loras / "a.safetensors", b"x" * 1234)
    os.utime(f, (1_760_000_000, 1_760_000_000))

    [entry] = models.list_models("loras", ALLOWED)

    assert entry == {
        "path": "a.safetensors",
        "name": "a.safetensors",
        "size": 1234,
        "modified": "2025-10-09T08:53:20Z",
        "root_index": 0,
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["modified"])


def test_nested_folders_use_forward_slashes_and_sort_by_path(loras):
    for rel in ["z.safetensors", "sdxl/b.safetensors", "sdxl/deep/er/a.ckpt", "a.pt"]:
        touch(loras / rel)

    result = models.list_models("loras", ALLOWED)

    assert [f["path"] for f in result] == [
        "a.pt",
        "sdxl/b.safetensors",
        "sdxl/deep/er/a.ckpt",
        "z.safetensors",
    ]
    assert result[2]["name"] == "a.ckpt"


def test_only_allowed_extensions_are_listed_case_insensitively(loras):
    for name in ["ok.safetensors", "UP.SAFETENSORS", "g.gguf", "m.onnx"]:
        touch(loras / name)
    for name in ["notes.txt", "c.json", "x.yaml", "noext", "x.safetensors.exe", "x.py"]:
        touch(loras / name)

    assert sorted(f["name"] for f in models.list_models("loras", ALLOWED)) == [
        "UP.SAFETENSORS",
        "g.gguf",
        "m.onnx",
        "ok.safetensors",
    ]


def test_extra_extensions_extend_the_listing(loras):
    touch(loras / "x.yaml")

    assert listing(allowed=ALLOWED | {".yaml"}).keys() == {("x.yaml", 0)}


def test_hidden_files_directories_and_upload_temp_files_are_skipped(loras):
    touch(loras / ".hidden.safetensors")
    touch(loras / ".model_crud_abc.part")
    touch(loras / ".model_crud_abc.json")
    touch(loras / ".git" / "obj.safetensors")
    touch(loras / "visible.safetensors")

    assert listing().keys() == {("visible.safetensors", 0)}


def test_names_the_api_could_not_address_are_skipped(loras):
    touch(loras / "bad\nname.safetensors")
    touch(loras / "fine.safetensors")

    assert listing().keys() == {("fine.safetensors", 0)}


def test_unicode_and_spaces(loras):
    touch(loras / "sdxl v2" / "Süß modell 日本 (1).safetensors")

    [entry] = models.list_models("loras", ALLOWED)

    assert entry["path"] == "sdxl v2/Süß modell 日本 (1).safetensors"
    assert entry["name"] == "Süß modell 日本 (1).safetensors"


def test_multiple_roots_with_duplicate_relative_paths(folder_paths, tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    touch(a / "same.safetensors", b"aa")
    touch(b / "same.safetensors", b"bbbb")
    touch(b / "only_b.safetensors")

    result = listing("controlnet")

    assert result.keys() == {
        ("same.safetensors", 0),
        ("same.safetensors", 1),
        ("only_b.safetensors", 1),
    }
    assert result[("same.safetensors", 0)]["size"] == 2
    assert result[("same.safetensors", 1)]["size"] == 4


def test_missing_and_unreadable_roots_do_not_fail_the_listing(folder_paths, tmp_path):
    present = tmp_path / "here"
    touch(present / "m.safetensors")
    folder_paths.folder_names_and_paths["mixed"] = (
        [str(tmp_path / "missing"), str(present)],
        set(DEFAULT_EXTENSIONS),
    )

    assert listing("mixed").keys() == {("m.safetensors", 1)}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_subdirectory_is_skipped(loras):
    touch(loras / "ok.safetensors")
    locked = loras / "locked"
    touch(locked / "hidden.safetensors")
    locked.chmod(0)
    try:
        assert listing().keys() == {("ok.safetensors", 0)}
    finally:
        locked.chmod(0o755)


def test_symlinks_escaping_the_root_are_skipped(loras, tmp_path):
    outside = tmp_path / "outside"
    touch(outside / "secret.safetensors", b"secret")
    (loras / "link.safetensors").symlink_to(outside / "secret.safetensors")
    (loras / "dirlink").symlink_to(outside, target_is_directory=True)
    touch(loras / "ok.safetensors")

    assert listing().keys() == {("ok.safetensors", 0)}


def test_symlink_to_a_file_inside_the_root_is_listed_with_the_targets_size(loras):
    touch(loras / "real" / "m.safetensors", b"x" * 77)
    (loras / "alias.safetensors").symlink_to(loras / "real" / "m.safetensors")

    result = listing()

    assert result[("alias.safetensors", 0)]["size"] == 77
    assert ("real/m.safetensors", 0) in result


def test_directory_symlinks_are_not_followed_so_loops_and_duplicates_cannot_happen(loras):
    touch(loras / "real" / "m.safetensors")
    (loras / "real" / "back").symlink_to(loras, target_is_directory=True)
    (loras / "dup").symlink_to(loras / "real", target_is_directory=True)

    assert listing().keys() == {("real/m.safetensors", 0)}


def test_dangling_symlink_and_directory_named_like_a_model_are_skipped(loras):
    (loras / "gone.safetensors").symlink_to(loras / "nowhere.safetensors")
    (loras / "dir.safetensors").mkdir()
    (loras / "dir.safetensors" / "inner.safetensors").write_bytes(b"x")

    assert listing().keys() == {("dir.safetensors/inner.safetensors", 0)}


def test_unknown_folder(folder_paths):
    with pytest.raises(UnknownFolder):
        models.list_models("nope", ALLOWED)


def test_listing_does_not_read_file_contents(loras, monkeypatch):
    touch(loras / "m.safetensors")
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("open() called"))
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("os.open() called"))

    assert len(models.list_models("loras", ALLOWED)) == 1


def test_listing_10000_files_takes_under_a_second(loras):
    for d in range(100):
        directory = loras / f"d{d:03}"
        directory.mkdir()
        for i in range(100):
            (directory / f"m{i:03}.safetensors").write_bytes(b"x")

    start = time.perf_counter()
    result = models.list_models("loras", ALLOWED)
    elapsed = time.perf_counter() - start

    assert len(result) == 10_000
    assert elapsed < 1.0, f"listing took {elapsed:.2f}s"


# --- list_folders ---------------------------------------------------------------------------


def test_folders_lists_model_keys_with_root_counts_sorted(folder_paths, tmp_path):
    folder_paths.add_folder("controlnet", tmp_path / "ra", tmp_path / "rb")

    assert models.list_folders(ALLOWED) == [
        {"key": "checkpoints", "roots": 1},
        {"key": "controlnet", "roots": 2},
        {"key": "loras", "roots": 1},
        {"key": "vae", "roots": 1},
    ]


def test_non_model_keys_are_not_offered(folder_paths, tmp_path):
    # Shapes copied from folder_paths.py in ComfyUI: empty set, list, {""}, ["folder"].
    for key, exts in {
        "custom_nodes": set(),
        "datasets": set(),
        "configs": [".yaml"],
        "classifiers": {""},
        "diffusers": ["folder"],
    }.items():
        (tmp_path / key).mkdir()
        folder_paths.folder_names_and_paths[key] = ([str(tmp_path / key)], exts)

    keys = {f["key"] for f in models.list_folders(ALLOWED)}

    assert keys == {"checkpoints", "loras", "vae"}


def test_extra_extension_makes_a_matching_key_appear(folder_paths, tmp_path):
    (tmp_path / "cfg").mkdir()
    folder_paths.folder_names_and_paths["configs"] = ([str(tmp_path / "cfg")], [".yaml"])

    assert "configs" not in {f["key"] for f in models.list_folders(ALLOWED)}
    assert "configs" in {f["key"] for f in models.list_folders(ALLOWED | {".yaml"})}


def test_keys_without_an_existing_root_are_not_offered(folder_paths, tmp_path):
    folder_paths.folder_names_and_paths["ghost"] = (
        [str(tmp_path / "nope")],
        set(DEFAULT_EXTENSIONS),
    )
    folder_paths.folder_names_and_paths["norooots"] = ([], set(DEFAULT_EXTENSIONS))

    keys = {f["key"] for f in models.list_folders(ALLOWED)}

    assert "ghost" not in keys and "norooots" not in keys


def test_roots_counts_configured_roots_even_if_some_are_missing(folder_paths, tmp_path):
    (tmp_path / "real").mkdir()
    folder_paths.folder_names_and_paths["half"] = (
        [str(tmp_path / "gone"), str(tmp_path / "real")],
        set(DEFAULT_EXTENSIONS),
    )

    assert {"key": "half", "roots": 2} in models.list_folders(ALLOWED)


def test_listing_a_non_model_key_is_unknown_not_empty(folder_paths, tmp_path):
    (tmp_path / "nodes").mkdir()
    folder_paths.folder_names_and_paths["custom_nodes"] = ([str(tmp_path / "nodes")], set())

    with pytest.raises(UnknownFolder):
        models.list_models("custom_nodes", ALLOWED)
