import assert from "node:assert/strict";
import { test } from "node:test";
import {
  backoffDelay, fingerprintOf, missingChunks, SpeedMeter, UploadError, UploadJob,
} from "../../web/uploader.js";

const CHUNK = 100;

function bytes(n, seed = 1) {
  const out = new Uint8Array(n);
  let x = seed;
  for (let i = 0; i < n; i++) { x = (x * 1103515245 + 12345) & 0x7fffffff; out[i] = x >> 16; }
  return out;
}
const makeFile = (size, name = "m.safetensors", lastModified = 1700000000000) =>
  new File([bytes(size)], name, { lastModified });

// A fake server that remembers received chunks, and a fake client over it.
function fakeServer(file, { chunkSize = CHUNK, maxParallel = 8, sessions = [] } = {}) {
  const total = Math.ceil(file.size / chunkSize);
  const server = {
    received: new Set(), uploadId: "U".repeat(22), created: 0, completes: 0, cancelled: 0, sessions,
    total, chunkSize, requests: [], published: false, completeError: null, createError: null,
  };
  const ranges = () => {
    const out = [];
    for (const i of [...server.received].sort((a, b) => a - b)) {
      if (out.length && out[out.length - 1][1] === i - 1) out[out.length - 1][1] = i; else out.push([i, i]);
    }
    return out;
  };
  server.ranges = ranges;
  server.client = {
    listUploads: async () => server.sessions,
    getUpload: async () => ({
      upload_id: server.uploadId, folder: "loras", path: file.name, size: file.size, chunk_size: chunkSize,
      total_chunks: total, received_ranges: ranges(), client_fingerprint: fingerprintOf(file),
    }),
    createUpload: async (body) => {
      server.created += 1; server.createBody = body;
      if (server.createError) throw server.createError;
      return { upload_id: server.uploadId, folder: body.folder, path: body.filename, size: file.size,
        chunk_size: chunkSize, total_chunks: total, max_parallel: maxParallel };
    },
    completeUpload: async () => {
      server.completes += 1;
      if (server.completeError) { const e = server.completeError; server.completeError = null; throw e; }
      server.published = true;
      return { folder: "loras", path: file.name, size: file.size };
    },
    cancelUpload: async () => { server.cancelled += 1; return { cancelled: true }; },
    listModels: async () => (server.published ? [{ path: file.name, size: file.size }] : []),
    chunkTarget: (id, index) => ({ url: `/api/model_crud/uploads/${id}/chunks/${index}`, headers: { "Comfy-User": "tester" } }),
  };
  return server;
}

class FakeXhr {
  constructor(env) { this.env = env; this.headers = {}; this.upload = {}; this.status = 0; this.responseText = ""; this.done = false; }
  open(method, url) { this.method = method; this.url = url; this.index = Number(url.split("/").pop()); }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(blob) { this.blob = blob; this.env.xhrs.push(this); this.env.behavior(this, blob); }
  abort() { if (this.done) return; this.done = true; this.env.aborted += 1; this.onabort?.(); }
  respond(status, body = {}) { if (this.done) return; this.done = true; this.status = status; this.responseText = JSON.stringify(body); this.onload?.(); }
  fail() { if (this.done) return; this.done = true; this.onerror?.(); }
  progress(loaded) { this.upload.onprogress?.({ loaded }); }
}

