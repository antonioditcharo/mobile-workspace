/**
 * Realism engine.
 *
 * Diffusion video models drift toward a "rendered" look by default: waxy skin,
 * perfect symmetry, glossy highlights, impossibly smooth camera motion. Getting
 * output that reads as *footage* rather than *render* is mostly a matter of
 * describing a physical capture — a real body, a real lens, real light, and the
 * imperfections that come with all three.
 *
 * This module compiles a plain subject description into that kind of prompt.
 */

/**
 * Capture presets. Each one describes a plausible real-world shooting setup.
 * Layers are ordered least-to-most aggressive; `intensity` decides how many
 * get applied, so a user can dial realism cues up without rewriting anything.
 */
const PRESETS = {
  documentary: {
    label: 'Documentary',
    summary: 'Handheld observational footage, available light, long lens.',
    camera: 'shot on a Sony FX3, 85mm lens at f/2.0',
    layers: [
      ['available light only', 'natural color temperature', 'handheld'],
      ['subtle handheld micro-shake', 'shallow depth of field', 'slight focus hunting'],
      ['sensor noise in the shadows', 'imperfect framing, subject slightly off-center'],
      ['lens breathing on focus pulls', 'brief overexposure as the subject moves toward a window'],
    ],
  },
  cinematic_film: {
    label: 'Cinematic film',
    summary: '35mm motion picture capture, motivated lighting, 180° shutter.',
    camera: 'shot on an ARRI Alexa 35, 40mm spherical lens at T2.8, 24fps, 180 degree shutter',
    layers: [
      ['motivated practical lighting', 'natural motion blur', 'Kodak Vision3 500T color response'],
      ['fine film grain', 'soft highlight rolloff', 'shallow depth of field with natural bokeh'],
      ['slight halation around bright practicals', 'gentle lens vignette'],
      ['faint chromatic aberration at the frame edges', 'organic dolly move with slight settle at the end'],
    ],
  },
  smartphone_candid: {
    label: 'Smartphone candid',
    summary: 'Phone video — the most convincing realism register for most scenes.',
    camera: 'shot on an iPhone 15 Pro, main wide camera, 4K 30fps',
    layers: [
      ['casual handheld framing', 'ambient indoor light', 'no color grading'],
      ['digital stabilization artifacts', 'aggressive auto-exposure adjustments'],
      ['minor rolling shutter skew on fast pans', 'slight overprocessed sharpening', 'HDR tone mapping'],
      ['smudge on the lens catching a light source', 'autofocus briefly missing then snapping back'],
    ],
  },
  security_cam: {
    label: 'Security camera',
    summary: 'Fixed CCTV — reads as real because nobody fakes this look.',
    camera: 'fixed CCTV camera, wide angle lens, high mounting position looking down',
    layers: [
      ['flat lighting', 'low contrast', 'slightly desaturated'],
      ['low frame rate with slight motion stutter', 'compression artifacts', 'fixed locked-off frame'],
      ['barrel distortion from the wide lens', 'infrared cast', 'visible sensor noise'],
      ['interlacing artifacts on movement', 'blown-out window in the background'],
    ],
  },
  broadcast_news: {
    label: 'Broadcast news',
    summary: 'ENG field camera, on-camera light, clean but not cinematic.',
    camera: 'shot on a broadcast ENG camera, 1/3 inch sensor, on-camera light',
    layers: [
      ['flat news lighting', 'deep depth of field', 'neutral white balance'],
      ['slight video sharpening halo', 'shoulder-mounted stability'],
      ['harsh on-camera key light with a hard shadow behind the subject'],
      ['minor tape compression artifacts', 'wind buffeting the frame slightly'],
    ],
  },
  vintage_home_video: {
    label: 'Vintage home video',
    summary: 'Consumer camcorder — heavy artifacting sells authenticity.',
    camera: 'shot on a Hi8 camcorder, 1994',
    layers: [
      ['soft focus', 'warm color cast', 'limited dynamic range'],
      ['analog tape noise', 'chroma bleeding', 'slight tracking distortion'],
      ['auto-exposure pumping', 'zoom rocker movement mid-shot'],
      ['timecode burn-in in the corner', 'horizontal interference bands'],
    ],
  },
  nature_doc: {
    label: 'Nature documentary',
    summary: 'Long-lens wildlife capture, natural light, patient framing.',
    camera: 'shot on a RED Komodo with a 600mm telephoto lens at f/5.6',
    layers: [
      ['golden hour natural light', 'extremely shallow depth of field', 'compressed perspective'],
      ['heat haze shimmer between camera and subject', 'gentle tripod-head tracking'],
      ['foreground foliage partially obscuring the frame edge', 'atmospheric depth'],
      ['subject briefly drifting out of focus', 'slight tripod vibration in wind'],
    ],
  },
  drone_aerial: {
    label: 'Drone aerial',
    summary: 'Airborne footage with the mechanical signature of a real gimbal.',
    camera: 'shot on a DJI Mavic 3, 24mm equivalent lens, 4K',
    layers: [
      ['overcast diffused daylight', 'smooth forward flight', 'high vantage point'],
      ['slight gimbal correction wobble', 'atmospheric haze over distance'],
      ['wind drift causing minor lateral correction', 'propeller vibration micro-jitter'],
      ['sun flare across the lens on turn', 'slight barrel distortion at frame edges'],
    ],
  },
  portrait_interview: {
    label: 'Interview portrait',
    summary: 'Seated interview setup — best preset for convincing human faces.',
    camera: 'shot on a Canon C70, 50mm lens at f/2.2, eye level',
    layers: [
      ['soft key light from a window', 'natural skin tones', 'shallow depth of field'],
      ['visible skin texture and pores', 'natural asymmetry in the face', 'subtle blinking'],
      ['stray hairs catching the backlight', 'slight sheen on the forehead', 'micro-expressions'],
      ['subject shifting weight in the chair', 'breath visible in the shoulders'],
    ],
  },
};

