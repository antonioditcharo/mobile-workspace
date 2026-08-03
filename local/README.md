# Running on your own GPU

Free, unlimited generation on your own graphics card. No credits, no rate
limits, no per-video cost. Slower and lower-resolution than the big hosted
models, but it runs as often as you like.

## What you need

- **An NVIDIA graphics card.** 4 GB of VRAM is the practical minimum; 6 GB or
  more is comfortable. AMD and Intel graphics won't work with this setup.
- **System RAM matters more than you would expect.** Under 16 GB, use the
  lighter models — see the table below. This is the most common reason a local
  setup fails.
- **Disk space:** ~8 GB for the libraries, plus the model — 4 GB for
  `animatediff`, 28 GB for `wan-1.3b`.
- **Patience on the first run.** The model downloads before the first frame is
  generated. It is a one-time cost; later runs start in seconds.

Check your card: **Ctrl+Shift+Esc** → **Performance** → **GPU**.

## Setup (Windows)

1. **Install Python 3.12** —
   [python.org/downloads/release/python-3128](https://www.python.org/downloads/release/python-3128/),
   scroll to *Windows installer (64-bit)*.

   **The version matters.** PyTorch lags new Python releases by months, so the
   newest Python is the wrong one — 3.14 has no PyTorch builds at all, and
   installing it produces a "No matching distribution found for torch" error
   that looks like a network failure but isn't. Supported range is 3.10–3.13.

   On the first installer screen, tick **"Add python.exe to PATH"**.

   Already have a newer Python? Leave it. Installing 3.12 alongside it is
   fine — the launcher script finds and uses the right one automatically.

2. **Double-click `start-local-gpu.bat`** in this folder.

   It creates its own isolated Python environment, installs everything, checks
   your GPU, and starts the server. Expect 10–20 minutes the first time. Leave
   the window open.

   When it finishes you'll see:

   ```
   RealFrame local GPU server
   http://localhost:8000/generate
   ```

3. **Point RealFrame at it.** Open the `.env` file in the main folder and
   change these two lines:

   ```
   PROVIDER=custom
   CUSTOM_ENDPOINT=http://localhost:8000/generate
   ```

   Save it.

4. **Restart RealFrame** (Ctrl+C in its window, then `npm start`).

You now have two windows open: the GPU server and RealFrame. Both need to stay
running. Generate as usual at `http://localhost:3000`.

The model loads on your *first* generation, not at startup — so the first video
waits on a 20-40 minute download before any generating begins. Watch the GPU
server window for progress; the RealFrame page shows only "generating". Later
runs skip all of this.

If a `.env` with an `HF_TOKEN` exists in the main folder, the server reuses it
for downloads — anonymous Hub traffic is rate limited and slower. Generation
itself stays entirely local either way.

## Long clips locally

RealFrame builds anything past ~5 seconds by chaining segments, which means each
continuation needs an **image-to-video** pipeline. `ltx` and `svd` have one;
`wan-1.3b` and `animatediff` do not. For long clips locally:

```
set LOCAL_MODEL=ltx
start-local-gpu.bat
```

With `wan-1.3b` loaded, a chained request fails with a message saying exactly
this. The stitching itself needs ffmpeg — this setup installs one, and RealFrame
finds it inside `.venv` automatically.

## Models

Set `LOCAL_MODEL` before starting to switch.

| Value | Type | Download | RAM to load | Character |
| --- | --- | --- | --- | --- |
| `animatediff` | text-to-video | ~4 GB | ~6 GB | Lightest. Runs where the others cannot |
| `svd` | image-to-video | ~5 GB | ~8 GB | Animates a still. No text encoder |
| `wan-1.3b` | text-to-video | ~28 GB | ~32 GB | Best small-model realism and motion |
| `ltx` | text-to-video | ~19 GB | ~24 GB | Faster than Wan, softer detail |

**The RAM column is what decides whether a model loads at all** — more often
the blocker than VRAM. Wan and LTX carry an 11 GB text encoder; that is nearly
all of their size, and Windows must be able to map it. With under 16 GB of
system RAM the server defaults to `animatediff` rather than failing after a
28 GB download.

Any diffusers-compatible repo id also works, at your own risk of a shape
mismatch.

## Settings

Set these before running the `.bat`, or edit them into it:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOCAL_MODEL` | from RAM | Which model to run; `animatediff` under 16 GB RAM, else `wan-1.3b` |
| `LOCAL_PORT` | `8000` | Server port |
| `LOCAL_WIDTH` / `LOCAL_HEIGHT` | from VRAM | Frame size — lower these if you run out of memory |
| `LOCAL_OFFLOAD` | `auto` | `sequential` (least VRAM), `model` (balanced), `none` (fastest) |
| `LOCAL_MAX_FRAMES` | from VRAM | Frame ceiling; requests above it are capped |
| `LOCAL_DTYPE` | `auto` | Precision. Auto picks `float16` on cards without hardware `bfloat16` (Turing and older) |
| `LOCAL_PRELOAD` | off | Set to `1` to load the model at startup instead of on first request |

## What your card can do

Frame size drives memory use more sharply than anything else, so the server
picks a default from the card it finds:

| VRAM | Default frame | Frame ceiling |
| --- | --- | --- |
| under 5 GB | 480×320 | 33 |
| 5–7 GB | 640×384 | 49 |
| 7–11 GB | 832×480 | 49 |
| 11 GB+ | 832×480 | 81 |

Override with `LOCAL_WIDTH`, `LOCAL_HEIGHT`, and `LOCAL_MAX_FRAMES`. Requests
above the ceiling are capped rather than left to fail twenty minutes in.

- **Roughly 3–10 minutes** for a 3-second clip on a 6 GB card, longer on 4 GB
  where more of the model is shuffled to system RAM. `ltx` is faster.
- **Quality is below the 14B hosted models.** A 1.3B model has less capacity for
  faces, hands, and complex motion, and a smaller frame gives it less to work
  with — a 480×320 clip will not match what a hosted 14B model produces. The realism engine helps — it's the same
  prompt compiler either way — but it can't close the whole gap.
- **Keep clips short.** The frame ceiling above is roughly 2–5 seconds at 16fps.
- **Close other GPU applications.** Games, video editors, and even a browser
  with hardware acceleration eat into the same VRAM.

## When it goes wrong

**"The paging file is too small for this operation" (os error 1455)** — Windows
ran out of virtual memory while loading. This is not the GPU and not a timeout.
Either switch to a lighter model:

```
set LOCAL_MODEL=animatediff
start-local-gpu.bat
```

or enlarge the page file and retry the heavy one:

1. Press the Windows key, type **Advanced system settings**, open it
2. **Performance → Settings → Advanced → Virtual memory → Change**
3. Untick **Automatically manage paging file size**
4. Select **C:**, choose **Custom size**
5. Initial size `8192`, Maximum size `65536`
6. **Set → OK**, then restart the computer

**"Out of VRAM"** — lower the frame count in RealFrame, or start the server with
a smaller frame:

```
set LOCAL_WIDTH=384
set LOCAL_HEIGHT=256
start-local-gpu.bat
```

On a 4 GB card this is the setting most likely to get a first clip out.

**"No CUDA GPU detected"** — your NVIDIA driver is likely out of date. Update at
[nvidia.com/Download](https://www.nvidia.com/Download/index.aspx) and restart.

**"Python is not installed"** after installing it — you missed the "Add
python.exe to PATH" checkbox. Re-run the Python installer, choose **Modify**,
and enable it.

**"No matching distribution found for torch"** — your Python is too new for
PyTorch, not a network problem. Install Python 3.12, delete the `.venv` folder
in this directory, and run the launcher again. It will pick up 3.12 on its own.

**Switched Python versions and it still fails** — the `.venv` folder remembers
which Python built it. Delete it and re-run; it rebuilds in seconds.

**Black or garbled video** — usually a precision problem. Force it:

```
set LOCAL_DTYPE=float16
start-local-gpu.bat
```

**Everything is extremely slow** — check the server window for the offload mode.
`sequential` is the slow-but-fits setting. If you have VRAM to spare, set
`LOCAL_OFFLOAD=model`.

## Going back to hosted models

Edit `.env`, set `PROVIDER=hf`, and restart RealFrame. Your settings for both
paths can live in the file at once — only `PROVIDER` decides which is used.
