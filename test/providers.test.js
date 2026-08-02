const test = require('node:test');
const assert = require('node:assert');

const {
  callHuggingFace, callCustom, extractVideo, toBareBase64,
  fetchProviderMapping, buildProviderRequest, ProviderError,
} = require('../server/providers');

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
    json: async () => JSON.parse(isBuffer ? body.toString() : String(body)),
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

/* ---------- provider resolution ---------- */

function jsonResponse(payload) {
  return mockResponse({ contentType: 'application/json', body: JSON.stringify(payload) });
}

test('fetchProviderMapping accepts the keyed-object shape', async () => {
  const mapping = await fetchProviderMapping('Wan-AI/Wan2.2-T2V-A14B', CONFIG, {
    fetchImpl: async () => jsonResponse({
      inferenceProviderMapping: {
        'fal-ai': { status: 'live', providerId: 'fal-ai/wan-t2v', task: 'text-to-video' },
      },
    }),
  });
  assert.deepStrictEqual(mapping, [
    { provider: 'fal-ai', providerId: 'fal-ai/wan-t2v', status: 'live', task: 'text-to-video' },
  ]);
});

test('fetchProviderMapping accepts the array shape', async () => {
  const mapping = await fetchProviderMapping('m/x', CONFIG, {
    fetchImpl: async () => jsonResponse({
      inferenceProviderMapping: [
        { provider: 'replicate', providerId: 'org/model', status: 'live', task: 'text-to-video' },
      ],
    }),
  });
  assert.strictEqual(mapping[0].provider, 'replicate');
  assert.strictEqual(mapping[0].providerId, 'org/model');
});

test('fetchProviderMapping returns [] rather than throwing when the Hub is unreachable', async () => {
  const offline = await fetchProviderMapping('m/x', CONFIG, {
    fetchImpl: async () => { throw new Error('ENOTFOUND'); },
  });
  assert.deepStrictEqual(offline, []);

  const notFound = await fetchProviderMapping('m/x', CONFIG, {
    fetchImpl: async () => mockResponse({ status: 404, contentType: 'application/json', body: '{}' }),
  });
  assert.deepStrictEqual(notFound, []);
});

test('buildProviderRequest shapes each provider correctly', () => {
  const job = { prompt: 'p', negativePrompt: 'n', params: { num_frames: 81 } };

  const fal = buildProviderRequest('fal-ai', 'fal-ai/wan', job);
  assert.strictEqual(fal.url, 'https://router.huggingface.co/fal-ai/fal-ai/wan');
  assert.strictEqual(fal.body.prompt, 'p');
  assert.strictEqual(fal.body.num_frames, 81);

  const rep = buildProviderRequest('replicate', 'org/model', job);
  assert.match(rep.url, /replicate\/v1\/models\/org\/model\/predictions$/);
  assert.strictEqual(rep.body.input.prompt, 'p');
  assert.strictEqual(rep.headers.Prefer, 'wait');

  const hfi = buildProviderRequest('hf-inference', 'org/model', job);
  assert.match(hfi.url, /hf-inference\/models\/org\/model$/);
  assert.strictEqual(hfi.body.inputs, 'p');
  assert.strictEqual(hfi.body.parameters.negative_prompt, 'n');
});

test('routing follows the mapped provider rather than assuming hf-inference', async () => {
  const urls = [];
  await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url, opts) => {
      if (url.startsWith('https://huggingface.co/api/models')) {
        return jsonResponse({
          inferenceProviderMapping: {
            'fal-ai': { status: 'live', providerId: 'fal-ai/wan-t2v', task: 'text-to-video' },
          },
        });
      }
      urls.push(url);
      return opts ? mockResponse() : mockResponse();
    },
  });
  assert.strictEqual(urls.length, 1);
  assert.strictEqual(urls[0], 'https://router.huggingface.co/fal-ai/fal-ai/wan-t2v');
});

test('live providers are tried before non-live ones, and hf-inference last', async () => {
  const urls = [];
  await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url) => {
      if (url.startsWith('https://huggingface.co/api/models')) {
        return jsonResponse({
          inferenceProviderMapping: [
            { provider: 'hf-inference', providerId: 'a', status: 'live' },
            { provider: 'fal-ai', providerId: 'b', status: 'live' },
          ],
        });
      }
      urls.push(url);
      return mockResponse();
    },
  });
  assert.match(urls[0], /fal-ai/, 'hf-inference should sort last for video models');
});

test('a 400 from one provider moves on to the next', async () => {
  const tried = [];
  const buf = await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url) => {
      if (url.startsWith('https://huggingface.co/api/models')) {
        return jsonResponse({
          inferenceProviderMapping: [
            { provider: 'novita', providerId: 'a', status: 'live' },
            { provider: 'fal-ai', providerId: 'b', status: 'live' },
          ],
        });
      }
      tried.push(url);
      if (url.includes('novita')) {
        return mockResponse({
          status: 400,
          contentType: 'application/json',
          body: '{"error":"Model not supported by provider novita"}',
        });
      }
      return mockResponse({ body: Buffer.from('from-fal') });
    },
  });
  assert.strictEqual(tried.length, 2);
  assert.strictEqual(buf.toString(), 'from-fal');
});

