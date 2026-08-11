import test from 'node:test';
import assert from 'node:assert/strict';

import {
  cleanAnswer,
  isNullAnswer,
  normalizeTag,
  dedupeTags,
  fieldToTags,
  snapAspect,
  eraContribution,
  compile,
  settingsBlock,
  withArticle,
  inferScene,
  isFlashScene,
  settingPhrase,
  lightingNoun,
} from '../src/compiler.js';

import { ERAS, ERA_IDS, REALISM_NEGATIVE, INTENSITY_LEVELS } from '../src/eras.js';
import { QUALITY_TAGS } from '../src/vocab.js';

/* ------------------------------------------------------------------ *
 * Text cleanup
 * ------------------------------------------------------------------ */

test('cleanAnswer strips model scaffolding', () => {
  assert.equal(cleanAnswer('Answer: a man in a red coat'), 'a man in a red coat');
  assert.equal(cleanAnswer('This image shows a man standing'), 'a man standing');
  assert.equal(cleanAnswer('In this image, there is a dog'), 'dog');
  assert.equal(cleanAnswer('It appears to be a kitchen'), 'a kitchen');
  assert.equal(cleanAnswer('  spaced   out   text  '), 'spaced out text');
});

test('cleanAnswer keeps only the first sentence', () => {
  assert.equal(
    cleanAnswer('A woman in a blue dress. She is smiling at the camera.'),
    'A woman in a blue dress',
  );
});

test('cleanAnswer strips hedging', () => {
  assert.equal(cleanAnswer('probably a living room'), 'a living room');
});

test('isNullAnswer catches empty and evasive answers', () => {
  for (const answer of ['', '   ', 'unknown', 'N/A', 'not sure', 'nothing', 'photo']) {
    assert.equal(isNullAnswer(answer), true, `expected null answer: ${answer}`);
  }
  assert.equal(isNullAnswer('a red bicycle'), false);
});

test('normalizeTag applies the controlled vocabulary', () => {
  assert.equal(normalizeTag('Male'), 'man');
  assert.equal(normalizeTag('a close up'), 'close-up shot');
  assert.equal(normalizeTag('The Kitchen'), 'domestic kitchen');
  assert.equal(normalizeTag('backlit'), 'backlighting');
});

test('dedupeTags collapses article and filler differences', () => {
  assert.deepEqual(dedupeTags(['a red dress', 'red dress', 'blue hat']), [
    'a red dress',
    'blue hat',
  ]);
});

test('fieldToTags runs the whole pipeline', () => {
  assert.deepEqual(
    fieldToTags('This image shows a man and a woman, the background'),
    ['man', 'woman'],
  );
  assert.deepEqual(fieldToTags('unknown'), []);
  assert.deepEqual(fieldToTags(undefined), []);
});

test('fieldToTags drops generic tokens', () => {
  assert.deepEqual(fieldToTags('a bicycle, background, objects'), ['bicycle']);
});

/* ------------------------------------------------------------------ *
 * Aspect ratio
 * ------------------------------------------------------------------ */

test('snapAspect snaps to the nearest conventional ratio', () => {
  assert.equal(snapAspect(4032, 3024), '4:3');
  assert.equal(snapAspect(1000, 1000), '1:1');
  assert.equal(snapAspect(1920, 1080), '16:9');
  assert.equal(snapAspect(1080, 1920), '9:16');
  assert.equal(snapAspect(0, 100), null);
});

/* ------------------------------------------------------------------ *
 * Era invariants — the load-bearing guarantees
 * ------------------------------------------------------------------ */

const PERIOD_ERAS = ERA_IDS.filter((id) => id !== 'none');

test('every period era suppresses quality boilerplate', () => {
  for (const id of PERIOD_ERAS) {
    assert.equal(ERAS[id].suppressQualityTags, true, `${id} must suppress quality tags`);
  }
});

test('period era prompts contain no quality boilerplate', () => {
  for (const id of PERIOD_ERAS) {
    for (const format of Object.keys(ERAS[id].formats)) {
      const result = compile(
        { subject: 'a man', setting: 'a kitchen' },
        { era: id, format, intensity: 'heavy' },
      );
      for (const tag of QUALITY_TAGS) {
        assert.equal(
          result.prompt.toLowerCase().includes(tag.toLowerCase()),
          false,
          `${id}/${format} prompt leaked quality tag "${tag}"`,
        );
      }
    }
  }
});