/**
 * Subject-level realism cues, applied when the prompt looks like it involves
 * people. Faces are where the render look is most obvious, so these get
 * injected separately from the capture layers.
 */
const HUMAN_DETAIL = [
  'visible skin pores and texture',
  'natural facial asymmetry',
  'subtle skin blemishes',
  'natural eye moisture and catchlights',
  'realistic hair strands with flyaways',
  'fabric with natural wrinkles and drape',
];

const HUMAN_HINTS = [
  'person', 'people', 'man', 'men', 'woman', 'women', 'boy', 'girl', 'child',
  'children', 'kid', 'guy', 'lady', 'face', 'portrait', 'crowd', 'someone',
  'he ', 'she ', 'they ', 'his ', 'her ', 'their ', 'human', 'worker',
  'teenager', 'adult', 'elderly', 'baby', 'couple', 'family',
];

/**
 * Negative prompt terms, grouped so the UI can explain what each block is for.
 */
const NEGATIVE_GROUPS = {
  render: [
    '3d render', 'cgi', 'unreal engine', 'octane render', 'blender',
    'video game', 'animation', 'cartoon', 'anime', 'illustration', 'painting',
  ],
  skin: [
    'plastic skin', 'waxy skin', 'airbrushed', 'smooth poreless skin',
    'mannequin', 'doll-like', 'uncanny valley',
  ],
  grade: [
    'oversaturated', 'HDR', 'overprocessed', 'heavy color grading',
    'teal and orange grade', 'glowing highlights', 'bloom',
  ],
  optics: [
    'over-sharpened', 'perfect symmetry', 'artificial lighting',
    'studio lighting', 'flawless composition', 'infinite depth of field',
  ],
  artifacts: [
    'text', 'watermark', 'logo', 'subtitles', 'distorted hands', 'extra fingers',
    'extra limbs', 'deformed face', 'morphing', 'flickering', 'warping',
  ],
};

/**
 * Terms that *sound* like they ask for realism but reliably push diffusion
 * models toward the rendered look, because that is what they were captioned
 * alongside in training data. Flagging these is the single highest-value
 * thing this engine does.
 */
const ANTI_PATTERNS = [
  { term: '8k', why: 'Trains toward upscaled render stills, not camera footage.' },
  { term: '4k', why: 'Same as 8k — associated with render showcases. Name a camera instead.' },
  { term: 'hyperrealistic', why: 'Paradoxically pulls toward glossy digital art. Describe the capture instead.' },
  { term: 'hyper realistic', why: 'Paradoxically pulls toward glossy digital art. Describe the capture instead.' },
  { term: 'photorealistic', why: 'Common caption on 3D renders that imitate photos. Say "shot on <camera>" instead.' },
  { term: 'ultra detailed', why: 'Pushes toward over-sharpened, micro-contrast-heavy render aesthetics.' },
  { term: 'hyperdetailed', why: 'Pushes toward over-sharpened, micro-contrast-heavy render aesthetics.' },
  { term: 'masterpiece', why: 'Booru-style quality tag. Biases toward illustration.' },
  { term: 'best quality', why: 'Booru-style quality tag. Biases toward illustration.' },
  { term: 'award winning', why: 'Biases toward stylized contest photography, not ordinary footage.' },
  { term: 'epic', why: 'Biases toward dramatic CGI compositions.' },
  { term: 'stunning', why: 'Biases toward over-graded stock imagery.' },
  { term: 'beautiful lighting', why: 'Produces artificial studio setups. Name a real light source instead.' },
  { term: 'perfect', why: 'Perfection is the primary tell of a render. Ask for imperfection.' },
  { term: 'flawless', why: 'Perfection is the primary tell of a render. Ask for imperfection.' },
  { term: 'cinematic', why: 'Overloaded token — often yields teal-and-orange grading. Use a preset instead.' },
  { term: 'trending on artstation', why: 'Explicitly requests digital art.' },
  { term: 'unreal engine', why: 'Explicitly requests a game render.' },
  { term: 'digital art', why: 'Explicitly requests illustration.' },
  { term: 'concept art', why: 'Explicitly requests illustration.' },
];

