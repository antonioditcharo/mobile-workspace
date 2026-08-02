# Running on your own GPU

Free, unlimited generation on your own graphics card. No credits, no rate
limits, no per-video cost. Slower and lower-resolution than the big hosted
models, but it runs as often as you like.

## What you need

- **An NVIDIA graphics card.** 6 GB of VRAM is the realistic floor. AMD and
  Intel graphics won't work with this setup.
- **~20 GB of free disk space** for Python, the libraries, and the model.
- **Patience on the first run.** The setup downloads roughly 8 GB and the model
  another 5 GB. Once. After that, startup takes seconds.

Check your card: **Ctrl+Shift+Esc** → **Performance** → **GPU**.

## Setup (Windows)

1. **Install Python** from [python.org/downloads](https://www.python.org/downloads/).
   On the first installer screen, tick **"Add python.exe to PATH"** before
   clicking Install. This matters — skipping it is the most common failure.

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
takes several extra minutes while it downloads. Later ones don't.

## Long clips locally

RealFrame builds anything past ~5 seconds by chaining segments, which means each
continuation needs an **image-to-video** pipeline. Of the two models here only
`ltx` has one, so long clips locally require:

```
set LOCAL_MODEL=ltx
start-local-gpu.bat
```

With `wan-1.3b` loaded, a chained request fails with a message saying exactly
this. The stitching itself needs ffmpeg — this setup installs one, and RealFrame
finds it inside `.venv` automatically.

## Models

Set `LOCAL_MODEL` before starting to switch.

| Value | Model | Download | Character |
| --- | --- | --- | --- |
| `wan-1.3b` *(default)* | Wan 2.1 T2V 1.3B | ~5 GB | Best small-model realism and motion |
| `ltx` | LTX-Video | ~9 GB | Several times faster, softer detail |

Any diffusers-compatible repo id also works, at your own risk of a shape
mismatch.

## Settings

Set these before running the `.bat`, or edit them into it:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOCAL_MODEL` | `wan-1.3b` | Which model to run |
| `LOCAL_PORT` | `8000` | Server port |
| `LOCAL_WIDTH` / `LOCAL_HEIGHT` | `832` / `480` | Frame size — lower these if you run out of memory |
| `LOCAL_OFFLOAD` | `auto` | `sequential` (least VRAM), `model` (balanced), `none` (fastest) |
| `LOCAL_DTYPE` | `auto` | Precision. Auto picks `float16` on GTX 16xx / RTX 20xx cards, which lack `bfloat16` support |
| `LOCAL_PRELOAD` | off | Set to `1` to load the model at startup instead of on first request |

## Expectations on a 6 GB card

- **Roughly 3–10 minutes** for a 3-second clip at 832×480, depending on step
  count. `ltx` is substantially faster.
- **Quality is below the 14B hosted models.** A 1.3B model has less capacity for
  faces, hands, and complex motion. The realism engine helps — it's the same
  prompt compiler either way — but it can't close the whole gap.
- **Keep clips short.** 49 frames (~3s at 16fps) is a sensible ceiling here.
- **Close other GPU applications.** Games, video editors, and even a browser
  with hardware acceleration eat into the same 6 GB.

## When it goes wrong

**"Out of VRAM"** — lower the frame count in RealFrame, or start the server with
a smaller frame:

```
set LOCAL_WIDTH=640
set LOCAL_HEIGHT=384
start-local-gpu.bat
```

**"No CUDA GPU detected"** — your NVIDIA driver is likely out of date. Update at
[nvidia.com/Download](https://www.nvidia.com/Download/index.aspx) and restart.

**"Python is not installed"** after installing it — you missed the "Add
python.exe to PATH" checkbox. Re-run the Python installer, choose **Modify**,
and enable it.

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
