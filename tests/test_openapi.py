"""``openapi.yaml`` must describe the API the code actually serves.

Three checks: the file is a valid OpenAPI 3.1 document; its (method, path) pairs are exactly the
routes the extension registers; and a scenario that calls every operation checks that each status
code seen is documented for that operation and that each JSON body matches the documented schema.
"""

import hashlib
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from comfyui_model_crud.api import routes as api_routes
from comfyui_model_crud.services import uploads
from comfyui_model_crud.services.config import Config
from comfyui_model_crud.services.uploads import UploadManager
from uploadkit import make_config, payload

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"
METHODS = ("get", "post", "put", "delete")
CHUNK = 1024


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text())


def test_the_document_is_valid_openapi(spec):
    validator = pytest.importorskip("openapi_spec_validator")

    validator.validate(spec)
    assert spec["openapi"].startswith("3.1")


def test_every_operation_has_an_id_tags_and_a_success_response(spec):
    ids = []
    for path, item in spec["paths"].items():
        for method in METHODS:
            if method not in item:
                continue
            op = item[method]
            ids.append(op["operationId"])
            assert op["tags"] and op["summary"], (method, path)
            assert any(code.startswith("2") for code in op["responses"]), (method, path)
            assert "500" in op["responses"], (method, path)
    assert len(ids) == len(set(ids))


def test_spec_and_registered_routes_are_the_same_set(spec, prompt_server):
    api_routes.register_routes(prompt_server.routes)
    registered = {
        (route.method.lower(), route.path.replace("{area:input|output}", "{area}"))
        for route in prompt_server.routes
    }
    documented = {(m, p) for p, item in spec["paths"].items() for m in METHODS if m in item}

    assert registered == documented


def test_error_codes_in_the_schema_are_the_codes_the_code_defines(spec):
    from comfyui_model_crud.services import errors

    defined = {
        cls.code
        for cls in vars(errors).values()
        if isinstance(cls, type) and issubclass(cls, errors.ModelCrudError)
    }
    assert set(spec["components"]["schemas"]["Error"]["properties"]["error"]["enum"]) == defined


# --- scenario -------------------------------------------------------------------------------


class Contract:
    """Wraps a test client: every call is checked against the operation it claims to be."""

    def __init__(self, spec: dict, client: TestClient) -> None:
        self.spec, self.client = spec, client
        self.seen: set[tuple[str, str, str]] = set()

    def _resolve(self, node: dict) -> dict:
        while "$ref" in node:
            target = self.spec
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            node = target
        return node

    async def call(self, method: str, template: str, url: str, expect: int, **kwargs):
        resp = await getattr(self.client, method)(url, **kwargs)
        text = await resp.text()
        assert resp.status == expect, f"{method} {url}: {resp.status} {text}"
        operation = self.spec["paths"][template][method]
        documented = operation["responses"]
        assert str(resp.status) in documented, f"{method} {template}: {resp.status} undocumented"
        content = self._resolve(documented[str(resp.status)]).get("content", {})
        assert resp.headers.get("Cache-Control") == "no-store"
        schema = content.get("application/json", {}).get("schema")
        body = await resp.json() if schema else None
        if schema:
            wrapper = {**schema, "components": self.spec["components"]}
            jsonschema.Draft202012Validator(wrapper).validate(body)
        self.seen.add((method, template, str(resp.status)))
        return body


