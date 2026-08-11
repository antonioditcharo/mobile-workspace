/**
 * The vision (observation) stage.
 *
 * Runs a small vision-language model entirely in the browser, so the app needs
 * no API key, no account, no server, and applies no content filter — and the
 * image never leaves the device.
 *
 * The model is asked a series of *narrow* questions rather than "write me a
 * prompt". A 256M-500M model answers "what is the subject wearing?" reliably;
 * it does not reliably produce structured prompt syntax. Turning those answers
 * into a prompt is `src/compiler.js`'s job.
 *
 * WHY THE LOWER-LEVEL API: transformers.js has no `image-text-to-text`
 * pipeline task — that task exists in Python transformers, but in transformers.js
 * the same string is only a *model architecture* mapping name. Calling
 * `pipeline('image-text-to-text', ...)` therefore fails with "Unsupported
 * pipeline" on every published version. Vision-language models are driven
 * through AutoProcessor + AutoModelForVision2Seq instead, which is what this
 * file does.
 *
 * The version is pinned deliberately. A floating range (`@3`) means the app's
 * behaviour can change under it without a commit, which is how the pipeline
 * mistake above stayed invisible until it hit a real device.
 */

import { OBSERVATION_FIELDS } from './compiler.js';

/**
 * Pinned, and pointing at an explicit file rather than relying on the CDN to
 * pick an entry point. `dist/transformers.min.js` is the self-contained browser
 * build with ESM named exports; `dist/transformers.web.js` cannot be used here
 * because it imports bare specifiers a browser cannot resolve.
 */
export const TRANSFORMERS_VERSION = '3.8.1';
export const TRANSFORMERS_CDN = `https://cdn.jsdelivr.net/npm/@huggingface/transformers@${TRANSFORMERS_VERSION}/dist/transformers.min.js`;

/**
 * Selectable models, smallest first. All are ONNX builds intended for
 * in-browser use.
 */
export const MODELS = {
  'smolvlm-256m-q4': {
    id: 'HuggingFaceTB/SmolVLM-256M-Instruct',
    dtype: 'q4',
    label: 'SmolVLM 256M (q4)',
    approxDownload: '~180 MB',
    note: 'Smallest. Runs on low-RAM devices.',
  },
  'smolvlm-256m-q8': {
    id: 'HuggingFaceTB/SmolVLM-256M-Instruct',
    dtype: 'q8',
    label: 'SmolVLM 256M (q8)',
    approxDownload: '~280 MB',
    note: 'Small and CPU-friendly.',
  },
  'smolvlm-500m-q4f16': {
    id: 'HuggingFaceTB/SmolVLM-500M-Instruct',
    dtype: 'q4f16',
    label: 'SmolVLM 500M (q4f16)',
    approxDownload: '~450 MB',
    note: 'Best captions. Needs WebGPU.',
  },
};

export const DEFAULT_MODEL_KEY = 'smolvlm-256m-q8';

/* ------------------------------------------------------------------ *
 * Capability detection
 * ------------------------------------------------------------------ */

/**
 * Probe the device. `navigator.deviceMemory` is not standard; `deviceMemory`
 * on Chrome is `navigator.deviceMemory` in some builds and
 * `navigator.hardwareConcurrency` is the only widely available signal, so
 * treat every reading as a hint rather than a fact.
 */
export async function detectCapabilities() {
  const caps = {
    webgpu: false,
    adapter: null,
    memoryGb: typeof navigator !== 'undefined' ? navigator.deviceMemory ?? null : null,
    cores: typeof navigator !== 'undefined' ? navigator.hardwareConcurrency ?? null : null,
    notes: [],
  };

  if (typeof navigator !== 'undefined' && navigator.gpu?.requestAdapter) {
    try {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) {
        caps.webgpu = true;
        caps.adapter = adapter.info?.vendor || 'unknown';
        const limit = adapter.limits?.maxBufferSize;
        if (limit) caps.notes.push(`max buffer ${(limit / 1024 / 1024) | 0} MB`);
      } else {
        caps.notes.push('WebGPU present but no adapter granted');
      }
    } catch (err) {
      caps.notes.push(`WebGPU probe failed: ${err.message}`);
    }
  } else {
    caps.notes.push('navigator.gpu unavailable');
  }

  return caps;
}

/**
 * Choose a model from detected capabilities. Returns the choice plus a plain
 * sentence explaining it, which the UI shows so a bad auto-detect is visible
 * and overridable rather than mysterious.
 */
