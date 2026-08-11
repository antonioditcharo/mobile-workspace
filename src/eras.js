/**
 * Era preset bundles — the heart of the app.
 *
 * Producing an image that reads as a genuine period photograph is mostly a
 * matter of *refusing* the conventional prompt boilerplate. Tags like
 * "masterpiece, 8k, ultra detailed, cinematic lighting" are what make output
 * look rendered; every era here therefore sets `suppressQualityTags` and leans
 * instead on concrete period apparatus — film stock, camera body, flash
 * behaviour, lab-print and sensor artifacts.
 *
 * Data only. Lists are ordered most-characteristic-first, because artifact
 * intensity works by truncation.
 *
 * `negativeExclude` exists because the base negative list rejects things some
 * eras deliberately want. An early-2000s digital compact *should* produce JPEG
 * blocks; an 80s print *should* have a white border and a date stamp. Any term
 * an era requests positively must be pulled back out of its negative list, or
 * the two halves of the prompt fight each other.
 */

/**
 * The load-bearing negative block. Rejects modern digital polish and the
 * "AI look" — applied to every era preset.
 */
export const REALISM_NEGATIVE = [
  'digital art',
  'illustration',
  'painting',
  'drawing',
  'sketch',
  'render',
  '3d render',
  'cgi',
  'anime',
  'cartoon',
  'concept art',
  'airbrushed',
  'retouched',
  'smooth skin',
  'flawless skin',
  'poreless skin',
  'symmetrical face',
  'glamour',
  'fashion model',
  'supermodel',
  'beauty shot',
  'hdr',
  'high dynamic range',
  'ultra sharp',
  'hyperdetailed',
  'ultra detailed',
  'masterpiece',
  'best quality',
  '8k',
  '4k',
  'cinematic lighting',
  'dramatic lighting',
  'studio lighting',
  'softbox',
  'color grading',
  'teal and orange',
  'instagram filter',
  'oversaturated',
  'vibrant colors',
  'neon',
  'artstation',
  'trending on artstation',
  'award winning',
  'professional photography',
  'modern smartphone photo',
  'iphone photo',
  'shallow depth of field',
  'creamy bokeh',
  'perfect composition',
];

/**
 * How many tags each intensity level emits. "Force real" overdone becomes
 * obvious pastiche, so subtle is a genuine option rather than a weak one.
 */
export const INTENSITY_LEVELS = {
  subtle: { label: 'Subtle', artifacts: 1, look: 3 },
  medium: { label: 'Medium', artifacts: 3, look: 5 },
  heavy: { label: 'Heavy', artifacts: 99, look: 99 },
};

export const DEFAULT_INTENSITY = 'medium';

