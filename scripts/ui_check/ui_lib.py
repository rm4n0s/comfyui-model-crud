import os, time, json, urllib.request
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import Select

SP = os.environ["SP"]
CU = os.environ["CU"]

def wait_for(fn, timeout=30, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            v = fn()
            if v: return v
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}" + (f" ({last})" if last else ""))

def start_browser(width=1280, height=900):
    o = Options(); o.add_argument("-headless")
    d = webdriver.Firefox(options=o); d.set_window_size(width, height)
    d.get("http://127.0.0.1:8188/")
    wait_for(lambda: d.execute_script("return !!(window.app && window.app.extensionManager && window.app.graph)"), 90, "frontend")
    d.execute_script("""window.__errs = []; const oe = console.error;
      console.error = (...a) => { __errs.push(a.map(String).join(' ')); oe(...a) };
      window.addEventListener('error', e => __errs.push('error: ' + e.message));
      window.addEventListener('unhandledrejection', e => __errs.push('rejection: ' + e.reason));""")
    return d

def open_tab(d):
    if d.find_elements(By.CSS_SELECTOR, ".model-crud-panel"): return
    icon = wait_for(lambda: d.find_element(By.CSS_SELECTOR, ".pi-folder-open"), 20, "sidebar icon")
    d.execute_script("arguments[0].closest('button').click()", icon)
    wait_for(lambda: d.find_elements(By.CSS_SELECTOR, ".model-crud-panel"), 20, "panel")
    wait_for(lambda: d.execute_script("return !document.querySelector('.model-crud-summary')?.textContent.startsWith('Loading')"), 20, "load finished")

def rows(d):
    return d.execute_script("""return [...document.querySelectorAll('.model-crud-row')].map(r => ({
      name: r.querySelector('.model-crud-name').textContent,
      dir: r.querySelector('.model-crud-dir')?.textContent ?? '',
      size: r.querySelector('.model-crud-cell-size').textContent,
      badge: r.querySelector('.model-crud-badge')?.textContent ?? '',
      deleteDisabled: r.querySelector('.model-crud-delete').disabled,
      deleteTitle: r.querySelector('.model-crud-delete').title,
    }))""")

def summary(d): return d.find_element(By.CSS_SELECTOR, ".model-crud-summary").text
def pick_folder(d, key):
    Select(d.find_element(By.CSS_SELECTOR, ".model-crud-folder")).select_by_value(key)
    wait_for(lambda: d.execute_script("return !document.querySelector('.model-crud-summary').textContent.startsWith('Loading')"), 20, "folder load")
    time.sleep(0.3)

def click_delete(d, name_text, nth=0):
    btns = d.execute_script("""return [...document.querySelectorAll('.model-crud-row')]
      .filter(r => r.querySelector('.model-crud-name').textContent === arguments[0])
      .map(r => r.querySelector('.model-crud-delete'))""", name_text)
    d.execute_script("arguments[0].click()", btns[nth])

def dialog_text(d):
    return wait_for(lambda: d.execute_script("""const dlg = document.querySelector('[role=dialog], [role=alertdialog]');
      return dlg ? dlg.innerText : null"""), 10, "confirm dialog")

def dialog_press(d, pattern):
    ok = d.execute_script("""const dlg = document.querySelector('[role=dialog], [role=alertdialog]');
      const b = [...dlg.querySelectorAll('button')].find(b => new RegExp(arguments[0], 'i').test(b.innerText.trim()));
      if (!b) return false; b.click(); return true""", pattern)
    assert ok, f"no dialog button matching {pattern}"
    time.sleep(0.4)

def api(path):
    return json.load(urllib.request.urlopen("http://127.0.0.1:8188/api/model_crud" + path))

# ---- helpers for the upload checks ----
import hashlib, subprocess

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(8 << 20): h.update(b)
    return h.hexdigest()

def show_file_input(d, which="first"):
    """Make the hidden file input interactable so Selenium can set a path on it."""
    return d.execute_script("""const inputs = [...document.querySelectorAll('.model-crud-upload input[type=file]')];
      const el = arguments[0] === 'first' ? inputs[0] : inputs[1]; el.hidden = false; el.style.display = 'block'; return !!el""", which)

def choose_files(d, paths, which="first"):
    show_file_input(d, which)
    inputs = d.find_elements(By.CSS_SELECTOR, ".model-crud-upload input[type=file]")
    inputs[0 if which == "first" else 1].send_keys("\n".join(paths))

def items(d):
    return d.execute_script("""return [...document.querySelectorAll('.model-crud-item')].map(li => ({
      name: li.querySelector('.model-crud-item-name').textContent, dest: li.querySelector('.model-crud-item-dest').textContent,
      status: li.dataset.status, text: li.querySelector('.model-crud-item-status').textContent,
      progress: li.querySelector('progress').value, actions: [...li.querySelectorAll('.model-crud-item-actions button')].map(b => b.textContent)}))""")