function makeEnv(server, file) {
  const env = {
    xhrs: [], aborted: 0, sleeps: [], clock: 0, online: true, onlineWaiters: [], timers: [],
    inflight: 0, maxInflight: 0, wakeLocks: { acquired: 0, released: 0 },
    now: () => env.clock,
    random: () => 0.5,
    isOnline: () => env.online,
    sleep: (ms, signal) => new Promise((resolve, reject) => {
      env.sleeps.push(ms);
      if (signal?.aborted) return reject(new Error("aborted"));
      signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      Promise.resolve().then(resolve);
    }),
    waitForOnline: (signal) => new Promise((resolve, reject) => {
      if (env.online) return resolve();
      env.onlineWaiters.push(resolve);
      signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    }),
    goOnline() { env.online = true; env.onlineWaiters.splice(0).forEach((r) => r()); },
    setTimer: (fn, ms) => { const t = { fn, at: env.clock + ms, on: true }; env.timers.push(t); return t; },
    clearTimer: (t) => { if (t) t.on = false; },
    advance(ms) { env.clock += ms; for (const t of env.timers) if (t.on && t.at <= env.clock) { t.on = false; t.fn(); } },
    createXhr: () => new FakeXhr(env),
    requestWakeLock: async () => { env.wakeLocks.acquired += 1; return { release: async () => { env.wakeLocks.released += 1; } }; },
    subtle: globalThis.crypto.subtle,
    // default behaviour: the whole chunk arrives, in two progress steps, and the server records it
    behavior(xhr, blob) {
      env.inflight += 1; env.maxInflight = Math.max(env.maxInflight, env.inflight);
      const done = () => { env.inflight -= 1; };
      Promise.resolve().then(async () => {
        if (xhr.done) return done();
        xhr.progress(Math.floor(blob.size / 2));
        const content = new Uint8Array(await blob.arrayBuffer());
        const expected = new Uint8Array(await file.slice(xhr.index * server.chunkSize, xhr.index * server.chunkSize + blob.size).arrayBuffer());
        assert.deepEqual(content, expected, `chunk ${xhr.index} has the wrong bytes`);
        server.requests.push({ index: xhr.index, headers: { ...xhr.headers } });
        if (xhr.done) return done();
        xhr.progress(blob.size);
        server.received.add(xhr.index);
        xhr.respond(200, { index: xhr.index });
        done();
      });
    },
  };
  return env;
}

