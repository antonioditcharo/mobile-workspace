import test from 'node:test';
import assert from 'node:assert/strict';

import {
  RESOLUTIONS,
  GUIDANCE_RANGE,
  STYLES,
  SAFE_STYLES,
  STYLE_NOTES,
  resolutionForAspect,
  clampGuidance,
  perchanceSettings,
} from '../src/perchance.js';
import { compile, settingsBlock } from '../src/compiler.js';
import { ERA_IDS, ERAS } from '../src/eras.js';

/* ------------------------------------------------------------------ *
 * Resolution mapping
 *
 * Perchance takes a discrete resolution string, not a free aspect ratio, so
 * every ratio the compiler can produce has to land on one of three shapes.
 * ------------------------------------------------------------------ */

test('every offered resolution is a literal WxH string', () => {
  for (const r of RESOLUTIONS) {
    assert.match(r.value, /^\d{3,4}x\d{3,4}$/, r.value);
    assert.ok(r.ratio > 0);
    assert.ok(r.label.length > 0);
  }
});

test('aspect ratios map onto the nearest available shape', () => {
  assert.equal(resolutionForAspect('1:1').value, '768x768');
  assert.equal(resolutionForAspect('3:2').value, '768x512');
  assert.equal(resolutionForAspect('16:9').value, '768x512');
  assert.equal(resolutionForAspect('2:3').value, '512x768');
  assert.equal(resolutionForAspect('9:16').value, '512x768');
  assert.equal(resolutionForAspect('4:3').value, '768x512');
  assert.equal(resolutionForAspect('3:4').value, '512x768');
});

test('an unusable aspect label falls back to square rather than throwing', () => {
  assert.equal(resolutionForAspect('source').value, '768x768');
  assert.equal(resolutionForAspect(undefined).value, '768x768');
  assert.equal(resolutionForAspect('nonsense').value, '768x768');
});

test('every era and format produces a resolution Perchance accepts', () => {
  const valid = new Set(RESOLUTIONS.map((r) => r.value));
  for (const id of ERA_IDS) {
    for (const format of Object.keys(ERAS[id].formats)) {
      const result = compile({ subject: 'a man' }, { era: id, format });
      const settings = perchanceSettings(result);
      assert.ok(
        valid.has(settings.resolution.value),
        `${id}/${format} produced ${settings.resolution.value}`,
      );
    }
  }
});

/* ------------------------------------------------------------------ *
 * Guidance scale
 * ------------------------------------------------------------------ */

test('guidance is clamped to what the generator accepts', () => {
  const [lo, hi] = GUIDANCE_RANGE;
  assert.equal(clampGuidance(5.5), 5.5);
  assert.equal(clampGuidance(0), lo);
  assert.equal(clampGuidance(999), hi);
  assert.equal(clampGuidance('not a number'), 7);
});

test('every era recommends a guidance value inside the accepted range', () => {
  const [lo, hi] = GUIDANCE_RANGE;
  for (const id of ERA_IDS) {
    const result = compile({ subject: 'a man' }, { era: id });
    const { guidanceScale } = perchanceSettings(result);
    assert.ok(guidanceScale >= lo && guidanceScale <= hi, `${id}: ${guidanceScale}`);
    // Clamping must not have been needed — the presets should already be valid.
    assert.equal(guidanceScale, result.cfg.value, `${id} guidance was clamped`);
  }
});

/* ------------------------------------------------------------------ *
 * The art-style trap
 *
 * Perchance's style dropdown appends text to the prompt and negative prompt.
 * Most styles append the exact quality boilerplate the era presets suppress, so
 * recommending the wrong one would silently undo the whole point of the app.
 * ------------------------------------------------------------------ */

test('styles known to inject quality boilerplate are marked unsafe', () => {
  for (const [name, style] of Object.entries(STYLES)) {
    const injects = /\b(8k|hdr|masterpiece|best quality|sharp focus|artstation|high resolution)\b/i.test(
      style.appends || '',
    );
    if (injects) {
      assert.equal(style.safe, false, `"${name}" injects boilerplate but is marked safe`);
    }
  }
});

test('safe styles inject nothing that fights period realism', () => {
  for (const name of SAFE_STYLES) {
    assert.equal(
      /\b(8k|hdr|masterpiece|best quality|sharp focus|artstation)\b/i.test(
        STYLES[name].appends || '',
      ),
      false,
      `"${name}" is marked safe but injects boilerplate`,
    );
  }
  assert.ok(SAFE_STYLES.includes('none'));
});

test('period eras always recommend the neutral style', () => {
  for (const id of ERA_IDS.filter((e) => e !== 'none')) {
    const settings = perchanceSettings(compile({ subject: 'a man' }, { era: id }));
    assert.ok(SAFE_STYLES.includes(settings.style), `${id} recommended ${settings.style}`);
    assert.equal(settings.style, 'none');
  }
});

test('the style warning names the risk concretely', () => {
  assert.match(STYLE_NOTES, /none/);
  assert.match(STYLE_NOTES, /8k|HDR|masterpiece/);
});

/* ------------------------------------------------------------------ *
 * Copy-out block
 * ------------------------------------------------------------------ */

test('the settings block is a Perchance field checklist', () => {
  const block = settingsBlock(compile({ subject: 'a man' }, { era: '1990s', useNegative: true }));
  for (const label of [
    'Prompt:',
    'Negative prompt:',
    'Art style:',
    'Guidance scale:',
    'Resolution:',
    'Seed:',
  ]) {
    assert.ok(block.includes(label), `missing "${label}"`);
  }
  // Resolution must be the literal string, not an aspect ratio.
  assert.match(block, /Resolution: \d{3,4}x\d{3,4}/);
});

test('steps are marked advisory, since Perchance has no such control', () => {
  const block = settingsBlock(compile({ subject: 'a man' }, { era: '1990s' }));
  assert.match(block, /Steps:.*advisory/i);
});

test('the block warns about the art style inline', () => {
  const block = settingsBlock(compile({ subject: 'a man' }, { era: '1990s' }));
  assert.match(block, /Art style: none/);
  assert.match(block, /breaks the era look|important/i);
});
