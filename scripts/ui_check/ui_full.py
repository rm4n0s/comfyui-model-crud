import os, sys, time
sys.path.insert(0, os.environ["SP"])
from ui_lib import *

d = start_browser()
results = []
def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)

open_tab(d)
d.save_screenshot(f"{SP}/ui_1_checkpoints_dark.png")
check("tab opens and lists the default folder (checkpoints)", [r["name"] for r in rows(d)] == ["old.ckpt", "sd15.safetensors"], rows(d))
check("summary shows count and total size", summary(d).startswith("2 files · "), summary(d))

pick_folder(d, "loras")
names = [r["name"] for r in rows(d)]
paths = [(r["dir"] + "/" if r["dir"] else "") + r["name"] for r in rows(d)]
check("loras lists model files only (notes.txt hidden)", "notes.txt" not in names and len(names) == 9, names)
check("natural, case-insensitive name order (B1 < b2 < b10)", names.index("B1.safetensors") < names.index("b2.safetensors") < names.index("b10.safetensors"), names)
shared = [r for r in rows(d) if r["name"] == "shared.safetensors"]
check("same path in two roots is shown twice with root badges", sorted(r["badge"] for r in shared) == ["root 0", "root 1"], shared)
check("unicode file name and its folder render", any(r["name"] == "Süß model 日本.safetensors" and r["dir"] == "sdxl" for r in rows(d)))

hostile = "<img src=x onerror=window.__xss=1>\"'.safetensors"
check("hostile name is shown as text", hostile in names)
check("no element was injected by the hostile name", d.execute_script("return document.querySelectorAll('.model-crud-panel img').length === 0 && window.__xss === undefined"))

# filter
d.find_element(By.CSS_SELECTOR, ".model-crud-filter").send_keys("SDXL")
time.sleep(0.4)
check("filter narrows the list (case-insensitive)", len(rows(d)) == 2, [r["name"] for r in rows(d)])
check("summary reflects the filter", "2 of 9 files" in summary(d), summary(d))
f = d.find_element(By.CSS_SELECTOR, ".model-crud-filter"); f.send_keys(Keys.CONTROL, "a"); f.send_keys(Keys.BACK_SPACE)
time.sleep(0.4)
check("clearing the filter restores the list", len(rows(d)) == 9)

# sort
d.execute_script("document.querySelector('.model-crud-col-size .model-crud-sort').click()")
sizes = [r["size"] for r in rows(d)]
check("sort by size ascending, aria-sort set", d.execute_script("return document.querySelector('.model-crud-col-size').getAttribute('aria-sort')") == "ascending" and sizes[0].endswith(" B") and sizes[-1] == "5.00 MiB", sizes)
d.execute_script("document.querySelector('.model-crud-col-size .model-crud-sort').click()")
check("second click sorts descending", rows(d)[0]["size"] == "5.00 MiB" and d.execute_script("return document.querySelector('.model-crud-col-size').getAttribute('aria-sort')") == "descending")
d.execute_script("document.querySelector('.model-crud-col-name .model-crud-sort').click()")

# loader dropdown before
d.execute_script("""window.__lora = LiteGraph.createNode('LoraLoader'); app.graph.add(window.__lora);""")
def combo(): return d.execute_script("""await app.refreshComboInNodes(); return window.__lora.widgets[0].options.values""") if False else d.execute_async_script("""const done = arguments[0]; app.refreshComboInNodes().then(() => done(window.__lora.widgets[0].options.values))""")
before = combo()
check("LoraLoader dropdown lists the file before deleting", "sdxl/lcm_lora_v10.safetensors" in before, before)

# cancel path
click_delete(d, "lcm_lora_v10.safetensors")
text = dialog_text(d)
print("--- dialog text:", repr(text))
check("confirm dialog names the file and the folder", "sdxl/lcm_lora_v10.safetensors" in text and "loras" in text, text)
time.sleep(1.0); d.save_screenshot(f"{SP}/ui_2_confirm_dialog.png")
dialog_press(d, "cancel|no|keep")
check("cancelling deletes nothing", os.path.exists(f"{CU}/models/loras/sdxl/lcm_lora_v10.safetensors"))
check("dialog closed after cancel", d.execute_script("return !document.querySelector('[role=dialog], [role=alertdialog]')"))

# confirm path
click_delete(d, "lcm_lora_v10.safetensors")
dialog_text(d); dialog_press(d, "delete|confirm|yes")
wait_for(lambda: not os.path.exists(f"{CU}/models/loras/sdxl/lcm_lora_v10.safetensors"), 10, "file removal")
wait_for(lambda: "lcm_lora_v10.safetensors" not in [r["name"] for r in rows(d)], 10, "row removal")
check("confirmed delete removes the file and the row", True)
toast = wait_for(lambda: d.execute_script("""const t = document.querySelector('.p-toast, [data-pc-name=toast]'); return t ? t.innerText : null"""), 8, "toast")
check("success toast shown", "deleted" in toast.lower(), toast)
d.save_screenshot(f"{SP}/ui_3_after_delete.png")
after = combo()
check("LoraLoader dropdown no longer lists it after refresh", "sdxl/lcm_lora_v10.safetensors" not in after, after)

