// Upload queue of the "Model files" tab: validates files, runs them one at a time, and keeps the
// list of unfinished server-side uploads. No DOM; the job class, client and callbacks are injected.

import { createStore, formatDuration, formatSize } from "./state.js";
import { fingerprintOf, UploadJob } from "./uploader.js";

const ACTIVE = new Set(["starting", "uploading", "waiting", "paused", "completing"]);
const FINISHED = new Set(["done", "failed", "cancelled"]);

function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot <= 0 ? "" : name.slice(dot).toLowerCase();
}

// Text and progress bar value for one queue row.
export function describeItem(item) {
  const snap = item.snapshot;
  const total = snap?.total ?? item.file.size;
  const fraction = total ? Math.min(1, (snap?.sent ?? 0) / total) : 0;
  const percent = `${Math.floor(fraction * 100)}%`;
  switch (item.status) {
    case "queued":
      return { text: "Waiting in the queue", fraction: 0, tone: "info" };
    case "starting":
      return { text: "Starting…", fraction, tone: "info" };
    case "uploading": {
      const speed = snap?.speed > 0 ? ` · ${formatSize(snap.speed)}/s` : "";
      const eta = snap?.eta != null ? ` · ETA ${formatDuration(snap.eta)}` : "";
      return { text: `${percent} · ${formatSize(snap?.sent ?? 0)} of ${formatSize(total)}${speed}${eta}`, fraction, tone: "info" };
    }
    case "waiting":
      return { text: `Waiting for the network… (${percent} sent, will continue by itself)`, fraction, tone: "warn" };
    case "paused":
      return { text: `Paused at ${percent}`, fraction, tone: "info" };
    case "completing":
      return { text: "Finishing on the server…", fraction: 1, tone: "info" };
    case "done":
      return { text: `Uploaded (${formatSize(total)})`, fraction: 1, tone: "ok" };
    case "failed":
      return {
        text: item.message ?? snap?.error ?? "The upload failed.",
        fraction,
        tone: "error",
      };
    case "cancelled":
      return { text: "Cancelled", fraction, tone: "info" };
    default:
      return { text: "", fraction, tone: "info" };
  }
}

