import ast
from pathlib import Path

import pytest
from comfyui_model_crud.services import comfy
from comfyui_model_crud.services.errors import UnknownFolder

SERVICES = Path(__file__).resolve().parent.parent / "services"


def test_known_and_unknown_keys(folder_paths):
    assert comfy.get_folder_paths("loras") == folder_paths.get_folder_paths("loras")
    assert "loras" in comfy.folder_keys()
    for key in ("unet", "clip", "nope", None, 1):
        with pytest.raises(UnknownFolder):
            comfy.get_folder_paths(key)


def test_legacy_alias_is_rejected_even_if_comfyui_would_map_it(folder_paths):
    # Real ComfyUI maps unet -> diffusion_models inside get_folder_paths. We must not follow it,
    # or the cache would be invalidated under the wrong key.
    folder_paths.add_folder("diffusion_models")
    folder_paths.get_folder_paths_orig = folder_paths.get_folder_paths
    folder_paths.get_folder_paths = lambda name: folder_paths.get_folder_paths_orig(
        {"unet": "diffusion_models"}.get(name, name)
    )

    assert comfy.get_folder_paths("diffusion_models")
    with pytest.raises(UnknownFolder):
        comfy.get_folder_paths("unet")


def test_returned_roots_are_a_copy(folder_paths):
    comfy.get_folder_paths("loras").append("/elsewhere")

    assert len(folder_paths.get_folder_paths("loras")) == 1


def test_folder_extensions_are_lowercased_and_tolerate_lists(folder_paths):
    folder_paths.folder_names_and_paths["odd"] = ([], [".YAML", ""])

    assert comfy.folder_extensions("odd") == {".yaml", ""}


def test_invalidate_cache_pops_only_that_key(folder_paths):
    folder_paths.filename_list_cache.update({"loras": 1, "vae": 2})

    comfy.invalidate_cache("loras")
    comfy.invalidate_cache("loras")  # idempotent

    assert folder_paths.filename_list_cache == {"vae": 2}


def test_invalidate_cache_tolerates_a_comfyui_without_the_cache(folder_paths):
    del folder_paths.filename_list_cache

    comfy.invalidate_cache("loras")


def test_services_never_import_aiohttp_or_comfyui_directly():
    forbidden = {"aiohttp", "server", "folder_paths", "nodes", "comfy"}
    for source in SERVICES.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                # folder_paths is imported lazily, and only inside services/comfy.py
                allowed = source.name == "comfy.py" and top == "folder_paths"
                assert top not in forbidden or allowed, f"{source.name} imports {name}"
