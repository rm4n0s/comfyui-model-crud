import assert from "node:assert/strict";
import { test } from "node:test";
import {
  createStore, duplicatePaths, filterFiles, formatDate, formatSize, sortFiles, splitPath, summarize,
} from "../../web/state.js";

const file = (path, size = 1, modified = "2026-01-01T00:00:00Z", root_index = 0) => ({
  path, name: path.split("/").pop(), size, modified, root_index,
});

test("formatSize uses binary units and sensible precision", () => {
  assert.equal(formatSize(0), "0 B");
  assert.equal(formatSize(1023), "1023 B");
  assert.equal(formatSize(1024), "1.00 KiB");
  assert.equal(formatSize(1536), "1.50 KiB");
  assert.equal(formatSize(15 * 1024 * 1024), "15.0 MiB");
  assert.equal(formatSize(171966520), "164 MiB");
  assert.equal(formatSize(5 * 1024 ** 3), "5.00 GiB");
  assert.equal(formatSize(3 * 1024 ** 4), "3.00 TiB");
  assert.equal(formatSize(5000 * 1024 ** 4), "5000 TiB");
  assert.equal(formatSize(-1), "?");
  assert.equal(formatSize(NaN), "?");
  assert.equal(formatSize(undefined), "?");
});

test("formatDate falls back to the raw text for garbage", () => {
  assert.equal(formatDate("not a date"), "not a date");
  assert.notEqual(formatDate("2026-10-01T12:30:00Z"), "2026-10-01T12:30:00Z");
});

test("sortFiles sorts names naturally and case-insensitively", () => {
  const files = [file("b10.safetensors"), file("B2.safetensors"), file("a.safetensors"), file("b1.safetensors")];
  assert.deepEqual(sortFiles(files, "name").map((f) => f.path), [
    "a.safetensors", "b1.safetensors", "B2.safetensors", "b10.safetensors",
  ]);
  assert.deepEqual(sortFiles(files, "name", "desc").map((f) => f.path).slice(0, 1), ["b10.safetensors"]);
});

test("sortFiles by size and modified, with stable tie-breaks and no mutation", () => {
  const files = [
    file("c", 5, "2026-03-01T00:00:00Z"),
    file("a", 5, "2026-01-01T00:00:00Z"),
    file("b", 1, "2026-02-01T00:00:00Z"),
  ];
  const copy = structuredClone(files);
  assert.deepEqual(sortFiles(files, "size").map((f) => f.path), ["b", "a", "c"]);
  assert.deepEqual(sortFiles(files, "size", "desc").map((f) => f.path), ["a", "c", "b"]);
  assert.deepEqual(sortFiles(files, "modified").map((f) => f.path), ["a", "b", "c"]);
  assert.deepEqual(sortFiles(files, "modified", "desc").map((f) => f.path), ["c", "b", "a"]);
  assert.deepEqual(files, copy);
});

test("duplicates are ordered by root index", () => {
  const files = [file("x", 1, "2026-01-01T00:00:00Z", 1), file("x", 1, "2026-01-01T00:00:00Z", 0)];
  assert.deepEqual(sortFiles(files, "name").map((f) => f.root_index), [0, 1]);
});

test("filterFiles needs every term, in any order, ignoring case", () => {
  const files = [file("sdxl/Detail_Tweaker.safetensors"), file("sd15/detail.safetensors"), file("vae.pt")];
  assert.equal(filterFiles(files, "").length, 3);
  assert.equal(filterFiles(files, "   ").length, 3);
  assert.deepEqual(filterFiles(files, "DETAIL").map((f) => f.path), [
    "sdxl/Detail_Tweaker.safetensors", "sd15/detail.safetensors",
  ]);
  assert.deepEqual(filterFiles(files, "sdxl tweaker").map((f) => f.path), ["sdxl/Detail_Tweaker.safetensors"]);
  assert.deepEqual(filterFiles(files, "tweaker sdxl").map((f) => f.path), ["sdxl/Detail_Tweaker.safetensors"]);
  assert.equal(filterFiles(files, "nothing").length, 0);
});

test("filterFiles with several terms is an AND, not an OR", () => {
  const files = [file("sdxl/detail.safetensors"), file("sd15/detail.safetensors"), file("vae.pt")];
  assert.deepEqual(filterFiles(files, "detail sdxl").map((f) => f.path), ["sdxl/detail.safetensors"]);
  assert.equal(filterFiles(files, "detail vae").length, 0);
});

test("filterFiles treats the text literally (no regex)", () => {
  const files = [file("a.b"), file("axb")];
  assert.deepEqual(filterFiles(files, "a.b").map((f) => f.path), ["a.b"]);
  assert.equal(filterFiles(files, ".*").length, 0);
  assert.equal(filterFiles(files, "(").length, 0);
});

test("summarize and duplicatePaths", () => {
  assert.deepEqual(summarize([]), { count: 0, bytes: 0 });
  assert.deepEqual(summarize([file("a", 10), file("b", 5)]), { count: 2, bytes: 15 });
  const dupes = duplicatePaths([file("a", 1, "", 0), file("b"), file("a", 1, "", 1), file("a", 1, "", 2)]);
  assert.deepEqual([...dupes], ["a"]);
});

test("splitPath", () => {
  assert.deepEqual(splitPath("m.safetensors"), { dir: "", name: "m.safetensors" });
  assert.deepEqual(splitPath("a/b/m.safetensors"), { dir: "a/b", name: "m.safetensors" });
});

test("store notifies subscribers with merged state until they unsubscribe", () => {
  const store = createStore({ a: 1, b: 2 });
  const seen = [];
  const stop = store.subscribe((s) => seen.push(s));
  store.set({ b: 3 });
  stop();
  store.set({ a: 9 });
  assert.deepEqual(seen, [{ a: 1, b: 3 }]);
  assert.deepEqual(store.get(), { a: 9, b: 3 });
});