@pytest.fixture
async def make_client(folder_paths, prompt_server, comfy_app, monkeypatch):
    clients = []

    async def make(config: Config) -> TestClient:
        monkeypatch.setattr(api_routes, "get_config", lambda: config)
        uploads.set_manager(UploadManager(config))
        table = type(prompt_server.routes)()
        api_routes.register_routes(table)
        client = TestClient(TestServer(comfy_app(table)))
        await client.start_server()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()
    uploads.set_manager(None)


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def upload_flow(api: Contract, template: str, base: str, create_body: dict, other: str):
    """create -> status -> list -> incomplete -> chunks -> complete, then a cancelled session."""
    data = payload(2 * CHUNK + 5)
    session = await api.call(
        "post", f"{template}", base, 201, json={**create_body, "size": len(data)}
    )
    upload = f"{base}/{session['upload_id']}"
    item = f"{template}/{{upload_id}}"
    await api.call("get", item, upload, 200)
    await api.call("get", template, base, 200)
    body = await api.call("post", f"{item}/complete", f"{upload}/complete", 409, json={})
    assert body["error"] == "incomplete_upload" and body["missing_ranges"] == [[0, 2]]
    chunk = f"{item}/chunks/{{index}}"
    await api.call("put", chunk, f"{upload}/chunks/0", 400, data=b"short")
    await api.call("put", chunk, f"{upload}/chunks/9", 400, data=data[:CHUNK])
    bad_hash = {"Upload-Chunk-SHA256": "0" * 64}
    await api.call("put", chunk, f"{upload}/chunks/0", 422, data=data[:CHUNK], headers=bad_hash)
    for i in range(3):
        piece = data[i * CHUNK : (i + 1) * CHUNK]
        good = {"Upload-Chunk-SHA256": hashlib.sha256(piece).hexdigest()}
        await api.call("put", chunk, f"{upload}/chunks/{i}", 200, data=piece, headers=good)
    status = await api.call("get", item, upload, 200)
    assert status["received_ranges"] == [[0, 2]] and status["received_bytes"] == len(data)
    await api.call("post", f"{item}/complete", f"{upload}/complete", 400, json={"sha256": "zz"})
    wrong = {"sha256": "0" * 64}
    await api.call("post", f"{item}/complete", f"{upload}/complete", 422, json=wrong)
    done = {"sha256": hashlib.sha256(data).hexdigest()}
    result = await api.call("post", f"{item}/complete", f"{upload}/complete", 201, json=done)
    await api.call("get", item, upload, 404)  # a finished session is gone

    cancelled = await api.call(
        "post", template, base, 201, json={**create_body, "filename": other, "size": 10}
    )
    gone = f"{base}/{cancelled['upload_id']}"
    await api.call("delete", item, gone, 200)
    await api.call("delete", item, gone, 404)
    return result


async def test_every_operation_behaves_as_documented(spec, make_client, folder_paths):
    root_loras = Path(folder_paths.get_folder_paths("loras")[0])
    output = Path(folder_paths.get_output_directory())
    touch(root_loras / "sdxl" / "detail.safetensors", b"abc")
    touch(output / "2026" / "a.png", b"abc")
    config = make_config()
    api = Contract(spec, await make_client(config))

    # server
    cfg = await api.call("get", "/model_crud/config", "/model_crud/config", 200)
    assert cfg["chunk_size"] == CHUNK and cfg["media_folders"] == ["input", "output"]
    folders = await api.call("get", "/model_crud/folders", "/model_crud/folders", 200)
    assert "loras" in [f["key"] for f in folders["folders"]]

    # model files
    listing = await api.call("get", "/model_crud/models/{folder}", "/model_crud/models/loras", 200)
    assert [f["path"] for f in listing["files"]] == ["sdxl/detail.safetensors"]
    await api.call("get", "/model_crud/models/{folder}", "/model_crud/models/nope", 404)
    delete = "/model_crud/models/{folder}"
    await api.call("delete", delete, "/model_crud/models/loras?path=../x.safetensors", 400)
    await api.call("delete", delete, "/model_crud/models/nope?path=a.safetensors", 404)
    await api.call("delete", delete, "/model_crud/models/loras?path=missing.safetensors", 404)
    await api.call("delete", delete, "/model_crud/models/loras?path=notes.txt", 415)
    url = "/model_crud/models/loras?path=sdxl%2Fdetail.safetensors&root_index=0"
    await api.call("delete", delete, url, 200)

    # input and output
    media = await api.call("get", "/model_crud/{area}", "/model_crud/output", 200)
    assert [f["path"] for f in media["files"]] == ["2026/a.png"] and media["area"] == "output"
    delete = "/model_crud/{area}"
    await api.call("delete", delete, "/model_crud/output?path=%2e%2e%2fx.png", 400)
    await api.call("delete", delete, "/model_crud/output?path=missing.png", 404)
    await api.call("delete", delete, "/model_crud/output?path=model.safetensors", 415)
    result = await api.call("delete", delete, "/model_crud/output?path=2026%2Fa.png", 200)
    assert result == {"area": "output", "path": "2026/a.png", "deleted": True}

    # model uploads
    template = "/model_crud/uploads"
    base = {"folder": "loras", "filename": "m.safetensors", "subfolder": "new"}
    result = await upload_flow(api, template, template, base, "other.safetensors")
    assert result == {"folder": "loras", "path": "new/m.safetensors", "size": 2 * CHUNK + 5}
    await api.call("post", template, template, 409, json={**base, "size": 10})
    await api.call("post", template, template, 404, json={**base, "folder": "nope", "size": 10})
    await api.call("post", template, template, 415, json={**base, "filename": "a.png", "size": 10})
    await api.call("post", template, template, 413, json={**base, "size": 10**15})
    await api.call("post", template, template, 400, json={**base, "size": 0})
    await api.call("get", "/model_crud/uploads/{upload_id}", "/model_crud/uploads/" + "A" * 22, 404)

    # media uploads
    template = "/model_crud/{area}/uploads"
    base_url = "/model_crud/output/uploads"
    base = {"filename": "r.png", "subfolder": "gen"}
    result = await upload_flow(api, template, base_url, base, "other.png")
    assert result == {"area": "output", "path": "gen/r.png", "size": 2 * CHUNK + 5}
    assert (output / "gen" / "r.png").is_file()
    await api.call("post", template, base_url, 409, json={**base, "size": 10})
    await api.call("post", template, base_url, 415, json={"filename": "m.safetensors", "size": 10})
    # busy target: two sessions for one file
    first = await api.call("post", template, base_url, 201, json={"filename": "b.png", "size": 10})
    await api.call("post", template, base_url, 409, json={"filename": "b.png", "size": 10})
    await api.call("delete", f"{template}/{{upload_id}}", f"{base_url}/{first['upload_id']}", 200)

    # every operation was seen succeeding at least once
    ok = {(m, p) for m, p, status in api.seen if status.startswith("2")}
    every = {(m, p) for p, item in spec["paths"].items() for m in METHODS if m in item}
    assert every - ok == set()


