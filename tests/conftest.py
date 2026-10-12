"""Test doubles for ComfyUI so the suite runs without a ComfyUI install.

ComfyUI is replaced by two fake modules injected through ``sys.modules``:

* ``folder_paths``: folder keys mapped to roots under ``tmp_path``.
* ``server``: ``PromptServer.instance.routes``, an aiohttp ``RouteTableDef``.

The repo root is a package whose ``__init__`` needs both, so tests import our modules through the
alias ``comfyui_model_crud`` (``from comfyui_model_crud.services import paths``). The alias is a
bare package object: it exposes the subpackages without running ``__init__.py``. Use
``load_package`` to execute the real ``__init__.py``.
"""

import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "comfyui_model_crud"

# Mirrors folder_paths.supported_pt_extensions in current ComfyUI.
SUPPORTED_PT_EXTENSIONS = {".ckpt", ".pt", ".pt2", ".bin", ".pth", ".safetensors", ".pkl", ".sft"}

_alias = types.ModuleType(PACKAGE)
_alias.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = _alias


class FakeFolderPaths(types.ModuleType):
    """The subset of ComfyUI's ``folder_paths`` that this extension may rely on."""

    def __init__(self, models_dir: Path) -> None:
        super().__init__("folder_paths")
        self.models_dir = str(models_dir)
        self.supported_pt_extensions = set(SUPPORTED_PT_EXTENSIONS)
        # Same shape as ComfyUI: key -> ([root, ...], {extension, ...})
        self.folder_names_and_paths: dict[str, tuple[list[str], set[str]]] = {}
        self.filename_list_cache: dict[str, object] = {}

    def add_folder(self, key: str, *roots: Path) -> list[Path]:
        """Register ``key`` with ``roots`` (created on disk). Defaults to ``models/<key>``."""
        paths = list(roots) or [Path(self.models_dir) / key]
        for p in paths:
            p.mkdir(parents=True, exist_ok=True)
        self.folder_names_and_paths[key] = (
            [str(p) for p in paths],
            set(self.supported_pt_extensions),
        )
        return paths

    def get_folder_paths(self, folder_name: str) -> list[str]:
        return list(self.folder_names_and_paths[folder_name][0])


class FakePromptServer:
    def __init__(self) -> None:
        self.routes = web.RouteTableDef()


@pytest.fixture
def folder_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeFolderPaths:
    fake = FakeFolderPaths(tmp_path / "models")
    fake.add_folder("checkpoints")
    fake.add_folder("loras")
    fake.add_folder("vae")
    monkeypatch.setitem(sys.modules, "folder_paths", fake)
    return fake


@pytest.fixture
def prompt_server(monkeypatch: pytest.MonkeyPatch) -> FakePromptServer:
    instance = FakePromptServer()
    module = types.ModuleType("server")
    module.PromptServer = type("PromptServer", (), {"instance": instance})
    monkeypatch.setitem(sys.modules, "server", module)
    return instance


@pytest.fixture
def load_package(
    folder_paths: FakeFolderPaths,
    prompt_server: FakePromptServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], types.ModuleType]:
    """Execute the real ``__init__.py`` under a throwaway name, with the fakes in place."""

    def load(name: str = "comfyui_model_crud_loaded") -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    return load
