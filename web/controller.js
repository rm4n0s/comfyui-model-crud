// Application logic of the "Model files" tab. Everything it needs from the outside (the API
// client, confirm dialog, toasts, storage) is passed in, so it runs under Node in the tests.

import { createStore, fileKey, splitPath } from "./state.js";

const PAGE = 300;
const FOLDER_KEY = "ModelCrud.folder";

export function createController({ client, confirm, toast, storage }) {
  const store = createStore({
    config: null,
    folders: [],
    folder: null,
    files: [],
    loading: true,
    error: null,
    filter: "",
    sortKey: "name",
    sortDir: "asc",
    limit: PAGE,
    deleting: new Set(),
  });
  let generation = 0; // a newer request makes older answers irrelevant
  let destroyed = false;

  const remember = (folder) => {
    try {
      storage?.setItem(FOLDER_KEY, folder);
    } catch (err) {
      // Storage may be blocked; remembering the folder is only a convenience.
    }
  };
  const recall = () => {
    try {
      return storage?.getItem(FOLDER_KEY) ?? null;
    } catch (err) {
      return null;
    }
  };

  async function loadFiles() {
    const { folder } = store.get();
    if (!folder) {
      store.set({ files: [], loading: false });
      return;
    }
    const mine = ++generation;
    store.set({ loading: true, error: null });
    try {
      const files = await client.listModels(folder);
      if (mine !== generation || destroyed) return;
      store.set({ files, loading: false, limit: PAGE });
    } catch (err) {
      if (mine !== generation || destroyed) return;
      store.set({ files: [], loading: false, error: err.message });
    }
  }

  async function init() {
    try {
      const [config, folders] = await Promise.all([client.getConfig(), client.listFolders()]);
      if (destroyed) return;
      const keys = folders.map((f) => f.key);
      const remembered = recall();
      const folder = keys.includes(remembered)
        ? remembered
        : keys.includes("checkpoints")
          ? "checkpoints"
          : (keys[0] ?? null);
      store.set({ config, folders, folder });
    } catch (err) {
      if (destroyed) return;
      store.set({ loading: false, error: err.message });
      return;
    }
    await loadFiles();
  }

  async function selectFolder(folder) {
    if (folder === store.get().folder) return;
    remember(folder);
    store.set({ folder, files: [], filter: "" });
    await loadFiles();
  }

  function setFilter(filter) {
    store.set({ filter, limit: PAGE });
  }

  function setSort(key) {
    const { sortKey, sortDir } = store.get();
    store.set({
      sortKey: key,
      sortDir: key === sortKey && sortDir === "asc" ? "desc" : "asc",
      limit: PAGE,
    });
  }

  function showMore() {
    store.set({ limit: store.get().limit + PAGE });
  }

  async function requestDelete(file) {
    const { config, folder, deleting } = store.get();
    if (!config?.delete_enabled || deleting.has(fileKey(file))) return;

    const { name } = splitPath(file.path);
    const confirmed = await confirm({
      title: "Delete model file?",
      message:
        `Permanently delete "${file.path}" from the "${folder}" folder on the server? ` +
        "This cannot be undone.",
      type: "delete",
    });
    if (!confirmed) return;

    const key = fileKey(file);
    store.set({ deleting: new Set(store.get().deleting).add(key) });
    try {
      await client.deleteModel(folder, file.path, file.root_index);
      toast({ severity: "success", summary: "Model deleted", detail: name, life: 4000 });
    } catch (err) {
      toast({
        severity: "error",
        summary: "Could not delete the model",
        detail: err.message,
        life: 8000,
      });
    } finally {
      const next = new Set(store.get().deleting);
      next.delete(key);
      store.set({ deleting: next });
    }
    // Refresh either way: after an error the list may be out of date (for example a 404).
    if (store.get().folder === folder) await loadFiles();
  }

  return {
    store,
    init,
    selectFolder,
    refresh: loadFiles,
    setFilter,
    setSort,
    showMore,
    requestDelete,
    destroy() {
      destroyed = true;
    },
  };
}
