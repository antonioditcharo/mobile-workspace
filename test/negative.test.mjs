import test from 'node:test';
import assert from 'node:assert/strict';

import { compile, settingsBlock } from '../src/compiler.js';
import {
  POSITIVE_REALISM,
  POSITIVE_REALISM_PERSON,
  PERSON_HINTS,
} from '../src/vocab.js';
import { ERA_IDS, ERAS } from '../src/eras.js';
import { TOKEN_BUDGET } from '../src/budget.js';

const PERSON = {
  subject: 'a woman',
  appearance: 'brown hair',
  setting: 'a beach by the ocean',
  placement: 'outdoors',
  shotType: 'waist-up shot',
};

const LANDSCAPE = { subject: 'a mountain range', setting: 'a valley', placement: 'outdoors' };

/* ------------------------------------------------------------------ *
 * Negative prompt is off by default
 *
 * The Perchance generator in use has no negative prompt field, so emitting one
 * was dead weight — and worse, it meant every realism rejection the era presets
 * relied on was silently doing nothing.
 * ------------------------------------------------------------------ */

test('no negative prompt is emitted by default', () => {
  const r = compile(PERSON, { era: '1990s' });
  assert.equal(r.useNegative, false);
  assert.equal(r.negative, '');
  assert.deepEqual(r.negativeList, []);
  assert.equal(r.budget.negativeTokens, 0);
});

test('the negative prompt can still be turned back on', () => {
  const r = compile(PERSON, { era: '1990s', useNegative: true });
  assert.equal(r.useNegative, true);
  assert.ok(r.negativeList.length > 10, `${r.negativeList.length} terms`);
  assert.ok(r.budget.negativeTokens > 0);
  assert.ok(r.budget.negativeTokens <= TOKEN_BUDGET);
});

/* ------------------------------------------------------------------ *
 * Positive substitution
 * ------------------------------------------------------------------ */

test('realism constraints are stated positively when there is no negative', () => {
  const r = compile(PERSON, { era: '1990s' });
  for (const tag of POSITIVE_REALISM) {
    assert.ok(r.prompt.includes(tag), `missing positive substitute "${tag}": ${r.prompt}`);
  }
});

test('person-only substitutes are gated on the subject being a person', () => {
  const person = compile(PERSON, { era: '1990s' });
  for (const tag of POSITIVE_REALISM_PERSON) {
    assert.ok(person.prompt.includes(tag), `person prompt missing "${tag}"`);
  }

  const landscape = compile(LANDSCAPE, { era: '1990s' });
  for (const tag of POSITIVE_REALISM_PERSON) {
    assert.equal(
      landscape.prompt.includes(tag),
      false,
      `landscape should not ask for "${tag}": ${landscape.prompt}`,
    );
  }
  // Specifically: no skin on a mountain.
  assert.equal(/skin/i.test(landscape.prompt), false, landscape.prompt);
});

test('PERSON_HINTS recognises people and not scenery', () => {
  for (const subject of ['a woman', 'two men', 'a couple', 'a child', 'someone', 'a portrait']) {
    assert.ok(PERSON_HINTS.test(subject), `should match "${subject}"`);
  }
  for (const subject of ['a mountain range', 'a red bicycle', 'an empty street', 'a bowl of fruit']) {
    assert.equal(PERSON_HINTS.test(subject), false, `should not match "${subject}"`);
  }
});

test('substitutes are not added when the negative prompt is in use', () => {
  const r = compile(PERSON, { era: '1990s', useNegative: true });
  for (const tag of POSITIVE_REALISM) {
    assert.equal(r.prompt.includes(tag), false, `duplicated constraint "${tag}"`);
  }
});

test('the no-era preset gets no substitutes, since it suppresses nothing', () => {
  const r = compile(PERSON, { era: 'none' });
  for (const tag of POSITIVE_REALISM) {
    assert.equal(r.prompt.includes(tag), false, `no-era should not add "${tag}"`);
  }
});

test('substitutes survive the token budget across every era', () => {
  for (const id of ERA_IDS.filter((e) => e !== 'none')) {
    for (const format of Object.keys(ERAS[id].formats)) {
      const r = compile(PERSON, { era: id, format, intensity: 'heavy' });
      assert.ok(r.budget.promptTokens <= TOKEN_BUDGET, `${id}/${format} over budget`);
      const kept = (r.groups.realism || []).length;
      assert.ok(kept >= 2, `${id}/${format} kept only ${kept} realism substitutes`);
    }
  }
});

/* ------------------------------------------------------------------ *
 * Copy-out block
 * ------------------------------------------------------------------ */

test('the settings block omits the negative prompt when unused', () => {
  const block = settingsBlock(compile(PERSON, { era: '1990s' }));
  assert.equal(/Negative prompt:/.test(block), false, block);
  // The rest of the checklist is still there.
  assert.ok(block.includes('Art style: none'));
  assert.ok(block.includes('Guidance scale:'));
});

test('the settings block includes the negative prompt when used', () => {
  const block = settingsBlock(compile(PERSON, { era: '1990s', useNegative: true }));
  assert.ok(block.includes('Negative prompt:'), block);
});