def click_action(d, name, label):
    ok = d.execute_script("""const li = [...document.querySelectorAll('.model-crud-item')].reverse().find(l => l.querySelector('.model-crud-item-name').textContent === arguments[0]);
      const b = [...li.querySelectorAll('.model-crud-item-actions button')].find(b => b.textContent === arguments[1]); if (!b) return false; b.click(); return true""", name, label)
    assert ok, f"no '{label}' button on {name}"

def item_state(d, name):
    matching = [i for i in items(d) if i["name"] == name]
    return matching[-1] if matching else None  # the newest row with that name

def firefox_rss_mb():
    out = subprocess.run("ps -C firefox,firefox-bin,Web\\ Content,plugin-container -o rss= 2>/dev/null; ps -eo rss,comm | awk '/[f]irefox|Web Content|Isolated/ {print $1}'", shell=True, capture_output=True, text=True).stdout
    vals = [int(x) for x in out.split() if x.isdigit()]
    return sum(vals) / 1024 if vals else 0.0

# ---- server lifecycle and fault injection for the resilience checks ----
import signal as _signal

ROOT = os.environ.get("MODEL_CRUD_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the repo

def server_pids():
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines() if "main.py --cpu" in l and "ps -eo" not in l and "grep" not in l]

def kill_server(sig=_signal.SIGKILL):
    for p in server_pids():
        try: os.kill(p, sig)
        except ProcessLookupError: pass
    end = time.time() + 20
    while server_pids() and time.time() < end: time.sleep(0.2)

def start_server(**env):
    e = dict(os.environ, MODEL_CRUD_CHUNK_MB="8", **env)
    proc = subprocess.Popen([f"{ROOT}/scripts/dev.sh"], env=e, stdout=open(f"{SP}/up_server.log", "ab"), stderr=subprocess.STDOUT)
    for _ in range(120):
        try: api("/config"); return proc
        except Exception: time.sleep(1)
    raise SystemExit("server did not start")

FAULTS = """
window.__net = { delay: 0, down: false, puts: 0, failed: 0, sent: [] };
const origSend = XMLHttpRequest.prototype.send, origOpen = XMLHttpRequest.prototype.open, origAbort = XMLHttpRequest.prototype.abort;
XMLHttpRequest.prototype.open = function (m, u) { this.__m = m; this.__u = u; return origOpen.apply(this, arguments); };
XMLHttpRequest.prototype.abort = function () {
  this.__aborted = true;
  if (this.__pending) { clearTimeout(this.__t); this.__pending = false; this.onabort && this.onabort(new Event('abort')); return; }
  return origAbort.apply(this, arguments);
};
XMLHttpRequest.prototype.send = function (body) {
  if (this.__m !== 'PUT' || !/\\/chunks\\//.test(this.__u)) return origSend.call(this, body);
  const net = window.__net; net.puts++; net.sent.push(Number(this.__u.split('/').pop()));
  if (net.down) { net.failed++; setTimeout(() => this.onerror && this.onerror(new Event('error')), 5); return; }
  if (!net.delay) return origSend.call(this, body);
  this.__pending = true;
  this.__t = setTimeout(() => { if (this.__aborted) return; this.__pending = false; origSend.call(this, body); }, net.delay);
};
"""
def inject_faults(d): d.execute_script(FAULTS)
def set_net(d, **kw): d.execute_script("Object.assign(window.__net, arguments[0])", kw)
def net(d): return d.execute_script("return window.__net")
def set_online(d, online):
    d.execute_script("""const v = arguments[0]; Object.defineProperty(navigator, 'onLine', {get: () => v, configurable: true});
      window.dispatchEvent(new Event(v ? 'online' : 'offline'))""", online)
def set_parallel(d, n): d.execute_script("app.extensionManager.setting.set('ModelCrud.Upload.Parallel', arguments[0])", n)
def upload_status(d, name): return item_state(d, name)
def wait_state(d, name, states, timeout=60, what=None):
    return wait_for(lambda: (lambda s: s if s and s["status"] in states else None)(item_state(d, name)), timeout, what or f"{name} -> {states}")
def server_sessions(): return api("/uploads")["uploads"]
def clean_server(dest_dir):
    for u in server_sessions():
        try: urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:8188/api/model_crud/uploads/{u['upload_id']}", method="DELETE"), timeout=10)
        except Exception: pass
    for n in os.listdir(dest_dir):
        if n.endswith(".safetensors") and n.startswith(("big", "mem")): os.unlink(os.path.join(dest_dir, n))
