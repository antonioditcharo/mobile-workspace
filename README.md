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
| **Content** | Safe (default). Suggestive and Explicit permit nudity and add anatomy support. |
| **Extra prompt terms** | Free text appended near the subject — write whatever you want added. |

### Body position

Pose, action and gaze are asked as three separate questions rather than one vague
"what is the subject doing?", which used to return things like "sunning herself" for
someone simply facing the camera. Pose gets the largest token budget of any pass, and the
answers are normalised per category — `sitting` → `seated`, `from behind` →
`back to the camera`, and for gaze only, `camera` → `at the camera`.

### Description length

Answers are allowed to be long. Per-pass token budgets are generous and scale with model
size (the 2.2B model gets double the base), because the earlier tight budgets clipped its
richer descriptions mid-clause. Multi-sentence answers are kept — an earlier version
retained only the first sentence, which was a fair defence against a 256M model rambling
but silently deleted good content from a larger one.

If a pass does hit its ceiling, the dangling fragment is trimmed back to the last clean
boundary rather than shown, and the field is named in the status line so a field that clips
every time is visible. The cap is `MAX_ANSWER_CHARS` in `src/compiler.js`.

### No negative prompt

**Off by default**, because many Perchance generators have no negative prompt field — in
which case emitting one is dead weight, and worse, every realism rejection the era presets
relied on was silently doing nothing.

So those constraints are now stated **positively** in the prompt, the one field the
generator does read:

| Instead of negating | It asks for |
|---|---|
| airbrushed, smooth skin, poreless | `visible skin texture` |
| colour grading, oversaturated, instagram filter | `unedited color` |
| shallow depth of field, creamy bokeh | `deep focus` |
| glamour, fashion model, beauty shot | `plain ordinary features` |

The skin and features terms are gated on the subject being a person — an earlier version
asked a mountain range for visible skin texture.

Turn **Include a negative prompt** back on in Settings if you switch to a generator that
supports one; the substitutes are withdrawn automatically so the constraint isn't stated
twice. Note that removing the negative frees **no** tokens for the prompt: each is encoded
separately with its own 75-token context.

### History reproducibility

**Load** on a past forge restores the whole recipe — era, format, intensity, prompt style,
content level, variant, extra terms and the negative-prompt setting — not just what the
model saw. It also tells you whether the prompt came back *exactly*, since presets may have
changed since it was saved.

### Token budget

CLIP text encoders — what Stable Diffusion uses — accept **75 usable tokens**, and discard
the rest silently. Both prompts are kept inside that, with live counters in the UI.

This mattered more than it sounds. Before budgeting, a realistic result emitted ~82 words
of prompt and 80 negative terms, and the overflow fell exactly where it hurt: the
film/camera/print block trails the prompt, and the anti-AI-look realism terms sat *after*
generic anatomy boilerplate in the negative. The app's whole purpose was in the part being
thrown away — including the `nude` terms that make the Safe content level work.

Emission order is unchanged, since earlier tokens carry more attention weight. Instead
there's an explicit **drop order** (`src/budget.js`): colours go first, then gaze, action,
observed lighting, appearance; surplus era look and artifacts thin next; clothing, pose and
setting are late; and the subject anchor, the era's film stock/camera, and anything you
typed into **Extra prompt terms** are never dropped. Several categories have floors, so a
list thins rather than empties. Anything removed is listed under the prompt.

The negative prompt is reordered by priority: content terms first (losing them changes
behaviour), then the anti-AI-look core, then era-specific terms, then everything else.

The token count is an estimate — a real CLIP tokenizer needs a megabyte of BPE vocabulary
this app has no other use for — deliberately tuned to over-count slightly, since
overflowing loses content while under-filling just wastes a little room.

### Long reads and leaving the app

**A web app cannot keep processing in the background on a phone.** When the OS
backgrounds the app it freezes the renderer — main thread and workers alike. Service
workers aren't an escape hatch: they're killed after ~30s idle and aren't a place to run a
several-hundred-megabyte model. Rather than pretend otherwise, the app attacks the three
things that actually cost you time:

| | |
|---|---|
| **Screen wake lock** | Held during a read, so setting the phone down doesn't stop it. "Backgrounded" is usually just the screen sleeping, so this is the biggest practical win. |
| **Inference in a worker** | The phone stays usable during a read instead of the UI freezing for minutes. Falls back to in-page automatically if a worker can't start. |
| **Checkpoint every pass** | Each answer is saved as it lands. If the OS does freeze or kill the app, reopening offers **Resume** and costs one question instead of the whole image. |

Also in Settings: **Notify me when a read finishes**, which fires if the read completes
while you're looking at something else. Checkpoints older than 24 hours are discarded.

### Content levels

Levels above Safe **permit** nudity and add anatomy support — they do not fabricate
explicit content from a clothed photo. This app reads a photograph; inventing acts it
doesn't show would misrepresent the source. You write what you want in the editable fields
and the free-text box, and the compiler structures it around the era.

Adult levels are withheld unless all three of these hold, and any one failing falls back to
Safe with the reason shown on screen:

1. The model's age read doesn't indicate a minor.
2. The subject and appearance fields don't name a minor.
3. You've confirmed the subject is an adult.

