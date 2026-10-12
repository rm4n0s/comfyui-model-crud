import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../../web/api.js";
import { createController } from "../../web/controller.js";

const file = (path, root_index = 0, size = 10) => ({
  path, name: path.split("/").pop(), size, modified: "2026-01-01T00:00:00Z", root_index,
});

function setup({ folders = ["checkpoints", "loras"], files = {}, config = { delete_enabled: true }, confirmAnswer = true, storage } = {}) {
  const log = { delete: [], toast: [], confirm: [], list: [] };
  const fs = structuredClone(files);
  const client = {
    getConfig: async () => config,
    listFolders: async () => folders.map((key) => ({ key, roots: 1 })),
    listModels: async (folder) => { log.list.push(folder); return structuredClone(fs[folder] ?? []); },
    deleteModel: async (folder, path, rootIndex) => {
      log.delete.push([folder, path, rootIndex]);
      if (client.failDelete) throw client.failDelete;
      fs[folder] = fs[folder].filter((f) => !(f.path === path && f.root_index === rootIndex));
      return { deleted: true };
    },
  };
  const controller = createController({
    client,
    confirm: async (options) => { log.confirm.push(options); return confirmAnswer; },
    toast: (message) => log.toast.push(message),
    storage,
  });
  return { controller, client, log, fs };
}

const memoryStorage = (initial = {}) => {
  const data = { ...initial };
  return { getItem: (k) => data[k] ?? null, setItem: (k, v) => { data[k] = v; }, data };
};

test("init loads config, folders and the first folder's files", async () => {
  const { controller } = setup({ files: { checkpoints: [file("a.safetensors")] } });
  await controller.init();
  const s = controller.store.get();
  assert.equal(s.folder, "checkpoints");
  assert.equal(s.loading, false);
  assert.deepEqual(s.files.map((f) => f.path), ["a.safetensors"]);
  assert.equal(s.config.delete_enabled, true);
});

test("init prefers the remembered folder, then checkpoints, then the first", async () => {
  const remembered = setup({ storage: memoryStorage({ "ModelCrud.folder": "loras" }) });
  await remembered.controller.init();
  assert.equal(remembered.controller.store.get().folder, "loras");

  const stale = setup({ storage: memoryStorage({ "ModelCrud.folder": "gone" }) });
  await stale.controller.init();
  assert.equal(stale.controller.store.get().folder, "checkpoints");

  const first = setup({ folders: ["vae", "loras"] });
  await first.controller.init();
  assert.equal(first.controller.store.get().folder, "vae");
});

test("init copes with no folders and with blocked storage", async () => {
  const none = setup({ folders: [] });
  await none.controller.init();
  assert.equal(none.controller.store.get().folder, null);
  assert.equal(none.controller.store.get().loading, false);

  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  const s = setup({ storage: blocked });
  await s.controller.init();
  await s.controller.selectFolder("loras");
  assert.equal(s.controller.store.get().folder, "loras");
});

test("init failure is reported, not thrown", async () => {
  const { controller, client } = setup();
  client.getConfig = async () => { throw new ApiError(0, "network_error", "Cannot reach the ComfyUI server."); };
  await controller.init();
  const s = controller.store.get();
  assert.equal(s.error, "Cannot reach the ComfyUI server.");
  assert.equal(s.loading, false);
});

test("selecting a folder loads it, remembers it and clears the filter", async () => {
  const storage = memoryStorage();
  const { controller, log } = setup({ storage, files: { loras: [file("x.safetensors")] } });
  await controller.init();
  controller.setFilter("abc");
  await controller.selectFolder("loras");
  assert.equal(storage.data["ModelCrud.folder"], "loras");
  assert.equal(controller.store.get().filter, "");
  assert.deepEqual(controller.store.get().files.map((f) => f.path), ["x.safetensors"]);
  await controller.selectFolder("loras");
  assert.deepEqual(log.list, ["checkpoints", "loras"]); // same folder: no reload
});

test("a slow answer for a folder we left does not overwrite the current one", async () => {
  const { controller, client } = setup();
  await controller.init();
  const slow = [];
  client.listModels = (folder) => new Promise((resolve) => slow.push({ folder, resolve }));
  const first = controller.selectFolder("loras");
  const second = controller.selectFolder("checkpoints");
  slow.find((r) => r.folder === "checkpoints").resolve([file("current.safetensors")]);
  await second;
  slow.find((r) => r.folder === "loras").resolve([file("stale.safetensors")]);
  await first;
  assert.deepEqual(controller.store.get().files.map((f) => f.path), ["current.safetensors"]);
});

test("a failed listing shows the server's message and an empty table", async () => {
  const { controller, client } = setup();
  await controller.init();
  client.listModels = async () => { throw new ApiError(404, "unknown_folder", "Unknown model folder."); };
  await controller.refresh();
  assert.equal(controller.store.get().error, "Unknown model folder.");
  assert.deepEqual(controller.store.get().files, []);
});

