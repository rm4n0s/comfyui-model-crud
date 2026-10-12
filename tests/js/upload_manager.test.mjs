import assert from "node:assert/strict";
import { test } from "node:test";
import { createUploadManager, describeItem } from "../../web/upload_manager.js";
import { fingerprintOf } from "../../web/uploader.js";

const CONFIG = {
  upload_enabled: true, delete_enabled: true, max_parallel: 4, max_upload_bytes: 1000,
  allowed_extensions: [".safetensors", ".ckpt", ".gguf"],
};
const file = (name = "m.safetensors", size = 100, lastModified = 1700000000000) =>
  new File([new Uint8Array(size)], name, { lastModified });

class FakeJob {
  static instances = [];
  constructor(opts) { this.opts = opts; this.state = "idle"; this.sent = 0; FakeJob.instances.push(this); }
  snapshot() {
    return {
      state: this.state, name: this.opts.file.name, total: this.opts.file.size, sent: this.sent, speed: 0, eta: null,
      uploadId: this.uploadId ?? null, path: this.path ?? null, error: this.error ?? null,
      errorCode: this.errorCode ?? null, resumable: this.resumable ?? false,
    };
  }
  setState(state) { this.state = state; this.opts.onChange(this.snapshot()); }
  start(args) { this.startArgs = args; this.setState("uploading"); return new Promise((r) => { this.end = r; }); }
  finish(state, extra = {}) { Object.assign(this, extra); this.setState(state); this.end(); }
  async pause() { this.setState("paused"); this.end(); }
  resume() { this.setState("uploading"); return new Promise((r) => { this.end = r; }); }
  async cancel() { this.setState("cancelled"); this.end?.(); }
  retry() { this.retried = true; this.setState("uploading"); return new Promise((r) => { this.end = r; }); }
}

function setup(config = CONFIG, extra = {}) {
  FakeJob.instances = [];
  const log = { uploaded: [], toasts: [], listed: 0, cancelled: [] };
  const state = { sessions: [], folder: "loras", options: { parallel: 3, chunkHash: false }, ...extra };
  const client = {
    listUploads: async () => { log.listed += 1; if (state.listError) throw new Error(state.listError); return structuredClone(state.sessions); },
    getUpload: async (id) => state.getUpload?.(id) ?? { upload_id: id, folder: "vae", path: "x.safetensors", size: 100, chunk_size: 10, total_chunks: 10, received_ranges: [[0, 2]] },
    cancelUpload: async (id) => { log.cancelled.push(id); if (state.cancelError) throw state.cancelError; return {}; },
  };
  const manager = createUploadManager({
    client, env: {}, JobClass: FakeJob,
    getFolder: () => state.folder, getOptions: () => state.options,
    onUploaded: (f) => log.uploaded.push(f), notify: (m) => log.toasts.push(m),
  });
  manager.setConfig(config);
  return { manager, log, state, jobs: FakeJob.instances };
}
const flush = () => new Promise((r) => setTimeout(r, 0));
const statuses = (m) => m.store.get().items.map((i) => i.status);

test("nothing happens while uploads are disabled or no folder is selected", async () => {
  const off = setup({ ...CONFIG, upload_enabled: false });
  assert.deepEqual(off.manager.enqueue([file()]), []);
  assert.equal(off.jobs.length, 0);
  assert.equal(off.log.listed, 0, "no session listing when uploads are disabled");
  const none = setup(CONFIG, { folder: null });
  assert.deepEqual(none.manager.enqueue([file()]), []);
});

test("files are validated before any request", async () => {
  const { manager, jobs } = setup();
  manager.enqueue([file("empty.safetensors", 0), file("virus.exe"), file("noext"), file("big.safetensors", 1001), file("UPPER.SAFETENSORS", 10)]);
  const items = manager.store.get().items;
  assert.deepEqual(items.map((i) => i.status), ["failed", "failed", "failed", "failed", "uploading"]);
  assert.match(items[0].message, /empty/);
  assert.match(items[1].message, /"\.exe" files are not allowed.*\.safetensors/);
  assert.match(items[2].message, /\(no extension\)/);
  assert.match(items[3].message, /accepts at most 1000 B/);
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].opts.file.name, "UPPER.SAFETENSORS");
});

