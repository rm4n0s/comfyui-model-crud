// Chunked, parallel, resumable upload of one file (SPEC FR-UI3, §6.4).
//
// No DOM and no frontend imports: everything the browser provides (XMLHttpRequest, timers,
// connectivity, wake lock, WebCrypto) comes in through `env`, so the scheduler runs under Node in
// the tests. The file is only ever read through Blob.slice(), which does not copy it; a chunk is
// held in memory only while hashing it (when per-chunk checksums are on).

export const DEFAULTS = {
  parallel: 3,
  retries: 5, // per chunk, after the first attempt
  stallMs: 60000, // abort a chunk request that moves no byte for this long
  baseDelayMs: 1000,
  maxDelayMs: 30000,
  maxIncompleteRounds: 3,
};

const RETRYABLE_STATUS = new Set([408, 422, 429, 500, 502, 503, 504]);

export class UploadError extends Error {
  constructor(message, { code = "upload_failed", status = 0, retryable = false, details = null } = {}) {
    super(message);
    this.name = "UploadError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.details = details;
  }
}

// Thrown inside the scheduler when pause or cancel interrupts work; never shown to users.
class Stopped extends Error {}

export function fingerprintOf(file) {
  // Same recipe as scripts/model_crud_upload.py, so either client can resume the other's upload.
  return `${file.name}:${file.size}:${file.lastModified}`;
}

export function missingChunks(ranges, total) {
  const have = new Uint8Array(total);
  for (const [first, last] of ranges) {
    for (let i = Math.max(first, 0); i <= Math.min(last, total - 1); i++) have[i] = 1;
  }
  const missing = [];
  for (let i = 0; i < total; i++) if (!have[i]) missing.push(i);
  return missing;
}

// 1 s, 2 s, 4 s ... capped, with jitter so parallel chunks do not retry in lockstep.
export function backoffDelay(attempt, { baseDelayMs, maxDelayMs }, random = Math.random) {
  const ceiling = Math.min(baseDelayMs * 2 ** (attempt - 1), maxDelayMs);
  return ceiling * (0.5 + random() * 0.5);
}

// Rolling-average speed over the last few seconds of (time, total bytes sent) samples.
export class SpeedMeter {
  constructor(windowMs = 5000) {
    this.windowMs = windowMs;
    this.samples = [];
  }

  add(now, totalBytes) {
    this.samples.push([now, totalBytes]);
    const cutoff = now - this.windowMs;
    while (this.samples.length > 2 && this.samples[1][0] <= cutoff) this.samples.shift();
  }

  reset() {
    this.samples = [];
  }

  bytesPerSecond() {
    if (this.samples.length < 2) return 0;
    const [t0, b0] = this.samples[0];
    const [t1, b1] = this.samples[this.samples.length - 1];
    return t1 > t0 ? Math.max(0, ((b1 - b0) * 1000) / (t1 - t0)) : 0;
  }
}

function parseError(xhr) {
  let body = null;
  try {
    body = JSON.parse(xhr.responseText);
  } catch (err) {
    // Not JSON, for example a proxy error page.
  }
  return new UploadError(body?.message ?? `The server answered HTTP ${xhr.status}.`, {
    code: body?.error ?? "http_error",
    status: xhr.status,
    retryable: RETRYABLE_STATUS.has(xhr.status),
    details: body,
  });
}

function toUploadError(err) {
  if (err instanceof UploadError) return err;
  return new UploadError(err?.message ?? "Upload failed.", {
    code: err?.code ?? "upload_failed",
    status: err?.status ?? 0,
    retryable: false,
  });
}

export class UploadJob {
  #file; #folder; #subfolder; #overwrite; #client; #env; #opts; #onChange;
  #state = "idle";
  #session = null;
  #queue = [];
  #completedBytes = 0;
  #inflight = new Map(); // chunk index -> { loaded, xhr }
  #error = null;
  #abort = null; // aborted by pause, cancel and failure: interrupts requests, sleeps, waits
  #stopReason = null;
  #workers = null;
  #runSeq = 0; // each #run() owns a number; a newer run supersedes older ones
  #meter = new SpeedMeter();
  #lastEmit = 0;
  #chunksDone = 0;
  #result = null;