/** Signals that a prompt is doing the right thing. */
const POSITIVE_SIGNALS = [
  { pattern: /\bshot on\b|\bfilmed on\b|\bcaptured on\b/i, points: 12, label: 'Names a capture device' },
  { pattern: /\b\d{2,3}\s?mm\b/i, points: 10, label: 'Specifies a focal length' },
  { pattern: /\bf\/\d(\.\d)?\b|\bT\d(\.\d)?\b/i, points: 8, label: 'Specifies an aperture' },
  { pattern: /\bhandheld\b|\btripod\b|\bgimbal\b|\bdolly\b|\bpan\b|\btilt\b|\btracking shot\b/i, points: 8, label: 'Describes camera movement' },
  { pattern: /\bwindow light\b|\bavailable light\b|\bpractical\b|\bovercast\b|\bgolden hour\b|\bfluorescent\b|\bstreetlight\b/i, points: 10, label: 'Names a real light source' },
  { pattern: /\bgrain\b|\bnoise\b|\bvignette\b|\bchromatic aberration\b|\bmotion blur\b|\bhalation\b/i, points: 8, label: 'Requests optical imperfection' },
  { pattern: /\bpores\b|\btexture\b|\bwrinkles\b|\bblemish\b|\bflyaway\b|\basymmetr/i, points: 8, label: 'Requests surface imperfection' },
  { pattern: /\b\d{1,2}fps\b|\bshutter\b|\bframe rate\b/i, points: 6, label: 'Specifies temporal capture detail' },
];

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

function looksHuman(text) {
  const lower = ` ${text.toLowerCase()} `;
  return HUMAN_HINTS.some((hint) => lower.includes(hint));
}

/**
 * Detect anti-patterns in a raw subject description.
 * Matches on word boundaries so "perfectly still" flags but "imperfect" does not.
 */
function findAntiPatterns(text) {
  const lower = text.toLowerCase();
  return ANTI_PATTERNS.filter(({ term }) => {
    const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(`(^|[^a-z])${escaped}([^a-z]|$)`, 'i').test(lower);
  });
}

/**
 * Score how likely a prompt is to produce footage rather than a render.
 * Starts at a deliberately low base — an unmodified prompt is not realistic.
 */
function scorePrompt(text) {
  const reasons = [];
  let score = 25;

  for (const signal of POSITIVE_SIGNALS) {
    if (signal.pattern.test(text)) {
      score += signal.points;
      reasons.push({ kind: 'good', label: signal.label });
    }
  }

  for (const hit of findAntiPatterns(text)) {
    score -= 12;
    reasons.push({ kind: 'bad', label: `"${hit.term}" — ${hit.why}` });
  }

  const words = text.trim().split(/\s+/).filter(Boolean).length;
  if (words < 8) {
    score -= 10;
    reasons.push({ kind: 'bad', label: 'Very short prompt — the model fills gaps with its default render look.' });
  } else if (words > 25) {
    score += 6;
    reasons.push({ kind: 'good', label: 'Detailed description leaves less room for default styling.' });
  }

  return { score: clamp(Math.round(score), 0, 100), reasons };
}

/** Pick "a" or "an" to match the following word, preserving capitalization. */
function agreeArticle(article, nextWord) {
  const wanted = /^[aeiou]/i.test(nextWord) ? 'an' : 'a';
  return article[0] === article[0].toUpperCase()
    ? wanted[0].toUpperCase() + wanted.slice(1)
    : wanted;
}

/**
 * Strip anti-pattern terms from a subject description.
 * Used when `autoClean` is on, so the user's own bad habits do not fight
 * the modifiers this engine adds.
 *
 * Deleting an adjective mid-sentence leaves grammatical debris — "a
 * masterpiece of a woman" becomes "a of a woman" — so removals are marked
 * with a sentinel and the text around each mark is repaired before the marks
 * are dropped. Repairs are confined to removal sites, which keeps the user's
 * own wording untouched everywhere else.
 */
const MARK = ' ';