test("files run one at a time, in order", async () => {
  const { manager, jobs } = setup();
  manager.enqueue([file("a.safetensors"), file("b.safetensors"), file("c.safetensors")]);
  assert.deepEqual(statuses(manager), ["uploading", "queued", "queued"]);
  assert.equal(jobs.length, 1);
  jobs[0].finish("done", { path: "a.safetensors" });
  await flush();
  assert.deepEqual(statuses(manager), ["done", "uploading", "queued"]);
  jobs[1].finish("done");
  await flush();
  jobs[2].finish("done");
  await flush();
  assert.deepEqual(statuses(manager), ["done", "done", "done"]);
  assert.deepEqual(jobs.map((j) => j.opts.file.name), ["a.safetensors", "b.safetensors", "c.safetensors"]);
});

test("each job gets the folder, subfolder, overwrite flag and capped options", async () => {
  const { manager, jobs } = setup(CONFIG, { options: { parallel: 6, chunkHash: true } });
  manager.enqueue([file()], { subfolder: "sdxl", overwrite: true });
  const { opts } = jobs[0];
  assert.deepEqual([opts.folder, opts.subfolder, opts.overwrite], ["loras", "sdxl", true]);
  assert.deepEqual(opts.options, { parallel: 6, chunkHash: true, maxParallel: 4 });
});

test("a finished upload refreshes the list, toasts, and starts the next file", async () => {
  const { manager, jobs, log } = setup();
  manager.enqueue([file("a.safetensors"), file("b.safetensors")]);
  jobs[0].finish("done", { path: "sdxl/a.safetensors" });
  await flush();
  assert.deepEqual(log.uploaded, ["loras"]);
  assert.equal(log.toasts[0].severity, "success");
  assert.equal(log.toasts[0].detail, "sdxl/a.safetensors");
  assert.equal(jobs.length, 2);
});

test("a failed upload shows the server message, toasts, and the queue continues", async () => {
  const { manager, jobs, log } = setup();
  manager.enqueue([file("a.safetensors"), file("b.safetensors")]);
  jobs[0].finish("failed", { error: "No space left.", errorCode: "insufficient_storage" });
  await flush();
  const [first] = manager.store.get().items;
  assert.equal(first.status, "failed");
  assert.equal(describeItem(first).text, "No space left.");
  assert.equal(log.toasts[0].severity, "error");
  assert.match(log.toasts[0].summary, /a\.safetensors/);
  assert.equal(jobs.length, 2);
  assert.deepEqual(log.uploaded, []);
});

test("pausing holds the queue; resuming continues and then starts the next file", async () => {
  const { manager, jobs } = setup();
  const [first] = manager.enqueue([file("a.safetensors"), file("b.safetensors")]);
  await manager.pause(first);
  await flush();
  assert.deepEqual(statuses(manager), ["paused", "queued"]);
  assert.equal(jobs.length, 1, "the second file waits while the first is paused");
  const resumed = manager.resume(first);
  await flush();
  assert.equal(statuses(manager)[0], "uploading");
  jobs[0].finish("done");
  await resumed;
  await flush();
  assert.deepEqual(statuses(manager), ["done", "uploading"]);
});

test("cancelling the active file starts the next; cancelling a queued file never starts it", async () => {
  const { manager, jobs } = setup();
  const [a, b, c] = manager.enqueue([file("a.safetensors"), file("b.safetensors"), file("c.safetensors")]);
  await manager.cancel(b);
  assert.deepEqual(statuses(manager), ["uploading", "cancelled", "queued"]);
  await manager.cancel(a);
  await flush();
  assert.deepEqual(statuses(manager), ["cancelled", "cancelled", "uploading"]);
  assert.deepEqual(jobs.map((j) => j.opts.file.name), ["a.safetensors", "c.safetensors"]);
  await manager.cancel(c);
});

