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
