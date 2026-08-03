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
start-local-gpu.bat ltx
```

With `wan-1.3b` loaded, a chained request fails with a message saying exactly
this. The stitching itself needs ffmpeg — this setup installs one, and RealFrame
finds it inside `.venv` automatically.

## Models

**To switch models, double-click one of these:**

| File | Model |
| --- | --- |
| `run-animatediff.bat` | AnimateDiff — lightest, runs anywhere |
| `run-wan.bat` | Wan 2.1 T2V 1.3B — best small-model realism |

Or pass the name as an argument: `start-local-gpu.bat ltx`. Setting
`LOCAL_MODEL` before launching still works too.

After switching, click **Check availability** in RealFrame once — it reads the
running model from the server and sets the parameters that suit it.

| Value | Type | Download | RAM to load | Character |
| --- | --- | --- | --- | --- |
| `animatediff` | text-to-video | ~4 GB | ~6 GB | Lightest. Runs where the others cannot. Fixed at 16 frames / 512x512 |
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
| `LOCAL_OFFLOAD` | `auto` | `none` (fastest) / `model` (balanced) / `sequential` (least VRAM). Auto picks the fastest that fits |
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

## Making it faster

Three levers actually matter. In order of effect:

**1. Offload mode — up to 4x.** Offloading moves weights between system RAM and
the GPU so a model larger than your VRAM can still run. `sequential` does this
for every layer of every step and is punishingly slow; `model` moves whole
components once per step; `none` keeps everything resident. The server now
estimates the working set and picks the fastest mode that fits, and if a
generation runs out of memory it retries one step more conservatively rather
than failing. Force it with `LOCAL_OFFLOAD=model`.

Note the working set is much smaller than the download. Wan's 28 GB is mostly
its text encoder, which runs once at the start and can live on the CPU; only
the transformer and VAE — about 3.4 GB — need to be resident while denoising.
That is why a 4 GB card can run it at all.

**2. Frame size and count — roughly linear.** Cost scales with pixels times
frames. Halving the frame area nearly halves the time. `LOCAL_WIDTH`,
`LOCAL_HEIGHT`, and the Frames field in RealFrame.

**3. Steps — exactly linear.** 30 steps takes twice as long as 15 and looks
only somewhat better. Past ~30 the returns are very small.

### Measure instead of guessing

```
http://localhost:8000/benchmark
```

Open that in a browser while the server is running. It times a deliberately
tiny generation and reports seconds per step, the offload mode in use, an
estimate for a full 32-step run, and specific advice. Use it to check whether a
change helped without waiting five minutes for a full clip.

Every real generation also logs its timing:

```
Done in 4.9 min (9.2s per step, offload=sequential).
```

### What will not help

- **torch.compile** — long compile times and poor Windows support for the
  Triton backend; the payoff rarely survives the setup.
- **More system RAM** — helps a model *load*, not generate. Generation speed is
  bounded by the GPU and the PCIe bus.
- **Overclocking** — a few percent, against real thermal risk on a laptop.

The honest ceiling: a 4 GB laptop GPU is roughly 30-50x slower than the
datacenter cards behind hosted inference. Tuning gets you a few times faster,
not an order of magnitude. Drafting locally and paying for a final render
remains the pragmatic split.

## When it goes wrong

**"The paging file is too small for this operation" (os error 1455)** — Windows
ran out of virtual memory while loading. This is not the GPU and not a timeout.
Either switch to a lighter model:

```
run-animatediff.bat
```

or enlarge the page file and retry the heavy one:

1. Press the Windows key, type **Advanced system settings**, open it
2. **Performance → Settings → Advanced → Virtual memory → Change**
3. Untick **Automatically manage paging file size**
4. Select **C:**, choose **Custom size**
5. Initial size `8192`, Maximum size `65536`
6. **Set → OK**, then restart the computer

**The video generates but never arrives** — if the server window shows a
finished progress bar and then a disconnect message, RealFrame's job timeout
fired while it was still working. Open `.env` in the main folder and set:

```
JOB_TIMEOUT_MS=0
```

Zero means no limit; cancel from the page instead. An existing `.env` copied
from an earlier setup may still carry the old 15-minute value.

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

**Switched Python versions and it still fails** — the environment remembers
which Python built it. Delete it and re-run the launcher; it rebuilds from
cached downloads. It lives at `%LOCALAPPDATA%\realframe-venv` unless an older
`.venv` exists inside this folder, which still takes precedence.

**"The term '.\.venv\Scripts\python' is not recognized"** — that path only
exists after the launcher has run at least once. A freshly downloaded copy of
RealFrame has no environment in it. Double-click `start-local-gpu.bat` first;
newer versions keep the environment outside the project folder so updating
no longer costs a reinstall.

**A vague blob that dissolves as the clip goes on** — that is AnimateDiff being
pushed past what it was trained for. Its motion adapter learned 16 frames at
512x512; ask for 32 frames at 480x320 and the temporal layers wash the picture
out progressively, so the clip starts with structure and ends flat. The server
now clamps frames, size and guidance to the trained range automatically.

**Flat grey video, or fine noise with no picture** — denoising did not
converge, which is nearly always precision. AnimateDiff and SVD are Stable
Diffusion 1.5 lineage and need `float16`; `bfloat16` loses enough mantissa to
collapse them into mush. This is now chosen automatically per model, but you
can force it:

```
set LOCAL_DTYPE=float16
start-local-gpu.bat
```

If it is already float16, try `float32` (slower but exact), raise **Steps**, or
lower **Guidance** to 3-4. The server checks its own output and prints a
warning when a clip comes out flat, so you are not left guessing.

**A vertical seam down the middle of the frame** — VAE tiling. It decodes in
overlapping patches to save memory and leaves a join where they meet. It is now
only enabled for large frames, where it is actually needed.

**Everything is extremely slow** — check the server window for the offload mode.
`sequential` is the slow-but-fits setting. If you have VRAM to spare, set
`LOCAL_OFFLOAD=model`.

## Going back to hosted models

Edit `.env`, set `PROVIDER=hf`, and restart RealFrame. Your settings for both
paths can live in the file at once — only `PROVIDER` decides which is used.
