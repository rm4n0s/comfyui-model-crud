import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, createClient } from "../../web/api.js";

function fakeApi(handler) {
  const calls = [];
  return {
    calls,
    fetchApi: async (path, init) => {
      calls.push({ path, init });
      return handler(path, init);
    },
  };
}
const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });

test("reads config, folders and files from the right URLs", async () => {
  const api = fakeApi((path) => {
    if (path.endsWith("/config")) return json({ delete_enabled: true });
    if (path.endsWith("/folders")) return json({ folders: [{ key: "loras", roots: 1 }] });
    return json({ folder: "loras", files: [{ path: "a" }] });
  });
  const client = createClient(api);
  assert.deepEqual(await client.getConfig(), { delete_enabled: true });
  assert.deepEqual(await client.listFolders(), [{ key: "loras", roots: 1 }]);
  assert.deepEqual(await client.listModels("loras"), [{ path: "a" }]);
  assert.deepEqual(api.calls.map((c) => c.path), [
    "/model_crud/config", "/model_crud/folders", "/model_crud/models/loras",
  ]);
  assert.ok(api.calls.every((c) => !c.path.startsWith("/api")), "never hardcode the /api prefix");
});

test("folder keys are percent-encoded", async () => {
  const api = fakeApi(() => json({ files: [] }));
  await createClient(api).listModels("we ird/key");
  assert.equal(api.calls[0].path, "/model_crud/models/we%20ird%2Fkey");
});

test("delete puts the path and root index in the query string, encoded", async () => {
  const api = fakeApi(() => json({ deleted: true }));
  await createClient(api).deleteModel("loras", "sdxl v2/Süß&a=b#c?+%.safetensors", 2);
  const { path, init } = api.calls[0];
  assert.equal(init.method, "DELETE");
  assert.equal(
    path,
    "/model_crud/models/loras?path=sdxl%20v2%2FS%C3%BC%C3%9F%26a%3Db%23c%3F%2B%25.safetensors&root_index=2",
  );
  const url = new URL(path, "http://x");
  assert.equal(url.searchParams.get("path"), "sdxl v2/Süß&a=b#c?+%.safetensors");
  assert.equal(url.searchParams.get("root_index"), "2");
});

test("delete defaults to root 0 and always sends the index", async () => {
  const api = fakeApi(() => json({}));
  await createClient(api).deleteModel("loras", "a.safetensors");
  assert.match(api.calls[0].path, /&root_index=0$/);
});

test("server errors surface the server's own message", async () => {
  const client = createClient(fakeApi(() => json({ error: "file_not_found", message: "File not found." }, 404)));
  await assert.rejects(client.deleteModel("loras", "x.safetensors"), (err) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 404);
    assert.equal(err.code, "file_not_found");
    assert.equal(err.message, "File not found.");
    return true;
  });
});

test("a non-JSON error page gives a generic message with the status", async () => {
  const bad = { ok: false, status: 502, json: async () => { throw new SyntaxError("<html>"); } };
  await assert.rejects(createClient(fakeApi(() => bad)).getConfig(), (err) => {
    assert.equal(err.status, 502);
    assert.equal(err.code, "http_error");
    assert.match(err.message, /HTTP 502/);
    return true;
  });
});

test("network failures become a friendly error", async () => {
  const client = createClient({ fetchApi: async () => { throw new TypeError("Failed to fetch"); } });
  await assert.rejects(client.listFolders(), (err) => {
    assert.equal(err.code, "network_error");
    assert.equal(err.status, 0);
    assert.match(err.message, /Cannot reach/);
    return true;
  });
});
