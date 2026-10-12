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