test('period era negatives include the realism block', () => {
  for (const id of PERIOD_ERAS) {
    const result = compile({ subject: 'a man' }, { era: id });
    // Spot-check the most important rejections rather than all of them, since
    // conflict-subtraction may legitimately drop a few per era.
    for (const term of ['digital art', 'cgi', 'airbrushed', 'modern smartphone photo']) {
      assert.ok(
        result.negativeList.includes(term),
        `${id} negative prompt missing "${term}"`,
      );
    }
  }
});

/**
 * The invariant that motivated `negativeExclude`: an era must never negate an
 * artifact it positively asks for. Checked semantically, since "visible JPEG
 * compression blocks" and "jpeg artifacts" do not overlap as substrings.
 */
const CONFLICTS = [
  { positive: /jpeg|compression/i, forbidden: ['jpeg artifacts'] },
  { positive: /border|\bframe\b/i, forbidden: ['border', 'frame'] },
  { positive: /timestamp|date stamp|timecode/i, forbidden: ['text', 'caption'] },
  { positive: /low[- ]resolution|low video resolution|low megapixel|640x480/i, forbidden: ['lowres'] },
  // Deliberately narrow: period lens softness ("soft lens character") is not the
  // same defect as a blurry photo, so a 35mm print should still negate "blurry".
  { positive: /unsharp|out of focus|smear|softness|soft focus/i, forbidden: ['blurry'] },
];

test('no era negates an artifact it positively requests', () => {
  for (const id of ERA_IDS) {
    for (const format of Object.keys(ERAS[id].formats)) {
      for (const intensity of Object.keys(INTENSITY_LEVELS)) {
        const result = compile(
          { subject: 'a man' },
          { era: id, format, intensity, periodSubject: true },
        );
        const positive = result.prompt.toLowerCase();
        for (const { positive: probe, forbidden } of CONFLICTS) {
          if (!probe.test(positive)) continue;
          for (const term of forbidden) {
            assert.equal(
              result.negativeList.map((t) => t.toLowerCase()).includes(term),
              false,
              `${id}/${format}/${intensity}: prompt matches ${probe} but negative still forbids "${term}"`,
            );
          }
        }
      }
    }
  }
});

test('positive tags never appear verbatim in the negative prompt', () => {
  for (const id of ERA_IDS) {
    for (const format of Object.keys(ERAS[id].formats)) {
      const result = compile({ subject: 'a man' }, { era: id, format, intensity: 'heavy' });
      const negatives = result.negativeList.map((t) => t.toLowerCase());
      const positives = Object.values(result.groups)
        .flat()
        .map((t) => t.toLowerCase());
      for (const tag of positives) {
        assert.equal(
          negatives.includes(tag),
          false,
          `${id}/${format}: "${tag}" is both requested and forbidden`,
        );
      }
    }
  }
});

test('the no-era escape hatch keeps quality tags and skips the realism block', () => {
  const result = compile({ subject: 'a man' }, { era: 'none' });
  assert.ok(result.prompt.includes('masterpiece'));
  assert.equal(result.suppressedQualityTags, false);
  for (const term of REALISM_NEGATIVE) {
    assert.equal(
      result.negativeList.includes(term),
      false,
      `no-era should not apply realism negative "${term}"`,
    );
  }
});

/* ------------------------------------------------------------------ *
 * Era contribution mechanics
 * ------------------------------------------------------------------ */

test('artifact intensity scales the number of emitted tags', () => {
  const counts = ['subtle', 'medium', 'heavy'].map(
    (intensity) => eraContribution('1990s', '35mm print', intensity).artifacts.length,
  );
  assert.equal(counts[0], 1);
  assert.equal(counts[1], 3);
  assert.ok(counts[2] > counts[1], 'heavy should emit more artifacts than medium');
});

test('film stock is phrased "shot on", digital sensors are not', () => {
  assert.ok(eraContribution('1990s', '35mm print').medium.includes('shot on Kodak Gold 200'));
  const digital = eraContribution('early 2000s', 'digital compact').medium;
  assert.ok(digital.includes('early digital sensor'));
  assert.equal(digital.some((m) => m.startsWith('shot on early digital')), false);
});

test('variant rotates stock and camera choices deterministically', () => {
  const a = eraContribution('1990s', '35mm print', 'medium', 0);
  const b = eraContribution('1990s', '35mm print', 'medium', 1);
  assert.notDeepEqual(a.medium, b.medium);
  assert.deepEqual(a.medium, eraContribution('1990s', '35mm print', 'medium', 0).medium);
});