function cleanSubject(text) {
  let out = text;
  const removed = [];

  for (const { term } of ANTI_PATTERNS) {
    const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`(^|[^a-z])${escaped}([^a-z]|$)`, 'gi');
    const next = out.replace(re, `$1${MARK}$2`);
    if (next !== out) {
      removed.push(term);
      out = next;
    }
  }

  if (removed.length) {
    // Several removed words in a row collapse into one site.
    out = out.replace(new RegExp(`${MARK}(\\s*[,;]?\\s*${MARK})+`, 'g'), MARK);
    // "a <removed> of X" -> "X": the removed noun was what "of" attached to.
    out = out.replace(new RegExp(`\\b(a|an|the)\\s*${MARK}\\s*of\\s+`, 'gi'), '');
    // A removal can change which article the next word needs.
    out = out.replace(
      new RegExp(`\\b(a|an)(\\s*)${MARK}\\s*([a-z]+)`, 'gi'),
      (_m, article, _sp, word) => `${agreeArticle(article, word)} ${word}`,
    );
    out = out.replace(new RegExp(`\\s*${MARK}\\s*`, 'g'), ' ');
  }

  // Collapse the punctuation and whitespace debris removal leaves behind.
  out = out
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([,.;])/g, '$1')
    .replace(/([,;])\s*([,;])+/g, '$1')
    .replace(/^[\s,;]+|[\s,;]+$/g, '')
    .replace(/(^|\s+)(a|an|the)$/i, '')
    .trim();

  return { text: out, removed };
}

/**
 * Compile a subject description into a realism-tuned prompt pair.
 *
 * @param {object} opts
 * @param {string} opts.subject      What the user actually wants to see.
 * @param {string} [opts.preset]     Key from PRESETS.
 * @param {number} [opts.intensity]  0-4. How many modifier layers to apply.
 * @param {boolean} [opts.autoClean] Strip known anti-patterns from the subject.
 * @param {string} [opts.extraNegative] Appended to the negative prompt.
 * @param {string[]} [opts.negativeGroups] Which NEGATIVE_GROUPS to include.
 */
function compilePrompt(opts = {}) {
  const subject = String(opts.subject || '').trim();
  if (!subject) {
    throw new Error('A subject description is required.');
  }

  const presetKey = PRESETS[opts.preset] ? opts.preset : 'documentary';
  const preset = PRESETS[presetKey];
  const intensity = clamp(Number.isFinite(+opts.intensity) ? +opts.intensity : 3, 0, 4);
  const autoClean = opts.autoClean !== false;

  const before = scorePrompt(subject);
  const cleaned = autoClean ? cleanSubject(subject) : { text: subject, removed: [] };
  const body = cleaned.text || subject;

  // Layer 0 is the camera itself; each intensity step adds one more layer.
  const modifiers = [];
  if (intensity > 0) {
    modifiers.push(preset.camera);
    for (let i = 0; i < intensity && i < preset.layers.length; i += 1) {
      modifiers.push(...preset.layers[i]);
    }
  }

  const human = looksHuman(body);
  if (human && intensity >= 2) {
    modifiers.push(...HUMAN_DETAIL.slice(0, intensity >= 3 ? HUMAN_DETAIL.length : 3));
  }

  const prompt = [body, ...modifiers].join(', ');

  const groups = Array.isArray(opts.negativeGroups) && opts.negativeGroups.length
    ? opts.negativeGroups.filter((g) => NEGATIVE_GROUPS[g])
    : Object.keys(NEGATIVE_GROUPS);

  const negativeTerms = groups.flatMap((g) => NEGATIVE_GROUPS[g]);
  if (opts.extraNegative) negativeTerms.push(String(opts.extraNegative).trim());
  const negativePrompt = negativeTerms.filter(Boolean).join(', ');

  const after = scorePrompt(prompt);

  const notes = [];
  if (cleaned.removed.length) {
    notes.push(`Removed render-biasing terms: ${cleaned.removed.join(', ')}.`);
  }
  if (human) {
    notes.push('Human subject detected — added skin, hair, and fabric detail cues.');
  }
  if (intensity === 0) {
    notes.push('Intensity 0 — subject passed through with only the negative prompt applied.');
  }

  return {
    prompt,
    negativePrompt,
    preset: { key: presetKey, label: preset.label, summary: preset.summary },
    intensity,
    notes,
    warnings: before.reasons.filter((r) => r.kind === 'bad').map((r) => r.label),
    score: { before: before.score, after: after.score, reasons: after.reasons },
  };
}

function listPresets() {
  return Object.entries(PRESETS).map(([key, p]) => ({
    key,
    label: p.label,
    summary: p.summary,
    camera: p.camera,
    layers: p.layers.length,
  }));
}

function listNegativeGroups() {
  return Object.entries(NEGATIVE_GROUPS).map(([key, terms]) => ({ key, terms }));
}

module.exports = {
  compilePrompt,
  scorePrompt,
  cleanSubject,
  findAntiPatterns,
  listPresets,
  listNegativeGroups,
  looksHuman,
  PRESETS,
  NEGATIVE_GROUPS,
  ANTI_PATTERNS,
};
