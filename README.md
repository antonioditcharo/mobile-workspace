# RealFrame

Video generation tuned for one thing: output that reads as **footage**, not as a render.

Diffusion video models default to a recognizable look — waxy skin, perfect symmetry,
glossy highlights, impossibly smooth camera motion. RealFrame fights that with a prompt
compiler that describes a *physical capture* (a real camera, a real lens, real light, and
the imperfections that come with all three), plus a negative prompt built to suppress the
render aesthetic.

Backends: Hugging Face, or any HTTP endpoint you control.

---

## Quick start

```bash
cp .env.example .env      # add your HF_TOKEN
npm start                 # http://localhost:3000
```

No dependencies to install — the server uses only the Node standard library. Node 18+.

```bash
npm test                  # 78 tests, no network required
```

---

## The realism engine

This is the part that does the work. Everything else is plumbing around it.

### Capture presets

Nine shooting setups, each a plausible real-world rig rather than a vibe:

| Preset | What it emulates |
| --- | --- |
| `documentary` | Handheld observational, available light, long lens |
| `cinematic_film` | ARRI Alexa, 40mm spherical, 24fps at a 180° shutter |
| `smartphone_candid` | Phone video — the most convincing register for most scenes |
| `security_cam` | Fixed CCTV, because nobody bothers faking this look |
| `broadcast_news` | ENG field camera with a hard on-camera key |
| `vintage_home_video` | Hi8 camcorder, 1994 |
| `nature_doc` | 600mm wildlife capture with heat haze |
| `drone_aerial` | Real gimbal signature — correction wobble, prop jitter |
| `portrait_interview` | Seated interview; the best preset for faces |

### Intensity

Presets apply in layers, least to most aggressive. `intensity` (0–4) decides how many land:

- **0** — passthrough, negative prompt only
- **1** — camera body and basic lighting
- **2** — optical character, depth of field
- **3** — full capture stack, plus skin/hair/fabric detail if a person is detected
- **4** — every imperfection layer, including capture faults (focus misses, flare, exposure pumping)

### Anti-pattern detection

The highest-value feature, and the least obvious. A set of terms *sound* like they ask for
realism but reliably push diffusion models toward the rendered look, because that is what
they were captioned alongside during training:

> `8k` · `4k` · `photorealistic` · `hyperrealistic` · `ultra detailed` · `masterpiece` ·
> `best quality` · `award winning` · `epic` · `stunning` · `perfect` · `flawless` ·
> `cinematic` · `beautiful lighting` · `trending on artstation` · `concept art`

RealFrame flags each one with a reason, and (by default) strips it from your text before
compiling — repairing the grammar left behind, so `"a hyperrealistic 8k masterpiece of a
woman stepping off a bus"` becomes `"a woman stepping off a bus"` rather than
`"a of a woman stepping off a bus"`.

### Realism score

0–100, scored on whether the prompt describes a physical capture: does it name a device, a
focal length, an aperture, camera movement, a real light source, optical imperfection,
surface imperfection? Shown before and after compilation so you can see what the engine
added. It grades prompt *construction*, not preset quality — sparse presets like
`security_cam` score lower by design.

### Length

Models cap out around 3–5 seconds in a single pass — past that, coherence
collapses and VRAM runs out. Longer clips are **chained**: generate a segment,
take its final frame, continue from that frame with image-to-video, repeat, then
stitch. The length slider goes to 20 seconds and the UI shows how many passes
that costs before you commit.

Two things to know. Chaining multiplies the time roughly by the segment count.
And detail drifts a little at each join — faces and clothing wander over a
20-second clip in a way they don't over 5. Shorter is usually the stronger
result.

Chaining needs an image-to-video counterpart for the model (`continuation` in
the catalog) and an ffmpeg binary for the frame extraction and stitching. If
you've set up the local GPU server, its Python environment already ships one and
it's found automatically; otherwise set `FFMPEG_PATH`.

### Frame rate

Video models generate at 8-24fps because every frame costs memory and time. The
**Smoothness** control raises the delivered rate afterwards using
motion-compensated interpolation — ffmpeg estimates motion between generated
frames and synthesises the ones between. It runs on the CPU once the clip
exists, so it costs no GPU budget and does not shorten the clip. Applied after
stitching, so chained joins are smoothed too.

### Quality

The **Quality** control sets denoising steps — draft 18, standard 32, high 50,
maximum 75 — and writes the number into the visible Steps field, so nothing is
applied invisibly. More steps means more time and finer detail, with returns
flattening off past roughly 50. Typing a step count yourself overrides the
preset.

Steps are the main lever, but not the only one. Resolution matters as much on
local hardware (`LOCAL_WIDTH`/`LOCAL_HEIGHT`), and guidance scale trades prompt
adherence against naturalness — lower values often look *more* real, since high
guidance produces the over-saturated, over-composed look.

### What actually moves the needle

In rough order of impact:

1. **Start from a real photograph.** Image-to-video with a real still beats anything
   text-to-video will give you — the hardest part of the problem is already solved by the
   photo. Use the start-frame upload under **Model & parameters**; selecting a frame
   switches to an image-to-video model automatically, and warns you if you later pick a
   text-to-video one that would ignore it.
2. **Pick an unglamorous register.** `smartphone_candid` and `security_cam` read as real
   because the failure modes people associate with fakery — cinematic grading, perfect
   motion — are absent.
3. **Describe the action plainly.** Let the engine supply the camera. Adjective stacking
   fights it.
4. **Ask for specific imperfections.** Grain, focus hunting, blown highlights, a smudge on
   the lens.
5. **Keep clips short.** Coherence degrades with length; artifacts are the giveaway.

