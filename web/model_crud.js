// Extension entry point: registers the "Model files" sidebar tab (FR-UI1) and its settings.
//
// ComfyUI serves this directory at /extensions/<folder name>/, so the frontend modules live at
// ../../scripts/ relative to this file. Use only official frontend APIs (docs/comfyui-notes.md).

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createClient } from "./api.js";
import { createController } from "./controller.js";
import { createBrowserEnv } from "./env.js";
import { createUploadManager } from "./upload_manager.js";
import { createUploadView } from "./upload_view.js";
import { createView } from "./view.js";

const STYLESHEET = new URL("./model_crud.css", import.meta.url).href;
const SETTING_PARALLEL = "ModelCrud.Upload.Parallel";
const SETTING_CHUNK_HASH = "ModelCrud.Upload.ChunkHash";

function ensureStylesheet() {
  if (document.querySelector("link[data-model-crud]")) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = STYLESHEET;
  link.dataset.modelCrud = "";
  document.head.append(link);
}

function setting(id, fallback) {
  try {
    return app.extensionManager.setting.get(id) ?? fallback;
  } catch (err) {
    return fallback;
  }
}

app.registerExtension({
  name: "ModelCrud.Sidebar",
  settings: [
    {
      id: SETTING_PARALLEL,
      category: ["Model files", "Upload", "Parallel chunks"],
      name: "Chunks sent in parallel per upload",
      tooltip: "More parallel chunks can use a fast or high-latency link better. The server caps this (MODEL_CRUD_MAX_PARALLEL).",
      type: "slider",
      attrs: { min: 1, max: 8, step: 1 },
      defaultValue: 3,
    },
    {
      id: SETTING_CHUNK_HASH,
      category: ["Model files", "Upload", "Verify chunks"],
      name: "Verify every chunk with SHA-256",
      tooltip: "The server rejects a chunk that was damaged in transit and it is sent again. Costs CPU and needs HTTPS or localhost.",
      type: "boolean",
      defaultValue: false,
    },
  ],
  setup() {
    const manager = app.extensionManager;
    if (typeof manager?.registerSidebarTab !== "function") {
      console.warn("[ModelCrud] This ComfyUI frontend has no sidebar tab API; the model file tab is unavailable.");
      return;
    }
    ensureStylesheet();
    const client = createClient(api);
    let mounted = null;
    let controller = null; // the list controller of the tab while it is open

    // The upload queue lives as long as the page: closing the tab (switching to another sidebar
    // panel) must not orphan a running transfer. Only the views come and go.
    const uploads = createUploadManager({
      client,
      env: createBrowserEnv(),
      getFolder: () => controller?.store.get().folder ?? null,
      getOptions: () => ({
        parallel: Number(setting(SETTING_PARALLEL, 3)),
        chunkHash: Boolean(setting(SETTING_CHUNK_HASH, false)),
      }),
      onUploaded: (folder) => {
        if (controller?.store.get().folder === folder) controller.refresh();
      },
      notify: (message) => manager.toast.add(message),
    });

    // Reloading or closing the page stops a running transfer (it stays resumable).
    window.addEventListener("beforeunload", (event) => {
      if (!uploads.hasActive()) return;
      event.preventDefault();
      event.returnValue = "";
    });

    manager.registerSidebarTab({
      id: "model-crud",
      icon: "pi pi-folder-open",
      title: "Model files",
      tooltip: "List, upload and delete model files on the server",
      label: "Files",
      type: "custom",
      render(container) {
        controller = createController({
          client,
          confirm: (options) => manager.dialog.confirm(options),
          toast: (message) => manager.toast.add(message),
          storage: window.localStorage,
        });
        const host = document.createElement("div");
        host.className = "model-crud-host";
        const uploadView = createUploadView(uploads, {
          dropTarget: host,
          getFolder: () => controller?.store.get().folder ?? null,
        });
        mounted = createView(controller, uploadView);
        host.append(mounted.element);
        container.replaceChildren(host);

        let configured = null;
        controller.store.subscribe((state) => {
          if (state.config && state.config !== configured) {
            configured = state.config;
            uploads.setConfig(state.config);
          }
        });
        controller.init();
      },
      destroy() {
        mounted?.destroy();
        mounted = null;
        controller = null;
      },
    });
  },
});
