const test = require('node:test');
const assert = require('node:assert');

const realism = require('../server/realism');

test('compilePrompt requires a subject', () => {
  assert.throws(() => realism.compilePrompt({}), /subject description is required/);
  assert.throws(() => realism.compilePrompt({ subject: '   ' }), /subject description is required/);
});

test('compilePrompt keeps the subject at the head of the prompt', () => {
  const out = realism.compilePrompt({ subject: 'a dog crossing a road', preset: 'documentary' });
  assert.ok(out.prompt.startsWith('a dog crossing a road,'));
});

test('intensity controls how many modifier layers are applied', () => {
  const subject = 'a cyclist on a bridge';
  const lengths = [0, 1, 2, 3, 4].map(
    (intensity) => realism.compilePrompt({ subject, intensity, preset: 'cinematic_film' }).prompt.length,
  );
  for (let i = 1; i < lengths.length; i += 1) {
    assert.ok(lengths[i] > lengths[i - 1], `intensity ${i} should add to intensity ${i - 1}`);
  }
});

test('intensity 0 emits the subject alone', () => {
  const out = realism.compilePrompt({ subject: 'a train arriving', intensity: 0 });
  assert.strictEqual(out.prompt, 'a train arriving');
  assert.ok(out.negativePrompt.length > 0, 'negative prompt still applies at intensity 0');
});

test('human subjects get skin and hair detail cues', () => {
  const withPerson = realism.compilePrompt({ subject: 'a woman waiting for a bus', intensity: 3 });
  const withoutPerson = realism.compilePrompt({ subject: 'an empty parking lot at night', intensity: 3 });

  assert.match(withPerson.prompt, /skin pores/);
  assert.doesNotMatch(withoutPerson.prompt, /skin pores/);
  assert.ok(withPerson.notes.some((n) => /Human subject detected/.test(n)));
});

test('looksHuman detects pronouns and nouns but not unrelated text', () => {
  assert.ok(realism.looksHuman('she walks to the door'));
  assert.ok(realism.looksHuman('a crowd gathers'));
  assert.ok(!realism.looksHuman('waves breaking on rocks'));
});

test('anti-patterns are detected on word boundaries', () => {
  const hits = realism.findAntiPatterns('a hyperrealistic 8k cinematic shot');
  const terms = hits.map((h) => h.term);
  assert.ok(terms.includes('8k'));
  assert.ok(terms.includes('hyperrealistic'));
  assert.ok(terms.includes('cinematic'));

  // "perfect" should match as a word, but not inside "imperfection".
  assert.strictEqual(realism.findAntiPatterns('imperfections everywhere').length, 0);
  assert.strictEqual(realism.findAntiPatterns('a perfect circle').length, 1);
});

test('autoClean strips render-biasing terms and reports them', () => {
  const out = realism.compilePrompt({
    subject: 'a masterpiece 8k photorealistic shot of a man drinking coffee',
    autoClean: true,
  });
  assert.doesNotMatch(out.prompt.toLowerCase(), /\bmasterpiece\b/);
  assert.doesNotMatch(out.prompt.toLowerCase(), /\b8k\b/);
  assert.match(out.prompt, /man drinking coffee/);
  assert.ok(out.notes.some((n) => /Removed render-biasing terms/.test(n)));
});

test('autoClean can be disabled', () => {
  const out = realism.compilePrompt({ subject: 'an 8k shot of a car', autoClean: false });
  assert.match(out.prompt, /8k/);
});

test('cleanSubject leaves tidy text and no punctuation debris', () => {
  const { text } = realism.cleanSubject('a perfect, 8k, stunning view of a lake');
  assert.doesNotMatch(text, /\s{2,}/);
  assert.doesNotMatch(text, /^[,\s]|[,\s]$/);
  assert.match(text, /view of a lake/);
});

