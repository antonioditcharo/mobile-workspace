import test from 'node:test';
import assert from 'node:assert/strict';

import { compile, contentDecision, fieldToTags, OBSERVATION_FIELDS, INTERNAL_FIELDS } from '../src/compiler.js';
import { CONTENT_LEVELS, MINOR_TERMS, NSFW_QUALITY_TAGS, SFW_NEGATIVE } from '../src/vocab.js';
import { PASSES } from '../src/vision.js';

const ADULT = {
  subject: 'a woman',
  appearance: 'brown hair',
  setting: 'a bedroom interior',
  placement: 'indoors',
  apparentAge: 'adult',
  shotType: 'waist-up shot',
};

/* ------------------------------------------------------------------ *
 * Pose, action and gaze
 * ------------------------------------------------------------------ */

test('pose, action and gaze are asked as separate passes', () => {
  const fields = PASSES.map((p) => p.field);
  for (const field of ['pose', 'action', 'gaze']) {
    assert.ok(fields.includes(field), `missing a ${field} pass`);
  }
  // Pose needs room to describe limbs and head, unlike the terse passes.
  const pose = PASSES.find((p) => p.field === 'pose');
  assert.ok(pose.tokens >= 30, `pose budget too small: ${pose.tokens}`);
});

test('pose vocabulary is normalised toward prompt terms', () => {
  assert.deepEqual(fieldToTags('sitting', 'pose'), ['seated']);
  assert.deepEqual(fieldToTags('laying down', 'pose'), ['lying down']);
  assert.deepEqual(fieldToTags('from behind', 'pose'), ['back to the camera']);
});

test('gaze vocabulary is category-specific', () => {
  assert.deepEqual(fieldToTags('camera', 'gaze'), ['at the camera']);
  assert.deepEqual(fieldToTags('away', 'gaze'), ['away from the camera']);
  // The same word must not be rewritten outside the gaze field.
  assert.deepEqual(fieldToTags('camera', 'subject'), ['camera']);
});

test('pose and gaze reach the prompt in a sensible order', () => {
  const result = compile(
    { ...ADULT, pose: 'seated, arms crossed', gaze: 'at the camera', action: 'smiling' },
    { era: '1990s', style: 'tags', emphasis: false },
  );
  const at = (t) => result.prompt.indexOf(t);
  assert.ok(at('seated') > at('woman'), result.prompt);
  assert.ok(at('at the camera') > at('seated'), result.prompt);
  assert.ok(at('shot on') > at('at the camera'), result.prompt);
});

test('natural style phrases gaze readably', () => {
  const result = compile({ ...ADULT, gaze: 'at the camera' }, { era: '1990s', style: 'natural' });
  assert.ok(result.prompt.includes('looking at the camera'), result.prompt);
  assert.equal(/looking\s+looking/.test(result.prompt), false, result.prompt);
});

/* ------------------------------------------------------------------ *
 * Content levels
 * ------------------------------------------------------------------ */

test('safe is the default and negates nudity', () => {
  const result = compile(ADULT, { era: '1990s' });
  assert.equal(result.content, 'sfw');
  for (const term of SFW_NEGATIVE) {
    assert.ok(result.negativeList.includes(term), `safe should negate "${term}"`);
  }
});

test('adult levels need the affirmation', () => {
  const withoutConfirm = compile(ADULT, { era: '1990s', content: 'explicit' });
  assert.equal(withoutConfirm.content, 'sfw');
  assert.equal(withoutConfirm.contentBlocked, true);
  assert.match(withoutConfirm.contentReason, /adult/i);

  const withConfirm = compile(ADULT, {
    era: '1990s',
    content: 'explicit',
    adultConfirmed: true,
  });
  assert.equal(withConfirm.content, 'explicit');
  assert.equal(withConfirm.contentBlocked, false);
});

test('enabling an adult level lifts the nudity negatives and adds anatomy support', () => {
  const result = compile(ADULT, { era: '1990s', content: 'explicit', adultConfirmed: true });
  for (const term of SFW_NEGATIVE) {
    assert.equal(result.negativeList.includes(term), false, `"${term}" should not be negated`);
  }
  assert.ok(result.prompt.includes(NSFW_QUALITY_TAGS[0]), result.prompt);
  assert.ok(result.negativeList.includes('plastic skin'), result.negative);
});

test('suggestive adds lighter anatomy support than explicit', () => {
  const suggestive = compile(ADULT, { era: '1990s', content: 'suggestive', adultConfirmed: true });
  const explicit = compile(ADULT, { era: '1990s', content: 'explicit', adultConfirmed: true });
  const count = (r) => NSFW_QUALITY_TAGS.filter((t) => r.prompt.includes(t)).length;
  assert.ok(count(explicit) > count(suggestive), `${count(suggestive)} vs ${count(explicit)}`);
});

test('adult levels keep the era framing rather than a modern studio look', () => {
  const result = compile(ADULT, { era: '1990s', content: 'explicit', adultConfirmed: true });
  assert.ok(/boudoir|private/i.test(result.prompt), result.prompt);
  assert.equal(/studio lighting/i.test(result.prompt), false, result.prompt);
});

