import os, sys, threading, time
sys.path.insert(0, os.environ["SP"])
from ui_lib import *
BENCH = f"{ROOT}/.bench"; DEST = f"{BENCH}/models/loras"
res = []
def check(label, cond, detail=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + label + (f"  [{str(detail)[:230]}]" if detail != "" else ""), flush=True)

kill_server(); server = start_server(); clean_server(DEST)
for n in os.listdir(DEST):
    if n.endswith(".safetensors") and (n.startswith("q") or n.startswith("small")): os.unlink(f"{DEST}/{n}")
d = start_browser(); inject_faults(d); open_tab(d); pick_folder(d, "loras")

# ---- multi-file queue is sequential ----
for i in range(3):
    with open(f"{BENCH}/src/q{i}.safetensors", "wb") as f: f.write(os.urandom(20 * 1024 * 1024))
set_parallel(d, 2); set_net(d, delay=700)
choose_files(d, [f"{BENCH}/src/q{i}.safetensors" for i in range(3)])
time.sleep(0.15)
snap = [i["status"] for i in items(d)]
check("with three files selected, the first runs and the rest wait in the queue", len(snap) == 3 and snap[0] in ("starting", "uploading") and snap[1:] == ["queued", "queued"], snap)
seen_overlap = False
def active_count(): return sum(1 for i in items(d) if i["status"] in ("starting", "uploading", "completing", "waiting", "paused"))
end = time.time() + 60
while time.time() < end and any(i["status"] != "done" for i in items(d)):
    seen_overlap |= active_count() > 1; time.sleep(0.05)
check("never more than one file is uploading at a time", not seen_overlap)
check("all three arrived intact", all(sha256_file(f"{DEST}/q{i}.safetensors") == sha256_file(f"{BENCH}/src/q{i}.safetensors") for i in range(3)))
set_net(d, delay=0)
for n in range(3): click_action(d, f"q{n}.safetensors", "Dismiss")

# ---- Retry with overwrite button ----
src = f"{BENCH}/src/q0.safetensors"
choose_files(d, [src]); st = wait_state(d, "q0.safetensors", ["failed"], 20)
check("uploading an existing name offers 'Retry with overwrite'", "Retry with overwrite" in st["actions"], st["actions"])
with open(src, "r+b") as f: f.seek(5); f.write(b"NEWDATA")
want = sha256_file(src)
click_action(d, "q0.safetensors", "Retry with overwrite")
wait_state(d, "q0.safetensors", ["done"], 30)
check("clicking 'Retry with overwrite' replaces the file", sha256_file(f"{DEST}/q0.safetensors") == want)
d.quit()

# ---- browser memory with a 300 MiB file ----
d = start_browser(); open_tab(d); pick_folder(d, "loras")
SRC = f"{BENCH}/src/mem300m.safetensors"; want = sha256_file(SRC); size = os.path.getsize(SRC)
time.sleep(2); base = firefox_rss_mb(); samples = []; stop = threading.Event()
def sample():
    while not stop.is_set(): samples.append(firefox_rss_mb()); time.sleep(0.1)
t = threading.Thread(target=sample); t.start()
t0 = time.time(); choose_files(d, [SRC]); wait_state(d, "mem300m.safetensors", ["done"], 120, "300 MiB upload"); dt = time.time() - t0
stop.set(); t.join(); time.sleep(1.0); after = firefox_rss_mb()
peak = max(samples)
print(f"  300 MiB in {dt:.1f}s ({size / 2**20 / dt:.0f} MiB/s); browser RSS: before {base:.0f} MB, peak {peak:.0f} MB, after {after:.0f} MB")
check("the 300 MiB file arrived intact", sha256_file(f"{DEST}/mem300m.safetensors") == want)
check("browser memory grew by far less than the file (peak growth %.0f MB for a 300 MB file)" % (peak - base), peak - base < 150, (base, peak, after))
d.quit(); clean_server(DEST)

# ---- read-only server ----
kill_server(); server = start_server(MODEL_CRUD_READ_ONLY="1")
d = start_browser(); inject_faults(d); open_tab(d); pick_folder(d, "loras")
check("on a read-only server the upload controls are hidden", not d.find_element(By.CSS_SELECTOR, ".model-crud-upload-controls").is_displayed())
note = d.find_element(By.CSS_SELECTOR, ".model-crud-upload > .model-crud-notice")
check("and an explanation is shown", note.is_displayed() and "disabled on this server" in note.text, note.text)
d.execute_script("""const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(100)], 'x.safetensors'));
  document.querySelector('.model-crud-host').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}))""")
time.sleep(1.0)
check("dropping a file does nothing: no row, no request", len(items(d)) == 0 and net(d)["puts"] == 0)
d.save_screenshot(f"{SP}/ui_14_upload_readonly.png")
d.quit(); kill_server()
for n in os.listdir(f"{BENCH}/src"):
    if n.startswith("q") or n.startswith("mem"): os.unlink(f"{BENCH}/src/{n}")
print(f"{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
