// Extension entry point: registers the "Model files" sidebar tab (FR-UI1).
//
// ComfyUI serves this directory at /extensions/<folder name>/, so the frontend modules live at
// ../../scripts/ relative to this file. Use only official frontend APIs (docs/comfyui-notes.md).

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createClient } from "./api.js";
import { createController } from "./controller.js";
import { createView } from "./view.js";

const STYLESHEET = new URL("./model_crud.css", import.meta.url).href;

function ensureStylesheet() {
  if (document.querySelector("link[data-model-crud]")) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = STYLESHEET;
  link.dataset.modelCrud = "";
  document.head.append(link);
}

app.registerExtension({
  name: "ModelCrud.Sidebar",
  setup() {
    const manager = app.extensionManager;
    if (typeof manager?.registerSidebarTab !== "function") {
      console.warn("[ModelCrud] This ComfyUI frontend has no sidebar tab API; the model file tab is unavailable.");
      return;
    }
    ensureStylesheet();
    const client = createClient(api);
    let mounted = null;

    manager.registerSidebarTab({
      id: "model-crud",
      icon: "pi pi-folder-open",
      title: "Model files",
      tooltip: "List and delete model files on the server",
      label: "Files",
      type: "custom",
      render(container) {
        const controller = createController({
          client,
          confirm: (options) => manager.dialog.confirm(options),
          toast: (message) => manager.toast.add(message),
          storage: window.localStorage,
        });
        mounted = createView(controller);
        container.replaceChildren(mounted.element);
        controller.init();
      },
      destroy() {
        mounted?.destroy();
        mounted = null;
      },
    });
  },
});
