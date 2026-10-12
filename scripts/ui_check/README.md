# Browser check of the "Model files" tab (optional developer tool)

Drives the real ComfyUI frontend in headless Firefox. Not part of `pytest` or CI: it needs a ComfyUI checkout, Firefox and
`selenium` (`pip install selenium`; Selenium Manager downloads geckodriver).

```bash
export CU=/path/to/ComfyUI            # a dev ComfyUI with this repo in custom_nodes/
export SP=/some/scratch/dir           # screenshots and the second model root go here
mkdir -p "$SP"
cat > "$CU/extra_model_paths.yaml" <<YAML      # second `loras` root, for duplicate-path rows
extra:
  base_path: $SP/extra
  loras: loras
YAML
bash scripts/ui_check/ui_seed.sh "$CU" "$SP"   # small model files, incl. hostile and unicode names
scripts/dev.sh &                               # start ComfyUI, wait for it
python scripts/ui_check/ui_full.py             # listing, sort, filter, delete, dropdown, themes, narrow window
# restart ComfyUI with MODEL_CRUD_READ_ONLY=1, then:
python scripts/ui_check/ui_readonly.py         # disabled buttons, notice, unreachable server
```

`ui_seed.sh` deletes the models in `$CU/models/loras` and `$CU/models/checkpoints`; point it only at a throwaway ComfyUI.
Screenshots land in `$SP/ui_*.png`.

## Upload checks

These need a model folder on a real disk and use files of at most 300 MiB, all under `.bench/` (gitignored):

```bash
mkdir -p .bench/models/loras .bench/src
cat > "$CU/extra_model_paths.yaml" <<YAML       # uploads go to the first root, so make .bench the first
bench:
  base_path: $PWD/.bench
  is_default: true
  loras: models/loras
YAML
head -c $((30*1024*1024))  /dev/urandom > .bench/src/small30m.safetensors
head -c $((3*1024*1024+17)) /dev/urandom > .bench/src/other3m.safetensors
head -c $((100*1024*1024)) /dev/urandom > .bench/src/big100m.safetensors
head -c $((300*1024*1024)) /dev/urandom > .bench/src/mem300m.safetensors
MODEL_CRUD_CHUNK_MB=8 scripts/dev.sh &                                  # small chunks so 100 MB has 13 of them
python scripts/ui_check/ui_upload_basic.py           # file input, overwrite, drag and drop, validation
python scripts/ui_check/ui_upload_resilience.py      # pause/resume, network drop, reload + resume, kill -9 and restart
python scripts/ui_check/ui_upload_queue_memory.py    # queue order, overwrite button, browser memory, read-only server
```

`ui_upload_resilience.py` and `ui_upload_queue_memory.py` start, kill and restart ComfyUI themselves (`scripts/dev.sh`), so stop
any ComfyUI you have running first. They inject faults into the page (delayed or failing chunk requests, a fake `navigator.onLine`)
to make the timing deterministic. Check `stat -f -c %T .bench` says `ext4`, not `tmpfs`, before trusting any I/O number.
