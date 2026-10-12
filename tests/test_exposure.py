import logging

import pytest
from comfyui_model_crud.services.config import Config
from comfyui_model_crud.services.exposure import warn_if_exposed


def warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize("listen", ["127.0.0.1", "localhost", "::1", "127.0.0.1,127.0.0.2"])
def test_loopback_listen_addresses_do_not_warn(cli_args, caplog, listen):
    cli_args.listen = listen

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        warn_if_exposed(Config())

    assert warnings(caplog) == []


@pytest.mark.parametrize(
    "listen", ["0.0.0.0,::", "192.168.1.5", "::", "127.0.0.1,10.0.0.2", "box.lan"]
)
def test_non_loopback_listen_warns_and_names_the_switch(cli_args, caplog, listen):
    cli_args.listen = listen

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        warn_if_exposed(Config())

    [record] = warnings(caplog)
    assert "MODEL_CRUD_READ_ONLY=1" in record.getMessage()


def test_read_only_or_both_writes_off_never_warns(cli_args, caplog):
    cli_args.listen = "0.0.0.0"
    cli_args.enable_cors_header = "*"

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        warn_if_exposed(Config(read_only=True))
        warn_if_exposed(Config(allow_upload=False, allow_delete=False))

    assert warnings(caplog) == []


def test_one_enabled_write_operation_is_enough_to_warn(cli_args, caplog):
    cli_args.listen = "0.0.0.0"

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        warn_if_exposed(Config(allow_upload=False))

    assert len(warnings(caplog)) == 1


def test_cors_flag_gets_its_own_warning(cli_args, caplog):
    cli_args.enable_cors_header = "*"

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        warn_if_exposed(Config())

    [record] = warnings(caplog)
    assert "--enable-cors-header" in record.getMessage()


def test_unknown_comfyui_internals_are_silent_and_safe(caplog, monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "comfy", raising=False)
    monkeypatch.delitem(sys.modules, "comfy.cli_args", raising=False)
    monkeypatch.setitem(sys.modules, "comfy.cli_args", None)  # import fails

    with caplog.at_level(logging.INFO, logger="comfyui-model-crud"):
        warn_if_exposed(Config())

    assert warnings(caplog) == []


def test_importing_the_package_runs_the_check(load_package, cli_args, caplog):
    cli_args.listen = "0.0.0.0,::"

    with caplog.at_level(logging.WARNING, logger="comfyui-model-crud"):
        load_package("comfyui_model_crud_exposed")

    assert "MODEL_CRUD_READ_ONLY=1" in caplog.text
