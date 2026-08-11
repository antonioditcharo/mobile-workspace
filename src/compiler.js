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
} from './vocab.js';

import {
  ERAS,
  REALISM_NEGATIVE,
  INTENSITY_LEVELS,
  DEFAULT_INTENSITY,
  DEFAULT_ERA,
  ASPECT_RATIOS,
} from './eras.js';

/** Categories the vision stage fills in, in the order it asks about them. */
export const OBSERVATION_FIELDS = [
  'subject',
  'appearance',
  'clothing',
  'action',
  'setting',
  'colors',
  'lighting',
  'shotType',
];

/* ------------------------------------------------------------------ *
 * Text cleanup
 * ------------------------------------------------------------------ */

/**
 * Strip the conversational scaffolding a small VLM wraps around its answers
 * and reduce the result to a bare descriptive phrase.
 */
export function cleanAnswer(raw) {
  if (typeof raw !== 'string') return '';
  let out = raw.replace(/\s+/g, ' ').trim();

  // Models often answer in several sentences; the first carries the content.
  out = out.split(/(?<=[.!?])\s+/)[0] || out;

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

/** Split a descriptive phrase into individual tag-sized chunks. */
export function toTags(phrase) {
  return String(phrase || '')
    .split(/\s*(?:,|;|\band\b|\bwith\b(?=\s+(?:a|an|the)\b))\s*/i)
    .map((t) => t.trim())
    .filter(Boolean);
}

/** Lowercase, de-article, and map onto the controlled vocabulary. */
export function normalizeTag(tag) {
  let out = String(tag || '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .replace(/[.!?]+$/, '')
    .replace(/^(?:a|an|the)\s+/, '')
    .trim();

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
export function fieldToTags(raw) {
  const cleaned = cleanAnswer(raw);
  if (isNullAnswer(cleaned)) return [];
  const tags = toTags(cleaned)
    .map(normalizeTag)
    .filter((t) => t && !GENERIC_TAGS.has(t) && !isNullAnswer(t));
  return dedupeTags(tags);
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
export function eraContribution(eraId, formatId, intensity = DEFAULT_INTENSITY, variant = 0) {
  const era = ERAS[eraId] || ERAS[DEFAULT_ERA];
  const formatKey = formatId && era.formats[formatId] ? formatId : era.defaultFormat;
  const format = era.formats[formatKey] || { camera: [], stock: [], artifacts: [] };
  const level = INTENSITY_LEVELS[intensity] || INTENSITY_LEVELS[DEFAULT_INTENSITY];

  const framing = pick(era.snapshotFraming, variant);
  const stock = phraseStock(pick(format.stock, variant));
  const camera = pick(format.camera, variant);

  return {
    era,
    format,
    formatKey,
    framing,
    medium: [framing, stock, camera].filter(Boolean),
    look: (era.look || []).slice(0, level.look),
    artifacts: (format.artifacts || []).slice(0, level.artifacts),
    aspect: format.aspect || era.aspect,
    cfg: format.cfg || era.cfg,
    steps: format.steps || era.steps,
    negativeExclude: [...(era.negativeExclude || []), ...(format.negativeExclude || [])],
  };
}

/* ------------------------------------------------------------------ *
 * Negative prompt
 * ------------------------------------------------------------------ */

/**
 * Assemble the negative prompt, then subtract anything the positive prompt
 * asks for. Without this an era that wants JPEG blocks or a white print border
 * would simultaneously forbid them.
 */
export function buildNegative(era, contribution, positiveTags) {
  const exclude = new Set(contribution.negativeExclude.map((t) => t.toLowerCase()));

  // REALISM_NEGATIVE only applies to realism-forcing presets; `suppressQualityTags`
  // is precisely the flag that marks a preset as one.
  const parts = [
    ...BASE_NEGATIVE,
    ...(era.suppressQualityTags ? REALISM_NEGATIVE : []),
    ...(era.negative || []),
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

function applyJoiner(category, tags) {
  const joiner = NL_JOINERS[category] || { lead: '', skipIf: [] };
  const phrase = tags.join(', ');
  if (!joiner.lead) return phrase;
  const opensWith = joiner.skipIf.some((word) =>
    new RegExp(`^${word}\\b`, 'i').test(phrase),
  );
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
  } = options;

  const contribution = eraContribution(eraId, formatId, intensity, variant);
  const era = contribution.era;

  // Observed fields.
  const groups = {};
  for (const field of OBSERVATION_FIELDS) {
    groups[field] = fieldToTags(observation[field]);
  }

  // Era contributions.
  groups.medium = [...(groups.medium || []), ...contribution.medium];
  groups.lighting = [...(groups.lighting || []), ...contribution.look];
  groups.artifacts = [...(groups.artifacts || []), ...contribution.artifacts];

  if (periodSubject && era.subjectPeriod?.length) {
    groups.appearance = [...groups.appearance, ...era.subjectPeriod];
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

  const positiveTags = [...CATEGORY_ORDER, 'quality'].flatMap((c) => groups[c] || []);

  const prompt =
    style === 'natural'
      ? renderNatural(groups, { framing: contribution.framing })
      : renderTagStyle(groups, { emphasis });

  const negative = buildNegative(era, contribution, positiveTags);

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
    suppressedQualityTags: Boolean(era.suppressQualityTags),
  };
}

/** Human-readable settings block for pasting alongside the prompt. */
export function settingsBlock(result) {
  const [lo, hi] = result.cfg.range;
  return [
    `Prompt: ${result.prompt}`,
    '',
    `Negative prompt: ${result.negative}`,
    '',
    `Guidance / CFG: ${result.cfg.value}  (usable range ${lo}-${hi})`,
    `Steps: ${result.steps}`,
    `Aspect ratio: ${result.aspect}`,
    `Era: ${result.eraLabel} — ${result.formatLabel}`,
    `Artifact intensity: ${result.intensity}`,
  ].join('\n');
}
