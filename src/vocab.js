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
  'waist up': 'waist-up shot',
  'waist-up': 'waist-up shot',
  wide: 'wide shot',
  // A selfie is close framing by definition; treat it as the constraint it is.
  selfie: 'close-up shot',
  'selfie shot': 'close-up shot',
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

/* ------------------------------------------------------------------ *
 * Scene inference
 *
 * Era presets used to apply indoor-flash-snapshot assumptions to every photo,
 * which produced "hard shadow on the wall behind the subject" for a picture
 * taken on a beach. These tables let the compiler notice where the photo was
 * taken and how tightly it is framed, and gate era tags accordingly.
 * ------------------------------------------------------------------ */

/** Settings and lighting that place a photo outdoors. */
export const OUTDOOR_HINTS =
  /\b(outdoor|outside|beach|ocean|sea|shore|coast|sand|lake|river|pool|sky|cloud|sunset|sunrise|street|road|sidewalk|park|garden|yard|field|meadow|forest|woods|mountain|hill|trail|desert|snow|city|rooftop|balcony|patio|stadium|parking lot)\b/i;

/** Settings that place a photo indoors. */
export const INDOOR_HINTS =
  /\b(indoor|inside|interior|room|kitchen|bedroom|bathroom|living room|hallway|basement|attic|office|classroom|restaurant|bar|club|store|shop|church|garage|studio|wall|ceiling|couch|sofa|bed|desk|table)\b/i;

/** Lighting that rules out a flash snapshot. */
export const DAYLIGHT_HINTS =
  /\b(sun|sunny|sunlight|sunlit|daylight|daytime|natural light|golden hour|overcast|cloudy|bright|window light|dusk|dawn|sunset|sunrise)\b/i;

/** Framing tight enough that legs and feet cannot be in shot. */
export const CLOSE_FRAMING_HINTS =
  /\b(close-?up|closeup|portrait|headshot|head and shoulders|face|selfie|bust)\b/i;

/** Framing that shows the upper body but not the legs. */
export const WAIST_UP_HINTS = /\b(waist-?up|upper body|half body|chest-?up|torso)\b/i;

/** Framing wide enough to show the whole subject. */
export const FULL_BODY_HINTS = /\b(full-?body|full-?length|wide|whole body|environmental)\b/i;

/**
 * Valid answers for the shot-type pass. A small model will happily answer
 * "wide shot" for a tight selfie; it cannot be stopped from being wrong, but it
 * can be stopped from inventing values outside this set.
 */
export const SHOT_TYPES = [
  'close-up shot',
  'portrait framing',
  'waist-up shot',
  'full-body shot',
  'wide shot',
  'wide-angle shot',
  'high angle shot',
  'low angle shot',
  'eye-level shot',
  'selfie',
];

/* ------------------------------------------------------------------ *
 * Pose and gaze
 *
 * Body position was previously folded into a single vague "what is the subject
 * doing?" pass, which returned things like "sunning herself" for someone simply
 * facing the camera. Pose, action and gaze are now asked separately, and the
 * answers normalised toward terms diffusion models respond to.
 * ------------------------------------------------------------------ */

export const POSE_SYNONYMS = {
  sitting: 'seated',
  'sitting down': 'seated',
  'sat down': 'seated',
  standing: 'standing upright',
  'standing up': 'standing upright',
  'laying down': 'lying down',
  'lying': 'lying down',
  'laying': 'lying down',
  'leaning': 'leaning',
  'bent over': 'bending forward',
  crouching: 'crouched',
  kneeling: 'kneeling',
  squatting: 'squatting',
  'arms crossed': 'arms crossed',
  'hands on hips': 'hands on hips',
  'arms raised': 'arms raised',
  'head tilted': 'head tilted',
  'over the shoulder': 'looking over the shoulder',
  'three quarter': 'three-quarter view',
  'side on': 'profile view',
  'facing away': 'back to the camera',
  'from behind': 'back to the camera',
};

/** Where the subject is looking. Kept short — long answers read as noise. */
export const GAZE_SYNONYMS = {
  camera: 'at the camera',
  'at camera': 'at the camera',
  'the camera': 'at the camera',
  away: 'away from the camera',
  down: 'downward',
  up: 'upward',
  side: 'to the side',
  'to the left': 'to the side',
  'to the right': 'to the side',
  'off camera': 'away from the camera',
  'into the distance': 'into the distance',
};

/* ------------------------------------------------------------------ *
 * Content level
 * ------------------------------------------------------------------ */

/**
 * Terms that indicate the subject is a minor.
 *
 * Used to withhold adult content styling. Deliberately errs toward blocking:
 * "girl"/"woman" are genuinely ambiguous in prompt vocabulary and are handled by
 * the separate model age check plus the user's explicit adult affirmation, but
 * anything unambiguous here blocks outright.
 */
