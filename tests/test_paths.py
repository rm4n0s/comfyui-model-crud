import logging
import os
import random
from pathlib import Path
from urllib.parse import unquote

import pytest
from comfyui_model_crud.services import paths
from comfyui_model_crud.services.config import DEFAULT_EXTENSIONS
from comfyui_model_crud.services.errors import (
    BadRequest,
    ExtensionNotAllowed,
    InvalidFilename,
    ModelCrudError,
    ModelFileNotFound,
    PathOutsideRoot,
    UnknownFolder,
)

ALLOWED = frozenset(DEFAULT_EXTENSIONS)


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def existing(folder: str, rel: object, **kw):
    return paths.resolve_existing(folder, rel, ALLOWED, **kw)


def target(folder: str, filename: object, subfolder: object = None):
    return paths.resolve_upload_target(folder, filename, subfolder, ALLOWED)


@pytest.fixture
def loras(folder_paths) -> Path:
    return Path(folder_paths.get_folder_paths("loras")[0])


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory next to the model roots, holding a file that must stay unreachable."""
    directory = tmp_path / "outside"
    touch(directory / "secret.safetensors", b"secret")
    return directory


# --- traversal ------------------------------------------------------------------------------

TRAVERSAL = [
    "../secret.safetensors",
    "../../x.safetensors",
    "a/../../x.safetensors",
    "a/../x.safetensors",  # stays inside, but ".." is never accepted
    "./x.safetensors",
    "a/./x.safetensors",
    "a//x.safetensors",
    "a/x.safetensors/",
    "/etc/passwd",
    "/x.safetensors",
    "//x.safetensors",
    "C:/x.safetensors",
    "c:x.safetensors",
    "..\\x.safetensors",
    "a\\x.safetensors",
    "a\\..\\..\\x.safetensors",
    "x\x00.safetensors",
    "a/\x00/x.safetensors",
    "..",
    ".",
    "/",
    "a/" + "b/" * 3000 + "x.safetensors",
    # what a server sees after URL-decoding common encodings of the above
    unquote("%2e%2e/secret.safetensors"),
    unquote("..%2fsecret.safetensors"),
    unquote("%2e%2e%2fsecret.safetensors"),
    unquote("%2e%2e%5csecret.safetensors"),
    unquote("%2fetc%2fpasswd"),
    unquote("a%00.safetensors"),
]


@pytest.mark.parametrize("rel", TRAVERSAL)
def test_existing_rejects_traversal(folder_paths, loras, outside, tmp_path, rel):
    touch(tmp_path / "models" / "secret.safetensors")  # sibling of the root: "../secret" exists

    with pytest.raises(PathOutsideRoot) as exc:
        existing("loras", rel)

    assert exc.value.status == 400 and exc.value.code == "path_outside_root"
    assert str(tmp_path) not in exc.value.message


@pytest.mark.parametrize(
    "sub", [t for t in TRAVERSAL if t.endswith(("x.safetensors", "/"))] + ["../", ".."]
)
def test_upload_subfolder_rejects_traversal(folder_paths, loras, sub):
    with pytest.raises(PathOutsideRoot):
        target("loras", "m.safetensors", sub)


def test_double_encoded_input_is_not_decoded_again(folder_paths, loras):
    literal = unquote("%252e%252e")  # == "%2e%2e": an ordinary, if odd, directory name

    resolved = target("loras", "m.safetensors", literal)

    assert resolved.relpath == "%2e%2e/m.safetensors"
    assert paths.is_inside(str(loras), resolved.entry)


def test_filename_with_traversal_is_reduced_to_basename(folder_paths, loras):
    for name in ["../../etc/m.safetensors", "..\\..\\m.safetensors", "/abs/m.safetensors"]:
        resolved = target("loras", name)

        assert resolved.entry == str(loras / "m.safetensors")
        assert resolved.relpath == "m.safetensors"


# --- symlinks -------------------------------------------------------------------------------


def test_directory_symlink_escaping_the_root_is_rejected(folder_paths, loras, outside):
    (loras / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathOutsideRoot):
        existing("loras", "escape/secret.safetensors")
    with pytest.raises(PathOutsideRoot):
        target("loras", "new.safetensors", "escape")


def test_file_symlink_escaping_the_root_is_rejected(folder_paths, loras, outside):
    (loras / "link.safetensors").symlink_to(outside / "secret.safetensors")

    with pytest.raises(PathOutsideRoot):
        existing("loras", "link.safetensors")
    with pytest.raises(PathOutsideRoot):  # an upload must not write through the link either
        target("loras", "link.safetensors")


def test_escaping_parent_is_rejected_even_if_the_final_link_points_back_inside(
    folder_paths, loras, outside
):
    # Writing "escape/back.safetensors" would land in `outside/`, whatever `back` points at.
    inner = touch(loras / "inner.safetensors")
    (outside / "back.safetensors").symlink_to(inner)
    (loras / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathOutsideRoot):
        existing("loras", "escape/back.safetensors")
    with pytest.raises(PathOutsideRoot):
        target("loras", "back.safetensors", "escape")


def test_symlink_to_the_root_itself_is_rejected(folder_paths, loras):
    (loras / "loop.safetensors").symlink_to(loras, target_is_directory=True)

    with pytest.raises(PathOutsideRoot):
        existing("loras", "loop.safetensors")


def test_symlink_inside_the_root_resolves_but_entry_stays_the_link(folder_paths, loras):
    real = touch(loras / "real" / "model.safetensors")
    (loras / "alias.safetensors").symlink_to(real)

    resolved = existing("loras", "alias.safetensors")

    assert resolved.entry == str(loras / "alias.safetensors")  # unlink removes the link only
    assert resolved.real == str(real)
    assert resolved.relpath == "alias.safetensors"


def test_directory_symlink_inside_the_root_is_followed(folder_paths, loras):
    touch(loras / "real" / "model.safetensors")
    (loras / "linkdir").symlink_to(loras / "real", target_is_directory=True)

    resolved = existing("loras", "linkdir/model.safetensors")

    assert resolved.relpath == "linkdir/model.safetensors"
    assert resolved.entry == str(loras / "real" / "model.safetensors")


def test_root_that_is_itself_a_symlink(folder_paths, tmp_path):
    disk = tmp_path / "disk" / "linked"
    touch(disk / "m.safetensors")
    link = tmp_path / "models" / "linked"
    link.symlink_to(disk, target_is_directory=True)
    folder_paths.folder_names_and_paths["linked"] = ([str(link)], set(DEFAULT_EXTENSIONS))

    resolved = existing("linked", "m.safetensors")

    assert resolved.root == str(disk)
    assert resolved.relpath == "m.safetensors"


def test_dangling_symlink_is_not_found(folder_paths, loras):
    (loras / "gone.safetensors").symlink_to(loras / "nowhere.safetensors")

    with pytest.raises(ModelFileNotFound):
        existing("loras", "gone.safetensors")


# --- extensions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["x.exe", "x.safetensors.exe", "noext", "x.", "x.py", "x.safetensors.part", "x.json"]
)
def test_disallowed_extensions_are_rejected(folder_paths, loras, name):
    touch(loras / name)

    with pytest.raises(ExtensionNotAllowed) as exc:
        existing("loras", name)
    assert exc.value.status == 415
    with pytest.raises(ExtensionNotAllowed):
        target("loras", name)


@pytest.mark.parametrize(
    "name", ["x.SAFETENSORS", "x.SafeTensors", "x.exe.safetensors", "a.b.ckpt"]
)
def test_allowed_extensions_are_case_insensitive_and_use_the_last_one(folder_paths, loras, name):
    touch(loras / name)

    assert existing("loras", name).relpath == name
    assert target("loras", name).relpath == name


def test_every_default_extension_is_accepted(folder_paths, loras):
    for ext in DEFAULT_EXTENSIONS:
        assert target("loras", f"m{ext}").relpath == f"m{ext}"


def test_extra_extension_is_honoured_when_passed_in(folder_paths, loras):
    with pytest.raises(ExtensionNotAllowed):
        target("loras", "c.yaml")

    resolved = paths.resolve_upload_target("loras", "c.yaml", None, ALLOWED | {".yaml"})
    assert resolved.relpath == "c.yaml"


def test_extension_is_checked_after_the_path_so_traversal_wins(folder_paths):
    with pytest.raises(PathOutsideRoot):
        existing("loras", "../x.exe")


# --- file names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a.safetensors", "a.safetensors"),
        ("dir/a.safetensors", "a.safetensors"),
        ("dir\\a.safetensors", "a.safetensors"),
        ("/etc/a.safetensors", "a.safetensors"),
        ("..\\..\\a.safetensors", "a.safetensors"),
        ("  a b .safetensors  ", "a b .safetensors"),
        ("a\tb\nc.safetensors", "abc.safetensors"),
        ("a\u202eb.safetensors", "ab.safetensors"),
        ("my model ünï 日本 (v2).safetensors", "my model ünï 日本 (v2).safetensors"),
        ("é" * 127, "é" * 127),
        ("a" * 255, "a" * 255),
    ],
)
def test_sanitize_filename(raw, expected):
    assert paths.sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "/",
        "a/",
        ".",
        "..",
        "a/..",
        ".hidden.safetensors",
        "dir/.hidden.safetensors",
        "a\x00b.safetensors",
        "\x07\x08",
        "a" * 256,
        "é" * 128,  # 256 bytes
        "\ud800.safetensors",  # lone surrogate: not representable as UTF-8
        None,
        5,
        b"a.safetensors",
        ["a.safetensors"],
    ],
)
def test_sanitize_filename_rejects(raw):
    with pytest.raises(InvalidFilename) as exc:
        paths.sanitize_filename(raw)
    assert exc.value.status == 400


def test_unicode_and_spaces_round_trip_through_existing_and_upload(folder_paths, loras):
    name = "Süß modell 日本.safetensors"
    sub = "sdxl v2/été"
    touch(loras / sub / name)

    found = existing("loras", f"{sub}/{name}")
    planned = target("loras", name, sub)

    assert found.relpath == planned.relpath == f"{sub}/{name}"
    assert found.entry == planned.entry == str(loras / sub / name)


@pytest.mark.parametrize("rel", [".git/x.safetensors", "a/.hidden/x.safetensors"])
def test_hidden_directories_are_refused(folder_paths, loras, rel):
    with pytest.raises(BadRequest):
        existing("loras", rel)


def test_hidden_final_name_is_an_invalid_filename(folder_paths):
    with pytest.raises(InvalidFilename):
        existing("loras", "a/.x.safetensors")


def test_overlong_segments_are_refused(folder_paths):
    with pytest.raises(InvalidFilename):
        existing("loras", "a/" + "n" * 300 + ".safetensors")
    with pytest.raises(BadRequest):
        existing("loras", "n" * 300 + "/x.safetensors")


def test_temp_file_names_can_never_be_addressed(folder_paths, loras):
    touch(loras / ".model_crud_abc.part")

    with pytest.raises(InvalidFilename):
        existing("loras", ".model_crud_abc.part")
    with pytest.raises(InvalidFilename):
        target("loras", ".model_crud_abc.part")


# --- upload targets -------------------------------------------------------------------------


def test_upload_target_does_not_touch_the_disk(folder_paths, loras):
    resolved = target("loras", "m.safetensors", "a/b")

    assert resolved.relpath == "a/b/m.safetensors"
    assert resolved.directory == str(loras / "a" / "b")
    assert not (loras / "a").exists()


@pytest.mark.parametrize("sub", [None, ""])
def test_upload_without_subfolder_goes_to_the_root(folder_paths, loras, sub):
    assert target("loras", "m.safetensors", sub).directory == str(loras)


def test_subfolder_depth_limit(folder_paths):
    assert target("loras", "m.safetensors", "a/b/c/d").relpath == "a/b/c/d/m.safetensors"
    with pytest.raises(BadRequest):
        target("loras", "m.safetensors", "a/b/c/d/e")


@pytest.mark.parametrize("sub", [".git", "a/.b", 5, ["a"], "a\x07b"])
def test_bad_subfolders(folder_paths, sub):
    with pytest.raises((BadRequest, PathOutsideRoot)):
        target("loras", "m.safetensors", sub)


def test_upload_works_when_the_root_does_not_exist_yet(folder_paths, tmp_path):
    missing = tmp_path / "later" / "loras2"
    folder_paths.folder_names_and_paths["fresh"] = ([str(missing)], set(DEFAULT_EXTENSIONS))

    resolved = target("fresh", "m.safetensors", "s")

    assert resolved.entry == str(missing / "s" / "m.safetensors")
    assert not missing.exists()


def test_upload_always_uses_the_first_root(folder_paths, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    folder_paths.add_folder("controlnet", a, b)

    resolved = target("controlnet", "m.safetensors")

    assert resolved.root_index == 0 and resolved.root == str(a)


# --- existing files, roots and keys ---------------------------------------------------------


def test_missing_file_and_directory_are_not_found(folder_paths, loras):
    (loras / "dir.safetensors").mkdir()

    with pytest.raises(ModelFileNotFound) as exc:
        existing("loras", "nope.safetensors")
    assert exc.value.status == 404
    with pytest.raises(ModelFileNotFound):
        existing("loras", "dir.safetensors")
    with pytest.raises(ModelFileNotFound):
        existing("loras", "missing_dir/x.safetensors")


def test_resolution_returns_relative_paths_only(folder_paths, loras):
    touch(loras / "sdxl" / "m.safetensors")

    resolved = existing("loras", "sdxl/m.safetensors")

    assert resolved.relpath == "sdxl/m.safetensors"
    assert not os.path.isabs(resolved.relpath)
    assert resolved.name == "m.safetensors" and resolved.root_index == 0


@pytest.fixture
def two_roots(folder_paths, tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    folder_paths.add_folder("controlnet", a, b)
    return a, b


def test_first_root_containing_the_file_wins(two_roots):
    a, b = two_roots
    touch(b / "only_b.safetensors")
    touch(a / "both.safetensors")
    touch(b / "both.safetensors")

    assert existing("controlnet", "both.safetensors").root_index == 0
    assert existing("controlnet", "only_b.safetensors").root_index == 1


def test_root_index_selects_the_duplicate(two_roots):
    a, b = two_roots
    touch(a / "both.safetensors")
    touch(b / "both.safetensors")

    resolved = existing("controlnet", "both.safetensors", root_index=1)

    assert resolved.root_index == 1 and resolved.entry == str(b / "both.safetensors")


def test_root_index_does_not_fall_through_to_other_roots(two_roots):
    _, b = two_roots
    touch(b / "only_b.safetensors")

    with pytest.raises(ModelFileNotFound):
        existing("controlnet", "only_b.safetensors", root_index=0)


@pytest.mark.parametrize("index", [2, -1, 99, "1", 1.0, True])
def test_invalid_root_index(two_roots, index):
    with pytest.raises(BadRequest):
        existing("controlnet", "x.safetensors", root_index=index)


@pytest.mark.parametrize(
    "key", ["nope", "", "../loras", "loras/", "LORAS", "unet", None, 3, ["loras"]]
)
def test_unknown_folder_keys(folder_paths, key):
    with pytest.raises(UnknownFolder) as exc:
        existing(key, "x.safetensors")
    assert exc.value.status == 404
    with pytest.raises(UnknownFolder):
        target(key, "x.safetensors")


def test_folder_without_roots_is_unknown(folder_paths):
    folder_paths.folder_names_and_paths["empty"] = ([], set(DEFAULT_EXTENSIONS))

    with pytest.raises(UnknownFolder):
        target("empty", "x.safetensors")


@pytest.mark.parametrize("rel", [None, 5, b"x.safetensors", ["a"], ""])
def test_non_string_relative_paths(folder_paths, rel):
    with pytest.raises(BadRequest):
        existing("loras", rel)


# --- logging --------------------------------------------------------------------------------


def test_traversal_attempt_is_logged_as_a_warning_without_log_injection(folder_paths, caplog):
    hostile = "../x.safetensors\nWARNING forged line\r\x1b[31m"

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        with pytest.raises(PathOutsideRoot):
            existing("loras", hostile)

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "\n" not in record.getMessage() and "\x1b" not in record.getMessage()
    assert "forged line" in record.getMessage()  # recorded, but escaped on one line


def test_logged_value_is_truncated(folder_paths, caplog):
    with caplog.at_level(logging.WARNING, logger="comfyui-model-crud"):
        with pytest.raises(PathOutsideRoot):
            existing("loras", "../" + "a" * 100_000)

    assert len(caplog.records[0].getMessage()) < 600


# --- randomised invariant -------------------------------------------------------------------

ATOMS = [
    "a",
    "b.safetensors",
    "..",
    ".",
    "/",
    "//",
    "\\",
    "%2e%2e",
    "é",
    " ",
    "\x00",
    "C:",
    "~",
    "\n",
]


def test_nothing_accepted_ever_leaves_the_root_and_nothing_else_is_raised(
    folder_paths, loras, outside, tmp_path
):
    (loras / "escape").symlink_to(outside, target_is_directory=True)
    (loras / "inside").mkdir()
    (loras / "inside" / "back").symlink_to(loras, target_is_directory=True)
    (loras / "a").mkdir()
    touch(loras / "a" / "b.safetensors")
    root = str(loras.resolve())
    rng = random.Random(1234)
    accepted = 0

    for _ in range(4000):
        raw = "".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 6)))
        for call, args in (
            (existing, ("loras", raw)),
            (target, ("loras", "m.safetensors", raw)),
            (target, ("loras", raw)),
        ):
            try:
                resolved = call(*args)
            except ModelCrudError:
                continue
            accepted += 1
            assert paths.is_inside(root, resolved.entry), raw
            assert paths.is_inside(root, resolved.real), raw
            assert not os.path.isabs(resolved.relpath), raw
            assert ".." not in resolved.relpath.split("/"), raw

    assert accepted > 0  # the generator does produce valid input, so the loop is not vacuous