---

## Configuration

All settings live in `.env` (see `.env.example`). `process.env` wins over the file.

### Hugging Face

```env
PROVIDER=hf
HF_TOKEN=hf_...
DEFAULT_MODEL=Wan-AI/Wan2.2-T2V-A14B
```

Create a token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
with **"Make calls to Inference Providers"** enabled.

Requests go to `router.huggingface.co`, falling back to the legacy `api-inference` host on
a 404 — which endpoint serves a given model moves around, and this survives that. Which
models actually respond depends on the inference providers enabled for your account; the
catalog in `server/catalog.js` is a starting list, not a guarantee. Any model id can be
typed into the model field directly.

Which partner actually serves a model changes over time, so routing looks it up
rather than assuming. `HF_PROVIDER=auto` queries the Hub's provider mapping and
tries the live providers in turn; set `HF_PROVIDER=fal-ai` (or `replicate`,
`novita`) to pin one. The **Check availability** button reports what a given
model is served by. Note that `hf-inference` is HF's own serverless pool and
does not host large video models — those are always on a partner.

### Your own GPU — free and unlimited

`local/` contains a small server that runs an open model on your own NVIDIA card
and speaks the custom-endpoint protocol, so the app and realism engine work
unchanged. No credits, no rate limits.

```env
PROVIDER=custom
CUSTOM_ENDPOINT=http://localhost:8000/generate
```

Windows: double-click `local/start-local-gpu.bat`. It builds an isolated Python
environment, installs PyTorch with CUDA, checks the card, and starts serving.
6 GB VRAM is the realistic floor, which fits the 1.3–2B models rather than the
14B ones. Precision is chosen per-device — Turing cards (GTX 16xx, RTX 20xx)
get `float16` since they have no hardware `bfloat16`. See `local/README.md`.

### Any other endpoint

```env
PROVIDER=custom
CUSTOM_ENDPOINT=http://localhost:8188/generate
CUSTOM_AUTH_HEADER=Authorization
CUSTOM_AUTH_VALUE=Bearer your-key
```

Points at anything that speaks HTTP: a local ComfyUI or diffusers wrapper, a rented GPU
box, a dedicated HF Inference Endpoint. Your weights, your hardware, nothing in between —
no shared queue, no third-party terms, no rate limit but your own.

If your endpoint expects a different request shape:

```env
CUSTOM_BODY_TEMPLATE={"text":"{{prompt}}","avoid":"{{negative_prompt}}","steps":30}
```

Placeholders: `{{prompt}}`, `{{negative_prompt}}`, `{{model}}`.

Responses are parsed permissively — raw video bytes, base64 in a JSON envelope, or a URL to
download all work, so most endpoints need no adapter.

### Server

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` / `HOST` | `3000` / `0.0.0.0` | Listen address |
| `OUTPUT_DIR` | `outputs` | Where videos and metadata land |
| `MAX_CONCURRENT` | `1` | Parallel generations |
| `JOB_TIMEOUT_MS` | `900000` | Per-job ceiling (15 min) |
| `RETAIN_JOBS` | `100` | Jobs kept in the gallery |

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Models, presets, negative groups, backend readiness |
| `POST` | `/api/compile` | Compile a subject into a prompt pair — no generation |
| `POST` | `/api/analyze` | Score a raw description and flag anti-patterns |
| `POST` | `/api/generate` | Enqueue a job → `202 {id, compiled}` |
| `GET` | `/api/jobs` | All jobs, newest first |
| `GET` | `/api/jobs/:id` | One job's status |
| `DELETE` | `/api/jobs/:id` | Cancel if running, delete if finished; `?purge=true` forces deletion |
| `DELETE` | `/api/jobs/all` | Delete every finished render |
| `GET` | `/api/probe?model=` | Which providers serve a model |
| `GET` | `/api/video/:id` | Stream the result (supports range requests) |

Generation runs for minutes, well past any sane HTTP timeout, so `/api/generate` returns
immediately and the client polls. Finished videos and their metadata are written to
`OUTPUT_DIR` and reload into the gallery on restart.

`compile` is separate from `generate` on purpose: iterate on prompts for free, spend GPU
time only when the compiled prompt looks right.

```bash
curl -s -X POST localhost:3000/api/compile \
  -H 'Content-Type: application/json' \
  -d '{"subject":"a woman stepping off a bus in the rain","preset":"documentary","intensity":3}'
```

---

## Layout

```
server/
  index.js       HTTP server, routing, static files, range-aware video streaming
  realism.js     Prompt compiler, presets, anti-patterns, scoring  ← the core
  providers.js   Hugging Face + custom endpoint, retries, response normalization
  jobs.js        Async queue, cancellation, disk persistence
  catalog.js     Model list, quality presets, segment planning for long clips
  ffmpeg.js      Frame extraction and stitching for chained segments
  config.js      .env parsing
public/          Frontend — no framework, no build step
test/            78 tests, all offline (providers are faked)
```

---

## Notes on scope

- **Model filtering is the provider's, not this app's.** RealFrame adds no content
  filtering of its own, and it also contains no code for defeating what a host enforces —
  that's their infrastructure and their terms. If you want a model with no third party in
  the loop, run your own weights and point `PROVIDER=custom` at them; that path is
  first-class here, not an afterthought.
- **Realistic output carries obligations.** Video that reads as real footage of real people
  is exactly the kind of thing that misleads when presented as authentic. Label synthetic
  media, and don't generate identifiable people without their consent.
- **Endpoints drift.** Hosted inference APIs change shape more often than the models do.
  `providers.js` is deliberately tolerant, and its failure messages are written to tell you
  which knob to turn.
