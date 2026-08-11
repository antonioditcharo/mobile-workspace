/**
 * Controlled vocabulary and text-cleanup data for the prompt compiler.
 *
 * Data only — no logic lives here. The compiler imports these tables so that
 * tuning the app's language behaviour is a matter of editing lists rather than
 * editing code.
 */

/**
 * Conversational scaffolding that small vision-language models wrap around
 * their answers. Stripped before anything else happens.
 *
 * Order matters: longer, more specific phrases must precede their own
 * substrings, otherwise the short pattern fires first and leaves a fragment
 * behind ("this image shows" -> "shows" if "image" went first).
 */
export const FILLER_PATTERNS = [
  /^\s*(answer|caption|description|response|output)\s*:\s*/i,
  /\b(?:in|from)\s+th(?:is|e)\s+(?:image|photo|photograph|picture|scene)\b,?\s*/gi,
  /\bth(?:is|e)\s+(?:image|photo|photograph|picture|scene)\s+(?:shows|depicts|features|contains|is\s+of)\b\s*/gi,
  /\bth(?:is|e)\s+is\s+(?:a|an)\s+(?:image|photo|photograph|picture)\s+of\b\s*/gi,
  /\b(?:we|you|i)\s+can\s+see\b\s*/gi,
  /\bit\s+(?:appears|seems)\s+to\s+be\b\s*/gi,
  /\b(?:appears|seems)\s+to\s+be\b\s*/gi,
  /\bthere\s+(?:is|are)\s+(?:a|an|some)?\b\s*/gi,
  /^\s*(?:a|an)\s+(?:image|photo|photograph|picture)\s+of\b\s*/i,
  /\b(?:likely|probably|possibly|perhaps|maybe|apparently)\b\s*/gi,
  /\b(?:overall|in\s+general|generally\s+speaking)\b,?\s*/gi,
];

/**
 * Answers that carry no information. A small model returns these constantly
 * when it cannot make out the detail being asked about, and emitting them
 * would poison the prompt with vague tokens.
 */
export const NULL_ANSWERS = new Set([
  '',
  'n/a',
  'na',
  'none',
  'nothing',
  'no',
  'yes',
  'unknown',
  'unclear',
  'not sure',
  'cannot tell',
  "can't tell",
  'not visible',
  'no idea',
  'unspecified',
  'indeterminate',
  'image',
  'photo',
  'photograph',
  'picture',
  'a photo',
  'an image',
  'something',
  'stuff',
  'things',
  'object',
  'objects',
]);

/**
 * Tokens too generic to earn a slot in a prompt. Dropped during compilation
 * even when they arrive inside an otherwise useful phrase's tag list.
 */
export const GENERIC_TAGS = new Set([
  'image',
  'photo',
  'photograph',
  'picture',
  'background',
  'foreground',
  'thing',
  'things',
  'object',
  'objects',
  'stuff',
  'area',
  'scene',
  'view',
  'part',
  'various',
  'several',
  'other',
  'others',
]);

/**
 * Normalisation toward conventional diffusion-prompt vocabulary. Keys are
 * matched whole-phrase, case-insensitively, after filler removal.
 */
