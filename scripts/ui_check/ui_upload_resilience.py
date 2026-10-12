import os, sys, time
sys.path.insert(0, os.environ["SP"])
from ui_lib import *
BENCH = f"{ROOT}/.bench"; DEST = f"{BENCH}/models/loras"
SRC = f"{BENCH}/src/big100m.safetensors"; NAME = "big100m.safetensors"; WRONG = f"{BENCH}/src/other3m.safetensors"
TOTAL_CHUNKS = 13   # 100 MiB / 8 MiB
res = []
def check(label, cond, detail=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + label + (f"  [{str(detail)[:230]}]" if detail != "" else ""), flush=True)

def fresh_page():
    d = start_browser(); inject_faults(d); open_tab(d); pick_folder(d, "loras"); return d
want = sha256_file(SRC)

# ---------- D: pause and resume ----------
kill_server(); server = start_server(); d = fresh_page(); clean_server(DEST)
set_parallel(d, 2); set_net(d, delay=300)
choose_files(d, [SRC])
wait_for(lambda: (item_state(d, NAME) or {}).get("progress", 0) >= 150, 30, "15% progress")
click_action(d, NAME, "Pause")
st = wait_state(d, NAME, ["paused"], 10)
check("Pause stops the transfer and says where", st["status"] == "paused" and "Paused at" in st["text"], st["text"])
check("the paused row offers Resume and Cancel", st["actions"] == ["Resume", "Cancel"], st["actions"])
p1, puts1 = st["progress"], net(d)["puts"]; time.sleep(2.0)
st2 = item_state(d, NAME)
check("nothing is sent while paused", st2["progress"] == p1 and net(d)["puts"] == puts1, (p1, st2["progress"], puts1, net(d)["puts"]))
ranges = api("/uploads")["uploads"][0]
check("the server kept the finished chunks", ranges["received_bytes"] > 0, ranges["received_bytes"])
sent_before = list(net(d)["sent"]); set_net(d, delay=0)
click_action(d, NAME, "Resume")
wait_state(d, NAME, ["done"], 60, "done after resume")
check("resume finishes the upload", sha256_file(f"{DEST}/{NAME}") == want)
counts = {}
for i in net(d)["sent"]: counts[i] = counts.get(i, 0) + 1
resent = sorted(i for i, c in counts.items() if c > 1)
check("only chunks that were in flight at the pause were sent twice", len(resent) <= 2 and len(counts) == TOTAL_CHUNKS, (resent, counts))
clean_server(DEST); d.quit()

# ---------- E: network drop and recovery ----------
d = fresh_page(); set_parallel(d, 2); set_net(d, delay=200)
choose_files(d, [SRC])
wait_for(lambda: (item_state(d, NAME) or {}).get("progress", 0) >= 150, 30, "15% progress")
set_net(d, down=True); set_online(d, False)
st = wait_state(d, NAME, ["waiting"], 20)
check("when the connection drops the row says it is waiting for the network", "Waiting for the network" in st["text"], st["text"])
f1 = net(d)["failed"]; time.sleep(4.0)
check("while offline it does not hammer the server or burn retries", net(d)["failed"] - f1 == 0 and item_state(d, NAME)["status"] == "waiting", (f1, net(d)["failed"]))
check("the upload is still resumable and not failed", item_state(d, NAME)["status"] == "waiting")
d.save_screenshot(f"{SP}/ui_11_waiting.png")
set_net(d, down=False, delay=0); set_online(d, True)
wait_state(d, NAME, ["done"], 60, "done after the network returned")
check("it continues by itself when the 'online' event fires", sha256_file(f"{DEST}/{NAME}") == want)
clean_server(DEST); d.quit()

# ---------- F: reload the page, resume by selecting the file ----------
d = fresh_page(); set_parallel(d, 2); set_net(d, delay=300)
choose_files(d, [SRC])
wait_for(lambda: (item_state(d, NAME) or {}).get("progress", 0) >= 350, 40, "35% progress")
before = api("/uploads")["uploads"][0]; sid = before["upload_id"]
ranges_before = api(f"/uploads/{sid}")["received_ranges"]
have = {i for a, b in ranges_before for i in range(a, b + 1)}
try:
    d.get("http://127.0.0.1:8188/")   # a reload; Firefox may ask about unloading while a transfer runs
