import test from 'node:test';
import assert from 'node:assert/strict';

import { readFileSync } from 'node:fs';

import {
  chooseModel,
  extractText,
  describeError,
  MODELS,
  PASSES,
  TRANSFORMERS_CDN,
  TRANSFORMERS_VERSION,
} from '../src/vision.js';
import { OBSERVATION_FIELDS } from '../src/compiler.js';

const visionSource = readFileSync(new URL('../src/vision.js', import.meta.url), 'utf8');

/** Source with comments removed, so prose about a mistake can't look like the mistake. */
const visionCode = visionSource
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '');

/* ------------------------------------------------------------------ *
 * Runtime wiring — regression guards
 *
 * Both of these encode real bugs that shipped. `image-text-to-text` is a
 * Python-transformers pipeline task that does not exist in transformers.js,
 * where the same string is only a model-architecture mapping; calling it failed
 * on device with "Unsupported pipeline". And a floating version range let the
 * dependency shift under the app without a commit.
 * ------------------------------------------------------------------ */

test('the transformers.js version is pinned exactly, not a floating range', () => {
  assert.match(TRANSFORMERS_VERSION, /^\d+\.\d+\.\d+$/, TRANSFORMERS_VERSION);
  assert.ok(
    TRANSFORMERS_CDN.includes(`@${TRANSFORMERS_VERSION}/`),
    `CDN url should embed the pinned version: ${TRANSFORMERS_CDN}`,
  );
  assert.match(TRANSFORMERS_CDN, /\.js$/, 'should point at an explicit file');
});

test('the CDN url uses the self-contained browser build', () => {
  // transformers.web.js imports bare specifiers a browser cannot resolve.
  assert.ok(!TRANSFORMERS_CDN.includes('transformers.web.js'), TRANSFORMERS_CDN);
  assert.ok(TRANSFORMERS_CDN.includes('transformers.min.js'), TRANSFORMERS_CDN);
});