export const SYNONYMS = {
  // People — models alternate between clinical and colloquial terms.
  male: 'man',
  female: 'woman',
  guy: 'man',
  gentleman: 'man',
  lady: 'woman',
  'young male': 'young man',
  'young female': 'young woman',
  'adult male': 'man',
  'adult female': 'woman',
  'male child': 'boy',
  'female child': 'girl',
  kid: 'child',
  kids: 'children',
  people: 'group of people',
  persons: 'group of people',

  // Framing — map to terms photographers and prompts actually use.
  'close up': 'close-up shot',
  closeup: 'close-up shot',
  'close-up': 'close-up shot',
  'a close up view': 'close-up shot',
  'full body': 'full-body shot',
  'full length': 'full-body shot',
  'head and shoulders': 'portrait framing',
  'upper body': 'waist-up shot',
  'from above': 'high angle shot',
  'from below': 'low angle shot',
  'eye level': 'eye-level shot',
  'wide shot': 'wide shot',
  'wide angle': 'wide-angle shot',
  landscape: 'landscape orientation',

  // Lighting.
  'sun light': 'sunlight',
  'natural light': 'natural lighting',
  'day light': 'daylight',
  daytime: 'daylight',
  'night time': 'night',
  nighttime: 'night',
  'artificial light': 'artificial lighting',
  'indoor lighting': 'indoor artificial lighting',
  'bright light': 'bright lighting',
  'dim light': 'dim lighting',
  'low light': 'dim lighting',
  'back light': 'backlighting',
  backlit: 'backlighting',
  'over head light': 'overhead lighting',
  flash: 'camera flash',
  'flash photography': 'camera flash',

  // Settings.
  indoors: 'indoor setting',
  inside: 'indoor setting',
  outdoors: 'outdoor setting',
  outside: 'outdoor setting',
  'living room': 'living room interior',
  kitchen: 'domestic kitchen',
  bedroom: 'bedroom interior',
  street: 'city street',
  road: 'roadside',
  field: 'open field',
  woods: 'forest',
  beach: 'beach',
  park: 'public park',
};

/**
 * Words ignored when testing two phrases for near-duplication, so that
 * "a red dress" and "red dress" collapse to one tag.
 */
export const DEDUPE_STOPWORDS = new Set([
  'a',
  'an',
  'the',
  'of',
  'with',
  'in',
  'on',
  'at',
  'to',
  'and',
  'is',
  'are',
  'was',
  'were',
  'some',
  'very',
  'quite',
  'that',
  'this',
  'their',
  'his',
  'her',
  'its',
  'wearing',
]);

/**
 * Assembly order for compiled prompts. Diffusion models weight earlier tokens
 * more heavily, so the subject leads and the photographic medium trails —
 * medium tags describe *how* the image was captured and should not compete
 * with the subject for attention.
 */
export const CATEGORY_ORDER = [
  'shotType',
  'subject',
  'appearance',
  'clothing',
  'action',
  'setting',
  'colors',
  'lighting',
  'composition',
  'medium',
  'artifacts',
];

/**
 * Natural-language joining behaviour per category.
 *
 * `lead` is prepended unless the phrase already opens with one of `skipIf`,
 * which prevents "in in a kitchen" when the model's answer already carries
 * its own preposition.
 */
export const NL_JOINERS = {
  shotType: { lead: '', skipIf: [] },
  subject: { lead: '', skipIf: [] },
  appearance: { lead: '', skipIf: [] },
  clothing: { lead: 'wearing ', skipIf: ['wearing', 'dressed', 'in a', 'in an'] },
  action: { lead: '', skipIf: [] },
  setting: { lead: 'in ', skipIf: ['in', 'on', 'at', 'inside', 'outside', 'near', 'by'] },
  colors: { lead: '', skipIf: [] },
  lighting: { lead: 'lit by ', skipIf: ['lit', 'under', 'backlighting', 'in', 'with'] },
  composition: { lead: '', skipIf: [] },
  medium: { lead: '', skipIf: [] },
  artifacts: { lead: '', skipIf: [] },
};

/**
 * Quality boilerplate. Deliberately kept in one place so era presets can
 * refuse it wholesale: these tags are the single biggest cause of output that
 * reads as AI-generated rather than as a real photograph.
 */
export const QUALITY_TAGS = [
  'masterpiece',
  'best quality',
  'highly detailed',
  'ultra detailed',
  'sharp focus',
  '8k',
  'high resolution',
  'professional photography',
];

/** Anatomy and rendering failures worth rejecting regardless of style. */
export const BASE_NEGATIVE = [
  'watermark',
  'signature',
  'text',
  'caption',
  'logo',
  'border',
  'frame',
  'cropped',
  'out of frame',
  'lowres',
  'jpeg artifacts',
  'blurry',
  'deformed',
  'disfigured',
  'mutated',
  'extra limbs',
  'extra fingers',
  'missing fingers',
  'fused fingers',
  'malformed hands',
  'bad anatomy',
  'bad proportions',
  'long neck',
];
