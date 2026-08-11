/**
 * Verified facts about Perchance's ai-text-to-image-generator.
 *
 * PROVENANCE — this matters, so it is written down rather than assumed.
 * perchance.org is not reachable from the build sandbox, so these values come
 * from two independently written, reverse-engineered client libraries that
 * agree with each other:
 *
 *   - PyPI `perchance` 0.1.0        (perchance/imagegenerator.py)
 *   - npm  `perchance-image-generator` 1.0.2 (lib/imageGenerator.js, src/styles.js)
 *
 * Both post to https://image-generation.perchance.org/api/generate with
 * channel `ai-text-to-image-generator`, and both send exactly these fields:
 *
 *   prompt, negativePrompt, guidanceScale, resolution, seed,
 *   channel, subChannel, userKey, requestId
 *
 * Two consequences drive real decisions in this app:
 *
 * 1. THERE IS NO STYLE PARAMETER. Perchance's art-style dropdown is a text
 *    macro: the npm client builds `${prompt}, ${styleText}` and
 *    `${negativePrompt}, ${styleNegativeText}`. This confirms that emitting era
 *    styling as prompt text — rather than depending on a dropdown — is correct.
 *    It also means the dropdown actively fights this app (see STYLE_NOTES).
 *
 * 2. THERE IS NO STEPS PARAMETER. Neither client sends one, so a step count is
 *    not something the user can set in Perchance. It is reported as advisory
 *    only, for other tools.
 *
 * Being reverse-engineered, this could drift if Perchance changes. Everything
 * here is advisory text shown to the user, never a hard dependency, so drift
 * degrades the advice rather than breaking the app.
 */

/** Guidance scale accepted by the generator; default is 7. */
export const GUIDANCE_RANGE = [1, 30];
export const GUIDANCE_DEFAULT = 7;

/**
 * The resolutions the generator is known to accept, as literal strings.
 *
 * Note this is a short list of discrete shapes, not a free aspect ratio — which
 * is why the app maps its ideal ratio onto the nearest available shape instead
 * of telling the user to set something like "16:9".
 */
export const RESOLUTIONS = [
  { id: 'portrait', value: '512x768', ratio: 512 / 768, label: 'Portrait 512×768' },
  { id: 'square', value: '768x768', ratio: 1, label: 'Square 768×768' },
  { id: 'landscape', value: '768x512', ratio: 768 / 512, label: 'Landscape 768×512' },
];

/**
 * Perchance's built-in art styles, with what each one appends.
 *
 * `safe` marks styles that do not inject quality boilerplate. This is the single
 * most important thing learned from the client libraries: every style except
 * `none` and `casual-photo` appends terms like "8k", "HDR", "masterpiece",
 * "sharp focus" and "trending on artstation" — precisely the boilerplate the era
 * presets exist to suppress. Leaving the dropdown on `cinematic` would undo the
 * period realism no matter how good the prompt is.
 */
export const STYLES = {
  none: { safe: true, appends: '' },
  'casual-photo': {
    safe: true,
    appends: 'casual photo',
    // This style's own negative list is well aligned with period realism.
    appendsNegative:
      'bad photo, bad lighting, high production value, unnatural studio lighting, commercial photoshoot, photoshopped, terrible photo, disfigured',
  },
  cinematic: {
    safe: false,
    appends:
      'cinematic shot, dynamic lighting, 75mm, Technicolor, sharp focus, fine details, 8k, HDR, realism, superb cinematic color grading, depth of field',
  },
  'digital-painting': {
    safe: false,
    appends: 'breathtaking digital art, trending on artstation, 8k, high resolution, best quality',
  },
  'concept-art': {
    safe: false,
    appends: 'concept art, digital art, illustration, 8k, fine details, sharp, masterpiece',
  },
  'painted-anime': {
    safe: false,
    appends: 'painterly anime artwork, masterpiece, fine details, 8k, very detailed, high resolution',
  },
  'traditional-japanese': {
    safe: false,
    appends: 'in ukiyo-e art style, traditional japanese masterpiece',
  },
};

/** Styles that will not sabotage an era preset. */
export const SAFE_STYLES = Object.keys(STYLES).filter((k) => STYLES[k].safe);

export const STYLE_NOTES =
  'Set Perchance\'s art style to "none". Every other style except "casual-photo" ' +
  'appends 8k / HDR / masterpiece / sharp-focus boilerplate to your prompt, which ' +
  'is exactly what stops an image looking like a real period photograph.';

/**
 * Map an aspect-ratio label onto the nearest resolution Perchance offers.
 * Falls back to square when the label is unrecognised.
 */
export function resolutionForAspect(aspectLabel) {
  const parsed = /^(\d+):(\d+)$/.exec(String(aspectLabel || '').trim());
  if (!parsed) return RESOLUTIONS.find((r) => r.id === 'square');

  const ratio = Number(parsed[1]) / Number(parsed[2]);
  let best = RESOLUTIONS[0];
  let bestDelta = Infinity;
  for (const candidate of RESOLUTIONS) {
    const delta = Math.abs(candidate.ratio - ratio);
    if (delta < bestDelta) {
      bestDelta = delta;
      best = candidate;
    }
  }
  return best;
}

/** Keep a recommended guidance value inside what the generator accepts. */
export function clampGuidance(value) {
  const [lo, hi] = GUIDANCE_RANGE;
  const n = Number(value);
  if (!Number.isFinite(n)) return GUIDANCE_DEFAULT;
  return Math.min(hi, Math.max(lo, n));
}

/**
 * The field-by-field checklist for the Perchance page, built from a compiled
 * result. This is what makes the output "Perchance-ready" rather than just a
 * prompt the user has to interpret.
 */
export function perchanceSettings(result) {
  const resolution = resolutionForAspect(result.aspect);
  return {
    style: 'none',
    guidanceScale: clampGuidance(result.cfg.value),
    guidanceRange: GUIDANCE_RANGE,
    resolution,
    seed: -1,
    // Reported for other tools; Perchance does not expose it.
    advisorySteps: result.steps,
  };
}
