// The browser services the uploader needs, bundled so tests can swap them for fakes.
// Nothing here runs at import time.

export function createBrowserEnv() {
  return {
    now: () => performance.now(),
    random: () => Math.random(),
    sleep: (ms, signal) =>
      new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, ms);
        signal?.addEventListener(
          "abort",
          () => {
            clearTimeout(timer);
            reject(new Error("aborted"));
          },
          { once: true },
        );
      }),
    createXhr: () => new XMLHttpRequest(),
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
    isOnline: () => navigator.onLine !== false,
    // Resolves on the browser's "online" event, so a dropped connection costs no retries.
    waitForOnline: (signal) =>
      new Promise((resolve, reject) => {
        if (navigator.onLine !== false) return resolve();
        const onOnline = () => {
          signal?.removeEventListener("abort", onAbort);
          resolve();
        };
        const onAbort = () => {
          window.removeEventListener("online", onOnline);
          reject(new Error("aborted"));
        };
        window.addEventListener("online", onOnline, { once: true });
        signal?.addEventListener("abort", onAbort, { once: true });
      }),
    // Keeps the screen (and so the tab) awake during a long upload; absent in some browsers.
    requestWakeLock: navigator.wakeLock ? () => navigator.wakeLock.request("screen") : undefined,
    // Only exists in secure contexts (HTTPS or localhost), so per-chunk hashing may be unavailable.
    subtle: globalThis.crypto?.subtle ?? null,
  };
}
