# Filmroom

A private, offline text-to-image generator that looks like a real camera took
the picture. Runs entirely on your own laptop. You drive it from your phone.

No API keys. No accounts. No third party ever sees a prompt or an image. After
the one-time model download you can unplug the internet and it keeps working.

---

## What it is

Two pieces:

- **A server** that runs on your laptop and does the actual image generation on
  your GPU.
- **A mobile web app** the server hosts. You open it in your phone's browser
  over your own WiFi and add it to your home screen, where it behaves like a
  normal installed app.

Your phone is the remote control. Your laptop is the darkroom.

---

## Setup (about 15 minutes, most of it downloading)

### 1. On the laptop

Install [Python 3.11](https://www.python.org/downloads/) if you don't have it.
**Tick "Add Python to PATH"** in the installer — almost every setup failure
traces back to this box.

Then clone this repo and run setup:

```
git clone <this-repo>
cd mobile-workspace
setup.bat            REM Windows
./setup.sh           # Linux / macOS
```

This creates a virtual environment and installs PyTorch with CUDA (~2.5 GB).
It finishes by printing whether your GPU was detected. If it says
`NO GPU DETECTED`, stop and fix that first — CPU generation takes minutes per
image instead of seconds.

### 2. Check the phone can reach it — before downloading any models

```
start.bat --stub     REM Windows
./start.sh --stub    # Linux / macOS
```

Stub mode generates placeholder images with no AI model at all. It exists purely
so you can prove the connection works before committing to a multi-GB download.

The terminal prints something like:

```
  ON YOUR PHONE   : http://192.168.1.42:7867
```

plus a QR code you can scan. Open that URL on your phone (same WiFi), tap
**Generate**, and confirm you get a placeholder image back.

**If the phone can't connect**, it's almost always the Windows Firewall. When
you first ran it, Windows should have shown a prompt — tick **Private
networks** and allow it. If you dismissed that prompt, run PowerShell as
Administrator:

```powershell
New-NetFirewallRule -DisplayName "Filmroom" -Direction Inbound -LocalPort 7867 -Protocol TCP -Action Allow -Profile Private
```

### 3. Install it on your home screen

In Chrome on your phone: **⋮ menu → Add to Home screen**. It now launches
fullscreen with no browser chrome.

### 4. Real generation

Stop the server (Ctrl+C) and start it normally:

```
start.bat
```

The first time you generate, it downloads the model (~2 GB for Realistic
Vision). The phone shows *"Loading model (first run downloads several GB)"* for
a few minutes. Every run after that is instant — the model is cached in
`data/models/`.

---

## Getting images that actually pass as real

This is the part that matters, so read it.

**The preset does most of the work.** Each one names a specific film stock and
camera and then describes that medium's *flaws* — blown flash highlights, grain,
chromatic aberration, tape smear. That's what sells realism. Tap **see full
prompt** in the app to read exactly what's being sent; nothing is hidden.

**Describe a moment, not a portrait.** The single biggest realism killer is
prompting like you're commissioning a photoshoot.

| Reads as AI | Reads as real |
|---|---|
| `beautiful woman, stunning, detailed face` | `woman laughing at a kitchen table, half-turned away` |
| `perfect lighting, 8k, masterpiece` | `overhead kitchen light, everything a bit yellow` |
| `professional portrait` | `someone took this without asking` |

Mundane beats impressive. Clutter, bad framing, and awkward moments are what
real photos are full of. Quality-booster words like `masterpiece`, `8k`, and
`ultra detailed` actively push toward the glossy render look — the app already
negates them, so don't add them back.

**Keep guidance low.** 4.5–6 is the realism band. Above ~8 the model starts
over-baking contrast and saturation, which instantly reads as generated. The
presets already set sensible values.

**Turn on Fix faces for people.** Faces, eyes, teeth and hands are where local
models fail, and the reason is resolution: in a 512×768 frame a face might be
90 pixels wide, which is about 11×14 in latent space — not enough to resolve an
eye. **Fix faces** detects each face, crops it, re-renders it at a full 512px,
and blends it back. **Fix hands** does the same for hands. This is the single
biggest quality win available and it's worth the extra time on anything with a
person in it.

Fix strength controls how much is repainted. 0.3–0.45 fixes structure while
keeping the person recognisable; above ~0.6 you get a clean face that may not
be the same face.

**Generate batches and throw most away.** Real photographers shoot a roll to
keep three frames. Set batch to 4, pick the one that works, hit **Reuse** on it
to lock its seed, then refine the prompt from there.

### Preset cheat sheet

| Preset | Best for |
|---|---|
| 90s Point & Shoot | The default "found in a drawer" look. Flash, grain, date stamp. |
| Disposable Camera | Parties, nightlife. Brutal flash, blown highlights. |
| 2000s Digicam | Early-digital harshness. Noisy shadows, bad white balance. |
| Polaroid SX-70 | Soft, milky, washed out. Very forgiving of model errors. |
| Kodak Portra 400 | The most "nice photo" option. Natural skin, soft grain. |
| Kodachrome 64 | 70s warmth, saturated reds. |
| B&W Tri-X 400 | Documentary monochrome. Hides colour mistakes well. |
| Camcorder / VHS | Low-fi tape still. Extremely convincing because it's so degraded. |
| Security Camera | Overhead CCTV with timestamp. |
| Early Smartphone | 2010s phone photo. Oversharpened, HDR halos. |
| 2000s Webcam | Terrible in exactly the right ways. |
| Night Flash | Subject lit hard against black. |
| Golden Hour | Natural late sun, no flash. |
| No Style | Realism scaffolding only, no era. |

**Tip:** the degraded presets (VHS, webcam, CCTV, disposable) are the most
convincing, because low-quality media hides the artifacts that give models away.
If an image looks *almost* right but slightly off, re-run it on a lo-fi preset.

---

## Fixing faces and hands

**Fix faces** / **Fix hands** in Advanced. Both need a detector, and the app
tells you which one it found in **Settings → Face & hand detection**.

Detection is tried best-first:

1. **YOLO (best)** — put ADetailer weights (`face_yolov8m.pt`,
   `hand_yolov8n.pt`) in `data/models/detectors/` and `pip install ultralytics`.
   Most accurate, and the only one that reliably catches faces in profile.
2. **MediaPipe (default)** — installed by setup. Handles faces and hands with
   no configuration.
3. **OpenCV Haar** — frontal faces only, last resort.

Two version traps that are handled for you but worth knowing: MediaPipe 0.10
bundles its models in the wheel, while 1.x ships none and uses a different API
— the app supports both and downloads the small model files on first use if
needed. And OpenCV 5 dropped the bundled Haar cascades, so that fallback isn't
always present despite what most guides claim.

If a face still comes out wrong: frame tighter so it starts with more pixels,
raise Fix strength, or generate a batch and pick a frame where the base
generation got the structure roughly right — the fix pass repairs detail, it
can't rescue a face pointed the wrong way.

---

## Film LoRAs

LoRAs are small style add-ons that stack on top of a checkpoint. Analog film
emulation, grain structure and era-specific colour LoRAs push realism further
than prompting alone.

Drop `.safetensors` LoRA files into:

```
data/models/loras/
```

They appear on the Create tab under **Film LoRAs**, each with an on/off toggle
and a weight slider. Stack as many as you like — 0.6–0.9 is the usual range,
and negative weights invert the effect.

**Trigger words are handled for you.** Many LoRAs do nothing unless a specific
token appears in the prompt, and that failure is silent — the image just comes
out unstyled. Put the trigger words in a `.txt` file next to the LoRA:

```
data/models/loras/analog_film_v2.safetensors
data/models/loras/analog_film_v2.txt     <- contains: analogfilm style, 35mm
```

The app prepends them automatically whenever that LoRA is enabled. Tap **see
full prompt** to confirm they landed.

Search Civitai for "analog", "film grain", or "amateur photo" LoRAs. Make sure
you get SD 1.5 LoRAs if you're running an SD 1.5 checkpoint — an SDXL LoRA on
an SD 1.5 model fails to load and is logged in the laptop terminal.

---

## Using your own checkpoints

The built-in models are a starting point. The photorealism community mostly
publishes on [Civitai](https://civitai.com), and those checkpoints are usually
better than anything on HuggingFace for this use case.

Download any SD 1.5 `.safetensors` file, drop it in:

```
data/models/checkpoints/
```

It appears in **Settings → Model** automatically, no restart needed. Files with
`xl` in the name are treated as SDXL.

Worth looking for: CyberRealistic, epiCPhotoGasm, Photon, absolutereality.

---

## Hardware notes

Built and tuned for a **4 GB VRAM** card (RTX 2050) with 8 GB system RAM:

- SD 1.5 models load fully into VRAM. Roughly 8–15 s per 512×768 image.
- Attention slicing and VAE slicing/tiling are on by default — big memory
  savings, negligible speed cost.
- SDXL models (RealVis XL) technically run via sequential CPU offload, but
  expect **1–3 minutes** per image on 4 GB. Better hands and anatomy; only
  worth it when you need those.
- FLUX is not offered. Even 4-bit quantized it needs ~7 GB and would thrash
  your 8 GB of system RAM. It also has a glossy signature look that fights the
  found-photo aesthetic, so you're not missing much for this particular job.

If you hit `CUDA out of memory`: use a smaller framing, drop batch to 1, turn
off Detail pass, and hit **Free GPU memory** in Settings. Closing Chrome on the
laptop frees a surprising amount of VRAM.

---

## Where images go

`data/outputs/`, as full-resolution PNG with all generation settings embedded
in the file plus a matching `.json` sidecar. Nothing is ever deleted unless you
delete it from the Gallery.

On your phone, **Save to phone** writes to your Downloads folder. **Share**
hands it to the Android share sheet, which is the easier route into your photo
gallery or a messaging app.

---

## Privacy

- All generation is local. No API keys exist anywhere in this codebase.
- The server binds your LAN so your phone can reach it. Anyone else on the same
  network can reach it too — fine on home WiFi, don't run it on public WiFi.
- The only outbound request the app ever makes is downloading model weights from
  HuggingFace during first use. After that it works with the internet off.
- No content filtering runs on your images, same as any desktop Stable Diffusion
  install. What you generate stays on your disk.

---

## Troubleshooting

**Phone can't connect** — Firewall (see step 2). Also confirm both devices are
on the same network; many routers isolate a "Guest" WiFi from the main one.

**`CUDA available: False`** — Update your NVIDIA driver, then re-run setup. If
your driver is old, edit `TORCH_INDEX` in the setup script to
`https://download.pytorch.org/whl/cu118`.

**Style presets barely change anything** — `compel` failed to install. Without
it, prompts get cut at 77 tokens and the style half is thrown away. Check
Settings → Machine → "Long prompts". Fix with `pip install compel` inside the
venv.

**First generation hangs for ages** — It's downloading the model. Watch the
laptop terminal for progress bars.

**Images look plastic** — Guidance too high (drop to 5), or you're using
portrait-style prompt words. Re-read the prompting section above.

**Faces are mangled** — Turn on **Fix faces**. If it reports "No faces or hands
found to refine", the detector didn't see it: faces in profile or very small
faces defeat MediaPipe, so add the YOLO weights (see above) or frame tighter.

**A LoRA seems to do nothing** — Either it needs trigger words (add a `.txt`
sidecar), the weight is too low (try 0.8–1.0), or it's an SDXL LoRA on an SD 1.5
checkpoint. The laptop terminal logs load failures.

---

## Layout

```
run.py                 Launcher - prints the phone URL and QR
server/
  main.py              FastAPI routes, SSE progress, static hosting
  pipeline.py          Model loading, VRAM tuning, generation, LoRAs
  presets.py           Style presets and prompt construction  <- the realism
  detect.py            Face/hand detection, three backends
  refine.py            Crop -> re-render -> feathered composite geometry
  jobs.py              Queue, progress events, image saving
  config.py            Paths and settings
web/
  index.html           The app
  app.js               Client logic
  styles.css           Mobile-first styling
  sw.js                Service worker (installable, offline shell)
data/
  outputs/             Generated images + metadata sidecars
  models/checkpoints/  Drop-in .safetensors checkpoints
  models/loras/        Drop-in .safetensors LoRAs (+ .txt trigger words)
  models/detectors/    YOLO / MediaPipe detector weights
```
(all of `data/` is gitignored)
