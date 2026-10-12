// DOM for uploading: file picker, drag-and-drop, queue rows and unfinished uploads. Server text
// (file names, error messages) is only ever assigned with textContent / title (SR-11).

import { describeItem } from "./upload_manager.js";
import { formatDate, formatSize } from "./state.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, className, onClick, title) {
  const b = el("button", `model-crud-button ${className}`, label);
  b.type = "button";
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

// Files from a drop, without directories (a dropped folder shows up as a "file" we cannot read).
export function filesFromDrop(dataTransfer) {
  const items = [...(dataTransfer.items ?? [])];
  if (items.length && typeof items[0].webkitGetAsEntry === "function") {
    return items
      .filter((item) => item.kind === "file" && item.webkitGetAsEntry()?.isFile !== false)
      .map((item) => item.getAsFile())
      .filter(Boolean);
  }
  return [...(dataTransfer.files ?? [])];
}

export function createUploadView(manager, { dropTarget, getFolder }) {
  const { store } = manager;
  const root = el("section", "model-crud-upload");

  const disabledNotice = el(
    "div",
    "model-crud-notice",
    "Uploading is disabled on this server (MODEL_CRUD_READ_ONLY or MODEL_CRUD_ALLOW_UPLOAD).",
  );

  const controls = el("div", "model-crud-upload-controls");
  const input = el("input");
  input.type = "file";
  input.multiple = true;
  input.hidden = true;
  const resumeInput = el("input");
  resumeInput.type = "file";
  resumeInput.hidden = true;
  const chooseButton = button("Upload files…", "model-crud-choose", () => input.click(), "Choose model files to upload");
  const hint = el("span", "model-crud-upload-hint");
  const subfolder = el("input", "model-crud-subfolder");
  subfolder.type = "text";
  subfolder.placeholder = "Subfolder (optional)";
  subfolder.setAttribute("aria-label", "Subfolder to upload into");
  subfolder.maxLength = 200;
  const overwriteLabel = el("label", "model-crud-overwrite");
  const overwrite = el("input");
  overwrite.type = "checkbox";
  overwriteLabel.append(overwrite, document.createTextNode(" Replace files that already exist"));
  const options = el("div", "model-crud-upload-options");
  options.append(subfolder, overwriteLabel);
  controls.append(chooseButton, hint, options, input, resumeInput);

  const message = el("div", "model-crud-notice model-crud-notice-error");
  message.setAttribute("role", "alert");
  message.hidden = true;
  const queue = el("ul", "model-crud-queue");
  const sessionsBox = el("div", "model-crud-sessions");
  root.append(disabledNotice, controls, message, queue, sessionsBox);

  const showMessage = (text) => {
    message.textContent = text ?? "";
    message.hidden = !text;
  };

  const startUpload = (files) => {
    showMessage("");
    const accepted = [...files];
    if (accepted.length) manager.enqueue(accepted, { subfolder: subfolder.value.trim(), overwrite: overwrite.checked });
  };
  input.addEventListener("change", () => {
    startUpload(input.files);
    input.value = "";
  });

  // -- queue rows, updated in place so a button keeps focus while progress events arrive --------
  const rows = new Map();

  function actionsFor(item) {
    const code = item.snapshot?.errorCode;
    switch (item.status) {
      case "queued":
        return ["cancel"];
      case "starting":
      case "uploading":
      case "waiting":
        return ["pause", "cancel"];
      case "paused":
        return ["resume", "cancel"];
      case "failed":
        if (code === "file_exists") return ["overwrite", "dismiss"];
        return item.snapshot?.resumable ? ["retry", "cancel", "dismiss"] : ["dismiss"];
      case "done":
      case "cancelled":
        return ["dismiss"];
      default:
        return [];
    }
  }

  function makeAction(name, id) {
    switch (name) {
      case "pause":
        return button("Pause", "model-crud-pause", () => manager.pause(id), "Pause; finished chunks are kept");
      case "resume":
        return button("Resume", "model-crud-resume", () => manager.resume(id));
      case "cancel":
        return button("Cancel", "model-crud-cancel", () => manager.cancel(id), "Stop and delete the partial upload from the server");
      case "retry":
        return button("Retry", "model-crud-retry", () => manager.retry(id), "Try again; finished chunks are not sent twice");
      case "overwrite":
        return button("Retry with overwrite", "model-crud-overwrite-retry", () => manager.retryWithOverwrite(id), "Replace the existing file with this one");
      default:
        return button("Dismiss", "model-crud-dismiss", () => manager.dismiss(id));
    }
  }

  function buildRow(item) {
    const li = el("li", "model-crud-item");
    const title = el("div", "model-crud-item-title");
    const name = el("span", "model-crud-item-name", item.file.name);
    name.title = item.file.name;
    const dest = el("span", "model-crud-item-dest");
    title.append(name, dest);
    const progress = el("progress", "model-crud-progress");
    progress.max = 1000;
    const status = el("div", "model-crud-item-status");
    const actions = el("div", "model-crud-item-actions");
    li.append(title, progress, status, actions);
    return { li, dest, progress, status, actions, actionKey: "" };
  }

  function updateRow(ref, item) {
    const shown = describeItem(item);
    ref.li.dataset.status = item.status;
    ref.li.dataset.tone = shown.tone;
    ref.dest.textContent = `→ ${item.folder}${item.subfolder ? `/${item.subfolder}` : ""}`;
    ref.progress.value = Math.round(shown.fraction * 1000);
    ref.progress.hidden = item.status === "failed" && !(item.snapshot?.sent > 0);
    ref.status.textContent = shown.text;
    const names = actionsFor(item);
    const key = names.join(",");
    if (key !== ref.actionKey) {
      ref.actionKey = key;
      ref.actions.replaceChildren(...names.map((n) => makeAction(n, item.id)));
    }
  }

  // -- unfinished uploads on the server (after a reload, or started elsewhere) ---------------
  let pendingSession = null;
  let sessionSignature = "";
  resumeInput.addEventListener("change", async () => {
    const file = resumeInput.files[0];
    resumeInput.value = "";
    if (!file || pendingSession === null) return;
    const result = await manager.resumeSession(pendingSession, file);
    showMessage(result.ok ? "" : result.message);
    pendingSession = null;
  });

  function renderSessions() {
    const sessions = manager.unfinishedSessions();
    const signature = sessions.map((s) => `${s.upload_id}:${s.received_bytes}`).join("|");
    if (signature === sessionSignature) return;
    sessionSignature = signature;
    if (sessions.length === 0) {
      sessionsBox.replaceChildren();
      return;
    }
    const list = el("ul", "model-crud-session-list");
    for (const s of sessions) {
      const li = el("li", "model-crud-session");
      const name = el("span", "model-crud-item-name", s.path);
      name.title = `${s.folder}/${s.path}`;
      const info = el(
        "span",
        "model-crud-item-status",
        `${s.folder} · ${formatSize(s.received_bytes)} of ${formatSize(s.size)} received · expires ${formatDate(s.expires_at)}`,
      );
      const actions = el("div", "model-crud-item-actions");
      actions.append(
        button("Resume (select file)", "model-crud-session-resume", () => {
          pendingSession = s.upload_id;
          resumeInput.click();
        }, "Choose the same file again to send only what is missing"),
        button("Discard", "model-crud-session-discard", () => manager.discardSession(s.upload_id), "Delete the partial upload from the server"),
      );
      li.append(name, info, actions);
      list.append(li);
    }
    sessionsBox.replaceChildren(el("h3", "model-crud-subheading", "Unfinished uploads"), list);
  }

  function render(state) {
    const folder = getFolder();
    disabledNotice.hidden = state.enabled || state.config === null;
    controls.hidden = !state.enabled;
    chooseButton.disabled = !folder;
    hint.textContent = folder ? `to “${folder}” — or drop files on this panel` : "";

    const live = new Set();
    for (const item of state.items) {
      live.add(item.id);
      let ref = rows.get(item.id);
      if (!ref) {
        ref = buildRow(item);
        rows.set(item.id, ref);
        queue.append(ref.li);
      }
      updateRow(ref, item);
    }
    for (const [id, ref] of rows) {
      if (!live.has(id)) {
        ref.li.remove();
        rows.delete(id);
      }
    }
    renderSessions();
  }

  // -- drag and drop ---------------------------------------------------------------------------
  let dragDepth = 0;
  const isFileDrag = (e) => [...(e.dataTransfer?.types ?? [])].includes("Files");
  const handlers = {
    dragenter(e) {
      if (!store.get().enabled || !isFileDrag(e)) return;
      e.preventDefault();
      dragDepth += 1;
      dropTarget.classList.add("model-crud-dropping");
    },
    dragover(e) {
      if (!store.get().enabled || !isFileDrag(e)) return;
      e.preventDefault(); // allows the drop
      e.dataTransfer.dropEffect = "copy";
    },
    dragleave() {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dropTarget.classList.remove("model-crud-dropping");
    },
    drop(e) {
      if (!store.get().enabled || !isFileDrag(e)) return;
      e.preventDefault();
      e.stopPropagation(); // ComfyUI also listens for drops (to open workflows); not for these
      dragDepth = 0;
      dropTarget.classList.remove("model-crud-dropping");
      startUpload(filesFromDrop(e.dataTransfer));
    },
  };
  for (const [type, fn] of Object.entries(handlers)) dropTarget.addEventListener(type, fn);

  const unsubscribe = store.subscribe(render);
  render(store.get());

  return {
    element: root,
    refresh: () => render(store.get()),
    destroy() {
      unsubscribe();
      for (const [type, fn] of Object.entries(handlers)) dropTarget.removeEventListener(type, fn);
    },
  };
}