except Exception as e:
    try: d.switch_to.alert.accept(); d.get("http://127.0.0.1:8188/")
    except Exception: raise
wait_for(lambda: d.execute_script("return !!(window.app && window.app.extensionManager && window.app.graph)"), 90, "frontend")
d.execute_script("window.__errs = []"); inject_faults(d); open_tab(d); pick_folder(d, "loras")
wait_for(lambda: d.find_elements(By.CSS_SELECTOR, ".model-crud-session"), 15, "unfinished upload listed")
sess = d.execute_script("return [...document.querySelectorAll('.model-crud-session')].map(s => s.innerText)")
check("after a reload the unfinished upload is listed with its progress", NAME in sess[0] and "received" in sess[0], sess)
d.save_screenshot(f"{SP}/ui_12_unfinished.png")
d.execute_script("[...document.querySelectorAll('.model-crud-session-resume')][0].click()")
choose_files(d, [WRONG], which="second")
msg = wait_for(lambda: (lambda m: m if m and m.is_displayed() else None)(d.find_element(By.CSS_SELECTOR, ".model-crud-upload .model-crud-notice-error")), 10, "mismatch message")
check("picking a different file is refused with an explanation", "not the file this upload was started with" in msg.text, msg.text)
check("nothing was sent for the wrong file", net(d)["puts"] == 0)
d.execute_script("[...document.querySelectorAll('.model-crud-session-resume')][0].click()")
choose_files(d, [SRC], which="second")
wait_state(d, NAME, ["done"], 60, "resumed upload done")
sent = set(net(d)["sent"])
check("resume sends exactly the chunks the server was missing", sent == set(range(TOTAL_CHUNKS)) - have and net(d)["puts"] == len(sent), (sorted(sent), sorted(have)))
check("the resumed file is identical", sha256_file(f"{DEST}/{NAME}") == want)
check("the unfinished list is empty afterwards", not d.find_elements(By.CSS_SELECTOR, ".model-crud-session"))
clean_server(DEST); d.quit()

# ---------- G1: long outage: the job fails, 'Retry' continues ----------
d = fresh_page(); set_parallel(d, 2); set_net(d, delay=400)
choose_files(d, [SRC])
wait_for(lambda: (item_state(d, NAME) or {}).get("progress", 0) >= 250, 40, "25% progress")
kill_server(); t0 = time.time()
st = wait_state(d, NAME, ["failed"], 120, "failure after a long outage")
check("a long outage ends in a clear error after the retries (took %.0fs)" % (time.time() - t0), "gave up after" in st["text"] or "Network error" in st["text"], st["text"])
check("the failed row offers Retry", "Retry" in st["actions"], st["actions"])
d.save_screenshot(f"{SP}/ui_13_failed.png")
server = start_server(); set_net(d, delay=0)
d.execute_script("document.querySelector('.model-crud-sessions') && 0")
click_action(d, NAME, "Retry")
wait_state(d, NAME, ["done"], 60, "done after retry")
check("Retry finishes the upload after the server is back", sha256_file(f"{DEST}/{NAME}") == want)
clean_server(DEST); d.quit()

# ---------- G2: short outage: it recovers on its own ----------
d = fresh_page(); set_parallel(d, 2); set_net(d, delay=500)
choose_files(d, [SRC])
wait_for(lambda: (item_state(d, NAME) or {}).get("progress", 0) >= 250, 40, "25% progress")
kill_server(); server = start_server()
set_net(d, delay=0)
st = wait_state(d, NAME, ["done", "failed"], 90, "outcome after a quick restart")
check("a quick server restart is survived without user action", st["status"] == "done" and sha256_file(f"{DEST}/{NAME}") == want, st)
clean_server(DEST)
errs = [e for e in d.execute_script("return window.__errs") if "Crud" in e or "model_crud" in e]
check("no console errors mention the extension", not errs, errs)
d.quit(); kill_server()
print(f"{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
