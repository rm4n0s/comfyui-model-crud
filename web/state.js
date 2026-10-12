// Pure helpers and a tiny observable store. No DOM, no network.

const UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

export function formatSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "?";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${UNITS[unit]}`;
}

export function formatDate(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}

export const SORT_KEYS = ["name", "size", "modified"];

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

export function sortFiles(files, key, direction = "asc") {
  const sign = direction === "desc" ? -1 : 1;
  const compare = {
    name: (a, b) => collator.compare(a.path, b.path),
    size: (a, b) => a.size - b.size,
    modified: (a, b) => (a.modified < b.modified ? -1 : a.modified > b.modified ? 1 : 0),
  }[key];
  return [...files].sort(
    (a, b) => sign * compare(a, b) || collator.compare(a.path, b.path) || a.root_index - b.root_index,
  );
}

// Every whitespace-separated term must appear in the path, case-insensitively.
export function filterFiles(files, text) {
  const terms = text.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return files;
  return files.filter((f) => {
    const haystack = f.path.toLowerCase();
    return terms.every((t) => haystack.includes(t));
  });
}

export function summarize(files) {
  return { count: files.length, bytes: files.reduce((sum, f) => sum + f.size, 0) };
}

// Relative paths that exist in more than one model root, so the UI can say which copy is which.
export function duplicatePaths(files) {
  const seen = new Set();
  const dupes = new Set();
  for (const f of files) {
    (seen.has(f.path) ? dupes : seen).add(f.path);
  }
  return dupes;
}

export function fileKey(file) {
  return `${file.root_index}\u0000${file.path}`;
}

export function splitPath(path) {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? { dir: "", name: path } : { dir: path.slice(0, slash), name: path.slice(slash + 1) };
}

export function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...patch };
      for (const listener of listeners) listener(state);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
