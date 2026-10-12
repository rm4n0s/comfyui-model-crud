// DOM for the "Model files" tab. Every string that comes from the server (file names, error
// messages) is assigned with textContent / title / aria-label, never as HTML (SR-11).

import {
  duplicatePaths,
  fileKey,
  filterFiles,
  formatDate,
  formatSize,
  SORT_KEYS,
  sortFiles,
  splitPath,
  summarize,
} from "./state.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const COLUMNS = { name: "Name", size: "Size", modified: "Modified" };

export function createView(controller) {
  const { store } = controller;

  const root = el("div", "model-crud-panel");

  const toolbar = el("div", "model-crud-toolbar");
  const folderSelect = el("select", "model-crud-folder");
  folderSelect.setAttribute("aria-label", "Model folder");
  folderSelect.addEventListener("change", () => controller.selectFolder(folderSelect.value));
  const filterInput = el("input", "model-crud-filter");
  filterInput.type = "search";
  filterInput.placeholder = "Filter by name";
  filterInput.setAttribute("aria-label", "Filter files by name");
  filterInput.addEventListener("input", () => controller.setFilter(filterInput.value));
  const refreshButton = el("button", "model-crud-button model-crud-refresh", "Refresh");
  refreshButton.type = "button";
  refreshButton.title = "Reload the file list from the server";
  refreshButton.addEventListener("click", () => controller.refresh());
  toolbar.append(folderSelect, filterInput, refreshButton);

  const summary = el("div", "model-crud-summary");
  summary.setAttribute("role", "status");
  const notice = el("div", "model-crud-notice");
  notice.setAttribute("role", "alert");

  const wrap = el("div", "model-crud-tablewrap");
  const table = el("table", "model-crud-table");
  const head = el("thead");
  const headRow = el("tr");
  const sortHeaders = {};
  for (const key of SORT_KEYS) {
    const th = el("th", `model-crud-col-${key}`);
    const button = el("button", "model-crud-sort", COLUMNS[key]);
    button.type = "button";
    button.addEventListener("click", () => controller.setSort(key));
    th.append(button);
    sortHeaders[key] = th;
    headRow.append(th);
  }
  const actionsHead = el("th", "model-crud-col-actions");
  const actionsLabel = el("span", "model-crud-visually-hidden", "Actions");
  actionsHead.append(actionsLabel);
  headRow.append(actionsHead);
  head.append(headRow);
  const body = el("tbody");
  table.append(head, body);
  wrap.append(table);

  const more = el("button", "model-crud-button model-crud-more");
  more.type = "button";
  more.addEventListener("click", () => controller.showMore());

  root.append(toolbar, summary, notice, wrap, more);

  function renderRow(file, dupes, state) {
    const { dir, name } = splitPath(file.path);
    const row = el("tr", "model-crud-row");

    const nameCell = el("td", "model-crud-cell-name");
    nameCell.title = file.path;
    nameCell.append(el("span", "model-crud-name", name));
    if (dir) nameCell.append(el("span", "model-crud-dir", dir));
    if (dupes.has(file.path)) {
      const badge = el("span", "model-crud-badge", `root ${file.root_index}`);
      badge.title = "The same path exists in more than one model folder on the server";
      nameCell.append(badge);
    }

    const sizeCell = el("td", "model-crud-cell-size", formatSize(file.size));
    sizeCell.title = `${file.size.toLocaleString()} bytes`;
    const modifiedCell = el("td", "model-crud-cell-modified", formatDate(file.modified));

    const actionCell = el("td", "model-crud-cell-actions");
    const remove = el("button", "model-crud-button model-crud-delete", "Delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete ${file.path}`);
    const allowed = state.config?.delete_enabled === true;
    const busy = state.deleting.has(fileKey(file));
    remove.disabled = !allowed || busy;
    remove.title = allowed
      ? busy
        ? "Deleting…"
        : "Delete this file from the server"
      : "Deleting is disabled on this server (MODEL_CRUD_READ_ONLY or MODEL_CRUD_ALLOW_DELETE)";
    remove.addEventListener("click", () => controller.requestDelete(file));
    actionCell.append(remove);

    row.append(nameCell, sizeCell, modifiedCell, actionCell);
    return row;
  }

  function render(state) {
    // Folder picker
    const folderKeys = state.folders.map((f) => f.key);
    if ([...folderSelect.options].map((o) => o.value).join("\n") !== folderKeys.join("\n")) {
      folderSelect.replaceChildren(
        ...folderKeys.map((key) => {
          const option = el("option", "", key);
          option.value = key;
          return option;
        }),
      );
    }
    folderSelect.value = state.folder ?? "";
    folderSelect.disabled = state.folders.length === 0;
    if (filterInput.value !== state.filter) filterInput.value = state.filter;
    refreshButton.disabled = state.loading;

    // Rows
    const visible = sortFiles(filterFiles(state.files, state.filter), state.sortKey, state.sortDir);
    const shown = visible.slice(0, state.limit);
    const dupes = duplicatePaths(state.files);
    body.replaceChildren(...shown.map((file) => renderRow(file, dupes, state)));

    // Sort indicators
    for (const key of SORT_KEYS) {
      sortHeaders[key].setAttribute(
        "aria-sort",
        key === state.sortKey ? (state.sortDir === "asc" ? "ascending" : "descending") : "none",
      );
    }

    // Summary and notices
    const total = summarize(state.files);
    const filtered = state.filter.trim() !== "";
    if (state.loading) {
      summary.textContent = "Loading…";
    } else if (state.error) {
      summary.textContent = "";
    } else if (!state.folder) {
      summary.textContent = "No model folders found.";
    } else if (total.count === 0) {
      summary.textContent = "No model files in this folder.";
    } else {
      const shownTotal = summarize(visible);
      summary.textContent = filtered
        ? `${shownTotal.count} of ${total.count} files · ${formatSize(shownTotal.bytes)} of ${formatSize(total.bytes)}`
        : `${total.count} ${total.count === 1 ? "file" : "files"} · ${formatSize(total.bytes)}`;
    }
    const readOnly = state.config && state.config.delete_enabled === false;
    notice.textContent = state.error
      ? state.error
      : readOnly
        ? "This server does not allow deleting models."
        : "";
    notice.classList.toggle("model-crud-notice-error", Boolean(state.error));
    notice.hidden = notice.textContent === "";

    more.hidden = visible.length <= shown.length;
    more.textContent = `Show ${Math.min(visible.length - shown.length, 300)} more (${visible.length - shown.length} hidden)`;
    wrap.hidden = state.files.length === 0;
  }

  const unsubscribe = store.subscribe(render);
  render(store.get());

  return {
    element: root,
    destroy() {
      unsubscribe();
      controller.destroy();
    },
  };
}