test('unknown era and format ids fall back instead of throwing', () => {
  const result = compile({ subject: 'a man' }, { era: 'nope', format: 'nope' });
  assert.ok(result.prompt.length > 0);
  assert.equal(result.format, ERAS[result.era].defaultFormat);
});

test('formats can override the era aspect ratio', () => {
  assert.equal(compile({}, { era: '1990s', format: 'vhs still' }).aspect, '4:3');
  assert.equal(compile({}, { era: '1980s', format: 'instant' }).aspect, '1:1');
});

test('no-era aspect follows the source image', () => {
  const result = compile({}, { era: 'none', source: { width: 1920, height: 1080 } });
  assert.equal(result.aspect, '16:9');
});

/* ------------------------------------------------------------------ *
 * Rendering
 * ------------------------------------------------------------------ */

const OBSERVATION = {
  subject: 'a man',
  clothing: 'a plaid shirt',
  setting: 'a domestic kitchen',
  lighting: 'harsh flash',
  shotType: 'waist-up shot',
};

test('tag style emphasises the subject anchor', () => {
  const result = compile(OBSERVATION, { era: '1990s', style: 'tags', emphasis: true });
  assert.ok(result.prompt.includes('(man:1.2)'), result.prompt);
});

test('emphasis can be disabled', () => {
  const result = compile(OBSERVATION, { era: '1990s', style: 'tags', emphasis: false });
  assert.equal(result.prompt.includes('(man:1.2)'), false);
  assert.ok(result.prompt.includes('man'));
});

test('tag style orders subject before medium', () => {
  const result = compile(OBSERVATION, { era: '1990s', style: 'tags' });
  const subjectAt = result.prompt.indexOf('man');
  const mediumAt = result.prompt.indexOf('shot on');
  assert.ok(subjectAt >= 0 && mediumAt > subjectAt, result.prompt);
});

test('natural style reads as prose and avoids doubled prepositions', () => {
  const result = compile(OBSERVATION, { era: '1990s', style: 'natural' });
  assert.ok(result.prompt.includes('of a man') || result.prompt.includes('of man'), result.prompt);
  assert.equal(/\bin\s+in\b/.test(result.prompt), false, result.prompt);
  assert.equal(/\bwearing\s+wearing\b/.test(result.prompt), false, result.prompt);
  assert.equal(result.prompt.includes('(man:1.2)'), false, 'natural style should not use weights');
});

test('natural style restores the article on the subject, tag style does not', () => {
  const natural = compile(OBSERVATION, { era: '1990s', style: 'natural' });
  assert.ok(natural.prompt.includes('of a man'), natural.prompt);

  const tags = compile(OBSERVATION, { era: '1990s', style: 'tags', emphasis: false });
  assert.equal(tags.prompt.includes('a man'), false, tags.prompt);
});

test('withArticle picks the right article and skips where inappropriate', () => {
  assert.equal(withArticle('man'), 'a man');
  assert.equal(withArticle('elderly woman'), 'an elderly woman');
  assert.equal(withArticle('a man'), 'a man');
  assert.equal(withArticle('group of people'), 'group of people');
  assert.equal(withArticle('two children'), 'two children');
  assert.equal(withArticle('people'), 'people');
  assert.equal(withArticle(''), '');
});

test('natural style does not repeat the framing term', () => {
  const result = compile(OBSERVATION, { era: '1990s', style: 'natural' });
  const matches = result.prompt.match(/amateur snapshot/g) || [];
  assert.equal(matches.length, 1, result.prompt);
});

test('subject appears once even if several fields mention it', () => {
  const result = compile(
    { subject: 'a man', appearance: 'a man with a beard', action: 'standing' },
    { era: '1990s', style: 'tags', emphasis: false },
  );
  const matches = result.prompt.match(/\bman\b/g) || [];
  assert.equal(matches.length, 1, result.prompt);
});

test('periodSubject adds era styling only when enabled', () => {
  const bare = { subject: 'a man', setting: 'a kitchen', shotType: 'full-body shot' };
  const off = compile(bare, { era: '1990s', periodSubject: false });
  const on = compile(bare, { era: '1990s', periodSubject: true });
  assert.equal(off.prompt.includes('1990s clothing'), false);
  assert.ok(on.prompt.includes('1990s clothing'), on.prompt);
  assert.ok(on.prompt.includes('baggy jeans'), on.prompt);
});

