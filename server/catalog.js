/**
 * Model catalog.
 *
 * These are open-weight video models reachable through Hugging Face. Which ones
 * respond for a given account depends on the inference providers enabled for
 * that account — availability moves around, so the app treats this as a
 * starting list, not a guarantee. Any model id can be typed in by hand, and
 * `custom` bypasses the list entirely.
 */

/**
 * `maxFrames` is the most a model produces in a single pass before coherence
 * degrades. Longer requests are chained across segments — see server/ffmpeg.js.
 * `continuation` names the image-to-video model used to extend a clip.
 */
const MODELS = [
  {
    id: 'Wan-AI/Wan2.2-T2V-A14B',
    label: 'Wan 2.2 T2V (14B)',
    kind: 'text-to-video',
    notes: 'Strongest open text-to-video for realism at time of writing. Slow.',
    realism: 5,
    maxFrames: 81,
    continuation: 'Wan-AI/Wan2.2-I2V-A14B',
    defaultParams: { num_frames: 81, fps: 16, guidance_scale: 5.0, num_inference_steps: 40 },
  },
  {
    id: 'Wan-AI/Wan2.1-T2V-14B',
    label: 'Wan 2.1 T2V (14B)',
    kind: 'text-to-video',
    notes: 'Previous generation. More widely available across providers.',
    realism: 4,
    maxFrames: 81,
    continuation: 'Wan-AI/Wan2.2-I2V-A14B',
    defaultParams: { num_frames: 81, fps: 16, guidance_scale: 5.0, num_inference_steps: 40 },
  },
  {
    id: 'Wan-AI/Wan2.2-I2V-A14B',
    label: 'Wan 2.2 I2V (14B)',
    kind: 'image-to-video',
    notes: 'Animate a still. Best realism route — start from a real photograph.',
    realism: 5,
    maxFrames: 81,
    continuation: 'Wan-AI/Wan2.2-I2V-A14B',
    defaultParams: { num_frames: 81, fps: 16, guidance_scale: 5.0, num_inference_steps: 40 },
  },
  {
    id: 'Lightricks/LTX-Video',
    label: 'LTX-Video',
    kind: 'text-to-video',
    notes: 'Much faster, lower fidelity. Good for iterating on a prompt.',
    realism: 3,
    maxFrames: 121,
    continuation: 'Lightricks/LTX-Video',
    defaultParams: { num_frames: 121, fps: 24, guidance_scale: 3.0, num_inference_steps: 30 },
  },
  {
    id: 'tencent/HunyuanVideo',
    label: 'Hunyuan Video',
    kind: 'text-to-video',
    notes: 'Strong motion coherence. Heavy.',
    realism: 4,
    maxFrames: 65,
    continuation: null,
    defaultParams: { num_frames: 65, fps: 24, guidance_scale: 6.0, num_inference_steps: 30 },
  },
  {
    id: 'THUDM/CogVideoX-5b',
    label: 'CogVideoX 5B',
    kind: 'text-to-video',
    notes: 'Lighter weight, widely hosted. Softer detail.',
    realism: 3,
    maxFrames: 49,
    continuation: null,
    defaultParams: { num_frames: 49, fps: 8, guidance_scale: 6.0, num_inference_steps: 50 },
  },
  {
    id: 'genmo/mochi-1-preview',
    label: 'Mochi 1 (preview)',
    kind: 'text-to-video',
    notes: 'Good physical motion. Preview quality.',
    realism: 3,
    maxFrames: 61,
    continuation: null,
    defaultParams: { num_frames: 61, fps: 30, guidance_scale: 4.5, num_inference_steps: 40 },
  },
  {
    id: 'stabilityai/stable-video-diffusion-img2vid-xt',
    label: 'Stable Video Diffusion XT',
    kind: 'image-to-video',
    notes: 'Short clips from a still. Reliable, limited motion range.',
    realism: 3,
    maxFrames: 25,
    continuation: 'stabilityai/stable-video-diffusion-img2vid-xt',
    defaultParams: { num_frames: 25, fps: 6, guidance_scale: 3.0, num_inference_steps: 25 },
  },
];

/**
 * Quality presets. Step count is the main lever: more denoising steps means
 * more time and more detail, with returns flattening off past roughly 50.
 * `timeFactor` is relative to standard, for the estimate shown in the UI.
 */
const QUALITY = {
  draft: {
    label: 'Draft',
    steps: 18,
    timeFactor: 0.5,
    summary: 'Fast and rough. For checking composition and motion before committing.',
  },
  standard: {
    label: 'Standard',
    steps: 32,
    timeFactor: 1,
    summary: 'The usual balance of detail and time.',
  },
  high: {
    label: 'High',
    steps: 50,
    timeFactor: 1.6,
    summary: 'Noticeably finer texture and more stable motion. Worth it for a keeper.',
  },
  max: {
    label: 'Maximum',
    steps: 75,
    timeFactor: 2.4,
    summary: 'Diminishing returns past here — mostly buys marginal texture stability.',
  },
};

const MAX_DURATION_SECONDS = 20;

/**
 * Work out how to reach a target duration for a given model.
 *
 * Models cap out well short of 20 seconds, so anything longer is chained:
 * each segment resumes from the last frame of the one before it. Segments
 * after the first need an image-to-video model, so a model with no
 * `continuation` cannot be extended.
 */
function planSegments({
  model, durationSeconds, fps, maxFrames: maxFramesOverride, continuation: continuationOverride,
}) {
  const spec = findModel(model);
  // A local backend reports its own per-pass ceiling, which depends on its GPU
  // and frame size rather than on the hosted model named in the UI.
  const maxFrames = maxFramesOverride || spec?.maxFrames || 81;
  const rate = fps || spec?.defaultParams?.fps || 16;
  const target = Math.max(1, Math.min(durationSeconds || 5, MAX_DURATION_SECONDS));

  const totalFrames = Math.ceil(target * rate);
  const wanted = Math.max(1, Math.ceil(totalFrames / maxFrames));

  const continuation = continuationOverride !== undefined
    ? continuationOverride
    : (spec?.continuation || null);
  const chainable = wanted === 1 || Boolean(continuation);
  const segments = chainable ? wanted : 1;

  // Spread frames evenly across the passes that will actually run. Dividing by
  // the number wanted rather than the number used would shorten a clip that
  // cannot be chained: asking for 5s from a model limited to one pass would
  // produce a third of a clip instead of as much as one pass can hold.
  const perSegment = Math.min(maxFrames, Math.ceil(totalFrames / segments));
  const delivered = perSegment * segments;

  return {
    segments,
    framesPerSegment: perSegment,
    fps: rate,
    totalFrames: delivered,
    actualSeconds: +((delivered / rate).toFixed(1)),
    continuation,
    chainable,
    truncated: delivered < totalFrames,
    reason: chainable
      ? null
      : `${spec?.label || model} has no image-to-video counterpart, so it cannot be extended past one pass `
        + `of ${maxFrames} frames (${(maxFrames / rate).toFixed(1)}s at ${rate}fps).`,
  };
}

function findModel(id) {
  return MODELS.find((m) => m.id === id) || null;
}

function defaultParamsFor(id) {
  const model = findModel(id);
  return model ? { ...model.defaultParams } : { num_frames: 81, fps: 16 };
}

module.exports = {
  MODELS, QUALITY, MAX_DURATION_SECONDS, findModel, defaultParamsFor, planSegments,
};