test("sorting toggles direction on the same column and resets on a new one", async () => {
  const { controller } = setup();
  await controller.init();
  controller.setSort("size");
  assert.deepEqual([controller.store.get().sortKey, controller.store.get().sortDir], ["size", "asc"]);
  controller.setSort("size");
  assert.equal(controller.store.get().sortDir, "desc");
  controller.setSort("name");
  assert.deepEqual([controller.store.get().sortKey, controller.store.get().sortDir], ["name", "asc"]);
});

test("paging: showMore grows the limit and filtering resets it", async () => {
  const many = Array.from({ length: 1000 }, (_, i) => file(`m${i}.safetensors`));
  const { controller } = setup({ files: { checkpoints: many } });
  await controller.init();
  assert.equal(controller.store.get().limit, 300);
  controller.showMore();
  assert.equal(controller.store.get().limit, 600);
  controller.setFilter("m1");
  assert.equal(controller.store.get().limit, 300);
});

test("delete asks first, naming the file and the folder, and does nothing if declined", async () => {
  const { controller, log } = setup({ files: { checkpoints: [file("sdxl/m.safetensors")] }, confirmAnswer: false });
  await controller.init();
  await controller.requestDelete(controller.store.get().files[0]);
  assert.equal(log.confirm.length, 1);
  assert.match(log.confirm[0].message, /sdxl\/m\.safetensors/);
  assert.match(log.confirm[0].message, /checkpoints/);
  assert.equal(log.confirm[0].type, "delete");
  assert.deepEqual(log.delete, []);
  assert.deepEqual(log.toast, []);
  assert.equal(controller.store.get().files.length, 1);
});

test("a confirmed delete calls the API with the exact root, toasts and refreshes", async () => {
  const files = { checkpoints: [file("a.safetensors", 0), file("a.safetensors", 1), file("b.safetensors")] };
  const { controller, log } = setup({ files });
  await controller.init();
  const second = controller.store.get().files.find((f) => f.path === "a.safetensors" && f.root_index === 1);
  await controller.requestDelete(second);
  assert.deepEqual(log.delete, [["checkpoints", "a.safetensors", 1]]);
  assert.equal(log.toast[0].severity, "success");
  assert.equal(log.toast[0].detail, "a.safetensors");
  assert.deepEqual(
    controller.store.get().files.map((f) => [f.path, f.root_index]),
    [["a.safetensors", 0], ["b.safetensors", 0]],
  );
  assert.equal(controller.store.get().deleting.size, 0);
  assert.equal(log.list.length, 2); // initial load plus the refresh
});

test("a failed delete toasts the server's message and refreshes the list", async () => {
  const { controller, client, log } = setup({ files: { checkpoints: [file("a.safetensors")] } });
  await controller.init();
  client.failDelete = new ApiError(404, "file_not_found", "File not found.");
  await controller.requestDelete(controller.store.get().files[0]);
  assert.equal(log.toast[0].severity, "error");
  assert.equal(log.toast[0].detail, "File not found.");
  assert.equal(controller.store.get().deleting.size, 0);
  assert.equal(log.list.length, 2);
});

test("delete is refused when the server has it disabled", async () => {
  const { controller, log } = setup({ config: { delete_enabled: false }, files: { checkpoints: [file("a.safetensors")] } });
  await controller.init();
  await controller.requestDelete(controller.store.get().files[0]);
  assert.deepEqual(log.confirm, []);
  assert.deepEqual(log.delete, []);
});

test("a second click while a delete is running is ignored", async () => {
  const { controller, client, log } = setup({ files: { checkpoints: [file("a.safetensors")] } });
  await controller.init();
  const target = controller.store.get().files[0];
  let release;
  client.deleteModel = (...args) => new Promise((resolve) => { log.delete.push(args); release = resolve; });
  const first = controller.requestDelete(target);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(controller.store.get().deleting.size, 1);
  await controller.requestDelete(target);
  release({});
  await first;
  assert.equal(log.delete.length, 1);
  assert.equal(log.confirm.length, 1);
});

test("hostile file names are only ever passed along as data", async () => {
  const evil = `<img src=x onerror=alert(1)>"'.safetensors`;
  const { controller, log } = setup({ files: { checkpoints: [file(evil)] } });
  await controller.init();
  await controller.requestDelete(controller.store.get().files[0]);
  assert.deepEqual(log.delete[0].slice(1, 2), [evil]); // unmodified: encoding is the client's job
  assert.ok(log.confirm[0].message.includes(evil)); // plain text for a dialog that renders text
});

test("after destroy, late answers are ignored", async () => {
  const { controller, client } = setup();
  let resolve;
  client.listModels = () => new Promise((r) => { resolve = r; });
  const loading = controller.init();
  await new Promise((r) => setTimeout(r, 0));
  controller.destroy();
  resolve([file("late.safetensors")]);
  await loading;
  assert.deepEqual(controller.store.get().files, []);
});