The three are independent on purpose: the observation fields are editable, so the model
check alone would be trivial to edit away. This is not configurable.

---

## Perchance specifics

⚠️ **Set Perchance's art style to `none`.** Its style dropdown is not an API parameter — it
appends text to your prompt *and* negative prompt. Every style except `none` and
`casual-photo` appends `8k, HDR, masterpiece, sharp focus, trending on artstation`, which is
exactly the boilerplate the era presets exist to suppress. Leaving it on `cinematic` undoes
the period realism no matter how good the prompt is.

The generator's real controls are `prompt`, `negativePrompt`, `guidanceScale` (1–30,
default 7), `resolution`, and `seed`. Notably it has **no steps control**, so the step count
this app reports is advisory only, for other tools. Resolution is a discrete string, not a
free ratio — one of `512x768`, `768x768`, `768x512` — so the app maps its ideal framing onto
the nearest of those rather than telling you to set something Perchance doesn't offer.

Provenance for the above, since perchance.org isn't reachable from the build sandbox: two
independently written reverse-engineered clients that agree with each other — PyPI
`perchance` 0.1.0 and npm `perchance-image-generator` 1.0.2. See `src/perchance.js`. It's
advisory text shown to you, never a hard dependency, so if Perchance changes the advice
degrades rather than the app breaking.

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
syntax. So it is asked a series of narrow questions, and a deterministic compiler
(`src/compiler.js`) handles cleanup, vocabulary normalisation, ordering, weighting and the
era overlay. That half is fully unit tested.

**Positive and negative prompts must not fight.** An early-2000s compact *should* produce
JPEG blocks; an 80s print *should* have a white border and a date stamp; a VHS still *is*
low-resolution. The base negative list rejects all of those, so era presets subtract from
it (`negativeExclude`), and a test asserts no era ever negates an artifact it asks for.

**Era tags must match the actual photo.** Presets originally asserted an indoor flash
snapshot on film for every image, which put "hard shadow on the wall behind the subject" on
a beach photo, indoor furniture on an outdoor one, sneakers on a head-and-shoulders crop,
and film grain on a VHS still. So the compiler now infers scene and framing
(`inferScene`) and gates accordingly:

| Condition | Effect |
|---|---|
| Outdoors or daylit | No flash, no wall shadow, no red-eye; daylight look instead |
| Outdoors | Indoor decor suppressed |
| Close-up / waist-up | Garments outside the crop suppressed |
| Framing unknown | Treated conservatively — no full-body garments claimed |
| Clothing already observed | Only a generic era marker added, so it can't contradict |
| Video format | Film grain and film saturation suppressed |

Look tags describe the **photograph**, never the subject's behaviour — an earlier version
emitted "squinting into the sun" for someone smiling with their eyes open.

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
| `src/budget.js` | CLIP token estimation, drop order, negative priority |
| `src/runner.js` | Worker/inline runners, wake lock, checkpointing |
| `src/worker.js` | Inference worker (module worker) |
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
| WebGPU, ≤2 GB RAM | SmolVLM-256M (q4), on GPU | ~180 MB |
| No WebGPU | SmolVLM-256M (q8), on CPU | ~280 MB |

A 2.2B model is available in Settings for better pose reading, but is **never
auto-selected and is unverified** — the model host isn't reachable from the build sandbox,
so it couldn't be confirmed to have an ONNX build. If it fails you'll get a readable error
and can switch back.

Downloaded once, then cached — after that it runs with no connection. On CPU expect a
minute or more per image.

## Development

```bash
node --test test/*.test.mjs                                # 158 unit tests
node test/ui.smoke.mjs                                    # 83 browser checks (needs playwright)
node build/bundle.mjs                                     # rebuild the single-file version
python3 build/make-icons.py                               # regenerate icons
```

Note: pass test files explicitly — `node --test test/` makes Node treat the directory as a
module and fail. The smoke test skips itself if Playwright isn't installed.

## Known limits

- **What is verified about the model path:** the pinned transformers.js build loads in a
  browser and exposes `AutoProcessor` / `AutoModelForVision2Seq`; `apply_chat_template`,
  `batch_decode` and the `Tensor.slice(null, [n, null])` prompt-trim all behave as used;
  `RawImage.read(canvas)` decodes correctly. **What is not:** the weight download and actual
  inference, since the sandbox has no egress to the model host. Real speed and caption
  quality still need a phone.
- transformers.js has **no `image-text-to-text` pipeline** — that task exists in Python
  transformers only, where the same string is a model-architecture mapping name. VLMs go
  through `AutoProcessor` + `AutoModelForVision2Seq`. `runPass` in `src/vision.js` is the
  single place that touches the model's call convention.
- The library version is **pinned deliberately**. A floating range lets behaviour change
  under the app without a commit, which is how the pipeline mistake above stayed hidden
  until it hit a real device.
- Perchance's own control labels could not be checked from that sandbox either, so nothing
  depends on them — era styling is emitted as **prompt text**, which works in any prompt
  box, and handoff is copy-to-clipboard rather than a URL scheme.
- A small vision model misses fine detail and sometimes misnames objects. That is why every
  field is editable.
- Artifact intensity and CFG are the two knobs to tune once you see real generations.
