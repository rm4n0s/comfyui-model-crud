// Client for the /model_crud REST API (SPEC §6). It takes ComfyUI's `api` object as an argument
// instead of importing it, so this file has no import-time side effects (ComfyUI loads every .js
// under web/ as an extension) and can be tested without the frontend.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function createClient(api) {
  async function request(path, init) {
    let response;
    try {
      response = await api.fetchApi(path, init);
    } catch (err) {
      throw new ApiError(0, "network_error", "Cannot reach the ComfyUI server.");
    }
    let body = null;
    try {
      body = await response.json();
    } catch (err) {
      // Not JSON (a proxy error page, for example); fall through to the generic message.
    }
    if (!response.ok) {
      throw new ApiError(
        response.status,
        body?.error ?? "http_error",
        body?.message ?? `The server answered HTTP ${response.status}.`,
      );
    }
    return body;
  }

  return {
    getConfig: () => request("/model_crud/config"),
    listFolders: async () => (await request("/model_crud/folders")).folders,
    listModels: async (folder) =>
      (await request(`/model_crud/models/${encodeURIComponent(folder)}`)).files,
    // `path` goes in the query string, as the API expects (SPEC §6.5); root_index picks the
    // right copy when the same relative path exists in more than one model root.
    deleteModel: (folder, path, rootIndex = 0) =>
      request(
        `/model_crud/models/${encodeURIComponent(folder)}` +
          `?path=${encodeURIComponent(path)}&root_index=${encodeURIComponent(rootIndex)}`,
        { method: "DELETE" },
      ),
  };
}
