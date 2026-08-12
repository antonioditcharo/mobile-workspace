/**
 * The prompt compiler.
 *
 * Everything here is a pure function of its inputs. The vision model is only
 * ever trusted to *observe* an image; all structure — cleanup, vocabulary
 * normalisation, ordering, weighting, era overlay, negative-prompt assembly —
 * happens deterministically in this file, which is why it can be unit tested
 * without a model or a browser.
 */

import {
  FILLER_PATTERNS,
  NULL_ANSWERS,
  GENERIC_TAGS,
  SYNONYMS,
  DEDUPE_STOPWORDS,
  CATEGORY_ORDER,
  NL_JOINERS,
  QUALITY_TAGS,
  BASE_NEGATIVE,
  OUTDOOR_HINTS,
  INDOOR_HINTS,
  DAYLIGHT_HINTS,
  CLOSE_FRAMING_HINTS,
  WAIST_UP_HINTS,
  FULL_BODY_HINTS,
  SHOT_TYPES,
  LIGHTING_NOUNS,
  SETTING_PREPOSITIONS,
  DEFAULT_SETTING_PREPOSITION,
  POSE_SYNONYMS,
  GAZE_SYNONYMS,
  CONTENT_LEVELS,
  DEFAULT_CONTENT,
  MINOR_TERMS,
  NON_ADULT_ANSWERS,
  NSFW_QUALITY_TAGS,
  PRONOUN_PREFIXES,
} from './vocab.js';

import {
  ERAS,
  REALISM_NEGATIVE,
  INTENSITY_LEVELS,
  DEFAULT_INTENSITY,
  DEFAULT_ERA,
  ASPECT_RATIOS,
} from './eras.js';

import { perchanceSettings } from './perchance.js';
import {
  TOKEN_BUDGET,
  estimateTokens,
  fitGroupsToBudget,
  prioritiseNegative,
} from './budget.js';

/**
 * Categories the vision stage fills in, in the order it asks about them.
 *
 * `placement` is asked but never emitted as a prompt tag — it only drives scene
 * inference, which decides whether era presets may talk about camera flash,
 * walls and indoor furniture.
 */
export const OBSERVATION_FIELDS = [
  'subject',
  'appearance',
  'clothing',
  'pose',
  'action',
  'gaze',
  'setting',
  'placement',
  'colors',
  'lighting',
  'shotType',
  'apparentAge',
];

/** Fields used for reasoning only — never emitted as prompt tags. */
export const INTERNAL_FIELDS = ['placement', 'apparentAge'];

/** Per-category vocabulary, consulted before the global synonym table. */
const CATEGORY_SYNONYMS = {
  pose: POSE_SYNONYMS,
  gaze: GAZE_SYNONYMS,
};

/* ------------------------------------------------------------------ *
 * Text cleanup
 * ------------------------------------------------------------------ */

/**
 * Ceiling on one field's description. Generous — the point is to stop a runaway
 * answer, not to trim a good one. Truncation is repaired rather than displayed.
 */
export const MAX_ANSWER_CHARS = 400;

/**
 * Repair an answer that generation cut off mid-thought.
 *
 * When a pass hits its token cap the model stops wherever it happens to be,
 * which surfaces as "wearing a blue shirt and a pair of jea". Rather than show
 * the fragment, drop back to the last clean boundary.
 *
 * `truncated` comes from comparing generated length against the cap, so a
 * single-clause answer is only shortened when generation really was cut off —
 * "a woman" has no terminator either, and must survive untouched.
 */
export function repairTruncation(text, truncated = false) {
  let out = String(text || '').trim();
  if (!out) return '';

  // A properly finished sentence needs no repair.
  if (/[.!?]$/.test(out)) return out.replace(/[.!?]+$/, '');

  // Prefer cutting at a clause boundary, which keeps whole descriptive units.
  const lastBoundary = Math.max(out.lastIndexOf(','), out.lastIndexOf(';'));
  if (lastBoundary > 0) {
    const head = out.slice(0, lastBoundary).trim();
    // Only worth doing if a boundary actually precedes the dangling tail.
    if (head) return head;
  }

  // No boundary to fall back to: drop the final, probably partial, word — but
  // only when generation is known to have been cut off.
  if (truncated) {
    const words = out.split(/\s+/);
    if (words.length > 2) return words.slice(0, -1).join(' ');
  }
  return out;
}

