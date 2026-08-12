import test from 'node:test';
import assert from 'node:assert/strict';

import {
  TOKEN_BUDGET,
  estimateTokens,
  withinBudget,
  pickDroppable,
  fitGroupsToBudget,
  prioritiseNegative,
  DROP_ORDER,
  KEEP_AT_LEAST,
  PROTECTED_CATEGORIES,
  NEGATIVE_TIER_1,
} from '../src/budget.js';
import { compile, mergeSubjectAppearance, cleanAnswer } from '../src/compiler.js';
import { ERAS, ERA_IDS } from '../src/eras.js';
import { CONTENT_LEVELS, SFW_NEGATIVE } from '../src/vocab.js';

/* ------------------------------------------------------------------ *
 * Token estimation
 * ------------------------------------------------------------------ */

test('the budget matches CLIP usable context', () => {
  assert.equal(TOKEN_BUDGET, 75);
});

test('estimateTokens counts commas, which a tag list pays for heavily', () => {
  const withCommas = estimateTokens('red, green, blue');
  const withoutCommas = estimateTokens('red green blue');
  assert.ok(withCommas > withoutCommas, `${withCommas} vs ${withoutCommas}`);
});

test('estimateTokens treats common words as single tokens', () => {
  assert.equal(estimateTokens('a woman'), 2);
  assert.equal(estimateTokens('seated'), 1);
});

test('estimateTokens charges more for rare words and hyphenates', () => {
  assert.ok(estimateTokens('Kodachrome') > estimateTokens('camera'));
  assert.ok(estimateTokens('dot-matrix') >= 3, estimateTokens('dot-matrix'));
});

test('estimateTokens handles digits, which tokenise badly', () => {
  assert.ok(estimateTokens('768x512') >= 3, estimateTokens('768x512'));
});

test('estimateTokens is empty-safe', () => {
  assert.equal(estimateTokens(''), 0);
  assert.equal(estimateTokens(undefined), 0);
});

test('withinBudget agrees with the estimate', () => {
  assert.equal(withinBudget('a woman'), true);
  assert.equal(withinBudget(Array(200).fill('word').join(', ')), false);
});

/* ------------------------------------------------------------------ *
 * Drop order
 * ------------------------------------------------------------------ */

test('the subject and era medium are never droppable', () => {
  assert.ok(PROTECTED_CATEGORIES.has('subject'));
  assert.ok(PROTECTED_CATEGORIES.has('medium'));
  assert.equal(DROP_ORDER.subject, undefined);
  assert.equal(DROP_ORDER.medium, undefined);
});

test('colours yield first, and subject content outlasts surplus era detail', () => {
  assert.ok(DROP_ORDER.colors > DROP_ORDER.pose, 'colours should go first');
  // The era look thins before clothing or pose is sacrificed: the protected
  // medium tags already carry the period identity, so a sixth grain descriptor is
  // worth less than knowing what the subject is wearing. The floor in
  // KEEP_AT_LEAST is what stops it thinning away entirely.
  assert.ok(DROP_ORDER.eraLook > DROP_ORDER.clothing, 'surplus era look yields before clothing');
  assert.ok(DROP_ORDER.eraLook > DROP_ORDER.pose, 'surplus era look yields before pose');
  assert.equal(KEEP_AT_LEAST.eraLook >= 2, true, 'but a period look floor is kept');
});

test('pickDroppable takes the lowest-value category and its last tag', () => {
  const groups = {
    subject: ['a woman'],
    colors: ['teal', 'warm pink'],
    pose: ['seated'],
  };
  assert.deepEqual(pickDroppable(groups), { category: 'colors', index: 1 });
});

test('pickDroppable respects per-category minimums', () => {
  const groups = { subject: ['a woman'], artifacts: ['date stamp'], eraLook: ['film grain'] };
  // artifacts keeps at least 1 and eraLook at least 2, so neither may be taken.
  assert.equal(pickDroppable(groups), null);
});

test('pickDroppable returns null when only protected content remains', () => {
  assert.equal(pickDroppable({ subject: ['a woman'], medium: ['shot on film'] }), null);
});

test('fitGroupsToBudget trims until it fits and reports what went', () => {
  const groups = {
    subject: ['a woman'],
    colors: Array.from({ length: 40 }, (_, i) => `colour number ${i}`),
    medium: ['shot on Kodak Gold 200'],
  };
  const render = (g) => Object.values(g).flat().join(', ');
  const result = fitGroupsToBudget(groups, render);
  assert.ok(result.tokens <= TOKEN_BUDGET, `${result.tokens} tokens`);
  assert.ok(result.dropped.length > 0);
  // Protected categories survive.
  assert.deepEqual(result.groups.subject, ['a woman']);
  assert.deepEqual(result.groups.medium, ['shot on Kodak Gold 200']);
});