/* ------------------------------------------------------------------ *
 * Scene awareness
 *
 * Each of these is a bug found on a real photo: a beach selfie that came back
 * with camera flash, a shadow on a nonexistent wall, indoor furniture, and
 * sneakers that could not be in frame.
 * ------------------------------------------------------------------ */

const BEACH_SELFIE = {
  subject: 'a woman',
  appearance: 'brown hair, nose stud',
  clothing: 'a coral top',
  action: 'smiling at the camera',
  setting: 'a beach by the ocean',
  placement: 'outdoors',
  colors: 'teal, warm pink',
  lighting: 'sunny',
  shotType: 'close-up shot',
};

test('inferScene reads placement, daylight and framing', () => {
  const scene = inferScene(BEACH_SELFIE);
  assert.equal(scene.outdoor, true);
  assert.equal(scene.daylight, true);
  assert.equal(scene.framing, 'close');

  const indoor = inferScene({ setting: 'a domestic kitchen', placement: 'indoors' });
  assert.equal(indoor.outdoor, false);

  const unknown = inferScene({});
  assert.deepEqual(unknown, { outdoor: null, daylight: null, framing: null });
});

test('flash is assumed by default but ruled out by daylight or outdoors', () => {
  assert.equal(isFlashScene({}), true, 'flash is the era archetype when unknown');
  assert.equal(isFlashScene({ daylight: true }), false);
  assert.equal(isFlashScene({ outdoor: true }), false);
  // A night flash snapshot is flash-lit by definition.
  assert.equal(isFlashScene({ outdoor: true }, { forceFlash: true }), true);
});

test('an outdoor daylit photo gets no flash and no wall', () => {
  const result = compile(BEACH_SELFIE, { era: '1990s', intensity: 'heavy' });
  assert.equal(result.flash, false);
  assert.equal(/on-camera flash|built-in flash/i.test(result.prompt), false, result.prompt);
  assert.equal(/shadow on the wall/i.test(result.prompt), false, result.prompt);
  assert.equal(/red-eye/i.test(result.prompt), false, result.prompt);
});

test('an indoor photo still gets the flash-snapshot look', () => {
  const result = compile(
    { subject: 'a man', setting: 'a living room interior', placement: 'indoors' },
    { era: '1990s', intensity: 'heavy' },
  );
  assert.equal(result.flash, true);
  assert.ok(/on-camera flash/i.test(result.prompt), result.prompt);
});

test('indoor decor is never added to an outdoor photo', () => {
  const result = compile(
    { ...BEACH_SELFIE, shotType: 'full-body shot' },
    { era: '1990s', periodSubject: true, intensity: 'heavy' },
  );
  for (const indoor of ['popcorn ceiling', 'beige carpet', 'CRT television']) {
    assert.equal(
      result.prompt.toLowerCase().includes(indoor.toLowerCase()),
      false,
      `leaked indoor decor "${indoor}": ${result.prompt}`,
    );
  }
});

test('garments outside the crop are not claimed', () => {
  const closeUp = compile(
    { ...BEACH_SELFIE, clothing: '' },
    { era: '1990s', periodSubject: true, intensity: 'heavy' },
  );
  for (const garment of ['baggy jeans', 'chunky sneakers', 'windbreaker']) {
    assert.equal(
      closeUp.prompt.includes(garment),
      false,
      `close-up should not claim "${garment}": ${closeUp.prompt}`,
    );
  }
  // Unknown framing is treated conservatively too.
  const unknown = compile(
    { subject: 'a man', setting: 'a kitchen' },
    { era: '1990s', periodSubject: true, intensity: 'heavy' },
  );
  assert.equal(unknown.prompt.includes('chunky sneakers'), false, unknown.prompt);
});

test('observed clothing is not contradicted by era wardrobe', () => {
  const result = compile(
    { ...BEACH_SELFIE, shotType: 'full-body shot' },
    { era: '1990s', periodSubject: true, intensity: 'heavy' },
  );
  assert.ok(result.prompt.includes('coral top'), result.prompt);
  assert.equal(result.prompt.includes('windbreaker'), false, result.prompt);
  assert.equal(result.prompt.includes('baggy jeans'), false, result.prompt);
  // The generic era marker is still safe to add.
  assert.ok(result.prompt.includes('1990s clothing'), result.prompt);
});

