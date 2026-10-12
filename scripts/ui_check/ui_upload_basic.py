import os, sys, time
sys.path.insert(0, os.environ["SP"])
from ui_lib import *
BENCH = f"{ROOT}/.bench"
DEST = f"{BENCH}/models/loras"
d = start_browser(); open_tab(d); pick_folder(d, "loras")
res = []
def check(label, cond, detail=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + label + (f"  [{str(detail)[:200]}]" if detail != "" else ""), flush=True)

check("upload controls are visible", d.find_element(By.CSS_SELECTOR, ".model-crud-upload-controls").is_displayed())
check("the hint names the target folder", "loras" in d.find_element(By.CSS_SELECTOR, ".model-crud-upload-hint").text, d.find_element(By.CSS_SELECTOR, ".model-crud-upload-hint").text)
check("overwrite checkbox is off by default", d.execute_script("return document.querySelector('.model-crud-overwrite input').checked") is False)

# A: real file input, 30 MB = 4 chunks
src = f"{BENCH}/src/small30m.safetensors"
choose_files(d, [src])
st = wait_for(lambda: (lambda s: s if s and s["status"] == "done" else None)(item_state(d, "small30m.safetensors")), 60, "upload done")
check("upload through the file input finishes", st["status"] == "done", st)
check("bytes on the server are identical", sha256_file(f"{DEST}/small30m.safetensors") == sha256_file(src))
check("no temp files left on the server", not [n for n in os.listdir(DEST) if n.startswith(".model_crud_")], os.listdir(DEST))
wait_for(lambda: "small30m.safetensors" in [r["name"] for r in rows(d)], 10, "file in table")
check("the file list refreshed by itself and shows the file", True)
d.execute_script("window.__lora = LiteGraph.createNode('LoraLoader'); app.graph.add(window.__lora);")
vals = d.execute_async_script("const done = arguments[0]; app.refreshComboInNodes().then(() => done(window.__lora.widgets[0].options.values))")
check("the new file is in a LoraLoader dropdown after refresh", "small30m.safetensors" in vals, vals)
time.sleep(0.5); d.save_screenshot(f"{SP}/ui_8_upload_done.png")

# C: 409 and retry with overwrite
os.makedirs(f"{BENCH}/src2", exist_ok=True)
choose_files(d, [src])
st = wait_for(lambda: (lambda s: s if s and s["status"] == "failed" else None)(item_state(d, "small30m.safetensors")), 30, "conflict")
check("uploading an existing name fails with the server's message", "already exists" in st["text"], st["text"])
check("the row offers 'Retry with overwrite' and Dismiss", "Retry with overwrite" in st["actions"] and "Dismiss" in st["actions"], st["actions"])
check("the existing file was not touched", sha256_file(f"{DEST}/small30m.safetensors") == sha256_file(src))
d.save_screenshot(f"{SP}/ui_9_conflict.png")
# new content under the same name and mtime-less: modify the file so the replacement is detectable
with open(src, "r+b") as f: f.seek(0); f.write(b"CHANGED!")
new_hash = sha256_file(src)
click_action(d, "small30m.safetensors", "Dismiss")
d.execute_script("document.querySelector('.model-crud-overwrite input').click()")  # tick overwrite
choose_files(d, [src])
wait_for(lambda: (lambda s: s and s["status"] == "done")(item_state(d, "small30m.safetensors")), 60, "overwrite done")
check("with the overwrite checkbox the file is replaced", sha256_file(f"{DEST}/small30m.safetensors") == new_hash)
d.execute_script("document.querySelector('.model-crud-overwrite input').click()")  # untick

# B: drag and drop of an in-page file onto the panel
d.execute_script("""
  const bytes = new Uint8Array(3 * 1024 * 1024 + 5); crypto.getRandomValues(bytes.subarray(0, 65536));
  window.__dropped = new File([bytes], 'dropped one.safetensors', {lastModified: 1700000000000});
  window.__droppedBytes = bytes;
  const dt = new DataTransfer(); dt.items.add(window.__dropped);
  const target = document.querySelector('.model-crud-host');
  for (const type of ['dragenter', 'dragover']) target.dispatchEvent(new DragEvent(type, {dataTransfer: dt, bubbles: true, cancelable: true}));
  window.__dragClass = target.classList.contains('model-crud-dropping');
  window.__bubbled = false; document.addEventListener('drop', () => { window.__bubbled = true });
  target.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
""")
check("dragging a file over the panel highlights it", d.execute_script("return window.__dragClass"))
wait_for(lambda: (lambda s: s and s["status"] == "done")(item_state(d, "dropped one.safetensors")), 60, "drop upload")
check("dropping a file uploads it (name with spaces)", os.path.exists(f"{DEST}/dropped one.safetensors") and os.path.getsize(f"{DEST}/dropped one.safetensors") == 3 * 1024 * 1024 + 5)
check("the highlight is removed after the drop", d.execute_script("return !document.querySelector('.model-crud-host').classList.contains('model-crud-dropping')"))
check("the drop did not bubble to ComfyUI's own handlers", d.execute_script("return window.__bubbled === false"))

# validation messages in the UI
d.execute_script("""const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(10)], 'virus.exe')); dt.items.add(new File([], 'empty.safetensors'));
  document.querySelector('.model-crud-host').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}))""")
time.sleep(0.5)
bad = {i["name"]: i for i in items(d)}
check("a disallowed extension and an empty file are rejected locally with a clear message", "not allowed" in bad["virus.exe"]["text"] and "empty" in bad["empty.safetensors"]["text"], [bad["virus.exe"]["text"], bad["empty.safetensors"]["text"]])
# hostile name in the queue
d.execute_script("""const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(100)], '<img src=x onerror=window.__xss2=1>.safetensors'));
  document.querySelector('.model-crud-host').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}))""")
time.sleep(1.5)
check("a hostile file name is inert text in the queue", d.execute_script("return window.__xss2 === undefined && !document.querySelector('.model-crud-upload img')"))
d.save_screenshot(f"{SP}/ui_10_queue.png")
errs = [e for e in d.execute_script("return window.__errs") if "model" in e.lower() or "Crud" in e]
check("no console errors mention the extension", not errs, errs)
d.quit(); print(f"{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
