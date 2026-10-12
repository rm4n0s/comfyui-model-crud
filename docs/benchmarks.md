# Upload benchmarks

Reference client: [scripts/model_crud_upload.py](../scripts/model_crud_upload.py). Server: this extension in a local ComfyUI
(commit `e8665ca8`, CPU only) on `127.0.0.1`, so there is **no network between client and server**; the numbers show
the Python/disk path, not what a LAN or the internet will give.

## Environment caveat

In the sandbox used for development, everything under the home directory **except the project directory** is RAM-backed
(`stat -f` reports `tmpfs`, 16 GB), even though `df` shows an NVMe device. Only the project directory is a real ext4 disk.
The dev ComfyUI therefore lives on tmpfs, and the first 2 GiB run below (on tmpfs) was discarded. All figures here were
measured with the model folder redirected to a gitignored `.bench/` directory on the real disk (`extra_model_paths.yaml`
with `is_default: true`, since removed). The same machine had only about 5 GB of free RAM.

## Results

### 2 GiB of random data (real disk, default settings: 64 MiB chunks, 4 in parallel)

| Check | Result |
|-------|--------|
| Wall time, client with `--sha256` (hashing runs alongside the upload) | 2.9 s (718 MiB/s) |
| SHA-256 of source vs. uploaded file | identical |
| Server RSS before / peak / after | 718 / 737 / 737 MB (+19 MB) |
| Client peak RSS | 41 MB |
| Dirty / writeback pages during the upload | at most 140 MB / 208 MB |
| Destination file resident in the page cache right after the upload | **0 B** (`fincore`), so `fadvise(DONTNEED)` works |

### Large file: 50 GiB (run twice, then stopped by request)

Source was a sparse 50 GiB file with 18 random 4 MiB regions (some straddling chunk boundaries), about 72 MB on disk. It
is mostly zeros, so this exercises the server's write path, chunking and memory, not data entropy. Space needed on the
server: 50 GiB preallocated plus the server's 1 GiB margin. Both runs used the defaults.

| Run | Wall time | Average | Notes |
|-----|-----------|---------|-------|
| Transfer only (800 chunks) | 21.9 s | 2.3 GiB/s | server RSS 737 → 731 MB |
| With `--sha256` (server hashes 50 GiB at the end) | 57.1 s | 0.9 GiB/s | server RSS 731 → 746 MB; **source and result SHA-256 identical** |

In both runs the destination had 0 B in the page cache afterwards, dirty pages stayed under 255 MB, writeback under
208 MB, and available RAM never fell below 6.3 GB. Per request, the server holds at most one 1 MiB block plus the pending
write.

**These two runs were made before the instruction to keep tests at 1 GB or less and will not be repeated.** They are the
only 50 GiB validation; a run with real (non-sparse) data or over a real network has not been done.

### Interruptions (2 GiB random file, client limited to 100 MiB/s so there is time to interrupt)

| Scenario | What happened | Result |
|----------|---------------|--------|
| A: Ctrl-C of the client after 7 s | exit status 130; server had 12 of 32 chunks | rerun sent 20 chunks, skipped 12; SHA-256 identical |
| B: `kill -9` of ComfyUI after 7 s, client retries then fails (exit 1) | `.part` and `.json` left on disk; after restart the server reported 12 of 32 chunks | rerun sent 20, skipped 12; SHA-256 identical |

Chunks that were in flight at the moment of interruption are not recorded and are sent again, which is expected.

### Parallelism sweep (512 MiB random file, 16 MiB chunks, server `MODEL_CRUD_MAX_PARALLEL=8`)

Six runs per setting, interleaved, after a warm-up. `--parallel 16` is capped by the server to 8, so it measures the same
configuration as 8.

| `--parallel` | median MiB/s | min | max |
|--------------|-------------:|----:|----:|
| 1  | 1095 |  552 | 1200 |
| 2  | 1279 |   88 | 1317 |
| 4  | 1284 |   48 | 1464 |
| 8  | 1376 |   12 | 1567 |
| 16 (capped to 8) | 1328 | 808 | 1539 |

Reading: one chunk in flight is about 15 % slower than several; between 2 and 8 the differences are within the noise, which
includes occasional stalls (the minimums, probably fsync or memory pressure on a RAM-starved machine). On loopback there is
no latency for parallelism to hide, so this does **not** show the benefit on a real link, which is the reason for the
feature. Conclusion: the data does not justify changing the SPEC defaults (4 parallel, 64 MiB chunks); revisit with a
test over a real network.

## Not measured

- A real network (latency, proxies, request-size limits).
- Real 50 GB of random data.
- The browser uploader (Step 9).