  constructor({ file, folder, subfolder = "", overwrite = false, client, env, options = {}, onChange = () => {} }) {
    this.#file = file;
    this.#folder = folder;
    this.#subfolder = subfolder.replace(/^\/+|\/+$/g, "");
    this.#overwrite = overwrite;
    this.#client = client;
    this.#env = env;
    this.#opts = { ...DEFAULTS, ...options };
    this.#onChange = onChange;
  }

  get state() { return this.#state; }
  get file() { return this.#file; }
  get folder() { return this.#folder; }
  get uploadId() { return this.#session?.upload_id ?? null; }

  snapshot() {
    const total = this.#file.size;
    const sent = Math.min(total, this.#completedBytes + this.#loadedBytes());
    const speed = this.#state === "uploading" ? this.#meter.bytesPerSecond() : 0;
    return {
      state: this.#state,
      name: this.#file.name,
      total,
      sent,
      speed,
      eta: speed > 0 ? (total - sent) / speed : null,
      chunksDone: this.#chunksDone,
      chunksTotal: this.#session?.total_chunks ?? 0,
      uploadId: this.uploadId,
      path: this.#result?.path ?? this.#session?.path ?? null,
      error: this.#error?.message ?? null,
      errorCode: this.#error?.code ?? null,
      resumable: this.#state === "failed" && this.uploadId !== null && this.#error?.code !== "unknown_upload",
    };
  }

  // Begin, or continue a server-side session found for the same file (or given by the caller).
  async start({ session = null } = {}) {
    if (this.#state !== "idle") return;
    this.#setState("starting");
    try {
      this.#session = session ?? (await this.#findOrCreateSession());
      if (this.#state !== "starting") {
        // Cancelled while the session was being created: do not leave it behind on the server.
        await this.#client.cancelUpload(this.#session.upload_id).catch(() => {});
        return;
      }
      const done = new Set();
      for (const [first, last] of this.#session.received_ranges ?? []) {
        for (let i = first; i <= last; i++) done.add(i);
      }
      this.#queue = missingChunks(this.#session.received_ranges ?? [], this.#session.total_chunks);
      this.#chunksDone = done.size;
      this.#completedBytes = [...done].reduce((sum, i) => sum + this.#chunkLength(i), 0);
    } catch (err) {
      return this.#fail(toUploadError(err));
    }
    await this.#run();
  }

  async pause() {
    if (this.#state !== "uploading" && this.#state !== "waiting") return;
    this.#stop("pause");
    this.#setState("paused");
    await this.#workers;
  }

  async resume() {
    if (this.#state !== "paused") return;
    await this.#workers; // the previous run must be fully stopped first
    await this.#run();
  }

  async cancel() {
    if (["done", "cancelled", "idle"].includes(this.#state)) {
      if (this.#state === "idle") this.#setState("cancelled");
      return;
    }
    const wasRunning = ["uploading", "waiting", "completing", "starting"].includes(this.#state);
    this.#stop("cancel");
    this.#setState("cancelled");
    if (wasRunning) await this.#workers;
    if (this.uploadId) {
      try {
        await this.#client.cancelUpload(this.uploadId);
      } catch (err) {
        // The session may already be gone (404) or expire on its own; nothing else to do.
      }
    }
  }

  // After a failure: try again with the same server session (only missing chunks are sent).
  async retry() {
    if (this.#state !== "failed" || !this.#session) return;
    this.#error = null;
    const status = await this.#client.getUpload(this.#session.upload_id).catch((err) => {
      this.#fail(toUploadError(err));
      return null;
    });
    if (!status) return;
    this.#session = status;
    this.#queue = missingChunks(status.received_ranges, status.total_chunks);
    this.#completedBytes = this.#queue.length
      ? this.#file.size - this.#queue.reduce((s, i) => s + this.#chunkLength(i), 0)
      : this.#file.size;
    this.#chunksDone = status.total_chunks - this.#queue.length;
    await this.#run();
  }

  // -- internals -------------------------------------------------------------------------------

  #chunkLength(index) {
    const size = this.#session.chunk_size;
    return Math.min(size, this.#file.size - index * size);
  }

  #loadedBytes() {
    let sum = 0;
    for (const { loaded } of this.#inflight.values()) sum += loaded;
    return sum;
  }

  #setState(state) {
    this.#state = state;
    this.#emit(true);
  }

  #emit(force = false) {
    const now = this.#env.now();
    if (!force && now - this.#lastEmit < 100) return;
    this.#lastEmit = now;
    this.#onChange(this.snapshot());
  }

  #stop(reason) {
    this.#stopReason = reason;
    this.#abort?.abort();
    for (const entry of this.#inflight.values()) entry.xhr?.abort();
  }

  #fail(error) {
    this.#error = toUploadError(error);
    this.#stop("fail");
    this.#queue = [];
    this.#setState("failed");
  }

  async #findOrCreateSession() {
    const finger = fingerprintOf(this.#file);
    const wanted = this.#subfolder ? `${this.#subfolder}/${this.#file.name}` : this.#file.name;
    const existing = (await this.#client.listUploads()).find(
      (u) =>
        u.client_fingerprint === finger &&
        u.folder === this.#folder &&
        u.size === this.#file.size &&
        u.path === wanted,
    );
    if (existing) return this.#client.getUpload(existing.upload_id);
    return this.#client.createUpload({
      folder: this.#folder,
      filename: this.#file.name,
      size: this.#file.size,
      subfolder: this.#subfolder || undefined,
      overwrite: this.#overwrite,
      client_fingerprint: finger,
    });
  }

  async #run() {
    const mine = ++this.#runSeq;
    for (let round = 0; ; round++) {
      this.#abort = new AbortController();
      this.#stopReason = null;
      this.#meter.reset();
      this.#setState("uploading");
      const wakeLock = await this.#acquireWakeLock();
      const advertised = this.#session.max_parallel ?? this.#opts.maxParallel ?? this.#opts.parallel;
      const parallel = Math.max(1, Math.min(this.#opts.parallel, advertised));
      this.#workers = Promise.all(Array.from({ length: parallel }, () => this.#worker()));
      await this.#workers;
      await this.#releaseWakeLock(wakeLock);
      // Paused, cancelled or failed, or a newer run (resume) has taken over: this one is done.
      if (this.#stopReason || mine !== this.#runSeq) return;
      if (this.#queue.length) continue;

      this.#setState("completing");
      try {
        const again = await this.#complete();
        if (again && round < this.#opts.maxIncompleteRounds) continue; // the server lost track of some chunks
        if (again) throw new UploadError("The server keeps reporting missing chunks.", { code: "incomplete_upload" });
      } catch (err) {
        return this.#fail(err);
      }
      return;
    }
  }

  async #worker() {
    while (!this.#stopReason && this.#queue.length) {
      const index = this.#queue.shift();
      try {
        await this.#sendWithRetries(index);
      } catch (err) {
        this.#queue.unshift(index);
        if (err instanceof Stopped) return;
        return this.#fail(err);
      }
    }
  }

  async #sendWithRetries(index) {
    let failures = 0;
    for (;;) {
      if (this.#stopReason) throw new Stopped();
      try {
        await this.#sendChunk(index);
        return;
      } catch (err) {
        if (err instanceof Stopped || this.#stopReason) throw new Stopped();
        if (!err.retryable) throw err;
        if (!this.#env.isOnline()) {
          // Offline is not the chunk's fault: wait for the connection, and do not use up a retry.
          this.#setState("waiting");
          await this.#interruptible(this.#env.waitForOnline(this.#abort.signal));
          this.#setState("uploading");
          continue;
        }
        failures += 1;
        if (failures > this.#opts.retries) {
          throw new UploadError(`${err.message} (gave up after ${failures} attempts)`, {
            code: err.code, status: err.status, retryable: false, details: err.details,
          });
        }
        await this.#interruptible(
          this.#env.sleep(backoffDelay(failures, this.#opts, this.#env.random), this.#abort.signal),
        );
      }
    }
  }

  async #interruptible(promise) {
    try {
      await promise;
    } catch (err) {
      throw new Stopped();
    }
    if (this.#stopReason) throw new Stopped();
  }

  async #sendChunk(index) {
    const size = this.#session.chunk_size;
    const start = index * size;
    const blob = this.#file.slice(start, start + this.#chunkLength(index));
    const headers = { "Content-Type": "application/octet-stream" };
    if (this.#opts.chunkHash) {
      // WebCrypto has no incremental SHA-256, so this chunk is briefly in memory (at most
      // `parallel` chunks at a time).
      headers["Upload-Chunk-SHA256"] = await this.#sha256Hex(blob);
      if (this.#stopReason) throw new Stopped();
    }
    const target = this.#client.chunkTarget(this.#session.upload_id, index);
    await new Promise((resolve, reject) => {
      const xhr = this.#env.createXhr();
      const entry = { loaded: 0, xhr };
      this.#inflight.set(index, entry);
      let stall = null;
      let stalled = false;
      const armStall = () => {
        this.#env.clearTimer(stall);
        stall = this.#env.setTimer(() => { stalled = true; xhr.abort(); }, this.#opts.stallMs);
      };
      const finish = (fn, value) => {
        this.#env.clearTimer(stall);
        this.#inflight.delete(index);
        fn(value);
      };
      xhr.open("PUT", target.url);
      for (const [name, value] of Object.entries({ ...headers, ...target.headers })) {
        xhr.setRequestHeader(name, value);
      }
      xhr.upload.onprogress = (event) => {
        entry.loaded = Math.min(event.loaded, blob.size);
        armStall();
        this.#meter.add(this.#env.now(), this.#completedBytes + this.#loadedBytes());
        this.#emit();
      };
      xhr.onload = () => {
        if (xhr.status === 200) {
          this.#completedBytes += blob.size;
          this.#chunksDone += 1;
          this.#meter.add(this.#env.now(), this.#completedBytes + this.#loadedBytes() - entry.loaded);
          finish(resolve);
          this.#emit();
        } else {
          finish(reject, parseError(xhr));
        }
      };
      xhr.onerror = () =>
        finish(reject, new UploadError("Network error while sending a chunk.", { code: "network_error", retryable: true }));
      xhr.onabort = () =>
        finish(
          reject,
          this.#stopReason
            ? new Stopped()
            : new UploadError(stalled ? "The connection stalled." : "The request was aborted.", {
                code: "network_error", retryable: true,
              }),
        );
      armStall();
      xhr.send(blob);
    });
  }

  async #sha256Hex(blob) {
    const subtle = this.#env.subtle;
    if (!subtle) throw new UploadError("This browser cannot compute SHA-256 here.", { code: "no_crypto" });
    const digest = await subtle.digest("SHA-256", await blob.arrayBuffer());
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // Returns true when the server says chunks are missing and they were queued again.
  async #complete() {
    let failures = 0;
    for (;;) {
      try {
        this.#result = await this.#client.completeUpload(this.#session.upload_id);
        this.#setState("done");
        return false;
      } catch (err) {
        if (err.code === "incomplete_upload") {
          const status = await this.#client.getUpload(this.#session.upload_id);
          this.#queue = missingChunks(status.received_ranges, status.total_chunks);
          this.#completedBytes = this.#file.size - this.#queue.reduce((s, i) => s + this.#chunkLength(i), 0);
          this.#chunksDone = status.total_chunks - this.#queue.length;
          return true;
        }
        if (err.code === "unknown_upload" && failures > 0 && (await this.#isPublished())) {
          this.#result = { path: this.#session.path };
          this.#setState("done"); // an earlier attempt succeeded and only its reply was lost
          return false;
        }
        const transient = err.status === 0 || err.status === undefined || RETRYABLE_STATUS.has(err.status);
        failures += 1;
        if (!transient || failures > this.#opts.retries) throw err;
        await this.#env.sleep(backoffDelay(failures, this.#opts, this.#env.random));
      }
    }
  }

  async #isPublished() {
    try {
      const files = await this.#client.listModels(this.#folder);
      return files.some((f) => f.path === this.#session.path && f.size === this.#file.size);
    } catch (err) {
      return false;
    }
  }

  async #acquireWakeLock() {
    try {
      return (await this.#env.requestWakeLock?.()) ?? null;
    } catch (err) {
      return null; // best effort: not available, or refused (for example, hidden tab)
    }
  }

  async #releaseWakeLock(lock) {
    try {
      await lock?.release();
    } catch (err) {
      // Already released by the browser.
    }
  }
}