test('film texture is never applied to a video format', () => {
  const vhs = compile(BEACH_SELFIE, {
    era: '1990s',
    format: 'vhs still',
    intensity: 'heavy',
  });
  assert.equal(/film grain|film saturation/i.test(vhs.prompt), false, vhs.prompt);
  assert.ok(/video/i.test(vhs.prompt), vhs.prompt);

  const film = compile(BEACH_SELFIE, { era: '1990s', format: '35mm print', intensity: 'heavy' });
  assert.ok(/film grain/i.test(film.prompt), film.prompt);
  assert.equal(/scanlines|camcorder/i.test(film.prompt), false, film.prompt);
});

test('era look never asserts what the subject is doing', () => {
  // "squinting into the sun" was emitted for a subject smiling with open eyes.
  for (const id of ERA_IDS) {
    const era = ERAS[id];
    for (const tag of [...(era.daylightLook || []), ...(era.look || [])]) {
      assert.equal(
        /squint|smil|pos(e|ing)|look(ing)? at/i.test(tag),
        false,
        `${id} look tag asserts subject behaviour: "${tag}"`,
      );
    }
  }
});

test('invalid shot-type answers are discarded', () => {
  const bogus = compile(
    { subject: 'a man', shotType: 'a photograph of someone standing near a wall' },
    { era: '1990s' },
  );
  assert.equal(bogus.groups.shotType.length, 0, JSON.stringify(bogus.groups.shotType));

  const valid = compile({ subject: 'a man', shotType: 'close-up' }, { era: '1990s' });
  assert.deepEqual(valid.groups.shotType, ['close-up shot']);
});

test('placement informs the scene but is never emitted as a tag', () => {
  const result = compile(BEACH_SELFIE, { era: '1990s' });
  assert.equal(result.prompt.includes('outdoors'), false, result.prompt);
  assert.equal(result.groups.placement, undefined);
});

/* ------------------------------------------------------------------ *
 * Natural-language grammar
 * ------------------------------------------------------------------ */

test('settings get a sensible preposition instead of "in ocean"', () => {
  assert.equal(settingPhrase(['ocean']), 'at the ocean');
  assert.equal(settingPhrase(['beach by the ocean']), 'at the beach by the ocean');
  assert.equal(settingPhrase(['domestic kitchen']), 'in a domestic kitchen');
  assert.equal(settingPhrase(['city street']), 'on a city street');
  // A preposition the model supplied is respected.
  assert.equal(settingPhrase(['in a park']), 'in a park');
});

test('bare lighting adjectives become noun phrases', () => {
  assert.equal(lightingNoun('sunny'), 'sunlight');
  assert.equal(lightingNoun('cloudy'), 'overcast light');
  assert.equal(lightingNoun('harsh flash'), 'harsh flash');
});

test('natural style reads correctly for the beach selfie', () => {
  const result = compile(BEACH_SELFIE, { era: '1990s', style: 'natural', format: '35mm print' });
  assert.ok(result.prompt.includes('at the beach'), result.prompt);
  assert.equal(result.prompt.includes('in ocean'), false, result.prompt);
  assert.ok(result.prompt.includes('lit by sunlight'), result.prompt);
  assert.equal(result.prompt.includes('lit by sunny'), false, result.prompt);
  assert.ok(result.prompt.includes('in shades of teal and warm pink'), result.prompt);
});

test('empty observations still produce a usable era-only prompt', () => {
  const result = compile({}, { era: '1980s' });
  assert.ok(result.prompt.length > 20, result.prompt);
  assert.ok(result.negative.length > 20);
});

test('compile reports recommended generation settings', () => {
  const result = compile(OBSERVATION, { era: 'early 2000s' });
  assert.equal(result.cfg.value, 6);
  assert.deepEqual(result.cfg.range, [4.5, 7.5]);
  assert.equal(result.steps, 26);
  assert.equal(result.aspect, '4:3');
});

test('settingsBlock renders a pasteable summary', () => {
  const block = settingsBlock(compile(OBSERVATION, { era: '1990s' }));
  // Field names match Perchance's actual controls — see test/perchance.test.mjs.
  for (const label of ['Prompt:', 'Negative prompt:', 'Guidance scale:', 'Resolution:', 'Seed:']) {
    assert.ok(block.includes(label), `missing ${label}`);
  }
});