# hostile name through the dialog and the delete URL
click_delete(d, hostile)
text = dialog_text(d)
check("hostile name is plain text in the confirm dialog", hostile in text and d.execute_script("return !document.querySelector('[role=dialog] img, [role=alertdialog] img') && window.__xss === undefined"), text)
dialog_press(d, "delete|confirm|yes")
wait_for(lambda: not any(n.startswith("<img") for n in os.listdir(f"{CU}/models/loras")), 10, "hostile file removal")
check("file with <>\"' characters is deleted through the real URL encoding", True)

# duplicate: delete root 1 only
click_delete(d, "shared.safetensors", nth=1)
dialog_text(d); dialog_press(d, "delete|confirm|yes")
time.sleep(1.0)
left = [r for r in rows(d) if r["name"] == "shared.safetensors"]
check("deleting the second copy leaves the first root's copy", len(left) == 1 and os.path.exists(f"{CU}/models/loras/v2/shared.safetensors") and not os.path.exists(f"{os.environ['SP']}/extra/loras/v2/shared.safetensors"), left)
check("the duplicate badge disappears once only one copy remains", left and left[0]["badge"] == "")

# server-side failure shows the server message: delete a file the server no longer has
open(f"{CU}/models/loras/ghost.safetensors", "wb").write(b"x"); pick_folder(d, "checkpoints"); pick_folder(d, "loras")
os.unlink(f"{CU}/models/loras/ghost.safetensors")
click_delete(d, "ghost.safetensors"); dialog_text(d); dialog_press(d, "delete|confirm|yes")
toast = wait_for(lambda: d.execute_script("""const t = [...document.querySelectorAll('.p-toast-message, [data-pc-name=toastmessage]')].map(x => x.innerText).find(x => /could not/i.test(x)); return t || null"""), 8, "error toast")
check("a 404 from the server is shown with its message and the list refreshes", "File not found" in toast and "ghost.safetensors" not in [r["name"] for r in rows(d)], toast)

# themes
check("dark theme: panel text is light on a dark background", d.execute_script("""
  const p = document.querySelector('.model-crud-panel'); const c = getComputedStyle(p);
  const rgb = s => s.match(/\\d+/g).slice(0,3).map(Number); const lum = ([r,g,b]) => 0.2126*r + 0.7152*g + 0.0722*b;
  let bg = null, el = p; while (el) { const b = getComputedStyle(el).backgroundColor; if (!b.startsWith('rgba(0, 0, 0, 0') && b !== 'transparent') { bg = b; break } el = el.parentElement }
  window.__dark = [c.color, bg]; return lum(rgb(c.color)) > lum(rgb(bg)) + 80"""), d.execute_script("return window.__dark"))
d.execute_script("app.extensionManager.setting.set('Comfy.ColorPalette', 'light')")
time.sleep(1.5)
open_tab(d)
d.save_screenshot(f"{SP}/ui_4_light.png")
check("light theme: panel text is dark on a light background", d.execute_script("""
  const p = document.querySelector('.model-crud-panel'); const c = getComputedStyle(p);
  const rgb = s => s.match(/\\d+/g).slice(0,3).map(Number); const lum = ([r,g,b]) => 0.2126*r + 0.7152*g + 0.0722*b;
  let bg = null, el = p; while (el) { const b = getComputedStyle(el).backgroundColor; if (!b.startsWith('rgba(0, 0, 0, 0') && b !== 'transparent') { bg = b; break } el = el.parentElement }
  window.__light = [c.color, bg]; return lum(rgb(bg)) > lum(rgb(c.color)) + 80"""), d.execute_script("return window.__light"))
d.execute_script("app.extensionManager.setting.set('Comfy.ColorPalette', 'dark')")

# narrow viewport
d.set_window_size(420, 800); time.sleep(1.0)
d.save_screenshot(f"{SP}/ui_5_narrow.png")
check("narrow window: no horizontal page overflow and the panel still shows rows", d.execute_script("return document.documentElement.scrollWidth <= window.innerWidth + 1") and len(rows(d)) > 0)
d.set_window_size(1280, 900)

errs = d.execute_script("return window.__errs")
real = [e for e in errs if "ModelCrud" in e or "model_crud" in e or "model-crud" in e]
check("no console errors mentioning the extension", not real, real)
print("all console errors/warnings captured (for information):", [e[:120] for e in errs][:6])
d.quit()
bad = [l for l, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
