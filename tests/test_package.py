import logging
import sys


def test_package_exports(load_package):
    pkg = load_package()

    assert pkg.NODE_CLASS_MAPPINGS == {}
    assert pkg.WEB_DIRECTORY == "./web"
    assert sorted(pkg.__all__) == ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]


def test_import_failure_is_logged_not_raised(load_package, monkeypatch, caplog):
    name = "comfyui_model_crud_broken"
    # A None entry makes `from . import api` raise ImportError.
    monkeypatch.setitem(sys.modules, f"{name}.api", None)

    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        pkg = load_package(name)

    assert pkg.NODE_CLASS_MAPPINGS == {}
    assert "failed to load" in caplog.text


def test_fake_comfy_environment(folder_paths, prompt_server):
    import folder_paths as fp
    from server import PromptServer

    assert fp.get_folder_paths("loras") == folder_paths.get_folder_paths("loras")
    assert ".safetensors" in fp.supported_pt_extensions
    assert PromptServer.instance.routes is prompt_server.routes