test("dismiss only removes finished items", async () => {
  const { manager, jobs } = setup();
  const [a, b] = manager.enqueue([file("a.safetensors"), file("b.safetensors")]);
  manager.dismiss(a); manager.dismiss(b);
  assert.equal(manager.store.get().items.length, 2);
  jobs[0].finish("done"); await flush();
  manager.dismiss(a);
  assert.deepEqual(manager.store.get().items.map((i) => i.id), [b]);
});

test("retry with overwrite is offered only for file_exists, and keeps the subfolder", async () => {
  const { manager, jobs } = setup();
  const [a] = manager.enqueue([file("a.safetensors")], { subfolder: "sdxl" });
  jobs[0].finish("failed", { error: "A file with this name already exists.", errorCode: "file_exists" });
  await flush();
  const replacement = manager.retryWithOverwrite(a);
  assert.ok(replacement);
  assert.equal(manager.store.get().items.some((i) => i.id === a), false, "the old row is replaced");
  const job = jobs[1];
  assert.deepEqual([job.opts.overwrite, job.opts.subfolder, job.opts.file.name], [true, "sdxl", "a.safetensors"]);

  const other = setup();
  const [x] = other.manager.enqueue([file()]);
  other.jobs[0].finish("failed", { error: "boom", errorCode: "insufficient_storage" });
  await flush();
  assert.equal(other.manager.retryWithOverwrite(x), null);
});

test("retry resumes a failed upload through the same job", async () => {
  const { manager, jobs } = setup();
  const [a] = manager.enqueue([file()]);
  jobs[0].finish("failed", { error: "x", errorCode: "http_error", uploadId: "U".repeat(22), resumable: true });
  await flush();
  const retried = manager.retry(a);
  await flush();
  assert.equal(jobs[0].retried, true);
  jobs[0].finish("done");
  await retried;
  assert.equal(statuses(manager)[0], "done");
});

test("hasActive covers queued, running and paused items but not finished ones", async () => {
  const { manager, jobs } = setup();
  assert.equal(manager.hasActive(), false);
  const [a] = manager.enqueue([file()]);
  assert.equal(manager.hasActive(), true);
  await manager.pause(a);
  assert.equal(manager.hasActive(), true);
  const r = manager.resume(a); await flush();
  jobs[0].finish("done"); await r; await flush();
  assert.equal(manager.hasActive(), false);
});

// --- unfinished sessions on the server ---------------------------------------------------------

const session = (over = {}) => ({
  upload_id: "S".repeat(22), folder: "loras", path: "sdxl/big.safetensors", size: 100, received_bytes: 30,
  client_fingerprint: null, expires_at: "2026-10-13T00:00:00Z", ...over,
});

test("unfinished sessions are listed once uploads are known to be enabled", async () => {
  const { manager, log } = setup(CONFIG, { sessions: [session()] });
  await flush();
  assert.equal(log.listed, 1);
  assert.equal(manager.unfinishedSessions().length, 1);
});

test("a session listing error is kept, not thrown", async () => {
  const { manager } = setup(CONFIG, { listError: "Cannot reach the ComfyUI server." });
  await flush();
  assert.equal(manager.store.get().sessionsError, "Cannot reach the ComfyUI server.");
});

test("sessions this tab is uploading are not listed as unfinished", async () => {
  const { manager, jobs, state } = setup(CONFIG, { sessions: [session({ upload_id: "A".repeat(22) })] });
  await flush();
  manager.enqueue([file()]);
  jobs[0].uploadId = "A".repeat(22);
  jobs[0].setState("uploading");
  assert.deepEqual(manager.unfinishedSessions(), []);
  jobs[0].finish("failed", { error: "x", errorCode: "http_error", resumable: true });
  await flush();
  assert.equal(manager.unfinishedSessions().length, 1, "after a failure the partial upload can be resumed or discarded");
});