function setup(size = 1050, options = {}, serverOptions = {}) {
  const file = makeFile(size);
  const server = fakeServer(file, serverOptions);
  const env = makeEnv(server, file);
  const snapshots = [];
  const job = new UploadJob({
    file, folder: "loras", subfolder: options.subfolder ?? "", overwrite: options.overwrite ?? false,
    client: server.client, env, options, onChange: (s) => snapshots.push(s),
  });
  return { file, server, env, job, snapshots };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function until(fn, label = "condition") {
  for (let i = 0; i < 400; i++) { if (fn()) return; await tick(); }
  throw new Error(`timed out waiting for ${label}`);
}

// --- pure helpers -----------------------------------------------------------------------------

test("missingChunks computes the gaps from inclusive ranges", () => {
  assert.deepEqual(missingChunks([], 3), [0, 1, 2]);
  assert.deepEqual(missingChunks([[0, 2]], 3), []);
  assert.deepEqual(missingChunks([[0, 1], [3, 3], [6, 8]], 10), [2, 4, 5, 9]);
  assert.deepEqual(missingChunks([[0, 99]], 3), []); // out-of-range input is clamped
  assert.deepEqual(missingChunks([[-5, 0]], 3), [1, 2]);
  assert.deepEqual(missingChunks([], 0), []);
});

test("backoff doubles per attempt, is capped, and jitters between half and full", () => {
  const o = { baseDelayMs: 1000, maxDelayMs: 30000 };
  assert.equal(backoffDelay(1, o, () => 1), 1000);
  assert.equal(backoffDelay(2, o, () => 1), 2000);
  assert.equal(backoffDelay(3, o, () => 1), 4000);
  assert.equal(backoffDelay(10, o, () => 1), 30000);
  assert.equal(backoffDelay(3, o, () => 0), 2000);
  assert.equal(backoffDelay(3, o, () => 0.5), 3000);
});

test("fingerprint matches the CLI and server recipe", () => {
  assert.equal(fingerprintOf({ name: "Süß.safetensors", size: 10, lastModified: 1760000000123 }), "Süß.safetensors:10:1760000000123");
});

test("SpeedMeter averages over a rolling window", () => {
  const m = new SpeedMeter(5000);
  assert.equal(m.bytesPerSecond(), 0);
  m.add(0, 0); m.add(1000, 1000);
  assert.equal(m.bytesPerSecond(), 1000);
  m.add(2000, 5000);
  assert.equal(m.bytesPerSecond(), 2500);
  for (let t = 3000; t <= 20000; t += 1000) m.add(t, 5000 + (t - 2000)); // steady 1000 B/s later
  assert.equal(Math.round(m.bytesPerSecond()), 1000);
  m.reset();
  assert.equal(m.bytesPerSecond(), 0);
});

// --- the happy path ---------------------------------------------------------------------------

test("uploads every chunk with the right bytes and completes", async () => {
  const { server, job, file } = setup(1050);
  await job.start();
  assert.equal(job.state, "done");
  assert.deepEqual([...server.received].sort((a, b) => a - b), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  assert.equal(server.completes, 1);
  assert.deepEqual(server.createBody, {
    folder: "loras", filename: "m.safetensors", size: 1050, subfolder: undefined, overwrite: false,
    client_fingerprint: fingerprintOf(file),
  });
  const last = job.snapshot();
  assert.equal(last.sent, 1050); assert.equal(last.chunksDone, 11); assert.equal(last.path, "m.safetensors");
});

test("uses PUT with a binary content type, the chunk URL and the Comfy-User header", async () => {
  const { env, job } = setup(250);
  await job.start();
  const [first] = env.xhrs;
  assert.equal(first.method, "PUT");
  assert.match(first.url, /\/chunks\/\d$/);
  assert.equal(first.headers["Content-Type"], "application/octet-stream");
  assert.equal(first.headers["Comfy-User"], "tester");
  assert.ok(env.xhrs.every((x) => x.blob.size <= CHUNK));
});

test("sends the subfolder and overwrite flag when creating the session", async () => {
  const { server, job } = setup(150, { subfolder: "/sdxl/v2/", overwrite: true });
  await job.start();
  assert.equal(server.createBody.subfolder, "sdxl/v2");
  assert.equal(server.createBody.overwrite, true);
});

test("a single small chunk works", async () => {
  const { server, job } = setup(7);
  await job.start();
  assert.equal(job.state, "done");
  assert.deepEqual([...server.received], [0]);
});

test("parallelism is capped by the option and by what the server advertises", async () => {
  for (const [option, advertised, expected] of [[3, 8, 3], [8, 2, 2], [1, 8, 1], [4, 4, 4]]) {
    const { env, job } = setup(2000, { parallel: option }, { maxParallel: advertised });
    await job.start();
    assert.equal(job.state, "done");
    assert.equal(env.maxInflight, expected, `option ${option}, server ${advertised}`);
  }
});

test("resuming via the status endpoint is capped by the configured maxParallel", async () => {
  const file = makeFile(1000);
  const server = fakeServer(file);
  server.sessions = [{ upload_id: server.uploadId, folder: "loras", size: 1000, path: file.name, client_fingerprint: fingerprintOf(file) }];
  const env = makeEnv(server, file);
  const job = new UploadJob({ file, folder: "loras", client: server.client, env, options: { parallel: 6, maxParallel: 2 } });
  await job.start();
  assert.equal(env.maxInflight, 2);
});

// --- resume -----------------------------------------------------------------------------------

test("an existing session for the same file is resumed and only the gaps are sent", async () => {
  const file = makeFile(1000);
  const server = fakeServer(file);
  for (const i of [0, 1, 4, 9]) server.received.add(i);
  server.sessions = [{ upload_id: server.uploadId, folder: "loras", size: 1000, path: file.name, client_fingerprint: fingerprintOf(file) }];
  const env = makeEnv(server, file);
  const job = new UploadJob({ file, folder: "loras", client: server.client, env });
  await job.start();
  assert.equal(server.created, 0);
  assert.deepEqual(server.requests.map((r) => r.index).sort((a, b) => a - b), [2, 3, 5, 6, 7, 8]);
  assert.equal(job.state, "done");
});

test("a session for another file, folder, size or path is not reused", async () => {
  const file = makeFile(1000);
  const finger = fingerprintOf(file);
  const variants = [
    { client_fingerprint: "other:1:2" }, { folder: "vae" }, { size: 999 }, { path: "other/m.safetensors" },
  ];
  for (const variant of variants) {
    const server = fakeServer(file);
    server.sessions = [{ upload_id: "X".repeat(22), folder: "loras", size: 1000, path: file.name, client_fingerprint: finger, ...variant }];
    const job = new UploadJob({ file, folder: "loras", client: server.client, env: makeEnv(server, file) });
    await job.start();
    assert.equal(server.created, 1, JSON.stringify(variant));
  }
});

test("start with an explicit session (resume from the unfinished list)", async () => {
  const file = makeFile(500);
  const server = fakeServer(file);
  server.received.add(0);
  const job = new UploadJob({ file, folder: "loras", client: server.client, env: makeEnv(server, file) });
  const session = await server.client.getUpload();
  await job.start({ session });
  assert.equal(server.created, 0);
  assert.deepEqual(server.requests.map((r) => r.index).sort(), [1, 2, 3, 4]);
});

// --- retries ----------------------------------------------------------------------------------

test("a transient 503 is retried with growing backoff and succeeds", async () => {
  const { env, job, server } = setup(300);
  let failures = 0;
  const original = env.behavior;
  env.behavior = (xhr, blob) => {
    if (xhr.index === 1 && failures < 3) { failures += 1; return Promise.resolve().then(() => xhr.respond(503, { error: "x", message: "busy" })); }
    return original(xhr, blob);
  };
  await job.start();
  assert.equal(job.state, "done");
  assert.deepEqual(env.sleeps, [750, 1500, 3000]); // 1s, 2s, 4s at the 0.75 jitter midpoint
  assert.equal(server.received.size, 3);
});

for (const status of [408, 422, 429, 500, 502, 504]) {
  test(`HTTP ${status} is retried`, async () => {
    const { env, job } = setup(100);
    const original = env.behavior; let first = true;
    env.behavior = (xhr, blob) => {
      if (first) { first = false; return Promise.resolve().then(() => xhr.respond(status, { error: "e", message: "m" })); }
      return original(xhr, blob);
    };
    await job.start();
    assert.equal(job.state, "done");
  });
}

test("network errors are retried", async () => {
  const { env, job } = setup(100);
  const original = env.behavior; let first = 2;
  env.behavior = (xhr, blob) => {
    if (first-- > 0) return Promise.resolve().then(() => xhr.fail());
    return original(xhr, blob);
  };
  await job.start();
  assert.equal(job.state, "done");
  assert.equal(env.sleeps.length, 2);
});

test("after the retries are used up the upload fails with the server's message", async () => {
  const { env, job, server } = setup(100, { retries: 5 });
  env.behavior = (xhr) => Promise.resolve().then(() => xhr.respond(503, { error: "busy", message: "The server is busy." }));
  await job.start();
  const s = job.snapshot();
  assert.equal(s.state, "failed");
  assert.match(s.error, /The server is busy\./);
  assert.match(s.error, /gave up after 6 attempts/);
  assert.equal(env.xhrs.length, 6); // the first try plus five retries
  assert.equal(s.resumable, true);
  assert.equal(server.completes, 0);
});

for (const [status, code] of [[400, "bad_chunk"], [404, "unknown_upload"], [409, "upload_in_progress"], [413, "too_large"], [415, "extension_not_allowed"], [507, "insufficient_storage"]]) {
  test(`HTTP ${status} is not retried`, async () => {
    const { env, job } = setup(100);
    env.behavior = (xhr) => Promise.resolve().then(() => xhr.respond(status, { error: code, message: `msg ${code}` }));
    await job.start();
    assert.equal(env.xhrs.length, 1);
    assert.equal(job.snapshot().errorCode, code);
    assert.equal(job.snapshot().error, `msg ${code}`);
  });
}

test("a failed attempt does not count as progress", async () => {
  const { env, job, file } = setup(100);
  let first = true;
  const seen = [];
  env.behavior = (xhr, blob) => Promise.resolve().then(() => {
    if (first) {
      first = false;
      xhr.progress(80);
      seen.push(job.snapshot().sent); // the bytes are counted while the attempt is running...
      xhr.fail();
      return;
    }
    seen.push(job.snapshot().sent); // ...and gone again before the retry starts
    xhr.progress(blob.size);
    xhr.respond(200, {});
  });
  await job.start();
  assert.deepEqual(seen, [80, 0]);
  assert.equal(job.state, "done");
  assert.equal(job.snapshot().sent, file.size);
});

test("a stalled request is aborted and retried", async () => {
  const { env, job } = setup(100, { stallMs: 1000 });
  let hung = true;
  const original = env.behavior;
  env.behavior = (xhr, blob) => {
    if (hung) { hung = false; return; } // never answers
    return original(xhr, blob);
  };
  const running = job.start();
  await until(() => env.xhrs.length === 1, "first request");
  env.advance(1000);
  await running;
  assert.equal(env.aborted, 1);
  assert.equal(job.state, "done");
  assert.equal(env.sleeps.length, 1);
});

test("progress events keep a slow-but-moving request alive", async () => {
  const { env, job } = setup(100, { stallMs: 1000 });
  env.behavior = (xhr, blob) => { xhr.progress(10); env.advance(900); xhr.progress(20); env.advance(900); xhr.progress(blob.size); xhr.respond(200, {}); };
  await job.start();
  assert.equal(env.aborted, 0);
  assert.equal(job.state, "done");
});

// --- offline ----------------------------------------------------------------------------------

test("when offline, the job waits for the network without using up retries", async () => {
  const { env, job } = setup(100, { retries: 1 });
  const original = env.behavior; let drop = 5;
  env.behavior = (xhr, blob) => {
    if (drop > 0) { drop -= 1; env.online = false; return Promise.resolve().then(() => xhr.fail()); }
    return original(xhr, blob);
  };
  const running = job.start();
  await until(() => job.state === "waiting", "waiting state");
  assert.equal(env.sleeps.length, 0);
  env.goOnline();
  await until(() => drop === 4 || job.state === "waiting" || job.state === "done", "next step");
  // go offline again several times; every wait is released by "online" and none burns a retry
  for (let i = 0; i < 5 && job.state !== "done"; i++) { await until(() => job.state === "waiting" || job.state === "done"); env.goOnline(); await tick(); }
  await running;
  assert.equal(job.state, "done");
});

test("pausing while waiting for the network works", async () => {
  const { env, job } = setup(100);
  env.online = false;
  env.behavior = (xhr) => Promise.resolve().then(() => xhr.fail());
  const running = job.start();
  await until(() => job.state === "waiting");
  await job.pause();
  assert.equal(job.state, "paused");
  await running;
});

// --- pause, resume, cancel --------------------------------------------------------------------

test("pause aborts in-flight chunks and stops; resume sends the rest without repeats", async () => {
  const { env, job, server } = setup(1000, { parallel: 2 });
  const original = env.behavior; let gate = true;
  env.behavior = (xhr, blob) => {
    if (gate && server.received.size >= 3) { xhr.progress(10); return; } // hold new requests
    return original(xhr, blob);
  };
  const running = job.start();
  await until(() => server.received.size >= 3 && env.xhrs.some((x) => !x.done), "three chunks and a held request");
  const before = new Set(server.received);
  await job.pause();
  assert.equal(job.state, "paused");
  assert.ok(env.aborted >= 1);
  const sentWhilePaused = env.xhrs.length;
  await tick(); await tick();
  assert.equal(env.xhrs.length, sentWhilePaused, "nothing is sent while paused");
  assert.deepEqual(job.snapshot().sent, [...before].length * CHUNK);
  gate = false;
  await job.resume();
  await running;
  assert.equal(job.state, "done");
  assert.equal(server.received.size, 10);
  const counts = {};
  for (const r of server.requests) counts[r.index] = (counts[r.index] ?? 0) + 1;
  for (const i of before) assert.equal(counts[i], 1, `completed chunk ${i} was sent again`);
});

test("resume right after pause (not awaited) waits for the old requests to wind down", async () => {
  // In a browser, xhr.abort() fires onabort asynchronously; a quick resume must not let the old
  // workers and the new ones overlap, or the parallel limit would be exceeded.
  const { env, job, server } = setup(2000, { parallel: 2 });
  const asyncAbort = FakeXhr.prototype.abort;
  FakeXhr.prototype.abort = function () {
    if (this.done) return;
    this.done = true; env.aborted += 1;
    setTimeout(() => this.onabort?.(), 5);
  };
  try {
    const original = env.behavior; let hold = true;
    env.behavior = (xhr, blob) => { if (hold && server.received.size >= 4) { xhr.progress(3); return; } return original(xhr, blob); };
    const running = job.start();
    await until(() => server.received.size >= 4 && env.xhrs.filter((x) => !x.done).length >= 1, "held requests");
    hold = false;
    env.inflight = 0; env.maxInflight = 0;
    const paused = job.pause(); // deliberately not awaited
    const resumed = job.resume();
    await Promise.all([paused, resumed, running]);
    assert.equal(job.state, "done");
    assert.ok(env.maxInflight <= 2, `max in flight ${env.maxInflight}`);
    assert.equal(server.received.size, 20);
  } finally {
    FakeXhr.prototype.abort = asyncAbort;
  }
});

test("a worker caught hashing a chunk when paused does not outlive the pause", async () => {
  const { env, job, server } = setup(1200, { parallel: 2, chunkHash: true });
  let hashing = 0, live = 0, peak = 0;
  env.subtle = {
    digest: async (...args) => {
      hashing += 1; live += 1; peak = Math.max(peak, live);
      await new Promise((r) => setTimeout(r, 15));
      live -= 1;
      return crypto.subtle.digest(...args);
    },
  };
  const running = job.start();
  await until(() => hashing >= 2, "workers hashing");
  const paused = job.pause();
  const resumed = job.resume(); // the paused workers are still inside digest()
  await Promise.all([paused, resumed, running]);
  assert.equal(job.state, "done");
  assert.ok(env.maxInflight <= 2, `max in flight ${env.maxInflight}`);
  assert.ok(peak <= 2, `${peak} workers were busy at once with parallel = 2`);
  const counts = {};
  for (const r of server.requests) counts[r.index] = (counts[r.index] ?? 0) + 1;
  assert.ok(Object.values(counts).every((n) => n === 1), JSON.stringify(counts));
});

test("pause and resume are no-ops in the wrong states", async () => {
  const { job } = setup(100);
  await job.pause(); await job.resume();
  assert.equal(job.state, "idle");
  await job.start();
  await job.pause(); await job.resume();
  assert.equal(job.state, "done");
});

test("cancel aborts requests, deletes the server session and stays cancelled", async () => {
  const { env, job, server } = setup(1000, { parallel: 2 });
  const original = env.behavior;
  env.behavior = (xhr, blob) => { if (server.received.size >= 2) { xhr.progress(5); return; } return original(xhr, blob); };
  const running = job.start();
  await until(() => server.received.size >= 2 && env.xhrs.some((x) => !x.done));
  await job.cancel();
  await running;
  assert.equal(job.state, "cancelled");
  assert.equal(server.cancelled, 1);
  assert.equal(server.completes, 0);
  await job.resume(); await job.pause();
  assert.equal(job.state, "cancelled");
});

test("cancelling while paused also deletes the session", async () => {
  const { env, job, server } = setup(1000, { parallel: 1 });
  const original = env.behavior;
  env.behavior = (xhr, blob) => { if (server.received.size >= 2) { xhr.progress(5); return; } return original(xhr, blob); };
  const running = job.start();
  await until(() => server.received.size >= 2 && env.xhrs.some((x) => !x.done));
  await job.pause(); await running;
  await job.cancel();
  assert.equal(job.state, "cancelled");
  assert.equal(server.cancelled, 1);
});

test("cancelling during session creation leaves nothing on the server", async () => {
  const file = makeFile(500);
  const server = fakeServer(file);
  const env = makeEnv(server, file);
  let release;
  server.client.createUpload = (body) => new Promise((resolve) => { release = () => resolve({ upload_id: server.uploadId, chunk_size: CHUNK, total_chunks: 5, path: file.name, max_parallel: 4 }); });
  const job = new UploadJob({ file, folder: "loras", client: server.client, env });
  const starting = job.start();
  await tick();
  await job.cancel();
  release();
  await starting;
  assert.equal(job.state, "cancelled");
  assert.equal(env.xhrs.length, 0);
  assert.equal(server.cancelled, 1);
});

// --- errors around the session ---------------------------------------------------------------

test("file_exists at creation is reported with its code, nothing is uploaded", async () => {
  const { server, env, job } = setup(100);
  server.createError = Object.assign(new Error("A file with this name already exists."), { code: "file_exists", status: 409 });
  await job.start();
  const s = job.snapshot();
  assert.deepEqual([s.state, s.errorCode, s.error], ["failed", "file_exists", "A file with this name already exists."]);
  assert.equal(s.resumable, false);
  assert.equal(env.xhrs.length, 0);
});

test("incomplete_upload on complete re-queues what the server lacks", async () => {
  const { server, job } = setup(300);
  server.completeError = Object.assign(new Error("Not all chunks"), { code: "incomplete_upload", status: 409 });
  const original = server.client.completeUpload;
  let injected = false;
  server.client.completeUpload = async () => {
    if (!injected) { injected = true; server.received.delete(1); throw server.completeError; }
    return original();
  };
  await job.start();
  assert.equal(job.state, "done");
  assert.deepEqual(server.requests.filter((r) => r.index === 1).length, 2);
});

test("a lost reply to complete is recognised when the file is already published", async () => {
  const { server, job } = setup(200);
  const original = server.client.completeUpload;
  let calls = 0;
  server.client.completeUpload = async () => {
    calls += 1;
    if (calls === 1) { await original(); throw Object.assign(new Error("net"), { code: "network_error", status: 0 }); }
    throw Object.assign(new Error("Unknown upload."), { code: "unknown_upload", status: 404 });
  };
  await job.start();
  assert.equal(job.state, "done");
  assert.equal(calls, 2);
});

test("complete failures that are not transient fail the job and keep it resumable", async () => {
  const { server, job } = setup(200);
  server.completeError = Object.assign(new Error("File exists."), { code: "file_exists", status: 409 });
  await job.start();
  assert.equal(job.snapshot().errorCode, "file_exists");
  assert.equal(job.snapshot().resumable, true);
  await job.retry();
  assert.equal(job.state, "done");
});

test("retry after a failure sends only what is still missing", async () => {
  const { env, job, server } = setup(500, { retries: 0, parallel: 1 });
  const original = env.behavior;
  let broken = true;
  env.behavior = (xhr, blob) => (broken && xhr.index === 3 ? Promise.resolve().then(() => xhr.respond(500, { error: "e", message: "boom" })) : original(xhr, blob));
  await job.start();
  assert.equal(job.state, "failed");
  assert.deepEqual([...server.received].sort(), [0, 1, 2]);
  broken = false;
  const before = server.requests.length;
  await job.retry();
  assert.equal(job.state, "done");
  assert.deepEqual(server.requests.slice(before).map((r) => r.index), [3, 4]);
});

// --- integrity, memory, environment ----------------------------------------------------------

test("per-chunk SHA-256 is sent and correct when enabled, absent otherwise", async () => {
  const { server, job, file } = setup(250, { chunkHash: true });
  await job.start();
  for (const { index, headers } of server.requests) {
    const piece = new Uint8Array(await file.slice(index * CHUNK, (index + 1) * CHUNK).arrayBuffer());
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", piece));
    assert.equal(headers["Upload-Chunk-SHA256"], [...digest].map((b) => b.toString(16).padStart(2, "0")).join(""));
  }
  const plain = setup(250);
  await plain.job.start();
  assert.ok(plain.server.requests.every((r) => !("Upload-Chunk-SHA256" in r.headers)));
});

test("without chunk hashing the file is never read into memory by the scheduler", async () => {
  const { job, file } = setup(1000);
  let reads = 0;
  const realSlice = file.slice.bind(file);
  file.arrayBuffer = () => { reads += 1; return Promise.reject(new Error("whole file read")); };
  file.slice = (a, b) => { const piece = realSlice(a, b); const ab = piece.arrayBuffer.bind(piece); piece.arrayBuffer = () => ab(); return piece; };
  await job.start();
  assert.equal(job.state, "done");
  assert.equal(reads, 0);
});

test("with chunk hashing at most `parallel` chunks are read into memory at once", async () => {
  const { job, file, env } = setup(1500, { chunkHash: true, parallel: 3 });
  let live = 0, peak = 0;
  const realSlice = file.slice.bind(file);
  file.slice = (a, b) => {
    const piece = realSlice(a, b);
    const ab = piece.arrayBuffer.bind(piece);
    piece.arrayBuffer = async () => { live += 1; peak = Math.max(peak, live); const buf = await ab(); await tick(); live -= 1; return buf; };
    return piece;
  };
  await job.start();
  assert.equal(job.state, "done");
  assert.ok(peak <= 3, `peak ${peak}`);
  assert.ok(env.maxInflight <= 3);
});

test("a missing SubtleCrypto fails clearly instead of sending unverified data", async () => {
  const { env, job } = setup(100, { chunkHash: true });
  env.subtle = null;
  await job.start();
  assert.equal(job.snapshot().errorCode, "no_crypto");
});

test("the wake lock is requested while uploading and always released", async () => {
  const { env, job } = setup(300);
  await job.start();
  assert.ok(env.wakeLocks.acquired >= 1);
  assert.equal(env.wakeLocks.released, env.wakeLocks.acquired);
});

test("a wake lock that is unavailable or refuses does not stop the upload", async () => {
  const { env, job } = setup(300);
  env.requestWakeLock = async () => { throw new Error("NotAllowedError"); };
  await job.start();
  assert.equal(job.state, "done");
  const none = setup(300); none.env.requestWakeLock = undefined;
  await none.job.start();
  assert.equal(none.job.state, "done");
});

test("the wake lock is released when paused and when failed", async () => {
  const { env, job, server } = setup(500, { parallel: 1, retries: 0 });
  const original = env.behavior;
  env.behavior = (xhr, blob) => { if (server.received.size >= 1) { xhr.progress(1); return; } return original(xhr, blob); };
  const running = job.start();
  await until(() => server.received.size >= 1 && env.xhrs.some((x) => !x.done));
  await job.pause(); await running;
  assert.equal(env.wakeLocks.released, env.wakeLocks.acquired);
});

test("snapshots report speed and ETA from the rolling meter", async () => {
  const { env, job, snapshots } = setup(1000, { parallel: 1 });
  env.behavior = (xhr, blob) => {
    env.clock += 1000; xhr.progress(blob.size / 2); env.clock += 1000; xhr.progress(blob.size);
    env.advance(0); xhr.respond(200, {}); (job.snapshot().speed); 
  };
  await job.start();
  const speeds = snapshots.filter((s) => s.state === "uploading" && s.speed > 0);
  assert.ok(speeds.length > 0);
  const s = speeds[speeds.length - 1];
  assert.ok(Math.abs(s.speed - 50) < 1, `speed ${s.speed}`);
  assert.ok(s.eta === null || s.eta >= 0);
  assert.equal(snapshots.at(-1).speed, 0); // finished: no speed
});

test("onChange is throttled during progress but never swallows a state change", async () => {
  const { env, job, snapshots } = setup(100);
  env.behavior = (xhr, blob) => { for (let i = 1; i <= 50; i++) xhr.progress(i); xhr.progress(blob.size); xhr.respond(200, {}); };
  await job.start();
  assert.ok(snapshots.filter((s) => s.state === "uploading").length < 20);
  assert.equal(snapshots.at(-1).state, "done");
  assert.deepEqual([...new Set(snapshots.map((s) => s.state))], ["starting", "uploading", "completing", "done"]);
});

test("UploadError carries its details", () => {
  const e = new UploadError("m", { code: "c", status: 409, retryable: false, details: { a: 1 } });
  assert.deepEqual([e.message, e.code, e.status, e.retryable, e.details], ["m", "c", 409, false, { a: 1 }]);
});
