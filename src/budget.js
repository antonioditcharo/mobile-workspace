/**
 * Prompt token budgeting.
 *
 * WHY THIS EXISTS
 *
 * CLIP text encoders — what Stable Diffusion and its descendants use — accept a
 * fixed 77-token context, 75 of which are usable after the start/end markers.
 * Anything past that is silently discarded by the image model.
 *
 * Measured on a realistic result, this app was emitting ~82 words of prompt
 * (82–136 tokens) and 80 negative terms (132–265 tokens). Both overflowed, and
 * the overflow fell exactly where it hurt most: the film/camera/print block that
 * creates the period look sat at the *end* of the prompt, and the anti-AI-look
 * realism terms sat *after* generic anatomy boilerplate in the negative. The
 * app's entire purpose was in the part being thrown away.
 *
 * So this module does two things:
 *
 *  1. Estimates token count, to keep both prompts inside budget.
 *  2. Provides an explicit drop order, so when something must go it is the least
 *     valuable tag rather than whatever happens to be last.
 *
 * Emission order is left alone: earlier tokens carry more attention weight, so
 * the subject still leads and the medium still trails. Trimming removes
 * low-value tags from the middle instead of truncating the tail.
 *
 * ON THE ESTIMATE: a real CLIP tokenizer needs its 49k-entry BPE vocabulary,
 * which is a megabyte of data this app has no other use for. The heuristic below
 * is tuned to *over*-count slightly, because overflowing the budget silently
 * loses content while under-filling it merely wastes a little room.
 */

/** Usable CLIP tokens: 77 context minus the start and end markers. */
export const TOKEN_BUDGET = 75;

/**
 * Estimate CLIP BPE tokens for a prompt.
 *
 * Three things drive the count:
 *  - Punctuation is tokenised. A comma-separated tag list pays a token per comma,
 *    which is easy to forget and adds up fast in this app's output.
 *  - Common words are a single token, even fairly long ones ("photograph",
 *    "saturation"), because they are in the vocabulary.
 *  - Rare words, brand names and hyphenates split ("Kodachrome", "dot-matrix").
 */
export function estimateTokens(text) {
  const input = String(text || '').trim();
  if (!input) return 0;

  let tokens = 0;

  // Punctuation that CLIP emits as its own token.
  const punctuation = input.match(/[,.;:!?()]/g);
  if (punctuation) tokens += punctuation.length;

  const words = input
    .replace(/[,.;:!?()]/g, ' ')
    .split(/\s+/)
    .filter(Boolean);

  for (const word of words) {
    // Hyphens and slashes are split points in BPE.
    const parts = word.split(/[-/]/).filter(Boolean);
    tokens += Math.max(0, parts.length - 1); // the separators themselves
    for (const part of parts) {
      const bare = part.replace(/[^A-Za-z0-9]/g, '');
      if (!bare) continue;
      if (/\d/.test(bare)) {
        // Digit runs tokenise poorly: "768x512", "4x6", "200".
        tokens += Math.max(1, Math.ceil(bare.length / 2));
      } else if (bare.length <= 8) {
        tokens += 1;
      } else {
        // Longer words are usually one vocabulary entry plus a fragment or two.
        tokens += 1 + Math.ceil((bare.length - 8) / 5);
      }
    }
  }

  return tokens;
}

/** True when a prompt will fit CLIP's context. */
export function withinBudget(text, budget = TOKEN_BUDGET) {
  return estimateTokens(text) <= budget;
}

/* ------------------------------------------------------------------ *
 * Drop order for the positive prompt
 * ------------------------------------------------------------------ */

/**
 * What to sacrifice first when a prompt is over budget. Higher goes first.
 *
 * `subject` and `medium` are absent deliberately: the subject anchor and the era
 * apparatus are the two things this app exists to produce, so they are never
 * dropped. If a prompt cannot fit without them, it is emitted over budget and
 * the UI says so.
 */
export const DROP_ORDER = {
  // Colours go first: the era's own colour cast overrides them anyway.
  colors: 60,
  gaze: 50,
  action: 45,
  // Observed lighting, not the era's look — see eraLook below.
  lighting: 40,
  appearance: 35,
  // Era look and artifacts thin down to their minimums before subject content is
  // sacrificed. The protected medium tags (film stock, camera, framing) already
  // carry the period identity, so the 6th grain descriptor is worth less than
  // knowing what the subject is wearing.
  eraLook: 28,
  artifacts: 25,
  setting: 20,
  clothing: 18,
  // Pose is expensive in tokens but it is a headline feature; it outranks
  // incidental detail.
  pose: 15,
  shotType: 5,
  // Last of all: the user typed these by hand, so they outrank everything the
  // model merely observed.
  extra: 1,
};

/** Categories that must survive trimming. */
export const PROTECTED_CATEGORIES = new Set(['subject', 'medium', 'quality']);

/**
 * Minimum tags to keep per category even while trimming, so a category is
 * thinned rather than erased. The first artifact is the era's signature one.
 */