test('cleanSubject repairs grammar around removals', () => {
  // Removing the noun that "of" attached to must not strand its article.
  assert.strictEqual(
    realism.cleanSubject('a hyperrealistic 8k masterpiece of a woman stepping off a bus').text,
    'a woman stepping off a bus',
  );
  // The article has to agree with whatever word ends up following it.
  assert.strictEqual(realism.cleanSubject('an epic view of a mountain').text, 'a view of a mountain');
  assert.strictEqual(realism.cleanSubject('a stunning epic image').text, 'an image');
  // A trailing article left behind by a removal is dropped entirely.
  assert.strictEqual(realism.cleanSubject('a masterpiece').text, '');
  // Untouched text keeps the user's exact wording.
  assert.strictEqual(realism.cleanSubject('a unicorn in a field').text, 'a unicorn in a field');
});

test('a subject that cleans to nothing falls back to the original text', () => {
  const out = realism.compilePrompt({ subject: 'a masterpiece', autoClean: true });
  assert.match(out.prompt, /^a masterpiece,/);
});

test('compiling raises the realism score', () => {
  const out = realism.compilePrompt({ subject: 'a man walking a dog', intensity: 3 });
  assert.ok(out.score.after > out.score.before, 'compiled prompt should score above the raw subject');
});

test('anti-patterns lower the raw score and surface as warnings', () => {
  const clean = realism.scorePrompt('a man walking a dog down a residential street');
  const dirty = realism.scorePrompt('a perfect epic masterpiece 8k shot of a man walking a dog down a street');
  assert.ok(dirty.score < clean.score);

  const compiled = realism.compilePrompt({ subject: 'an epic 8k masterpiece of a city' });
  assert.ok(compiled.warnings.length >= 3);
});

test('a well-formed manual prompt scores highly', () => {
  const { score } = realism.scorePrompt(
    'a woman reading by a window, shot on a Sony FX3, 85mm lens at f/2.0, available light, '
    + 'handheld, visible skin pores and texture, fine grain, natural motion blur at 24fps',
  );
  assert.ok(score >= 80, `expected a high score, got ${score}`);
});

test('negative prompt groups can be selected', () => {
  const all = realism.compilePrompt({ subject: 'a street' });
  const some = realism.compilePrompt({ subject: 'a street', negativeGroups: ['render'] });
  assert.ok(some.negativePrompt.length < all.negativePrompt.length);
  assert.match(some.negativePrompt, /cgi/);
  assert.doesNotMatch(some.negativePrompt, /waxy skin/);
});

test('unknown negative groups are ignored rather than throwing', () => {
  const out = realism.compilePrompt({ subject: 'a street', negativeGroups: ['nope'] });
  assert.strictEqual(out.negativePrompt, '');
});

test('extra negative terms are appended', () => {
  const out = realism.compilePrompt({ subject: 'a street', extraNegative: 'rain, umbrellas' });
  assert.match(out.negativePrompt, /rain, umbrellas$/);
});

test('unknown presets fall back to documentary', () => {
  const out = realism.compilePrompt({ subject: 'a street', preset: 'does-not-exist' });
  assert.strictEqual(out.preset.key, 'documentary');
});

test('every preset compiles at every intensity', () => {
  for (const preset of Object.keys(realism.PRESETS)) {
    for (let intensity = 0; intensity <= 4; intensity += 1) {
      const out = realism.compilePrompt({ subject: 'a person walking', preset, intensity });
      assert.ok(out.prompt.length > 0, `${preset}@${intensity} produced an empty prompt`);
      assert.strictEqual(out.preset.key, preset);
    }
  }
});

test('intensity is clamped to the valid range', () => {
  assert.strictEqual(realism.compilePrompt({ subject: 'x y z', intensity: 99 }).intensity, 4);
  assert.strictEqual(realism.compilePrompt({ subject: 'x y z', intensity: -5 }).intensity, 0);
});

test('a subject made entirely of anti-patterns still produces a prompt', () => {
  const out = realism.compilePrompt({ subject: 'masterpiece 8k perfect' });
  assert.ok(out.prompt.length > 0);
});

test('listPresets and listNegativeGroups expose the catalog', () => {
  const presets = realism.listPresets();
  assert.ok(presets.length >= 9);
  assert.ok(presets.every((p) => p.key && p.label && p.camera));

  const groups = realism.listNegativeGroups();
  assert.ok(groups.every((g) => Array.isArray(g.terms) && g.terms.length));
});