test('HF_PROVIDER forces a provider and skips the lookup', async () => {
  let lookups = 0;
  const urls = [];
  await callHuggingFace(JOB, { ...CONFIG, hfProvider: 'replicate' }, {
    fetchImpl: async (url) => {
      if (url.startsWith('https://huggingface.co/api/models')) {
        lookups += 1;
        return jsonResponse({});
      }
      urls.push(url);
      return mockResponse();
    },
  });
  assert.strictEqual(lookups, 0, 'a forced provider should not need the Hub lookup');
  assert.match(urls[0], /router\.huggingface\.co\/replicate\//);
});

test('when nothing serves the model the error names what was tried', async () => {
  await assert.rejects(
    () => callHuggingFace(JOB, CONFIG, {
      fetchImpl: async (url) => {
        if (url.startsWith('https://huggingface.co/api/models')) {
          return jsonResponse({
            inferenceProviderMapping: [{ provider: 'fal-ai', providerId: 'x', status: 'live' }],
          });
        }
        return mockResponse({
          status: 400,
          contentType: 'application/json',
          body: '{"error":"Model not supported by provider"}',
        });
      },
    }),
    (err) => {
      assert.match(err.message, /No provider served/);
      assert.match(err.message, /fal-ai/);
      assert.match(err.message, /Check availability/);
      return true;
    },
  );
});

test('an unmapped model still falls back to the legacy host', async () => {
  const urls = [];
  const buf = await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url) => {
      if (url.startsWith('https://huggingface.co/api/models')) return jsonResponse({});
      urls.push(url);
      if (url.includes('router.huggingface.co')) {
        return mockResponse({ status: 404, contentType: 'application/json', body: '{}' });
      }
      return mockResponse({ body: Buffer.from('legacy') });
    },
  });
  assert.match(urls[urls.length - 1], /api-inference\.huggingface\.co/);
  assert.strictEqual(buf.toString(), 'legacy');
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

test('a 404 from the mapped provider falls through to the legacy host', async () => {
  const tried = [];
  const buf = await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url) => {
      if (url.startsWith('https://huggingface.co/api/models')) {
        return jsonResponse({
          inferenceProviderMapping: [{ provider: 'fal-ai', providerId: 'x', status: 'live' }],
        });
      }
      tried.push(url);
      if (url.includes('router.huggingface.co')) {
        return mockResponse({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' });
      }
      return mockResponse({ body: Buffer.from('legacy') });
    },
  });

  assert.strictEqual(tried.length, 2);
  assert.match(tried[1], /api-inference\.huggingface\.co/);
  assert.strictEqual(buf.toString(), 'legacy');
});

test('a 401 produces an actionable token message and stops immediately', async () => {
  let posts = 0;
  await assert.rejects(
    () => callHuggingFace(JOB, { ...CONFIG, hfProvider: 'fal-ai' }, {
      fetchImpl: async () => {
        posts += 1;
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
  assert.strictEqual(posts, 1, 'auth failures should not be retried against other providers');
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

test('toBareBase64 strips a data URL prefix and passes bare payloads through', () => {
  assert.strictEqual(toBareBase64('data:image/png;base64,AAAB'), 'AAAB');
  assert.strictEqual(toBareBase64('data:image/jpeg;base64,/9j/4AAQ'), '/9j/4AAQ');
  assert.strictEqual(toBareBase64('AAAB'), 'AAAB');
});

test('a start frame reaches the HF payload as bare base64', async () => {
  let captured;
  await callHuggingFace(
    { ...JOB, model: 'Wan-AI/Wan2.2-I2V-A14B', initImage: 'data:image/png;base64,SGVsbG8=' },
    CONFIG,
    {
      fetchImpl: async (url, opts) => {
        captured = JSON.parse(opts.body);
        return mockResponse();
      },
    },
  );
  assert.strictEqual(captured.parameters.image, 'SGVsbG8=');
  assert.ok(!captured.parameters.image.startsWith('data:'), 'the data URL prefix must be stripped');
});

test('text-to-video jobs send no image parameter at all', async () => {
  let captured;
  await callHuggingFace(JOB, CONFIG, {
    fetchImpl: async (url, opts) => {
      captured = JSON.parse(opts.body);
      return mockResponse();
    },
  });
  assert.ok(!('image' in captured.parameters));
});

test('a start frame reaches a custom endpoint in both shapes', async () => {
  let captured;
  await callCustom(
    { ...JOB, initImage: 'data:image/png;base64,SGVsbG8=' },
    CONFIG,
    {
      fetchImpl: async (url, opts) => {
        captured = JSON.parse(opts.body);
        return mockResponse();
      },
    },
  );
  assert.strictEqual(captured.image, 'SGVsbG8=');
  assert.strictEqual(captured.image_data_url, 'data:image/png;base64,SGVsbG8=');
  assert.strictEqual(captured.parameters.image, 'SGVsbG8=');
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