/**
 * Strip the conversational scaffolding a small VLM wraps around its answers
 * and reduce the result to a bare descriptive phrase.
 */
export function cleanAnswer(raw, { maxChars = MAX_ANSWER_CHARS } = {}) {
  if (typeof raw !== 'string') return '';
  let out = raw.replace(/\s+/g, ' ').trim();

  // Keep as many whole sentences as fit.
  //
  // This used to keep only the first sentence, which was a reasonable defence
  // against a 256M model rambling — but on a larger model it silently deleted
  // good description. Now the cap is length, not sentence count, and a trailing
  // sentence is only dropped when keeping it would blow the budget.
  const sentences = out.split(/(?<=[.!?])\s+/);
  if (sentences.length > 1) {
    let kept = '';
    for (const sentence of sentences) {
      if (kept && (kept + ' ' + sentence).length > maxChars) break;
      kept = kept ? `${kept} ${sentence}` : sentence;
    }
    out = kept || sentences[0];
  }
  if (out.length > maxChars) out = out.slice(0, maxChars);

  for (const pattern of FILLER_PATTERNS) {
    out = out.replace(pattern, ' ');
  }

  out = out
    .replace(/\s+/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .replace(/^[\s,.;:—-]+/, '')
    .replace(/[\s,.;:]+$/, '')
    .trim();

  return out;
}

/** True when an answer carries no usable information. */
export function isNullAnswer(text) {
  const key = String(text || '')
    .toLowerCase()
    .replace(/[.!?]+$/, '')
    .trim();
  return key.length === 0 || NULL_ANSWERS.has(key);
}

/**
 * Split a descriptive phrase into individual tag-sized chunks.
 *
 * Sentence terminators are boundaries too. Once answers were allowed to run to
 * several sentences, splitting only on commas left tags like
 * "side-swept bangs. she has warm brown eyes" — a full stop buried in a tag.
 */
export function toTags(phrase) {
  return String(phrase || '')
    .split(/\s*(?:[.!?]+|,|;|\band\b|\bwith\b(?=\s+(?:a|an|the)\b))\s*/i)
    .map((t) => t.trim())
    .filter(Boolean);
}

/** Lowercase, de-article, and map onto the controlled vocabulary. */
export function normalizeTag(tag, category = null) {
  let out = String(tag || '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .replace(/[.!?]+$/, '')
    .trim();

  // Strip the clause subject before the article, so "she has a red coat" loses
  // both "she has" and "a".
  for (const pattern of PRONOUN_PREFIXES) {
    const stripped = out.replace(pattern, '');
    if (stripped !== out) {
      out = stripped.trim();
      break;
    }
  }

  out = out.replace(/^(?:a|an|the)\s+/, '').trim();

  // Category vocabulary wins: "camera" means "at the camera" for gaze but not
  // for a subject.
  const categoryMap = category ? CATEGORY_SYNONYMS[category] : null;
  if (categoryMap && Object.prototype.hasOwnProperty.call(categoryMap, out)) {
    return categoryMap[out];
  }
  if (Object.prototype.hasOwnProperty.call(SYNONYMS, out)) {
    out = SYNONYMS[out];
  }
  return out;
}

/** Comparison key that ignores articles and filler so near-duplicates collapse. */
export function dedupeKey(tag) {
  return String(tag || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .split(/\s+/)
    .filter((w) => w && !DEDUPE_STOPWORDS.has(w))
    .join(' ');
}

/** Remove near-duplicates, keeping first occurrence. */
export function dedupeTags(tags) {
  const seen = new Set();
  const out = [];
  for (const tag of tags) {
    const key = dedupeKey(tag);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(tag);
  }
  return out;
}

/** Full cleanup pipeline for one raw model answer. */
export function fieldToTags(raw, category = null) {
  const cleaned = cleanAnswer(raw);
  if (isNullAnswer(cleaned)) return [];
  const tags = toTags(cleaned)
    .map((t) => normalizeTag(t, category))
    .filter((t) => t && !GENERIC_TAGS.has(t) && !isNullAnswer(t));
  return dedupeTags(tags);
}

/**
 * Fold a richer appearance phrase into the subject anchor.
 *
 * A longer answer often restates the subject: subject "a woman" plus appearance
 * "young woman with long wavy brown hair" put *woman* in the prompt twice, once
 * as the weighted anchor and once as a detail. Merging gives a single stronger
 * anchor and costs fewer tokens.
 *
 * Only fires when the appearance phrase genuinely contains the subject's head
 * noun, so unrelated details are left where they are.
 */
export function mergeSubjectAppearance(groups) {
  const subject = groups.subject?.[0];
  const appearance = groups.appearance || [];
  if (!subject || !appearance.length) return groups;

  // The head noun is the last word of the subject tag: "young woman" -> "woman".
  const head = subject.split(/\s+/).pop()?.toLowerCase();
  if (!head || head.length < 3) return groups;

  const index = appearance.findIndex(
    (tag) => new RegExp(`\\b${head}\\b`, 'i').test(tag) && tag.length > subject.length,
  );
  if (index === -1) return groups;

  return {
    ...groups,
    subject: [appearance[index], ...groups.subject.slice(1)],
    appearance: appearance.filter((_, i) => i !== index),
  };
}

/* ------------------------------------------------------------------ *
 * Scene inference
 * ------------------------------------------------------------------ */

/**
 * Work out where the photo was taken and how tightly it is framed, so era
 * presets can be applied honestly.
 *
 * This exists because the presets previously asserted an indoor flash snapshot
 * unconditionally, which put "hard shadow on the wall behind the subject" on a
 * photo taken at the beach, and "chunky sneakers" on a head-and-shoulders crop.
 *
 * Returns `null` for anything genuinely unknown rather than guessing, so
 * callers can choose their own default.
 */
export function inferScene(observation = {}) {
  const text = [
    observation.setting,
    observation.placement,
    observation.lighting,
    observation.action,
    observation.subject,
  ]
    .filter(Boolean)
    .join(' ');

  const framingText = [observation.shotType, observation.subject].filter(Boolean).join(' ');

  let outdoor = null;
  if (OUTDOOR_HINTS.test(text)) outdoor = true;
  else if (INDOOR_HINTS.test(text)) outdoor = false;

  const daylight = DAYLIGHT_HINTS.test(text) ? true : null;

  // Most specific framing wins: a "close-up" claim beats a "wide" one, because
  // the tight crop is what constrains which garments can be visible.
  let framing = null;
  if (CLOSE_FRAMING_HINTS.test(framingText)) framing = 'close';
  else if (WAIST_UP_HINTS.test(framingText)) framing = 'waistUp';
  else if (FULL_BODY_HINTS.test(framingText)) framing = 'full';

  return { outdoor, daylight, framing };
}

/**
 * Should era presets describe this as a flash-lit photo?
 *
 * Flash is the era archetype, so it stays the default when nothing is known —
 * but daylight or an outdoor setting rules it out.
 */
export function isFlashScene(scene = {}, format = {}) {
  if (format.forceFlash) return true;
  if (scene.daylight === true) return false;
  if (scene.outdoor === true) return false;
  return true;
}

/* ------------------------------------------------------------------ *
 * Aspect ratio
 * ------------------------------------------------------------------ */

/** Snap real image dimensions to the nearest conventional ratio label. */
export function snapAspect(width, height) {
  if (!width || !height) return null;
  const ratio = width / height;
  let best = ASPECT_RATIOS[0];
  let bestDelta = Infinity;
  for (const candidate of ASPECT_RATIOS) {
    const delta = Math.abs(candidate.value - ratio);
    if (delta < bestDelta) {
      bestDelta = delta;
      best = candidate;
    }
  }
  return best.label;
}

/* ------------------------------------------------------------------ *
 * Era overlay
 * ------------------------------------------------------------------ */

/** Pick from a list deterministically; `variant` rotates through the options. */
function pick(list, variant = 0) {
  if (!Array.isArray(list) || list.length === 0) return null;
  return list[((variant % list.length) + list.length) % list.length];
}

/**
 * Phrase a capture medium. Film stocks read naturally as "shot on X"; digital
 * sensors and tape do not, so they are emitted bare.
 */
function phraseStock(stock) {
  if (!stock) return null;
  return /sensor|tape|digital/i.test(stock) ? stock : `shot on ${stock}`;
}

/**
 * Build the medium, look and artifact tag groups contributed by an era preset.
 */
export function eraContribution(
  eraId,
  formatId,
  intensity = DEFAULT_INTENSITY,
  variant = 0,
  scene = {},
  contentLevel = DEFAULT_CONTENT,
) {
  const era = ERAS[eraId] || ERAS[DEFAULT_ERA];
  const formatKey = formatId && era.formats[formatId] ? formatId : era.defaultFormat;
  const format = era.formats[formatKey] || { camera: [], stock: [], artifacts: [] };
  const level = INTENSITY_LEVELS[intensity] || INTENSITY_LEVELS[DEFAULT_INTENSITY];

  // Adult content keeps the era's own intimate framing, so the result still
  // reads as a period photograph rather than a modern studio shoot.
  const intimate = CONTENT_LEVELS[contentLevel]?.intimate && era.intimateFraming?.length;
  const framing = pick(intimate ? era.intimateFraming : era.snapshotFraming, variant);
  const stock = phraseStock(pick(format.stock, variant));
  const camera = pick(format.camera, variant);

  // Lighting character depends on the scene; medium texture depends on the
  // format. Ordered most-characteristic-first, since intensity truncates.
  const flash = isFlashScene(scene, format);
  // Order matters twice over: intensity truncates this list, and so does the
  // token budget, which trims from the end. The medium's own texture (film grain,
  // video noise) is the period signature, so it leads — an earlier order put the
  // scene lighting first and let "visible film grain" be trimmed off a 35mm print.
  const look = [
    ...(format.look || []),
    ...(flash ? era.flashLook || [] : era.daylightLook || []),
    ...(era.look || []),
  ];

  return {
    era,
    format,
    formatKey,
    framing,
    flash,
    medium: [framing, stock, camera].filter(Boolean),
    look: dedupeTags(look).slice(0, level.look),
    artifacts: (format.artifacts || []).slice(0, level.artifacts),
    aspect: format.aspect || era.aspect,
    cfg: format.cfg || era.cfg,
    steps: format.steps || era.steps,
    negativeExclude: [...(era.negativeExclude || []), ...(format.negativeExclude || [])],
  };
}

/**
 * Choose era styling for the subject that the photo could actually show.
 *
 * Three rules, each from a real failure:
 *  - Indoor decor (popcorn ceiling, CRT television) must not be added to an
 *    outdoor photo.
 *  - Garments below the crop (jeans, sneakers) must not be added to a
 *    head-and-shoulders shot.
 *  - If the model already described the clothing, only a generic era marker is
 *    added, so the prompt doesn't ask for a coral top *and* a windbreaker.
 */
export function periodSubjectTags(era, scene = {}, { hasObservedClothing = false } = {}) {
  const period = era.subjectPeriod;
  if (!period || Array.isArray(period)) return { appearance: [], clothing: [], setting: [] };

  const framing = scene.framing;
  const appearance = [...(period.hair || [])];
  const clothing = [...(period.marker || [])];

  if (!hasObservedClothing) {
    // Visible from the waist up, so safe unless the crop is tighter than that.
    if (framing !== 'close') clothing.push(...(period.wardrobeTop || []));
    // Needs legs and feet in frame — and only when framing is actually known to
    // be wide. When it's unknown, stay quiet rather than claiming trousers and
    // shoes that may be outside the crop.
    if (framing === 'full') clothing.push(...(period.wardrobeFull || []));
  }

  const setting = [];
  if (framing !== 'close') {
    if (scene.outdoor === true) setting.push(...(period.decorOutdoor || []));
    else if (scene.outdoor === false) setting.push(...(period.decorIndoor || []));
  }

  return { appearance, clothing, setting };
}

/* ------------------------------------------------------------------ *
 * Content level
 * ------------------------------------------------------------------ */

/**
 * Decide whether adult content styling may be applied.
 *
 * Three independent checks, because no single one is reliable:
 *
 *  1. The model's age read (`apparentAge`) — cheap, and catches the obvious case.
 *  2. A lexical scan of the observed subject and appearance — catches it when the
 *     age pass failed or was edited away, since the fields are user-editable.
 *  3. The user's explicit affirmation that the subject is an adult — the only one
 *     that carries any actual accountability.
 *
 * Any one of them failing withholds adult styling. This is intentionally not
 * overridable: the fields are editable precisely so a wrong model answer can be
 * corrected, which would otherwise make check 1 trivial to defeat on its own.
 *
 * `girl` and `woman` are deliberately absent from MINOR_TERMS: they are
 * ambiguous in prompt vocabulary, and treating them as blocking would make the
 * feature useless while catching nothing that checks 1 and 3 don't.
 */
export function contentDecision(observation = {}, { content = DEFAULT_CONTENT, adultConfirmed = false } = {}) {
  const level = CONTENT_LEVELS[content] ? content : DEFAULT_CONTENT;
  const config = CONTENT_LEVELS[level];

  if (!config.requiresAdult) {
    return { level, config, allowed: true, blocked: false, reason: null };
  }

  const ageAnswer = String(observation.apparentAge || '');
  if (ageAnswer && NON_ADULT_ANSWERS.test(ageAnswer)) {
    return {
      level: DEFAULT_CONTENT,
      config: CONTENT_LEVELS[DEFAULT_CONTENT],
      allowed: false,
      blocked: true,
      reason:
        'The subject does not read as an adult, so adult content settings are disabled for this image.',
    };
  }

  const described = `${observation.subject || ''} ${observation.appearance || ''}`.toLowerCase();
  const hit = MINOR_TERMS.find((term) => new RegExp(`\\b${term}\\b`, 'i').test(described));
  if (hit) {
    return {
      level: DEFAULT_CONTENT,
      config: CONTENT_LEVELS[DEFAULT_CONTENT],
      allowed: false,
      blocked: true,
      reason: `The subject is described as "${hit}", so adult content settings are disabled for this image.`,
    };
  }

  if (!adultConfirmed) {
    return {
      level: DEFAULT_CONTENT,
      config: CONTENT_LEVELS[DEFAULT_CONTENT],
      allowed: false,
      blocked: true,
      reason: 'Confirm the subject is an adult to enable this content level.',
    };
  }

  return { level, config, allowed: true, blocked: false, reason: null };
}

/* ------------------------------------------------------------------ *
 * Negative prompt
 * ------------------------------------------------------------------ */

/**
 * Assemble the negative prompt, then subtract anything the positive prompt
 * asks for. Without this an era that wants JPEG blocks or a white print border
 * would simultaneously forbid them.
 */
export function buildNegative(era, contribution, positiveTags, contentConfig = null) {
  const exclude = new Set(contribution.negativeExclude.map((t) => t.toLowerCase()));

  // REALISM_NEGATIVE only applies to realism-forcing presets; `suppressQualityTags`
  // is precisely the flag that marks a preset as one.
  const parts = [
    ...BASE_NEGATIVE,
    ...(era.suppressQualityTags ? REALISM_NEGATIVE : []),
    ...(era.negative || []),
    ...(contentConfig?.negative || []),
  ];

  const positive = positiveTags.map((t) => t.toLowerCase());

  const out = [];
  const seen = new Set();
  for (const raw of parts) {
    const term = raw.toLowerCase();
    if (exclude.has(term) || seen.has(term)) continue;
    // Safety net for direct overlaps between the two halves of the prompt.
    const conflicts = positive.some((p) => p.includes(term) || term.includes(p));
    if (conflicts) continue;
    seen.add(term);
    out.push(raw);
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * Rendering
 * ------------------------------------------------------------------ */

function renderTagStyle(groups, { emphasis }) {
  const flat = [];
  for (const category of CATEGORY_ORDER) {
    const tags = groups[category] || [];
    tags.forEach((tag, i) => {
      const isSubjectAnchor = emphasis && category === 'subject' && i === 0;
      flat.push(isSubjectAnchor ? `(${tag}:1.2)` : tag);
    });
  }
  if (groups.quality) flat.push(...groups.quality);
  return flat.join(', ');
}

/**
 * Pick the preposition and article for a setting phrase. "in ocean" reads as
 * being in the water; "at the ocean" is what a caption means.
 */
export function settingPhrase(tags) {
  const phrase = tags.join(', ');
  if (!phrase) return '';
  // Respect a preposition the model supplied itself.
  if (/^(in|on|at|inside|outside|near|by|beside|under)\b/i.test(phrase)) return phrase;

  for (const [pattern, lead] of SETTING_PREPOSITIONS) {
    if (pattern.test(phrase)) return `${lead} ${phrase}`;
  }
  return `${DEFAULT_SETTING_PREPOSITION} ${phrase}`;
}

/**
 * Turn a bare lighting adjective into a noun phrase, so "lit by sunny" becomes
 * "lit by sunlight".
 */
export function lightingNoun(tag) {
  const text = String(tag || '').trim();
  if (!text) return text;
  const single = text.toLowerCase();
  if (Object.prototype.hasOwnProperty.call(LIGHTING_NOUNS, single)) {
    return LIGHTING_NOUNS[single];
  }
  // "sunny day" / "bright" + noun already reads fine; only bare adjectives are
  // a problem, and those are single words.
  return text;
}

/** Join a short list as prose: "a, b and c". */
function proseList(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

function applyJoiner(category, tags) {
  if (category === 'setting') return settingPhrase(tags);

  const joiner = NL_JOINERS[category] || { lead: '', skipIf: [] };
  const values = category === 'lighting' ? tags.map(lightingNoun) : tags;
  const phrase = category === 'colors' ? proseList(values) : values.join(', ');
  if (!joiner.lead) return phrase;
  const opensWith = joiner.skipIf.some((word) => new RegExp(`^${word}\\b`, 'i').test(phrase));
  return opensWith ? phrase : `${joiner.lead}${phrase}`;
}

/**
 * Put an indefinite article back on a subject phrase for prose output.
 * `normalizeTag` strips articles, which is right for tag style but reads wrong
 * in a sentence ("amateur snapshot of man").
 */
export function withArticle(phrase) {
  const text = String(phrase || '').trim();
  if (!text) return text;
  // Already determined, counted, or plural — leave alone.
  if (/^(?:a|an|the|his|her|their|some|one|two|three|several|group|\d)\b/i.test(text)) return text;
  if (/^(?:people|men|women|children|kids|couples?|crowd)\b/i.test(text)) return text;
  const head = text.split(/\s+/)[0];
  if (/[^s]s$/.test(head) && !/(?:ss|us|is)$/.test(head)) return text; // plural head noun
  return /^[aeiou]/i.test(text) ? `an ${text}` : `a ${text}`;
}

function renderNatural(groups, { framing }) {
  const clauses = [];

  const shot = groups.shotType || [];
  const subject = groups.subject || [];

  // Lead clause: "close-up shot" + "amateur snapshot of a man"
  if (shot.length) clauses.push(shot.join(', '));
  const subjectPhrase = subject.length ? withArticle(subject[0]) + subject.slice(1).map((t) => `, ${t}`).join('') : '';
  if (framing && subjectPhrase) clauses.push(`${framing} of ${subjectPhrase}`);
  else if (subjectPhrase) clauses.push(subjectPhrase);
  else if (framing) clauses.push(framing);

  for (const category of CATEGORY_ORDER) {
    if (category === 'shotType' || category === 'subject') continue;
    const tags = groups[category] || [];
    if (!tags.length) continue;
    // The framing term already opened the sentence; don't repeat it.
    const filtered = category === 'medium' ? tags.filter((t) => t !== framing) : tags;
    if (!filtered.length) continue;
    clauses.push(applyJoiner(category, filtered));
  }

  if (groups.quality) clauses.push(groups.quality.join(', '));
  return clauses.join(', ');
}

/* ------------------------------------------------------------------ *
 * Entry point
 * ------------------------------------------------------------------ */

/**
 * Compile a set of raw observations into a Perchance-ready prompt bundle.
 *
 * @param {Object} observation  Raw per-field answers from the vision stage.
 * @param {Object} [options]
 * @param {string} [options.era]            Era id from ERAS.
 * @param {string} [options.format]         Format key within that era.
 * @param {string} [options.intensity]      'subtle' | 'medium' | 'heavy'.
 * @param {string} [options.style]          'tags' | 'natural'.
 * @param {boolean} [options.periodSubject] Also period-style the subject.
 * @param {boolean} [options.emphasis]      Weight the subject anchor.
 * @param {number} [options.variant]        Rotates stock/camera/framing choices.
 * @param {{width:number,height:number}} [options.source] Source image dimensions.
 */
export function compile(observation = {}, options = {}) {
  const {
    era: eraId = DEFAULT_ERA,
    format: formatId = null,
    intensity = DEFAULT_INTENSITY,
    style = 'tags',
    periodSubject = false,
    emphasis = true,
    variant = 0,
    source = null,
    content = DEFAULT_CONTENT,
    adultConfirmed = false,
    extra = '',
    budget = TOKEN_BUDGET,
  } = options;

  const scene = inferScene(observation);
  const decision = contentDecision(observation, { content, adultConfirmed });
  const contribution = eraContribution(eraId, formatId, intensity, variant, scene, decision.level);
  const era = contribution.era;

  // Observed fields.
  let groups = {};
  for (const field of OBSERVATION_FIELDS) {
    groups[field] = fieldToTags(observation[field], field);
  }

  // Reasoning-only fields are never prompt tags.
  for (const field of INTERNAL_FIELDS) delete groups[field];

  // A small model invents shot types ("wide shot" for a tight selfie). It can't
  // be made accurate, but it can be kept to real values.
  groups.shotType = groups.shotType.filter((tag) => SHOT_TYPES.includes(tag));

  // Era contributions.
  groups.medium = [...(groups.medium || []), ...contribution.medium];
  groups.eraLook = [...(groups.eraLook || []), ...contribution.look];
  groups.artifacts = [...(groups.artifacts || []), ...contribution.artifacts];

  if (periodSubject) {
    const period = periodSubjectTags(era, scene, {
      hasObservedClothing: groups.clothing.length > 0,
    });
    groups.appearance = [...groups.appearance, ...period.appearance];
    groups.clothing = [...groups.clothing, ...period.clothing];
    groups.setting = [...groups.setting, ...period.setting];
  }

  // Anatomy support for figure work. The realism presets already reject the
  // airbrushed look, so what adult content needs is a push toward natural bodies.
  if (decision.allowed && decision.config.qualityTags > 0) {
    groups.appearance = [
      ...groups.appearance,
      ...NSFW_QUALITY_TAGS.slice(0, decision.config.qualityTags),
    ];
  }

  // Free-text terms the user supplies themselves, kept close to the subject
  // rather than trailing after the medium tags.
  if (extra) {
    groups.extra = fieldToTags(extra);
  }

  if (!era.suppressQualityTags) {
    groups.quality = [...QUALITY_TAGS];
  }

  // Global dedupe: earlier categories win, so the subject keeps its terms.
  const seen = new Set();
  for (const category of [...CATEGORY_ORDER, 'quality']) {
    if (!groups[category]) continue;
    groups[category] = groups[category].filter((tag) => {
      const key = dedupeKey(tag);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  // Fold a restated subject into one anchor before budgeting, since it removes a
  // duplicate rather than sacrificing information.
  groups = mergeSubjectAppearance(groups);

  const render = (g) =>
    style === 'natural'
      ? renderNatural(g, { framing: contribution.framing })
      : renderTagStyle(g, { emphasis });

  // Keep the prompt inside CLIP's 75-token context. Trimming drops the lowest
  // value tags rather than letting the image model silently discard the tail —
  // which, given the era apparatus trails the prompt, was cutting exactly the
  // part that matters. Whole tags go before rendering so prose stays grammatical.
  const fitted = fitGroupsToBudget(groups, render, budget);
  groups = fitted.groups;
  const prompt = fitted.text;

  const positiveTags = [...CATEGORY_ORDER, 'quality'].flatMap((c) => groups[c] || []);

  const rawNegative = buildNegative(era, contribution, positiveTags, decision.config);
  // Era and content terms are request-specific, so they outrank leftover
  // boilerplate when the negative prompt has to be trimmed.
  const negativeFit = prioritiseNegative(
    rawNegative,
    { content: decision.config.negative || [], era: era.negative || [] },
    budget,
  );
  const negative = negativeFit.kept;

  const aspect =
    contribution.aspect === 'source'
      ? snapAspect(source?.width, source?.height) || '1:1'
      : contribution.aspect;

  return {
    prompt,
    negative: negative.join(', '),
    negativeList: negative,
    cfg: contribution.cfg,
    steps: contribution.steps,
    aspect,
    era: era.id,
    eraLabel: era.label,
    format: contribution.formatKey,
    formatLabel: contribution.format.label || contribution.formatKey,
    intensity,
    style,
    groups,
    scene,
    flash: contribution.flash,
    budget: {
      limit: budget,
      promptTokens: fitted.tokens,
      negativeTokens: negativeFit.tokens,
      droppedFromPrompt: fitted.dropped,
      droppedFromNegative: negativeFit.dropped,
      // True when even the protected tags exceed the context on their own.
      promptOverBudget: fitted.tokens > budget,
    },
    content: decision.level,
    contentRequested: content,
    contentBlocked: decision.blocked,
    contentReason: decision.reason,
    suppressedQualityTags: Boolean(era.suppressQualityTags),
  };
}

/**
 * Copy-out block, written as a field-by-field checklist for the Perchance page.
 *
 * The field names and values match what the generator actually accepts (see
 * src/perchance.js for provenance), including the art-style warning, which is
 * the difference between a period prompt working and being silently overridden.
 */
export function settingsBlock(result) {
  const [lo, hi] = result.cfg.range;
  const p = perchanceSettings(result);
  return [
    `Prompt: ${result.prompt}`,
    '',
    `Negative prompt: ${result.negative}`,
    '',
    '--- Perchance settings ---',
    `Art style: ${p.style}   <- important: any other style adds 8k/HDR/masterpiece and breaks the era look`,
    `Guidance scale: ${p.guidanceScale}   (this era works in ${lo}-${hi}; Perchance accepts ${p.guidanceRange[0]}-${p.guidanceRange[1]})`,
    `Resolution: ${p.resolution.value}   (${p.resolution.label})`,
    `Seed: ${p.seed} for random, or reuse a number to repeat an image`,
    '',
    `Era: ${result.eraLabel} — ${result.formatLabel}`,
    `Artifact intensity: ${result.intensity}`,
    `Steps: ${p.advisorySteps} (advisory — Perchance does not expose a step count)`,
  ].join('\n');
}