test('no pipeline() task is used for vision-language inference', () => {
  const call = visionCode.match(/pipeline\(\s*['"][a-z-]+['"]/);
  assert.equal(call, null, `vision.js should not call pipeline(): ${call?.[0]}`);
});

test('vision-language inference goes through the Auto classes', () => {
  for (const symbol of ['AutoProcessor', 'AutoModelForVision2Seq', 'RawImage']) {
    assert.ok(visionCode.includes(symbol), `expected ${symbol} to be used`);
  }
  assert.ok(
    visionCode.includes('apply_chat_template'),
    'expected the chat template to be applied',
  );
});

test('images reach the model without a needless encode/decode round trip', () => {
  // prepareImage already builds a canvas; RawImage.read() takes it directly.
  // Going back out through a data URL would re-encode to JPEG and re-decode it.
  assert.ok(visionCode.includes('RawImage.read('), 'expected RawImage.read to be used');
  assert.ok(!visionCode.includes('RawImage.fromURL('), 'should not round-trip through a URL');
  assert.ok(visionCode.includes('canvas,'), 'prepareImage should return the canvas');
});

test('only the generated tail of the output is decoded', () => {
  // Decoding the full sequence would hand the compiler back its own question.
  assert.ok(visionCode.includes('input_ids.dims'), 'expected the prompt length to be measured');
  assert.ok(/\.slice\(\s*null/.test(visionCode), 'expected the prompt tokens to be sliced off');
});

/* ------------------------------------------------------------------ *
 * Model selection
 * ------------------------------------------------------------------ */

test('WebGPU with adequate memory picks the 500M model on the GPU', () => {
  const choice = chooseModel({ webgpu: true, memoryGb: 8, adapter: 'arm' });
  assert.equal(choice.key, 'smolvlm-500m-q4f16');
  assert.equal(choice.device, 'webgpu');
  assert.match(choice.why, /WebGPU/);
});

test('WebGPU with genuinely low memory drops to the smallest model', () => {
  const choice = chooseModel({ webgpu: true, memoryGb: 2 });
  assert.equal(choice.key, 'smolvlm-256m-q4');
  assert.equal(choice.device, 'webgpu');
  assert.match(choice.why, /RAM/);
});

test('a common 4 GB phone still gets the better model', () => {
  // 4 GB is the typical reading and runs the 500M model fine on the GPU; the
  // larger model reads body position noticeably better.
  const choice = chooseModel({ webgpu: true, memoryGb: 4 });
  assert.equal(choice.key, 'smolvlm-500m-q4f16');
});

test('the large model is never auto-selected', () => {
  for (const memoryGb of [2, 4, 8, null]) {
    for (const webgpu of [true, false]) {
      const choice = chooseModel({ webgpu, memoryGb });
      assert.notEqual(choice.key, 'smolvlm-2.2b-q4f16', `auto-picked at ${memoryGb}/${webgpu}`);
    }
  }
  // But it is available on request.
  assert.equal(
    chooseModel({ webgpu: true, memoryGb: 8 }, 'smolvlm-2.2b-q4f16').key,
    'smolvlm-2.2b-q4f16',
  );
});

test('unverified models are labelled as such', () => {
  for (const [key, model] of Object.entries(MODELS)) {
    if (model.unverified) {
      assert.match(model.note, /unverified/i, `${key} should say so in its note`);
    }
  }
});

test('no WebGPU falls back to CPU and warns about speed', () => {
  const choice = chooseModel({ webgpu: false, memoryGb: null });
  assert.equal(choice.key, 'smolvlm-256m-q8');
  assert.equal(choice.device, 'wasm');
  assert.match(choice.why, /slow/i);
});

test('unknown memory with WebGPU still takes the fast path', () => {
  const choice = chooseModel({ webgpu: true, memoryGb: null });
  assert.equal(choice.key, 'smolvlm-500m-q4f16');
});

test('a manual override wins and says so', () => {
  const choice = chooseModel({ webgpu: false }, 'smolvlm-500m-q4f16');
  assert.equal(choice.key, 'smolvlm-500m-q4f16');
  assert.match(choice.why, /manually/i);
});

test('an invalid override is ignored rather than crashing', () => {
  const choice = chooseModel({ webgpu: true, memoryGb: 8 }, 'no-such-model');
  assert.equal(choice.key, 'smolvlm-500m-q4f16');
});

test('every model advertises a label, download size and note', () => {
  for (const [key, model] of Object.entries(MODELS)) {
    assert.ok(model.id, `${key} missing id`);
    assert.ok(model.dtype, `${key} missing dtype`);
    assert.ok(model.label, `${key} missing label`);
    assert.match(model.approxDownload, /MB|GB/, `${key} download size unclear`);
    assert.ok(model.note, `${key} missing note`);
  }
});

/* ------------------------------------------------------------------ *
 * Output parsing — permissive by design
 * ------------------------------------------------------------------ */

test('extractText handles every plausible library return shape', () => {
  assert.equal(extractText('a man'), 'a man');
  assert.equal(extractText([{ generated_text: 'a man' }]), 'a man');
  assert.equal(
    extractText([
      {
        generated_text: [
          { role: 'user', content: 'question' },
          { role: 'assistant', content: 'a man' },
        ],
      },
    ]),
    'a man',
  );
  assert.equal(extractText({ content: [{ type: 'text', text: 'a man' }] }), 'a man');
  assert.equal(extractText({ text: 'a man' }), 'a man');
});

test('extractText returns empty string for junk instead of throwing', () => {
  assert.equal(extractText(null), '');
  assert.equal(extractText(undefined), '');
  assert.equal(extractText([]), '');
  assert.equal(extractText({}), '');
  assert.equal(extractText(42), '');
});

/* ------------------------------------------------------------------ *
 * Errors
 * ------------------------------------------------------------------ */

test('describeError turns runtime failures into actionable advice', () => {
  assert.match(describeError(new Error('Out of memory')), /smaller model/i);
  assert.match(describeError(new Error('Failed to fetch')), /connection/i);
  assert.match(describeError(new Error('WebGPU device lost')), /CPU/i);
  assert.match(describeError(new DOMException('x', 'AbortError')), /Cancelled/i);
  assert.match(describeError(new Error('weird internal thing')), /weird internal thing/);
});

/* ------------------------------------------------------------------ *
 * Pass configuration
 * ------------------------------------------------------------------ */

test('there is exactly one pass per observation field', () => {
  assert.deepEqual(
    PASSES.map((p) => p.field).sort(),
    [...OBSERVATION_FIELDS].sort(),
  );
});

test('every pass asks one short, complete instruction', () => {
  for (const pass of PASSES) {
    // Some passes are questions, others imperatives ("Describe the ...") —
    // what matters is that each is one short instruction.
    assert.match(pass.question, /[.?]$/, `${pass.field} should be one complete instruction`);
    // Roomy enough for a pass that enumerates its valid answers, which is how
    // the shot-type pass is kept to a closed set.
    assert.ok(pass.question.length < 140, `${pass.field} instruction is too long`);
    // Per-field budgets are asserted separately; this is just a sanity ceiling.
    assert.ok(pass.tokens > 0 && pass.tokens <= 128, `${pass.field} token budget looks wrong`);
  }
});

/* ------------------------------------------------------------------ *
 * Answer length budgets
 *
 * The 2.2B model's descriptions were being clipped mid-clause. The cause was
 * these budgets, not the model.
 * ------------------------------------------------------------------ */

test('descriptive passes get enough room for a real description', () => {
  const budget = (field) => PASSES.find((p) => p.field === field).tokens;
  // Roughly: 1 token ≈ 4 characters, so 48 tokens ≈ a full clause-rich answer.
  for (const field of ['appearance', 'clothing', 'pose', 'setting', 'lighting']) {
    assert.ok(budget(field) >= 48, `${field} budget is only ${budget(field)}`);
  }
  // Pose is the most detailed answer of the set.
  assert.ok(budget('pose') >= budget('appearance'), 'pose should have the largest budget');
});

test('closed-answer passes stay small, since a long answer there is noise', () => {
  const budget = (field) => PASSES.find((p) => p.field === field).tokens;
  for (const field of ['placement', 'apparentAge', 'shotType']) {
    assert.ok(budget(field) <= 16, `${field} budget is ${budget(field)}`);
  }
});

test('larger models are given proportionally more room', () => {
  const scale = (key) => MODELS[key].tokenScale ?? 1;
  assert.equal(scale('smolvlm-256m-q8'), 1);
  assert.ok(scale('smolvlm-500m-q4f16') > scale('smolvlm-256m-q8'));
  assert.ok(scale('smolvlm-2.2b-q4f16') > scale('smolvlm-500m-q4f16'));
});

test('every model declares a token scale', () => {
  for (const [key, model] of Object.entries(MODELS)) {
    assert.equal(typeof model.tokenScale, 'number', `${key} missing tokenScale`);
    assert.ok(model.tokenScale >= 1, `${key} scale below 1`);
  }
});

test('the scaled budget for the largest model is generous', () => {
  const pose = PASSES.find((p) => p.field === 'pose').tokens;
  const scaled = Math.round(pose * MODELS['smolvlm-2.2b-q4f16'].tokenScale);
  // ~190 tokens is several hundred characters — well past where clipping was hit.
  assert.ok(scaled >= 150, `scaled pose budget is only ${scaled}`);
});

test('truncation is detected and repaired, not shown', () => {
  // runPass is not exported, so this pins the contract it relies on: a cap-hit
  // answer is repaired by the compiler before it reaches the observation.
  assert.ok(visionCode.includes('output.dims.at(-1) - promptLength'), 'expected length comparison');
  assert.ok(visionCode.includes('repairTruncation'), 'expected repair to be applied');
  assert.ok(/clipped/.test(visionCode), 'expected clipped fields to be reported');
});