test('fitGroupsToBudget leaves an already-small prompt untouched', () => {
  const groups = { subject: ['a woman'], colors: ['teal'] };
  const render = (g) => Object.values(g).flat().join(', ');
  const result = fitGroupsToBudget(groups, render);
  assert.deepEqual(result.dropped, []);
  assert.deepEqual(result.groups, groups);
});

test('fitGroupsToBudget does not loop forever on an unfittable prompt', () => {
  const groups = { subject: [Array(200).fill('word').join(' ')] };
  const render = (g) => Object.values(g).flat().join(', ');
  const result = fitGroupsToBudget(groups, render);
  // Cannot fit, but must terminate and say so via the token count.
  assert.ok(result.tokens > TOKEN_BUDGET);
});

/* ------------------------------------------------------------------ *
 * Negative prompt priority
 * ------------------------------------------------------------------ */

test('content terms outrank everything, since losing them changes behaviour', () => {
  const terms = [...NEGATIVE_TIER_1, ...SFW_NEGATIVE, 'watermark'];
  const { kept } = prioritiseNegative(terms, { content: SFW_NEGATIVE });
  for (const term of SFW_NEGATIVE) {
    assert.ok(kept.includes(term), `Safe term "${term}" was dropped`);
  }
  assert.equal(kept[0], SFW_NEGATIVE[0], kept.slice(0, 3).join(', '));
});

test('era terms outrank leftover boilerplate', () => {
  const era = ['modern clothing', 'smartphone'];
  const terms = [...Array(60).fill(0).map((_, i) => `filler${i}`), ...NEGATIVE_TIER_1, ...era];
  const { kept } = prioritiseNegative(terms, { era });
  for (const term of era) {
    assert.ok(kept.includes(term), `era term "${term}" was dropped`);
  }
});

test('the negative prompt is trimmed to budget', () => {
  const terms = Array.from({ length: 200 }, (_, i) => `term number ${i}`);
  const { kept, dropped, tokens } = prioritiseNegative(terms);
  assert.ok(tokens <= TOKEN_BUDGET, `${tokens} tokens`);
  assert.ok(dropped.length > 0);
  assert.ok(kept.length > 0);
});

test('prioritiseNegative never duplicates or invents a term', () => {
  const terms = ['digital art', 'watermark', 'nude', 'digital art'];
  const { kept, dropped } = prioritiseNegative(terms, { content: ['nude'] });
  const all = [...kept, ...dropped];
  assert.equal(new Set(all).size, all.length, 'duplicates present');
  for (const term of all) assert.ok(terms.includes(term), `invented "${term}"`);
});

/* ------------------------------------------------------------------ *
 * Subject / appearance merge
 * ------------------------------------------------------------------ */

test('a restated subject is folded into one anchor', () => {
  const merged = mergeSubjectAppearance({
    subject: ['woman'],
    appearance: ['young woman with long wavy brown hair', 'side-swept bangs'],
  });
  assert.deepEqual(merged.subject, ['young woman with long wavy brown hair']);
  assert.deepEqual(merged.appearance, ['side-swept bangs']);
});

test('an unrelated appearance detail is left alone', () => {
  const groups = { subject: ['woman'], appearance: ['red scarf', 'freckles'] };
  assert.deepEqual(mergeSubjectAppearance(groups), groups);
});

test('the merge does not fire on a shorter appearance phrase', () => {
  const groups = { subject: ['young woman with brown hair'], appearance: ['woman'] };
  assert.deepEqual(mergeSubjectAppearance(groups).subject, ['young woman with brown hair']);
});

test('the merge is safe with missing groups', () => {
  assert.deepEqual(mergeSubjectAppearance({}), {});
  assert.deepEqual(mergeSubjectAppearance({ subject: ['a woman'] }).subject, ['a woman']);
});

test('the compiled prompt does not name the subject twice', () => {
  const result = compile(
    {
      subject: 'a woman',
      appearance: cleanAnswer('A young woman with long wavy brown hair. She has brown eyes.'),
    },
    { era: '1990s', style: 'tags' },
  );
  const matches = result.prompt.match(/\bwoman\b/g) || [];
  assert.equal(matches.length, 1, result.prompt);
});