export function chooseModel(caps, override = null) {
  if (override && MODELS[override]) {
    return {
      key: override,
      ...MODELS[override],
      device: caps.webgpu ? 'webgpu' : 'wasm',
      why: 'Chosen manually in settings.',
    };
  }

  const lowMemory = typeof caps.memoryGb === 'number' && caps.memoryGb <= 4;

  if (caps.webgpu && !lowMemory) {
    return {
      key: 'smolvlm-500m-q4f16',
      ...MODELS['smolvlm-500m-q4f16'],
      device: 'webgpu',
      why: `WebGPU is available${caps.adapter ? ` (${caps.adapter})` : ''}, so the larger 500M model can run on the GPU for the best captions.`,
    };
  }

  if (caps.webgpu && lowMemory) {
    return {
      key: 'smolvlm-256m-q4',
      ...MODELS['smolvlm-256m-q4'],
      device: 'webgpu',
      why: `WebGPU is available but the device reports only ${caps.memoryGb} GB of RAM, so the smallest model is used to avoid running out of memory.`,
    };
  }

  return {
    key: 'smolvlm-256m-q8',
    ...MODELS['smolvlm-256m-q8'],
    device: 'wasm',
    why: 'No WebGPU on this browser, so the model runs on the CPU. It works, but expect it to be slow — a minute or more per image is normal.',
  };
}

/* ------------------------------------------------------------------ *
 * Image preparation
 * ------------------------------------------------------------------ */

/**
 * Downscale to `maxEdge` before inference. These models see a small fixed
 * resolution anyway, and shrinking first saves noticeable time on a phone.
 *
 * Returns the canvas as well as a data URL: the canvas feeds inference directly
 * (RawImage reads its pixels, no encode/decode round trip), while the data URL
 * is what the preview <img> displays. Also returns the original dimensions,
 * which the compiler uses to snap the aspect ratio.
 */
export async function prepareImage(blob, maxEdge = 512) {
  const bitmap = await createImageBitmap(blob);
  const original = { width: bitmap.width, height: bitmap.height };

  const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close?.();

  const dataUrl = canvas.toDataURL('image/jpeg', 0.9);
  return { dataUrl, canvas, original, width, height };
}

/* ------------------------------------------------------------------ *
 * Extraction passes
 * ------------------------------------------------------------------ */

/**
 * One narrow question per observation field. Phrasing is deliberately blunt and
 * length-capped: small models ramble, and every extra word is filler the
 * compiler has to strip back out.
 */
export const PASSES = [
  { field: 'subject', question: 'In a few words, what is the main subject of this photo?', tokens: 20 },
  { field: 'appearance', question: "Describe the main subject's appearance in a few words.", tokens: 28 },
  { field: 'clothing', question: 'What is the subject wearing? Answer in a few words.', tokens: 24 },
  { field: 'action', question: 'What is the subject doing? Answer in a few words.', tokens: 20 },
  { field: 'setting', question: 'Where was this taken? Answer in a few words.', tokens: 20 },
  { field: 'colors', question: 'List the two or three most dominant colors.', tokens: 18 },
  { field: 'lighting', question: 'Describe the lighting in a few words.', tokens: 20 },
  {
    field: 'shotType',
    question:
      'Is this a close-up, waist-up shot, full-body shot, or wide shot? Answer with just one.',
    tokens: 12,
  },
];

let cached = { key: null, engine: null };

/**
 * Load the processor and model, reusing them when the selection is unchanged.
 * `onProgress` receives `{ phase, loaded, total, file }`.
 *
 * Returns an "engine" — the processor, the model, and the RawImage constructor
 * taken from the same module instance, so callers never need to import the
 * library themselves.
 */
export async function loadVision(choice, { onProgress = () => {} } = {}) {
  const key = `${choice.id}:${choice.dtype}:${choice.device}`;
  if (cached.key === key && cached.engine) return cached.engine;

  onProgress({ phase: 'library' });
  const module = await import(/* @vite-ignore */ TRANSFORMERS_CDN);

  const { AutoProcessor, AutoModelForVision2Seq, RawImage } = module;
  if (!AutoProcessor || !AutoModelForVision2Seq || !RawImage) {
    throw new Error(
      `transformers.js ${TRANSFORMERS_VERSION} did not expose AutoProcessor, ` +
        'AutoModelForVision2Seq and RawImage. The pinned version may have moved.',
    );
  }

  // The library reports download progress per file; forward it verbatim so the
  // UI can show which shard is landing.
  const relay = (p) => {
    if (p?.status === 'progress') {
      onProgress({ phase: 'download', file: p.file, loaded: p.loaded, total: p.total });
    }
  };

  onProgress({ phase: 'processor' });
  const processor = await AutoProcessor.from_pretrained(choice.id, {
    progress_callback: relay,
  });

  onProgress({ phase: 'model' });
  const model = await AutoModelForVision2Seq.from_pretrained(choice.id, {
    dtype: choice.dtype,
    device: choice.device,
    progress_callback: relay,
  });

  const engine = { processor, model, RawImage, id: choice.id };
  cached = { key, engine };
  return engine;
}