export const MINOR_TERMS = [
  'child',
  'children',
  'kid',
  'kids',
  'baby',
  'babies',
  'infant',
  'toddler',
  'boy',
  'little girl',
  'little boy',
  'young girl',
  'young boy',
  'teen',
  'teens',
  'teenager',
  'teenage',
  'adolescent',
  'preteen',
  'pre-teen',
  'minor',
  'schoolgirl',
  'schoolboy',
  'schoolchild',
  'youngster',
  'juvenile',
  'underage',
  'pupil',
];

/** Answers to the age pass that mean "not an adult". */
export const NON_ADULT_ANSWERS =
  /\b(child|kid|baby|infant|toddler|boy|teen|teenager|teenage|adolescent|preteen|minor|underage|young)\b/i;

/**
 * Anatomy and skin-texture terms for adult content.
 *
 * These are corrective rather than decorative: anatomy is the dominant failure
 * mode for figure work, and the realism presets already reject the airbrushed
 * look, so what is needed is an explicit push toward natural bodies.
 */
export const NSFW_QUALITY_TAGS = [
  'anatomically correct',
  'natural body proportions',
  'natural skin texture',
  'visible skin pores',
  'natural body hair',
  'realistic body',
];

/**
 * Failure modes specific to figure work, added to the negative prompt when
 * adult content is enabled.
 */
export const NSFW_NEGATIVE = [
  'plastic skin',
  'doll-like',
  'mannequin',
  'airbrushed body',
  'impossible anatomy',
  'extra nipples',
  'malformed limbs',
  'fused limbs',
  'distorted torso',
  'unnatural proportions',
];

/** Keeps unintended nudity out of ordinary prompts. */
export const SFW_NEGATIVE = [
  'nsfw',
  'nude',
  'nudity',
  'topless',
  'explicit content',
  'sexual content',
];

/**
 * Content levels.
 *
 * Note what these do and do not do. They control whether nudity is *permitted*
 * and add anatomy support for figure work — they do not fabricate explicit
 * content from a clothed photo. This app reads a photograph; inventing acts the
 * photo does not show would both misrepresent the source and be precisely the
 * behaviour worth not building. The user writes what they want in the editable
 * fields and the free-text box; the compiler structures it.
 */
export const CONTENT_LEVELS = {
  sfw: {
    label: 'Safe',
    requiresAdult: false,
    negative: SFW_NEGATIVE,
    qualityTags: 0,
    intimate: false,
  },
  suggestive: {
    label: 'Suggestive',
    requiresAdult: true,
    negative: [],
    qualityTags: 3,
    intimate: true,
  },
  explicit: {
    label: 'Explicit',
    requiresAdult: true,
    negative: NSFW_NEGATIVE,
    qualityTags: 99,
    intimate: true,
  },
};

export const DEFAULT_CONTENT = 'sfw';

/**
 * Bare adjectives the lighting pass returns. "lit by sunny" is not English, so
 * these are mapped to noun phrases before the natural-language joiner runs.
 */
export const LIGHTING_NOUNS = {
  sunny: 'sunlight',
  sunlit: 'sunlight',
  bright: 'bright light',
  dark: 'dim light',
  dim: 'dim light',
  cloudy: 'overcast light',
  overcast: 'overcast light',
  golden: 'golden hour light',
  warm: 'warm light',
  cool: 'cool light',
  harsh: 'harsh light',
  soft: 'soft light',
  natural: 'natural light',
  artificial: 'artificial light',
  fluorescent: 'fluorescent light',
  indoor: 'indoor light',
  outdoor: 'daylight',
};

/**
 * Preposition and article for a setting phrase in natural-language mode.
 * Without this the compiler emitted "in ocean", which reads as being in the
 * water rather than beside it. First match wins.
 */
export const SETTING_PREPOSITIONS = [
  [/\b(ocean|sea|beach|shore|coast|lake|river|pool)\b/i, 'at the'],
  [/\b(street|road|sidewalk|bridge|trail|path|rooftop|balcony)\b/i, 'on a'],
  [/\b(mountain|hill|field|meadow|desert)\b/i, 'in an open'],
  [/\b(sunset|sunrise|dusk|dawn|golden hour)\b/i, 'at'],
];

/** Fallback preposition for settings that match nothing above. */
export const DEFAULT_SETTING_PREPOSITION = 'in a';

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
  'pose',
  'action',
  'gaze',
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
  pose: { lead: '', skipIf: [] },
  gaze: { lead: 'looking ', skipIf: ['looking', 'gazing', 'eyes', 'staring', 'facing'] },
  action: { lead: '', skipIf: [] },
  // The setting preposition is chosen per phrase — see SETTING_PREPOSITIONS.
  setting: { lead: '', skipIf: [] },
  colors: { lead: 'in shades of ', skipIf: ['in shades'] },
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