export const KEEP_AT_LEAST = {
  // These floors are what actually protect the period look: the list thins, but
  // never empties.
  artifacts: 1,
  eraLook: 2,
  appearance: 1,
  setting: 1,
  pose: 1,
  // What the subject is wearing is core image content, not a detail: thin the
  // list, but never lose it completely.
  clothing: 1,
};

/**
 * Choose the next tag to drop: lowest-value category first, and within a
 * category the last tag, since the model listed the most salient detail first.
 *
 * Returns `null` when nothing further may be dropped.
 */
export function pickDroppable(groups) {
  const candidates = Object.keys(groups)
    .filter((category) => !PROTECTED_CATEGORIES.has(category))
    .filter((category) => Array.isArray(groups[category]) && groups[category].length > 0)
    .filter((category) => groups[category].length > (KEEP_AT_LEAST[category] || 0))
    .filter((category) => DROP_ORDER[category] !== undefined);

  if (!candidates.length) return null;

  candidates.sort((a, b) => DROP_ORDER[b] - DROP_ORDER[a]);
  const category = candidates[0];
  return { category, index: groups[category].length - 1 };
}

/**
 * Trim a group map until its rendered form fits the budget.
 *
 * Rendering is passed in rather than assumed, because trimming has to work for
 * both output styles: dropping whole tags before rendering keeps prose grammar
 * intact, which truncating rendered text would not.
 */
export function fitGroupsToBudget(groups, render, budget = TOKEN_BUDGET) {
  const working = {};
  for (const [category, tags] of Object.entries(groups)) {
    working[category] = Array.isArray(tags) ? [...tags] : tags;
  }

  const dropped = [];
  let text = render(working);
  let guard = 0;

  while (estimateTokens(text) > budget && guard < 200) {
    guard += 1;
    const victim = pickDroppable(working);
    if (!victim) break;
    dropped.push(working[victim.category][victim.index]);
    working[victim.category].splice(victim.index, 1);
    text = render(working);
  }

  return { groups: working, text, dropped, tokens: estimateTokens(text) };
}

/* ------------------------------------------------------------------ *
 * Priority for the negative prompt
 * ------------------------------------------------------------------ */

/**
 * The negative terms that matter most to this app, in order.
 *
 * These were previously emitted *after* generic anatomy boilerplate, which meant
 * they were the first thing CLIP discarded — for an app whose whole thesis is
 * suppressing the AI look, exactly backwards.
 *
 * Tier 1 is the anti-AI-look core. Tier 2 is a short list of anatomy failures,
 * kept high because they are cheap and very visible. Era and content terms come
 * next, since they are specific to the current request. Everything else follows.
 */
export const NEGATIVE_TIER_1 = [
  // Deliberately short. Every term here costs budget that the era and content
  // tiers need, so this is the potent core rather than the whole realism list —
  // the remainder still follows, it just yields first.
  'digital art',
  'illustration',
  'render',
  'cgi',
  'anime',
  'airbrushed',
  'smooth skin',
  'hdr',
  'masterpiece',
  '8k',
  'cinematic lighting',
  'studio lighting',
  'color grading',
  'oversaturated',
  'professional photography',
  'modern smartphone photo',
  'creamy bokeh',
];

export const NEGATIVE_TIER_2 = [
  'bad anatomy',
  'deformed',
  'extra fingers',
  'malformed hands',
  'extra limbs',
];

/**
 * Order a negative list by priority, then trim to budget.
 *
 * `contextual` holds era and content terms, which are request-specific and so
 * rank above the remaining generic boilerplate.
 */
export function prioritiseNegative(
  terms,
  { content = [], era = [] } = {},
  budget = TOKEN_BUDGET,
) {
  const remaining = new Set(terms.map((t) => t.toLowerCase()));
  const ordered = [];

  const take = (list) => {
    for (const term of list) {
      const key = term.toLowerCase();
      if (remaining.has(key)) {
        ordered.push(term);
        remaining.delete(key);
      }
    }
  };

  // Content terms come first and are few. They are behaviour, not polish: if the
  // Safe level's nudity terms get trimmed away, the setting silently stops
  // working, which is worse than any loss of period character.
  take(content);
  take(NEGATIVE_TIER_1);
  // Era terms are request-specific and cheap, and "modern clothing" in a 1990s
  // prompt does more work than a generic quality rejection.
  take(era);
  take(NEGATIVE_TIER_2);

  // Whatever is left keeps its original relative order.
  for (const term of terms) {
    if (remaining.has(term.toLowerCase())) {
      ordered.push(term);
      remaining.delete(term.toLowerCase());
    }
  }

  const kept = [];
  const dropped = [];
  for (const term of ordered) {
    const candidate = kept.concat(term).join(', ');
    if (estimateTokens(candidate) > budget) dropped.push(term);
    else kept.push(term);
  }

  return { kept, dropped, tokens: estimateTokens(kept.join(', ')) };
}