test('era realism still applies at adult levels', () => {
  const result = compile(ADULT, { era: '1990s', content: 'explicit', adultConfirmed: true });
  assert.equal(/masterpiece|\b8k\b|ultra detailed/i.test(result.prompt), false, result.prompt);
  assert.ok(result.negativeList.includes('digital art'));
  assert.ok(/film grain|Kodak/i.test(result.prompt), result.prompt);
});

/* ------------------------------------------------------------------ *
 * The minor safeguard
 *
 * Three independent checks, each verified separately, because the fields are
 * user-editable and any single check alone would be trivial to defeat.
 * ------------------------------------------------------------------ */

test('the model age read blocks adult content', () => {
  const result = compile(
    { ...ADULT, apparentAge: 'child' },
    { era: '1990s', content: 'explicit', adultConfirmed: true },
  );
  assert.equal(result.content, 'sfw');
  assert.equal(result.contentBlocked, true);
  assert.match(result.contentReason, /adult/i);
});

test('the age read blocks on every non-adult phrasing', () => {
  for (const answer of ['child', 'a young child', 'teenager', 'teen', 'baby', 'toddler', 'minor']) {
    const decision = contentDecision(
      { ...ADULT, apparentAge: answer },
      { content: 'explicit', adultConfirmed: true },
    );
    assert.equal(decision.allowed, false, `"${answer}" should block`);
  }
});

test('a minor described in the subject blocks adult content', () => {
  for (const term of MINOR_TERMS) {
    const decision = contentDecision(
      { subject: `a ${term}`, apparentAge: 'adult' },
      { content: 'explicit', adultConfirmed: true },
    );
    assert.equal(decision.allowed, false, `subject "${term}" should block`);
  }
});

test('a minor described in the appearance field blocks too', () => {
  const decision = contentDecision(
    { subject: 'a person', appearance: 'a schoolgirl uniform', apparentAge: 'adult' },
    { content: 'explicit', adultConfirmed: true },
  );
  assert.equal(decision.allowed, false);
});

test('the block cannot be defeated by editing the age field alone', () => {
  // Clearing the model's answer must not re-enable adult content when the
  // subject itself is described as a minor.
  const decision = contentDecision(
    { subject: 'a child', apparentAge: '' },
    { content: 'explicit', adultConfirmed: true },
  );
  assert.equal(decision.allowed, false);
});

test('blocked adult content falls back to safe, not to nothing', () => {
  const result = compile(
    { ...ADULT, subject: 'a child' },
    { era: '1990s', content: 'explicit', adultConfirmed: true },
  );
  assert.equal(result.content, 'sfw');
  assert.ok(result.prompt.length > 20, 'should still produce a usable safe prompt');
  for (const term of SFW_NEGATIVE) {
    assert.ok(result.negativeList.includes(term), `fallback should negate "${term}"`);
  }
});

test('an unknown content level falls back to safe', () => {
  const result = compile(ADULT, { era: '1990s', content: 'nonsense', adultConfirmed: true });
  assert.equal(result.content, 'sfw');
});

test('ordinary adult subjects are not blocked', () => {
  for (const subject of ['a woman', 'a man', 'a couple', 'a girl', 'two women']) {
    const decision = contentDecision(
      { subject, apparentAge: 'adult' },
      { content: 'explicit', adultConfirmed: true },
    );
    assert.equal(decision.allowed, true, `"${subject}" should be allowed`);
  }
});

/* ------------------------------------------------------------------ *
 * Internal fields
 * ------------------------------------------------------------------ */

test('reasoning-only fields never appear in the prompt', () => {
  const result = compile(
    { ...ADULT, apparentAge: 'adult', placement: 'indoors' },
    { era: '1990s', content: 'suggestive', adultConfirmed: true },
  );
  for (const field of INTERNAL_FIELDS) {
    assert.equal(result.groups[field], undefined, `${field} should not be a tag group`);
  }
  assert.equal(/\badult\b|\bindoors\b/i.test(result.prompt), false, result.prompt);
});

test('every observation field has a matching pass', () => {
  assert.deepEqual(PASSES.map((p) => p.field).sort(), [...OBSERVATION_FIELDS].sort());
});

test('free-text extra terms reach the prompt', () => {
  const result = compile(ADULT, {
    era: '1990s',
    extra: 'silk robe, soft window light',
    content: 'suggestive',
    adultConfirmed: true,
  });
  assert.ok(result.prompt.includes('silk robe'), result.prompt);
  assert.ok(result.prompt.includes('soft window light'), result.prompt);
});

test('content levels are declared coherently', () => {
  for (const [name, level] of Object.entries(CONTENT_LEVELS)) {
    assert.ok(level.label, `${name} missing label`);
    assert.equal(typeof level.requiresAdult, 'boolean');
    assert.ok(Array.isArray(level.negative));
    // Only the safe level may negate nudity.
    if (name !== 'sfw') {
      assert.equal(level.negative.some((t) => /nude|nudity|nsfw/i.test(t)), false, name);
    }
  }
});
