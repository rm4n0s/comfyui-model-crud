import os, sys, time, signal, subprocess
sys.path.insert(0, os.environ["SP"])
from ui_lib import *
d = start_browser(); open_tab(d)
pick_folder(d, "loras")
r = rows(d)
ok = []
def check(label, cond, detail=""):
    ok.append(cond); print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
check("every Delete button is disabled", r and all(x["deleteDisabled"] for x in r), len(r))
check("tooltip explains why", all("disabled on this server" in x["deleteTitle"] for x in r), r[0]["deleteTitle"])
notice = d.find_element(By.CSS_SELECTOR, ".model-crud-notice")
check("a notice says deleting is not allowed", notice.is_displayed() and "does not allow deleting" in notice.text, notice.text)
d.execute_script("document.querySelector('.model-crud-delete').click()")
time.sleep(0.5)
check("clicking a disabled button opens no dialog", d.execute_script("return !document.querySelector('[role=dialog], [role=alertdialog]')"))
check("no file was touched", len(os.listdir(f"{CU}/models/loras/sdxl")) == 2)
time.sleep(0.2); d.save_screenshot(f"{SP}/ui_6_readonly.png")
cfg = api("/config"); check("config says delete_enabled=false", cfg["delete_enabled"] is False and cfg["upload_enabled"] is False, cfg)
# server goes away: refresh shows an error instead of a stale/empty table
subprocess.run(["bash", "-c", "for p in $(ps -eo pid,args | awk '/[m]ain.py --cpu/ {print $1}'); do kill -9 $p; done"])
time.sleep(1.5)
d.execute_script("document.querySelector('.model-crud-refresh').click()")
wait_for(lambda: d.find_element(By.CSS_SELECTOR, ".model-crud-notice").is_displayed(), 20, "error notice")
n = d.find_element(By.CSS_SELECTOR, ".model-crud-notice")
check("unreachable server shows a clear error", "Cannot reach" in n.text or "HTTP" in n.text, n.text)
check("the error notice is styled as an error and the table is cleared", "model-crud-notice-error" in n.get_attribute("class") and len(rows(d)) == 0)
d.save_screenshot(f"{SP}/ui_7_error.png")
d.quit(); print(f"{sum(ok)}/{len(ok)} passed"); sys.exit(0 if all(ok) else 1)
