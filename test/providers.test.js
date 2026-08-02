const test = require('node:test');
const assert = require('node:assert');

const { callHuggingFace, callCustom, extractVideo, ProviderError } = require('../server/providers');

const CONFIG = {
  provider: 'hf',
  hfToken: 'tok',
  customEndpoint: 'https://gpu.example.internal/generate',
  customAuthHeader: 'Authorization',
  customAuthValue: 'Bearer local',
  customBodyTemplate: '',
};

const JOB = {
  model: 'Wan-AI/Wan2.2-T2V-A14B',
  prompt: 'a man walking, shot on a Sony FX3',
  negativePrompt: 'cgi, 3d render',
  params: { num_frames: 81, fps: 16 },
};

/** Minimal Response stand-in — enough surface for the provider code. */
function mockResponse({ status = 200, contentType = 'video/mp4', body = Buffer.from('mp4') } = {}) {
  const isBuffer = Buffer.isBuffer(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (k.toLowerCase() === 'content-type' ? contentType : null) },
    arrayBuffer: async () => (isBuffer ? body : Buffer.from(String(body))),
    text: async () => (isBuffer ? body.toString() : String(body)),
  };
}

test('extractVideo passes through raw video bytes', async () => {
  const buf = await extractVideo(mockResponse({ body: Buffer.from('rawmp4') }), async () => {
    throw new Error('should not fetch');
  });
  assert.strictEqual(buf.toString(), 'rawmp4');
});

test('extractVideo decodes base64 in a JSON envelope', async () => {
  const payload = Buffer.from('x'.repeat(1000)).toString('base64');
  const res = mockResponse({ contentType: 'application/json', body: JSON.stringify({ video: payload }) });
  const buf = await extractVideo(res, async () => { throw new Error('should not fetch'); });
  assert.strictEqual(buf.toString(), 'x'.repeat(1000));
});

test('extractVideo follows a URL in a JSON envelope', async () => {
  const res = mockResponse({
    contentType: 'application/json',
    body: JSON.stringify({ output: { video_url: 'https://cdn.example.com/out.mp4' } }),
  });
  let requested = null;
  const buf = await extractVideo(res, async (url) => {
    requested = url;
    return mockResponse({ body: Buffer.from('downloaded') });
  });
  assert.strictEqual(requested, 'https://cdn.example.com/out.mp4');
  assert.strictEqual(buf.toString(), 'downloaded');
});

test('extractVideo errors clearly when there is no video in the response', async () => {
  const res = mockResponse({ contentType: 'application/json', body: JSON.stringify({ status: 'queued' }) });
  await assert.rejects(
    () => extractVideo(res, async () => mockResponse()),
    /no video payload/,
  );
});

test('callHuggingFace without a token fails before any network call', async () => {
  await assert.rejects(
    () => callHuggingFace(JOB, { ...CONFIG, hfToken: '' }, {
      fetchImpl: async () => { throw new Error('should not be called'); },
    }),
    /No HF_TOKEN set/,
  );
});

test('callHuggingFace sends the prompt, negative prompt and params', async () => {
  let captured;
  await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url, opts) => {
      captured = { url, body: JSON.parse(opts.body), headers: opts.headers };
      return mockResponse();
    },
  });

  assert.match(captured.url, /router\.huggingface\.co/);
  assert.match(captured.url, /Wan-AI\/Wan2\.2-T2V-A14B$/);
  assert.strictEqual(captured.body.inputs, JOB.prompt);
  assert.strictEqual(captured.body.parameters.negative_prompt, JOB.negativePrompt);
  assert.strictEqual(captured.body.parameters.num_frames, 81);
  assert.strictEqual(captured.headers.Authorization, 'Bearer tok');
});

test('callHuggingFace falls back to the legacy host on 404', async () => {
  const tried = [];
  const buf = await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url) => {
      tried.push(url);
      if (url.includes('router.huggingface.co')) return mockResponse({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' });
      return mockResponse({ body: Buffer.from('legacy') });
    },
  });

  assert.strictEqual(tried.length, 2);
  assert.match(tried[1], /api-inference\.huggingface\.co/);
  assert.strictEqual(buf.toString(), 'legacy');
});

test('a 401 produces an actionable token message and does not fall through', async () => {
  let calls = 0;
  await assert.rejects(
    () => callHuggingFace(JOB, CONFIG, {
      fetchImpl: async () => {
        calls += 1;
        return mockResponse({ status: 401, contentType: 'application/json', body: '{"error":"invalid credentials"}' });
      },
    }),
    (err) => {
      assert.ok(err instanceof ProviderError);
      assert.match(err.message, /Authentication failed/);
      assert.match(err.message, /HF_TOKEN/);
      return true;
    },
  );
  assert.strictEqual(calls, 1, 'auth failures should not retry against the other host');
});

test('503 and 429 are marked retryable, 400 is not', async () => {
  const statusOf = async (status) => {
    try {
      await callHuggingFace(JOB, CONFIG, {
        fetchImpl: async () => mockResponse({ status, contentType: 'application/json', body: '{}' }),
      });
    } catch (err) {
      return err;
    }
    throw new Error('expected a rejection');
  };

  assert.strictEqual((await statusOf(503)).retryable, true);
  assert.strictEqual((await statusOf(429)).retryable, true);
  assert.strictEqual((await statusOf(400)).retryable, false);
});

test('callCustom posts to the configured endpoint with its auth header', async () => {
  let captured;
  await callCustom({ ...JOB, provider: 'custom' }, CONFIG, {
    fetchImpl: async (url, opts) => {
      captured = { url, body: JSON.parse(opts.body), headers: opts.headers };
      return mockResponse();
    },
  });

  assert.strictEqual(captured.url, CONFIG.customEndpoint);
  assert.strictEqual(captured.headers.Authorization, 'Bearer local');
  assert.strictEqual(captured.body.prompt, JOB.prompt);
  assert.strictEqual(captured.body.negative_prompt, JOB.negativePrompt);
});

test('callCustom applies a body template when configured', async () => {
  let captured;
  const config = {
    ...CONFIG,
    customBodyTemplate: '{"text":"{{prompt}}","avoid":"{{negative_prompt}}","steps":30}',
  };
  await callCustom(JOB, config, {
    fetchImpl: async (url, opts) => {
      captured = JSON.parse(opts.body);
      return mockResponse();
    },
  });

  assert.deepStrictEqual(captured, { text: JOB.prompt, avoid: JOB.negativePrompt, steps: 30 });
});

test('a template that is not valid JSON reports itself clearly', async () => {
  const config = { ...CONFIG, customBodyTemplate: '{"text": {{prompt}}}' };
  await assert.rejects(
    () => callCustom(JOB, config, { fetchImpl: async () => mockResponse() }),
    /CUSTOM_BODY_TEMPLATE is not valid JSON/,
  );
});

test('callCustom without an endpoint fails clearly', async () => {
  await assert.rejects(
    () => callCustom(JOB, { ...CONFIG, customEndpoint: '' }, { fetchImpl: async () => mockResponse() }),
    /CUSTOM_ENDPOINT is not configured/,
  );
});

test('prompts with quotes survive template substitution', async () => {
  let captured;
  const config = { ...CONFIG, customBodyTemplate: '{"text":"{{prompt}}"}' };
  await callCustom(
    { ...JOB, prompt: 'a man saying "hello" \\ goodbye' },
    config,
    {
      fetchImpl: async (url, opts) => {
        captured = JSON.parse(opts.body);
        return mockResponse();
      },
    },
  );
  assert.strictEqual(captured.text, 'a man saying "hello" \\ goodbye');
});
