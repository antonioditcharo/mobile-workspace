import test from 'node:test';
import assert from 'node:assert/strict';

import { chooseModel, extractText, describeError, MODELS, PASSES } from '../src/vision.js';
import { OBSERVATION_FIELDS } from '../src/compiler.js';

/* ------------------------------------------------------------------ *
 * Model selection
 * ------------------------------------------------------------------ */

test('WebGPU with adequate memory picks the 500M model on the GPU', () => {
  const choice = chooseModel({ webgpu: true, memoryGb: 8, adapter: 'arm' });
  assert.equal(choice.key, 'smolvlm-500m-q4f16');
  assert.equal(choice.device, 'webgpu');
  assert.match(choice.why, /WebGPU/);
});

test('WebGPU with low memory drops to the smallest model', () => {
  const choice = chooseModel({ webgpu: true, memoryGb: 4 });
  assert.equal(choice.key, 'smolvlm-256m-q4');
  assert.equal(choice.device, 'webgpu');
  assert.match(choice.why, /RAM/);
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

test('passes give short prompts with tight token budgets', () => {
  for (const pass of PASSES) {
    // Some passes are questions, others imperatives ("Describe the ...") —
    // what matters is that each is one short instruction with a small budget.
    assert.match(pass.question, /[.?]$/, `${pass.field} should be one complete instruction`);
    assert.ok(pass.question.length < 100, `${pass.field} instruction is too long`);
    assert.ok(pass.tokens > 0 && pass.tokens <= 32, `${pass.field} token budget looks wrong`);
  }
});