/* ------------------------------------------------------------------ *
 * End-to-end invariants
 *
 * These are the point of the whole module: whatever the settings, both prompts
 * must fit CLIP's context, and the tags that make the app work must survive.
 * ------------------------------------------------------------------ */

const VERBOSE = {
  subject: 'a woman',
  appearance: cleanAnswer(
    'A young woman with long wavy brown hair and side-swept bangs. She has warm brown eyes, a small silver nose stud and lightly freckled cheeks.',
  ),
  clothing: 'a coral tank top and a thin gold necklace',
  pose: cleanAnswer(
    'The subject is seated on a low stone wall. She is leaning back on both hands with her head tilted.',
  ),
  action: 'smiling broadly at the camera',
  gaze: 'at the camera',
  setting: 'a wide sandy beach beside the ocean at sunset',
  placement: 'outdoors',
  colors: 'teal, warm pink, sandy beige',
  lighting: 'warm low sunlight',
  shotType: 'waist-up shot',
  apparentAge: 'adult',
};

test('every era, format and style fits the token budget', () => {
  for (const id of ERA_IDS) {
    for (const format of Object.keys(ERAS[id].formats)) {
      for (const style of ['tags', 'natural']) {
        for (const intensity of ['subtle', 'heavy']) {
          const r = compile(VERBOSE, { era: id, format, style, intensity, periodSubject: true });
          assert.ok(
            r.budget.promptTokens <= TOKEN_BUDGET,
            `${id}/${format}/${style}/${intensity} prompt ${r.budget.promptTokens} tokens`,
          );
          assert.ok(
            r.budget.negativeTokens <= TOKEN_BUDGET,
            `${id}/${format}/${style}/${intensity} negative ${r.budget.negativeTokens} tokens`,
          );
        }
      }
    }
  }
});

test('the era apparatus always survives trimming', () => {
  for (const id of ERA_IDS.filter((e) => e !== 'none')) {
    for (const format of Object.keys(ERAS[id].formats)) {
      const r = compile(VERBOSE, { era: id, format, intensity: 'heavy' });
      // The film stock or camera body — the era's identity — must be present.
      const medium = r.groups.medium || [];
      assert.ok(medium.length >= 2, `${id}/${format} lost its medium tags`);
      for (const tag of medium) {
        assert.ok(r.prompt.includes(tag), `${id}/${format} dropped medium tag "${tag}"`);
      }
    }
  }
});

test('the subject anchor always survives trimming', () => {
  const r = compile(VERBOSE, { era: '1990s', intensity: 'heavy' });
  assert.ok(r.groups.subject.length >= 1);
  assert.ok(r.prompt.includes(r.groups.subject[0].replace(/[()]/g, '').split(':')[0]));
});

test('content negatives survive at every level', () => {
  for (const level of Object.keys(CONTENT_LEVELS)) {
    const r = compile(VERBOSE, {
      era: '1990s',
      content: level,
      adultConfirmed: true,
      useNegative: true,
      intensity: 'heavy',
    });
    for (const term of CONTENT_LEVELS[r.content].negative) {
      assert.ok(
        r.negativeList.includes(term),
        `${level}: content negative "${term}" was trimmed away`,
      );
    }
  }
});

test('the anti-AI-look core survives in the negative prompt', () => {
  const r = compile(VERBOSE, { era: '1990s', intensity: 'heavy', useNegative: true });
  for (const term of ['digital art', 'airbrushed', 'smooth skin', 'masterpiece']) {
    assert.ok(r.negativeList.includes(term), `lost core negative "${term}"`);
  }
});

test('what was trimmed is reported rather than silently dropped', () => {
  const r = compile(VERBOSE, { era: '1990s', intensity: 'heavy', useNegative: true });
  assert.ok(Array.isArray(r.budget.droppedFromPrompt));
  assert.ok(Array.isArray(r.budget.droppedFromNegative));
  assert.ok(
    r.budget.droppedFromPrompt.length + r.budget.droppedFromNegative.length > 0,
    'a verbose observation should have needed trimming',
  );
  assert.equal(r.budget.promptOverBudget, false);
});

test('a short observation is never trimmed', () => {
  const r = compile({ subject: 'a man' }, { era: '1990s', intensity: 'subtle' });
  assert.deepEqual(r.budget.droppedFromPrompt, []);
});

test('the budget limit is configurable', () => {
  const tight = compile(VERBOSE, { era: '1990s', budget: 30 });
  assert.ok(tight.budget.promptTokens <= 30 || tight.budget.promptOverBudget, tight.prompt);
  assert.equal(tight.budget.limit, 30);
});