/** Free the loaded model. Useful on a phone when memory is tight. */
export async function unloadVision() {
  try {
    await cached.engine?.model?.dispose?.();
  } catch {
    /* disposal is best-effort */
  }
  cached = { key: null, engine: null };
}

/**
 * Run a single question against an already-decoded image.
 *
 * This is the one function that touches the model's call convention, so it is
 * the only place to adjust if a future library version changes it.
 */
async function runPass(engine, image, question, maxTokens) {
  const messages = [
    {
      role: 'user',
      content: [{ type: 'image' }, { type: 'text', text: question }],
    },
  ];

  // The chat template inserts the image placeholder token the processor expands.
  const text = engine.processor.apply_chat_template(messages, {
    add_generation_prompt: true,
  });

  const inputs = await engine.processor(text, [image]);

  const output = await engine.model.generate({
    ...inputs,
    max_new_tokens: maxTokens,
    do_sample: false,
  });

  // generate() returns the prompt tokens followed by the completion. Decoding
  // the whole thing would hand the compiler back its own question, so keep only
  // the newly generated tail.
  const promptLength = inputs.input_ids.dims.at(-1);
  const generated = output.slice(null, [promptLength, null]);

  return extractText(engine.processor.batch_decode(generated, { skip_special_tokens: true }));
}

/**
 * Pull the assistant's text out of whatever shape the library returns.
 * Written permissively on purpose: the wrapper shape has changed across
 * versions, and a caption is not worth crashing over.
 */
export function extractText(output) {
  if (typeof output === 'string') return output;
  if (Array.isArray(output)) {
    if (!output.length) return '';
    return extractText(output[0]);
  }
  if (output && typeof output === 'object') {
    if (typeof output.generated_text === 'string') return output.generated_text;
    if (Array.isArray(output.generated_text)) {
      const last = output.generated_text[output.generated_text.length - 1];
      return extractText(last);
    }
    if (typeof output.content === 'string') return output.content;
    if (Array.isArray(output.content)) {
      return output.content.map((c) => (typeof c === 'string' ? c : c?.text || '')).join(' ');
    }
    if (typeof output.text === 'string') return output.text;
  }
  return '';
}

/**
 * Run every pass over one image and return a raw observation object keyed by
 * `OBSERVATION_FIELDS`. Cleanup happens in the compiler, not here.
 *
 * A failed individual pass is left empty rather than aborting the run — seven
 * good fields still make a usable prompt.
 */
export async function observe(engine, source, { onProgress = () => {}, signal } = {}) {
  const observation = {};
  const failures = [];

  // Decode once and reuse across all eight passes — re-decoding per question
  // would be the single most wasteful thing this loop could do on a phone.
  // `read` dispatches on type, so a canvas (the fast path, straight from
  // prepareImage), a Blob, or a URL all work.
  const image = await engine.RawImage.read(source);

  for (let i = 0; i < PASSES.length; i++) {
    if (signal?.aborted) throw new DOMException('Observation cancelled', 'AbortError');
    const pass = PASSES[i];
    onProgress({ index: i, total: PASSES.length, field: pass.field });
    try {
      observation[pass.field] = await runPass(engine, image, pass.question, pass.tokens);
    } catch (err) {
      observation[pass.field] = '';
      failures.push(`${pass.field}: ${err.message}`);
    }
  }

  for (const field of OBSERVATION_FIELDS) {
    if (!(field in observation)) observation[field] = '';
  }

  return { observation, failures };
}

/** Turn a thrown error into something worth showing a user. */
export function describeError(err) {
  // Include the error *name*: AbortError carries its meaning there, not in the
  // message, so matching on message alone reports a cancel as a crash.
  const message = `${err?.name || ''} ${err?.message || err}`.trim();
  if (/abort|cancel/i.test(message)) return 'Cancelled.';
  if (/out of memory|allocation|OOM/i.test(message)) {
    return 'The device ran out of memory loading the model. Pick a smaller model in Settings.';
  }
  if (/network|fetch|Failed to load|CORS|ERR_/i.test(message)) {
    return 'Could not download the model. Check your connection and try again — the first load needs internet, after that it works offline.';
  }
  if (/webgpu|adapter|device lost/i.test(message)) {
    return 'The GPU backend failed. Switch to the CPU model in Settings.';
  }
  return `Model error: ${message}`;
}