test("resuming a session needs the same file (fingerprint)", async () => {
  const f = file("big.safetensors", 100, 1700000000000);
  const { manager, jobs } = setup(CONFIG, { sessions: [session({ client_fingerprint: fingerprintOf(f) })] });
  await flush();
  const wrong = await manager.resumeSession("S".repeat(22), file("big.safetensors", 100, 1));
  assert.equal(wrong.ok, false);
  assert.match(wrong.message, /not the file this upload was started with/);
  assert.equal(jobs.length, 0);
  const right = await manager.resumeSession("S".repeat(22), f);
  assert.equal(right.ok, true);
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].startArgs.session.upload_id, "S".repeat(22));
  assert.equal(jobs[0].opts.folder, "vae"); // the session's own folder, not the one selected now
});

test("without a fingerprint, name and size must match", async () => {
  const { manager } = setup(CONFIG, { sessions: [session()] });
  await flush();
  assert.equal((await manager.resumeSession("S".repeat(22), file("other.safetensors", 100))).ok, false);
  assert.equal((await manager.resumeSession("S".repeat(22), file("big.safetensors", 99))).ok, false);
  assert.equal((await manager.resumeSession("S".repeat(22), file("big.safetensors", 100))).ok, true);
});

test("resuming a session that vanished or cannot be read reports it", async () => {
  const { manager, state } = setup(CONFIG, { sessions: [session()] });
  await flush();
  assert.match((await manager.resumeSession("Z".repeat(22), file())).message, /no longer exists/);
  state.getUpload = () => { throw new Error("Unknown upload."); };
  const failed = await manager.resumeSession("S".repeat(22), file("big.safetensors", 100));
  assert.deepEqual([failed.ok, failed.message], [false, "Unknown upload."]);
});

test("discarding a session cancels it on the server and refreshes; a 404 is fine", async () => {
  const { manager, log, state } = setup(CONFIG, { sessions: [session()] });
  await flush();
  const before = log.listed;
  state.sessions = [];
  await manager.discardSession("S".repeat(22));
  assert.deepEqual(log.cancelled, ["S".repeat(22)]);
  assert.equal(log.listed, before + 1);
  assert.deepEqual(manager.unfinishedSessions(), []);
  state.cancelError = Object.assign(new Error("gone"), { status: 404 });
  await manager.discardSession("S".repeat(22));
  assert.equal(log.toasts.length, 0);
  state.cancelError = Object.assign(new Error("denied"), { status: 403 });
  await manager.discardSession("S".repeat(22));
  assert.equal(log.toasts[0].severity, "error");
});

// --- describeItem ------------------------------------------------------------------------------

test("describeItem gives a readable line and a fraction for every status", () => {
  const base = { file: { size: 1000 }, snapshot: null };
  assert.equal(describeItem({ ...base, status: "queued" }).text, "Waiting in the queue");
  const up = describeItem({ ...base, status: "uploading", snapshot: { total: 1000, sent: 500, speed: 100, eta: 5 } });
  assert.match(up.text, /^50% · 500 B of 1000 B · 100 B\/s · ETA 0:05$/);
  assert.equal(up.fraction, 0.5);
  assert.match(describeItem({ ...base, status: "waiting", snapshot: { total: 1000, sent: 250 } }).text, /Waiting for the network.*25%/);
  assert.equal(describeItem({ ...base, status: "paused", snapshot: { total: 1000, sent: 900 } }).text, "Paused at 90%");
  assert.equal(describeItem({ ...base, status: "completing" }).fraction, 1);
  assert.match(describeItem({ ...base, status: "done" }).text, /^Uploaded/);
  assert.equal(describeItem({ ...base, status: "failed", message: "Nope." }).tone, "error");
  assert.equal(describeItem({ ...base, status: "cancelled" }).text, "Cancelled");
  assert.equal(describeItem({ ...base, status: "uploading", snapshot: { total: 1000, sent: 5000 } }).fraction, 1);
});
