# Perchance Prompt Forge

Turn a photo into a **Perchance-ready text-to-image prompt** — prompt text, matching
negative prompt, and recommended CFG / steps / aspect ratio — entirely on your phone.

The output is tuned for one job: **convincingly real period photographs**. Pick
**1980s**, **1990s**, or **early 2000s** and the app builds a prompt around era-accurate
film stock, camera bodies, flash behaviour and lab-print or sensor artifacts.

No account. No API key. No rate limit. No content filter. Your photo is never uploaded.

---

## Get it on your phone

### Option 1 — install as an app (recommended)

1. In this repo on GitHub: **Settings → Pages**.
2. Under *Build and deployment*, set **Source: Deploy from a branch**, pick branch
   `claude/mobile-image-text-perchance-ttd2oz`, folder `/ (root)`, and **Save**.
3. Wait a minute, then open the URL GitHub shows you on your phone:
   `https://<your-username>.github.io/mobile-workspace/`
4. Browser menu → **Add to Home Screen**.

It now launches like a native app and works offline after the first load.

### Option 2 — one file, no setup

Download **`dist/prompt-forge.html`** to your phone and open it. Everything is inlined in
that single file. (Offline install and camera capture are more reliable with Option 1.)

---

## Using it

1. **Photo** — take one or pick from your gallery.
2. **Era** — choose the decade, then a format (35mm print, disposable, Polaroid, VHS still,
   digital compact, VGA cameraphone…), then how heavy the artifacts should be.
3. **Read image** — the AI describes what it sees. First run downloads the model once.
4. **Check the fields** — every observation is editable. Fix anything misread.
5. **Copy** — prompt, negative prompt, or everything including the settings block. Paste
   into Perchance and set CFG / steps / aspect to the recommended values.

You can skip step 3 entirely and just type the fields yourself — the prompt compiler
doesn't need the model.

### Controls worth knowing

| Control | What it does |
|---|---|
| **Artifact intensity** | Subtle → Heavy. Heavy adds more grain, date stamps and compression. Overdone reads as pastiche, so Subtle is a real option. |
| **Period the subject too** | Off (default) keeps the subject exactly as photographed and only applies the period *medium*. On also adds era clothing, hair and decor. |
| **Tag style / Natural language** | Comma tags suit anime and booru-trained models; natural language suits photoreal models. |
| **Weight the subject** | Wraps the main subject as `(subject:1.2)`. Tag style only. |
| **Reroll film** | Rotates through that era's film stocks and camera bodies. |

---

## Why it's built this way

**The quality tags are the enemy.** `masterpiece, 8k, ultra detailed, cinematic lighting`
is exactly what makes an image look AI-generated. Every era preset therefore *suppresses*
that boilerplate and substitutes concrete period apparatus, backed by a negative prompt
that rejects modern digital polish — airbrushed skin, HDR, colour grading, studio lighting,
creamy bokeh.

**Low CFG reads more real.** Era presets recommend 5–6 rather than the usual 7–10, because
high guidance produces the over-saturated, crunchy look that gives AI images away.

**The model only observes; code does the structure.** A 256M–500M vision model answers
"what is the subject wearing?" reliably but cannot be trusted to emit structured prompt
syntax. So it is asked eight narrow questions, and a deterministic compiler
(`src/compiler.js`) handles cleanup, vocabulary normalisation, ordering, weighting and the
era overlay. That half is fully unit tested.

**Positive and negative prompts must not fight.** An early-2000s compact *should* produce
JPEG blocks; an 80s print *should* have a white border and a date stamp; a VHS still *is*
low-resolution. The base negative list rejects all of those, so era presets subtract from
it (`negativeExclude`), and a test asserts no era ever negates an artifact it asks for.

---

## Layout

| Path | Purpose |
|---|---|
| `index.html` | Mobile-first UI |
| `src/app.js` | UI wiring and orchestration |
| `src/vision.js` | Device detection, model loading, multi-pass observation |
| `src/compiler.js` | Prose → structured prompt (pure functions) |
| `src/eras.js` | Era preset data |
| `src/vocab.js` | Controlled vocabulary and cleanup tables |
| `sw.js`, `manifest.webmanifest`, `icons/` | PWA shell |
| `build/bundle.mjs` | Emits `dist/prompt-forge.html` |
| `build/make-icons.py` | Regenerates the icon PNGs |
| `test/` | Unit tests + browser smoke test |

No build step and no dependencies are needed to run the app — it is plain ES modules.

## The AI model

Chosen automatically, overridable in **Settings & device**:

| Device | Model | Download |
|---|---|---|
| WebGPU available | SmolVLM-500M (q4f16), on GPU | ~450 MB |
| WebGPU, low RAM | SmolVLM-256M (q4), on GPU | ~180 MB |
| No WebGPU | SmolVLM-256M (q8), on CPU | ~280 MB |

Downloaded once, then cached — after that it runs with no connection. On CPU expect a
minute or more per image.

## Development

```bash
node --test test/compiler.test.mjs test/vision.test.mjs   # 45 unit tests
node test/ui.smoke.mjs                                    # 39 browser checks (needs playwright)
node build/bundle.mjs                                     # rebuild the single-file version
python3 build/make-icons.py                               # regenerate icons
```

Note: pass test files explicitly — `node --test test/` makes Node treat the directory as a
module and fail. The smoke test skips itself if Playwright isn't installed.

## Known limits

- The **model path is unverified end to end**: it was written in a sandbox with no network
  access to the model host, so first-load download, real speed and caption quality need
  checking on an actual phone. `runPass` in `src/vision.js` is the one place to adjust if a
  library version changes the call signature.
- Perchance's own control labels could not be checked from that sandbox either, so nothing
  depends on them — era styling is emitted as **prompt text**, which works in any prompt
  box, and handoff is copy-to-clipboard rather than a URL scheme.
- A small vision model misses fine detail and sometimes misnames objects. That is why every
  field is editable.
- Artifact intensity and CFG are the two knobs to tune once you see real generations.