export function createUploadManager({
  client,
  env,
  JobClass = UploadJob,
  getFolder,
  getOptions = () => ({}),
  onUploaded = () => {},
  notify = () => {},
}) {
  const store = createStore({
    config: null,
    enabled: false,
    items: [],
    sessions: [],
    sessionsError: null,
  });
  const jobs = new Map();
  let nextId = 1;

  const find = (id) => store.get().items.find((i) => i.id === id);
  function update(id, patch) {
    store.set({ items: store.get().items.map((i) => (i.id === id ? { ...i, ...patch } : i)) });
  }

  function setConfig(config) {
    store.set({ config, enabled: config?.upload_enabled === true });
    if (config?.upload_enabled) refreshSessions();
  }

  function rejection(file, config) {
    if (file.size === 0) return "The file is empty.";
    const ext = extensionOf(file.name);
    if (!config.allowed_extensions.includes(ext)) {
      return `"${ext || "(no extension)"}" files are not allowed. Allowed: ${config.allowed_extensions.join(" ")}`;
    }
    if (file.size > config.max_upload_bytes) {
      return `The file is ${formatSize(file.size)}; this server accepts at most ${formatSize(config.max_upload_bytes)}.`;
    }
    return null;
  }

  function addItem(file, { folder, subfolder, overwrite, session = null }) {
    const config = store.get().config;
    const id = nextId++;
    const problem = rejection(file, config);
    const item = {
      id, file, folder, subfolder, overwrite, session,
      status: problem ? "failed" : "queued",
      message: problem,
      snapshot: null,
    };
    store.set({ items: [...store.get().items, item] });
    return id;
  }

  function enqueue(files, { subfolder = "", overwrite = false } = {}) {
    const state = store.get();
    const folder = getFolder();
    if (!state.enabled || !folder) return [];
    const ids = [...files].map((file) => addItem(file, { folder, subfolder, overwrite }));
    pump();
    return ids;
  }

  function pump() {
    const items = store.get().items;
    if (items.some((i) => ACTIVE.has(i.status))) return;
    const next = items.find((i) => i.status === "queued");
    if (next) run(next);
  }

  async function run(item) {
    const config = store.get().config;
    const job = new JobClass({
      file: item.file,
      folder: item.folder,
      subfolder: item.subfolder,
      overwrite: item.overwrite,
      client,
      env,
      options: { ...getOptions(), maxParallel: config.max_parallel },
      onChange: (snapshot) => update(item.id, { status: snapshot.state, snapshot }),
    });
    jobs.set(item.id, job);
    update(item.id, { status: "starting" });
    await drive(item, job.start({ session: item.session }));
  }

  // The job promises resolve when the job ends *or pauses*, so look at its state afterwards.
  async function drive(item, promise) {
    await promise;
    const job = jobs.get(item.id);
    if (!job || !FINISHED.has(job.state)) return;
    const snapshot = job.snapshot();
    update(item.id, { status: job.state, snapshot, message: snapshot.error });
    if (job.state === "done") {
      notify({ severity: "success", summary: "Upload finished", detail: snapshot.path ?? item.file.name, life: 4000 });
      onUploaded(item.folder);
      refreshSessions();
    } else if (job.state === "failed") {
      notify({ severity: "error", summary: `Upload failed: ${item.file.name}`, detail: snapshot.error, life: 8000 });
      refreshSessions();
    }
    pump();
  }

  async function pause(id) {
    await jobs.get(id)?.pause();
  }

  async function resume(id) {
    const job = jobs.get(id);
    if (job) await drive(find(id), job.resume());
  }

  async function retry(id) {
    const job = jobs.get(id);
    if (job) await drive(find(id), job.retry());
  }

  async function cancel(id) {
    const item = find(id);
    if (!item) return;
    const job = jobs.get(id);
    if (job) {
      await job.cancel();
      update(id, { status: "cancelled", snapshot: job.snapshot() });
    } else {
      update(id, { status: "cancelled" });
    }
    refreshSessions();
    pump();
  }

  function dismiss(id) {
    const item = find(id);
    if (!item || !FINISHED.has(item.status)) return;
    jobs.delete(id);
    store.set({ items: store.get().items.filter((i) => i.id !== id) });
  }

  // A 409 file_exists is the one failure where the user can decide differently: offer to replace.
  function retryWithOverwrite(id) {
    const item = find(id);
    if (!item || item.status !== "failed" || item.snapshot?.errorCode !== "file_exists") return null;
    dismiss(id);
    const [newId] = enqueue([item.file], { subfolder: item.subfolder, overwrite: true });
    return newId ?? null;
  }

  async function refreshSessions() {
    if (!store.get().enabled) return;
    try {
      const sessions = await client.listUploads();
      store.set({ sessions, sessionsError: null });
    } catch (err) {
      store.set({ sessionsError: err.message });
    }
  }

  // "Resume (select file)": the chosen file must be the one the session was created for.
  async function resumeSession(sessionId, file) {
    const listed = store.get().sessions.find((s) => s.upload_id === sessionId);
    if (!listed) return { ok: false, message: "That upload no longer exists on the server." };
    const fingerprintMatches = listed.client_fingerprint
      ? listed.client_fingerprint === fingerprintOf(file)
      : listed.size === file.size && listed.path.split("/").pop() === file.name;
    if (!fingerprintMatches) {
      return {
        ok: false,
        message:
          `"${file.name}" is not the file this upload was started with ` +
          `(${listed.path.split("/").pop()}, ${formatSize(listed.size)}). Pick the same, unchanged file.`,
      };
    }
    let session;
    try {
      session = await client.getUpload(sessionId);
    } catch (err) {
      return { ok: false, message: err.message };
    }
    const id = addItem(file, { folder: session.folder, subfolder: "", overwrite: false, session });
    pump();
    return { ok: true, id };
  }

  async function discardSession(sessionId) {
    try {
      await client.cancelUpload(sessionId);
    } catch (err) {
      if (err.status !== 404) notify({ severity: "error", summary: "Could not discard the upload", detail: err.message, life: 8000 });
    }
    await refreshSessions();
  }

  return {
    store,
    setConfig,
    enqueue,
    pause,
    resume,
    retry,
    cancel,
    dismiss,
    retryWithOverwrite,
    refreshSessions,
    resumeSession,
    discardSession,
    hasActive: () => store.get().items.some((i) => ACTIVE.has(i.status) || i.status === "queued"),
    // Sessions that another tab or an earlier page load started, not the ones running here.
    unfinishedSessions() {
      const mine = new Set(
        store.get().items.filter((i) => !FINISHED.has(i.status)).map((i) => i.snapshot?.uploadId ?? i.session?.upload_id).filter(Boolean),
      );
      return store.get().sessions.filter((s) => !mine.has(s.upload_id));
    },
  };
}
