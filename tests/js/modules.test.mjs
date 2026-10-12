import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const webDir = new URL("../../web/", import.meta.url);
const jsFiles = readdirSync(fileURLToPath(webDir)).filter((f) => f.endsWith(".js"));

// ComfyUI loads EVERY .js file under web/ as an extension, so the helpers must be inert on import.
test("helper modules import without a DOM, a frontend or any side effect", async () => {
  const before = Object.keys(globalThis);
  for (const file of jsFiles.filter((f) => f !== "model_crud.js")) {
    await import(new URL(file, webDir));
  }
  assert.deepEqual(Object.keys(globalThis).sort(), before.sort());
});

test("only the entry point talks to the ComfyUI frontend", () => {
  for (const file of jsFiles) {
    const source = readFileSync(new URL(file, webDir), "utf8");
    const touchesFrontend = /scripts\/(app|api)\.js|registerExtension|registerSidebarTab/.test(source);
    assert.equal(touchesFrontend, file === "model_crud.js", file);
  }
});

test("server-provided text never reaches innerHTML or other HTML sinks", () => {
  for (const file of jsFiles) {
    const source = readFileSync(new URL(file, webDir), "utf8");
    assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/.test(source), file);
  }
});

test("all CSS classes are namespaced with model-crud-", () => {
  const css = readFileSync(new URL("model_crud.css", webDir), "utf8");
  const classes = [...css.matchAll(/\.([A-Za-z_][\w-]*)/g)].map((m) => m[1]);
  assert.ok(classes.length > 10);
  assert.deepEqual(classes.filter((c) => !c.startsWith("model-crud-")), []);
  const view = readFileSync(new URL("view.js", webDir), "utf8");
  for (const [, list] of view.matchAll(/el\("[a-z]+", "([^"]+)"/g)) {
    for (const cls of list.split(" ")) assert.ok(cls.startsWith("model-crud-"), cls);
  }
});

test("no build step: the entry point only uses relative ES module imports and the frontend shims", () => {
  for (const file of jsFiles) {
    const source = readFileSync(new URL(file, webDir), "utf8");
    for (const [, spec] of source.matchAll(/^import .* from "([^"]+)";?$/gm)) {
      assert.ok(spec.startsWith("./") || spec.startsWith("../../scripts/"), `${file}: ${spec}`);
    }
  }
});

test("the scheduler and the queue manager never touch browser globals", () => {
  for (const file of ["uploader.js", "upload_manager.js", "controller.js", "state.js", "api.js"]) {
    const source = readFileSync(new URL(file, webDir), "utf8");
    assert.ok(!/\b(document|window|navigator|localStorage|XMLHttpRequest|fetch)\b/.test(source.replace(/\/\/.*$/gm, "")), file);
  }
});