async def test_disabled_features_are_documented_403_and_404(spec, make_client, folder_paths):
    output = Path(folder_paths.get_output_directory())
    touch(output / "a.png")

    read_only = Contract(spec, await make_client(make_config(read_only=True)))
    cfg = await read_only.call("get", "/model_crud/config", "/model_crud/config", 200)
    assert cfg["upload_enabled"] is False and cfg["delete_enabled"] is False
    await read_only.call("delete", "/model_crud/{area}", "/model_crud/output?path=a.png", 403)
    await read_only.call(
        "delete", "/model_crud/models/{folder}", "/model_crud/models/loras?path=a.pt", 403
    )
    body = {"filename": "b.png", "size": 5}
    await read_only.call(
        "post", "/model_crud/{area}/uploads", "/model_crud/output/uploads", 403, json=body
    )
    await read_only.call("get", "/model_crud/uploads", "/model_crud/uploads", 403)

    no_output = Contract(spec, await make_client(make_config(media_folders=("input",))))
    await no_output.call("get", "/model_crud/{area}", "/model_crud/output", 404)
    await no_output.call("delete", "/model_crud/{area}", "/model_crud/output?path=a.png", 404)
    await no_output.call(
        "post", "/model_crud/{area}/uploads", "/model_crud/output/uploads", 404, json=body
    )
    await no_output.call("get", "/model_crud/{area}/uploads", "/model_crud/output/uploads", 404)
    assert (output / "a.png").exists()


async def test_a_non_area_name_is_not_a_route(make_client):
    client = await make_client(make_config())

    for name in ("temp", "user", "inputs"):
        assert (await client.get(f"/model_crud/{name}")).status == 404


def test_the_error_examples_are_valid_errors(spec):
    errors = spec["components"]["schemas"]["Error"]
    for name, response in spec["components"]["responses"].items():
        example = response["content"]["application/json"].get("example")
        assert example is not None, name
        wrapper = {**errors, "components": spec["components"]}
        if "missing_ranges" in example:
            wrapper = {
                "$ref": "#/components/schemas/IncompleteUploadError",
                "components": spec["components"],
            }
        jsonschema.Draft202012Validator(wrapper).validate(example)
