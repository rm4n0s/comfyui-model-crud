import dataclasses
import logging

import pytest
from comfyui_model_crud.services import errors
from comfyui_model_crud.services.config import DEFAULT_EXTENSIONS, Config, get_config

GIB = 1024**3
MIB = 1024**2


def env(**values: str) -> dict[str, str]:
    return {f"MODEL_CRUD_{k}": v for k, v in values.items()}


def test_defaults_match_spec():
    cfg = Config.from_env({})

    assert cfg.upload_enabled and cfg.delete_enabled
    assert cfg.max_upload_bytes == 100 * GIB
    assert cfg.chunk_size == 64 * MIB
    assert cfg.max_parallel == 4
    assert cfg.max_active_uploads == 4
    assert cfg.upload_ttl_hours == 24
    assert cfg.allowed_extensions == DEFAULT_EXTENSIONS
    assert ".gguf" in DEFAULT_EXTENSIONS and ".onnx" in DEFAULT_EXTENSIONS


def test_is_immutable():
    cfg = Config.from_env({})

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.read_only = True  # type: ignore[misc]


def test_reads_every_variable():
    cfg = Config.from_env(
        env(
            MAX_UPLOAD_GB="1.5",
            CHUNK_MB="16",
            MAX_PARALLEL="8",
            MAX_ACTIVE_UPLOADS="2",
            UPLOAD_TTL_HOURS="0.5",
            ALLOW_UPLOAD="0",
            ALLOW_DELETE="false",
        )
    )

    assert cfg.max_upload_bytes == int(1.5 * GIB)
    assert cfg.chunk_size == 16 * MIB
    assert cfg.max_parallel == 8
    assert cfg.max_active_uploads == 2
    assert cfg.upload_ttl_hours == 0.5
    assert not cfg.upload_enabled and not cfg.delete_enabled


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_boolean_true_spellings(value):
    assert Config.from_env(env(READ_ONLY=value)).read_only is True


@pytest.mark.parametrize("value", ["0", "false", "No", "off"])
def test_boolean_false_spellings(value):
    assert Config.from_env(env(ALLOW_UPLOAD=value)).allow_upload is False


def test_read_only_overrides_allow_flags():
    cfg = Config.from_env(env(READ_ONLY="1", ALLOW_UPLOAD="1", ALLOW_DELETE="1"))

    assert cfg.allow_upload and cfg.allow_delete  # the raw flags are kept...
    assert not cfg.upload_enabled and not cfg.delete_enabled  # ...but READ_ONLY wins


def test_allow_flags_are_independent():
    cfg = Config.from_env(env(ALLOW_DELETE="0"))

    assert cfg.upload_enabled and not cfg.delete_enabled


@pytest.mark.parametrize("value", ["7", "257", "0", "-8", "abc", "64.5", "1e2"])
def test_chunk_mb_outside_8_to_256_falls_back(value, caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        cfg = Config.from_env(env(CHUNK_MB=value))

    assert cfg.chunk_size == 64 * MIB
    assert "MODEL_CRUD_CHUNK_MB" in caplog.text


@pytest.mark.parametrize("value", ["8", "256"])
def test_chunk_mb_bounds_are_inclusive(value):
    assert Config.from_env(env(CHUNK_MB=value)).chunk_size == int(value) * MIB


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", "-inf"])
def test_max_upload_gb_rejects_non_positive_or_non_finite(value, caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        cfg = Config.from_env(env(MAX_UPLOAD_GB=value))

    assert cfg.max_upload_bytes == 100 * GIB
    assert "MODEL_CRUD_MAX_UPLOAD_GB" in caplog.text


@pytest.mark.parametrize("name", ["MAX_PARALLEL", "MAX_ACTIVE_UPLOADS"])
@pytest.mark.parametrize("value", ["0", "-3", "x", "2.5"])
def test_count_variables_reject_bad_values(name, value, caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        cfg = Config.from_env(env(**{name: value}))

    assert cfg.max_parallel == 4 and cfg.max_active_uploads == 4
    assert f"MODEL_CRUD_{name}" in caplog.text


def test_ttl_rejects_zero(caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        assert Config.from_env(env(UPLOAD_TTL_HOURS="0")).upload_ttl_hours == 24


def test_bad_boolean_falls_back_and_other_variables_still_apply(caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        cfg = Config.from_env(env(READ_ONLY="maybe", MAX_PARALLEL="2"))

    assert cfg.read_only is False
    assert cfg.max_parallel == 2
    assert "MODEL_CRUD_READ_ONLY" in caplog.text


def test_empty_and_blank_values_mean_unset(caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        cfg = Config.from_env(env(CHUNK_MB="", MAX_PARALLEL="   ", READ_ONLY=""))

    assert cfg == Config()
    assert caplog.text == ""


def test_extra_extensions_are_normalised_and_appended():
    cfg = Config.from_env(env(EXTRA_EXTENSIONS=" YAML, .Json ,safetensors,,.yaml"))

    assert cfg.extra_extensions == (".yaml", ".json", ".safetensors")
    # defaults first, extras after, no duplicates
    assert cfg.allowed_extensions == (*DEFAULT_EXTENSIONS, ".yaml", ".json")


@pytest.mark.parametrize("value", [".part", "../x", ".tar/gz", "a b", ".", "..", ".x.y", "é"])
def test_bad_extra_extension_discards_whole_variable(value, caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        cfg = Config.from_env(env(EXTRA_EXTENSIONS=f".yaml,{value}"))

    assert cfg.extra_extensions == ()
    assert "MODEL_CRUD_EXTRA_EXTENSIONS" in caplog.text


def test_log_message_does_not_echo_unbounded_input(caplog):
    with caplog.at_level(logging.ERROR, logger="comfyui-model-crud"):
        Config.from_env(env(CHUNK_MB="9" * 5000 + "x"))

    assert len(caplog.text) < 600


def test_get_config_reads_the_environment_once(monkeypatch):
    get_config.cache_clear()
    monkeypatch.setenv("MODEL_CRUD_MAX_PARALLEL", "3")
    try:
        first = get_config()
        monkeypatch.setenv("MODEL_CRUD_MAX_PARALLEL", "9")

        assert first.max_parallel == 3
        assert get_config() is first
    finally:
        get_config.cache_clear()


def test_error_catalogue_covers_spec_section_6():
    spec_codes = {
        "bad_request": 400,
        "invalid_filename": 400,
        "path_outside_root": 400,
        "bad_chunk": 400,
        "uploads_disabled": 403,
        "delete_disabled": 403,
        "unknown_folder": 404,
        "file_not_found": 404,
        "unknown_upload": 404,
        "file_exists": 409,
        "upload_in_progress": 409,
        "incomplete_upload": 409,
        "too_large": 413,
        "extension_not_allowed": 415,
        "checksum_mismatch": 422,
        "too_many_uploads": 429,
        "internal_error": 500,
        "insufficient_storage": 507,
    }
    classes = [
        c
        for c in vars(errors).values()
        if isinstance(c, type) and issubclass(c, errors.ModelCrudError)
    ]

    assert {c.code: c.status for c in classes} == spec_codes


def test_error_body_shape_and_details():
    exc = errors.IncompleteUpload(missing_ranges=[[1, 2]])

    assert exc.to_body() == {
        "error": "incomplete_upload",
        "message": errors.IncompleteUpload.default_message,
        "missing_ranges": [[1, 2]],
    }
    assert errors.BadRequest("nope").to_body() == {"error": "bad_request", "message": "nope"}
