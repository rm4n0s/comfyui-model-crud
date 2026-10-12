import logging
import re
import sys
from pathlib import Path


def test_package_exports(load_package):
    pkg = load_package()

    assert pkg.NODE_CLASS_MAPPINGS == {}
    assert pkg.WEB_DIRECTORY == "./web"
    assert sorted(pkg.__all__) == ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]


def test_import_failure_is_logged_not_raised(load_package, monkeypatch, caplog):
    # What a ComfyUI that renamed `server` would look like: a None entry makes the import fail.
    monkeypatch.setitem(sys.modules, "server", None)

    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        pkg = load_package("comfyui_model_crud_broken")

    assert pkg.NODE_CLASS_MAPPINGS == {}
    assert "failed to load" in caplog.text


def test_fake_comfy_environment(folder_paths, prompt_server):
    import folder_paths as fp
    from server import PromptServer

    assert fp.get_folder_paths("loras") == folder_paths.get_folder_paths("loras")
    assert ".safetensors" in fp.supported_pt_extensions
    assert PromptServer.instance.routes is prompt_server.routes


def test_version_matches_pyproject():
    from comfyui_model_crud.services.version import VERSION

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1)

    assert VERSION == declared


def test_importing_the_package_registers_the_routes(load_package, prompt_server, folder_paths):
    load_package()

    paths = {r.path for r in prompt_server.routes}
    assert {"/model_crud/config", "/model_crud/folders", "/model_crud/models/{folder}"} <= paths
