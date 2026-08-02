/**
 * Model catalog.
 *
 * These are open-weight video models reachable through Hugging Face. Which ones
 * respond for a given account depends on the inference providers enabled for
 * that account — availability moves around, so the app treats this as a
 * starting list, not a guarantee. Any model id can be typed in by hand, and
 * `custom` bypasses the list entirely.
 */

const MODELS = [
  {
    id: 'Wan-AI/Wan2.2-T2V-A14B',
    label: 'Wan 2.2 T2V (14B)',
    kind: 'text-to-video',
    notes: 'Strongest open text-to-video for realism at time of writing. Slow.',
    realism: 5,
    defaultParams: { num_frames: 81, fps: 16, guidance_scale: 5.0, num_inference_steps: 40 },
  },
  {
    id: 'Wan-AI/Wan2.1-T2V-14B',
    label: 'Wan 2.1 T2V (14B)',
    kind: 'text-to-video',
    notes: 'Previous generation. More widely available across providers.',
    realism: 4,
    defaultParams: { num_frames: 81, fps: 16, guidance_scale: 5.0, num_inference_steps: 40 },
  },
  {
    id: 'Wan-AI/Wan2.2-I2V-A14B',
    label: 'Wan 2.2 I2V (14B)',
    kind: 'image-to-video',
    notes: 'Animate a still. Best realism route — start from a real photograph.',
    realism: 5,
    defaultParams: { num_frames: 81, fps: 16, guidance_scale: 5.0, num_inference_steps: 40 },
  },
  {
    id: 'Lightricks/LTX-Video',
    label: 'LTX-Video',
    kind: 'text-to-video',
    notes: 'Much faster, lower fidelity. Good for iterating on a prompt.',
    realism: 3,
    defaultParams: { num_frames: 121, fps: 24, guidance_scale: 3.0, num_inference_steps: 30 },
  },
  {
    id: 'tencent/HunyuanVideo',
    label: 'Hunyuan Video',
    kind: 'text-to-video',
    notes: 'Strong motion coherence. Heavy.',
    realism: 4,
    defaultParams: { num_frames: 65, fps: 24, guidance_scale: 6.0, num_inference_steps: 30 },
  },
  {
    id: 'THUDM/CogVideoX-5b',
    label: 'CogVideoX 5B',
    kind: 'text-to-video',
    notes: 'Lighter weight, widely hosted. Softer detail.',
    realism: 3,
    defaultParams: { num_frames: 49, fps: 8, guidance_scale: 6.0, num_inference_steps: 50 },
  },
  {
    id: 'genmo/mochi-1-preview',
    label: 'Mochi 1 (preview)',
    kind: 'text-to-video',
    notes: 'Good physical motion. Preview quality.',
    realism: 3,
    defaultParams: { num_frames: 61, fps: 30, guidance_scale: 4.5, num_inference_steps: 40 },
  },
  {
    id: 'stabilityai/stable-video-diffusion-img2vid-xt',
    label: 'Stable Video Diffusion XT',
    kind: 'image-to-video',
    notes: 'Short clips from a still. Reliable, limited motion range.',
    realism: 3,
    defaultParams: { num_frames: 25, fps: 6, guidance_scale: 3.0, num_inference_steps: 25 },
  },
];

function findModel(id) {
  return MODELS.find((m) => m.id === id) || null;
}

function defaultParamsFor(id) {
  const model = findModel(id);
  return model ? { ...model.defaultParams } : { num_frames: 81, fps: 16 };
}

module.exports = { MODELS, findModel, defaultParamsFor };
