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
  // Borrowed from Perchance's own "casual-photo" style negative list, which is
  // well aimed at the same target as these presets.
  'high production value',
  'commercial photoshoot',
  'photoshopped',
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
      'corner vignetting',
      'green-tinted shadows',
      'low contrast in the shadows',
    ],
    flashLook: ['direct on-camera flash', 'hard shadow on the wall behind the subject', 'red-eye'],
    daylightLook: ['halation around highlights', 'lens flare'],
    subjectPeriod: {
      hair: ['feathered hair', 'permed hair'],
      marker: ['1980s clothing'],
      wardrobeTop: ['shoulder pads', 'oversized knit sweater'],
      wardrobeFull: ['high-waisted jeans', 'white leather sneakers'],
      decorIndoor: ['wood-paneled wall', 'floral wallpaper', 'CRT television in the background'],
      decorOutdoor: ['1980s station wagon in the background'],
    },
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
        look: ['visible film grain', 'soft lens character'],
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
        look: ['milky low contrast', 'soft lens character'],
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
        look: ['visible film grain'],
        // A night flash shot is flash-lit by definition, whatever the scene.
        forceFlash: true,
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
    // Universal to the era, true regardless of scene or format.
    look: ['warm color cast', 'slight overexposure'],
    // Only when the photo really is a flash snapshot — never outdoors in daylight,
    // where "hard shadow on the wall" describes a wall that isn't there.
    flashLook: [
      'direct on-camera flash',
      'hard shadow on the wall behind the subject',
      'flat frontal lighting',
      'red-eye',
    ],
    // Used instead when the scene is daylit.
    // Describes the photograph, never the subject's behaviour: an earlier
    // version asserted "squinting into the sun" on a subject smiling with open
    // eyes, and over-specified "midday" for a sunset.
    daylightLook: ['slightly blown highlights', 'strong daylight contrast'],
    subjectPeriod: {
      hair: ['1990s hairstyle'],
      // Safe at any framing — a generic marker rather than specific garments.
      marker: ['1990s clothing'],
      // Visible from the waist up.
      wardrobeTop: ['windbreaker jacket', 'graphic t-shirt'],
      // Needs the legs and feet in frame.
      wardrobeFull: ['baggy jeans', 'chunky sneakers'],
      decorIndoor: ['popcorn ceiling', 'beige carpet', 'boxy CRT television'],
      decorOutdoor: ['1990s parked cars in the background'],
    },
    negative: ['modern clothing', 'modern interior', 'smartphone', 'flat screen television'],
    negativeExclude: ['border', 'frame', 'text', 'caption'],
    defaultFormat: '35mm print',
    formats: {
      '35mm print': {
        label: '35mm print',
        camera: ['compact point-and-shoot', 'Canon Sure Shot', 'Olympus Stylus'],
        stock: ['Kodak Gold 200', 'Fujicolor Superia 400', 'Konica Centuria'],
        // Texture belongs to the medium, not the era: film grain on a VHS still
        // is a contradiction.
        look: ['visible film grain', 'punchy consumer film saturation'],
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
        look: ['heavy film grain', 'punchy consumer film saturation'],
        artifacts: [
          'harsh unmodulated flash',
          'red-eye',
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
        // Video, not film — no grain, no film saturation.
        look: ['soft video image', 'washed-out video color', 'analogue video noise'],
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
    look: ['limited dynamic range', 'digital noise in the shadows', 'small sensor look'],
    flashLook: [
      'harsh built-in flash',
      'background falling off underexposed',
      'flat frontal lighting',
      'cyan-green fluorescent color cast',
    ],
    daylightLook: ['blown-out sky', 'washed-out daylight color', 'heavy sensor noise in shadows'],
    subjectPeriod: {
      hair: ['frosted hair tips', 'flat-ironed hair'],
      marker: ['early 2000s clothing'],
      wardrobeTop: ['velour tracksuit top', 'spaghetti-strap top'],
      wardrobeFull: ['low-rise jeans', 'chunky flip-flops'],
      decorIndoor: ['beige desktop computer', 'silver electronics'],
      decorOutdoor: ['early 2000s parked cars in the background'],
    },
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
        look: ['oversharpened digital texture', 'low megapixel softness'],
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
        look: ['smeared low-resolution detail', 'muddy desaturated color'],
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
        look: ['visible film grain'],
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
    flashLook: [],
    daylightLook: [],
    subjectPeriod: {},
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