export const ERAS = {
  '1980s': {
    id: '1980s',
    label: '1980s',
    blurb: 'Warm film stock, hard flash, white-bordered lab prints.',
    suppressQualityTags: true,
    cfg: { value: 5, range: [3.5, 6.5] },
    steps: 30,
    aspect: '3:2',
    snapshotFraming: ['amateur snapshot', 'vernacular family photograph', 'candid snapshot'],
    look: [
      'warm amber color cast',
      'visible film grain',
      'corner vignetting',
      'green-tinted shadows',
      'halation around highlights',
      'soft lens character',
      'low contrast in the shadows',
    ],
    subjectPeriod: [
      '1980s clothing',
      'feathered hair',
      'high-waisted jeans',
      'shoulder pads',
      'wood-paneled wall',
      'floral wallpaper',
      'CRT television in the background',
    ],
    negative: ['modern clothing', 'modern interior', 'smartphone', 'flat screen television'],
    // The white print border and orange date stamp are wanted, so the base
    // negative must stop rejecting borders, frames and text.
    negativeExclude: ['border', 'frame', 'text', 'caption'],
    defaultFormat: '35mm print',
    formats: {
      '35mm print': {
        label: '35mm print',
        camera: ['manual focus 35mm SLR', 'Canon AE-1', 'Nikon FM2'],
        stock: ['Kodachrome 64', 'Kodacolor VR 400', 'Ektachrome'],
        artifacts: [
          'white-bordered matte lab print',
          'orange dot-matrix date stamp',
          'slight color fading',
          'dust specks',
          'fine emulsion scratches',
        ],
      },
      instant: {
        label: 'Instant / Polaroid',
        camera: ['Polaroid SX-70', 'Polaroid 600'],
        stock: ['instant film'],
        aspect: '1:1',
        artifacts: [
          'square instant film frame with wide white bottom border',
          'milky low contrast',
          'cyan-shifted shadows',
          // Avoid the literal substring "sharp focus" — it is a quality tag, and
          // models tokenize those words regardless of the "un-" prefix.
          'gently out of focus',
          'uneven chemical development',
        ],
        // Instant film is genuinely soft; don't also forbid softness.
        negativeExclude: ['blurry'],
      },
      'flash snapshot': {
        label: 'Night flash snapshot',
        camera: ['compact 35mm camera with built-in flash'],
        stock: ['Kodacolor VR 400'],
        artifacts: [
          'harsh direct flash',
          'background falling off to black',
          'red-eye',
          'hard flash shadow behind the subject',
          'overexposed skin highlights',
        ],
      },
    },
  },

  '1990s': {
    id: '1990s',
    label: '1990s',
    blurb: 'Drugstore prints, on-camera flash, punchy consumer film.',
    suppressQualityTags: true,
    cfg: { value: 5.5, range: [4, 7] },
    steps: 28,
    aspect: '3:2',
    snapshotFraming: ['amateur snapshot', 'candid family photograph', 'vernacular snapshot'],
    look: [
      'direct on-camera flash',
      'hard shadow on the wall behind the subject',
      'visible film grain',
      'punchy consumer film saturation',
      'slight overexposure',
      'warm color cast',
      'flat frontal lighting',
    ],
    subjectPeriod: [
      '1990s clothing',
      'baggy jeans',
      'windbreaker jacket',
      'chunky sneakers',
      'popcorn ceiling',
      'beige carpet',
      'boxy CRT television',
    ],
    negative: ['modern clothing', 'modern interior', 'smartphone', 'flat screen television'],
    negativeExclude: ['border', 'frame', 'text', 'caption'],
    defaultFormat: '35mm print',
    formats: {
      '35mm print': {
        label: '35mm print',
        camera: ['compact point-and-shoot', 'Canon Sure Shot', 'Olympus Stylus'],
        stock: ['Kodak Gold 200', 'Fujicolor Superia 400', 'Konica Centuria'],
        artifacts: [
          '4x6 glossy drugstore print',
          'orange dot-matrix date stamp',
          'one-hour photo lab color balance',
          'slight print scuffing',
        ],
      },
      disposable: {
        label: 'Disposable camera',
        camera: ['Kodak FunSaver disposable camera'],
        stock: ['ISO 800 consumer film'],
        artifacts: [
          'harsh unmodulated flash',
          'red-eye',
          'heavy film grain',
          'soft plastic lens distortion',
          'light leak streak',
          'crooked handheld framing',
        ],
        negativeExclude: ['blurry'],
      },
      'vhs still': {
        label: 'VHS still',
        camera: ['consumer camcorder'],
        stock: ['VHS tape'],
        aspect: '4:3',
        artifacts: [
          'interlaced video still with visible scanlines',
          'tracking distortion',
          'chroma bleed',
          'timecode overlay in the corner',
          'motion smear',
          'low video resolution',
        ],
        // A VHS frame is legitimately low-resolution and soft.
        negativeExclude: ['lowres', 'blurry'],
      },
    },
  },

  'early 2000s': {
    id: 'early 2000s',
    label: 'Early 2000s',
    blurb: 'Early digital compacts: JPEG mush, blown flash, yellow timestamp.',
    suppressQualityTags: true,
    cfg: { value: 6, range: [4.5, 7.5] },
    steps: 26,
    aspect: '4:3',
    snapshotFraming: ['amateur digital snapshot', 'candid party snapshot', 'vernacular snapshot'],
    look: [
      'harsh built-in flash',
      'background falling off underexposed',
      'limited dynamic range',
      'digital noise in the shadows',
      'cyan-green fluorescent color cast',
      'flat frontal lighting',
      'small sensor look',
    ],
    subjectPeriod: [
      'early 2000s clothing',
      'low-rise jeans',
      'frosted hair tips',
      'velour tracksuit',
      'beige desktop computer',
      'silver electronics',
    ],
    negative: ['modern clothing', 'modern interior', 'smartphone'],
    // Compression, softness and the burnt-in timestamp are the whole point of
    // this era, so they cannot also be negated.
    negativeExclude: ['jpeg artifacts', 'lowres', 'text', 'caption', 'blurry'],
    defaultFormat: 'digital compact',
    formats: {
      'digital compact': {
        label: 'Digital compact (3MP)',
        camera: ['3 megapixel Sony Cyber-shot', 'Canon PowerShot A-series', 'Nikon Coolpix'],
        stock: ['early digital sensor'],
        artifacts: [
          'visible JPEG compression blocks',
          'yellow digital timestamp in the corner',
          'oversharpening halos',
          'purple chromatic fringing',
          'blown-out highlights',
          'low megapixel softness',
        ],
      },
      cameraphone: {
        label: 'VGA cameraphone',
        camera: ['early 2000s VGA cameraphone'],
        stock: ['640x480 phone sensor'],
        artifacts: [
          'extreme JPEG compression',
          'smeared low-resolution detail',
          'muddy desaturated color',
          'heavy sensor noise',
          'crushed dynamic range',
        ],
        negativeExclude: ['blurry'],
      },
      '35mm print': {
        label: '35mm print (still common)',
        camera: ['compact point-and-shoot'],
        stock: ['Kodak Max 400', 'Fujicolor Superia 400'],
        artifacts: [
          '4x6 glossy lab print',
          'orange dot-matrix date stamp',
          'film grain',
          'one-hour photo lab color balance',
        ],
        negativeExclude: ['border', 'frame'],
      },
    },
  },

  /**
   * Escape hatch: no era forcing. Unlike the period presets this one *keeps*
   * the quality boilerplate, since without an era to protect there is no
   * reason to suppress it.
   */
  none: {
    id: 'none',
    label: 'No era',
    blurb: 'Plain prompt, no period forcing. Keeps standard quality tags.',
    suppressQualityTags: false,
    cfg: { value: 7, range: [6, 9] },
    steps: 30,
    aspect: 'source',
    snapshotFraming: [],
    look: [],
    subjectPeriod: [],
    negative: [],
    negativeExclude: [],
    defaultFormat: 'plain',
    formats: {
      plain: { label: 'Plain', camera: [], stock: [], artifacts: [] },
    },
  },
};

export const ERA_IDS = ['1980s', '1990s', 'early 2000s', 'none'];
export const DEFAULT_ERA = '1990s';

/** Standard aspect ratios, used to snap a source image to an era-plausible frame. */
export const ASPECT_RATIOS = [
  { label: '1:1', value: 1 },
  { label: '4:3', value: 4 / 3 },
  { label: '3:2', value: 3 / 2 },
  { label: '16:9', value: 16 / 9 },
  { label: '3:4', value: 3 / 4 },
  { label: '2:3', value: 2 / 3 },
  { label: '9:16', value: 9 / 16 },
];
