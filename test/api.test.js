const test = require('node:test');
const assert = require('node:assert');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');

const { createServer } = require('../server/index');
const providers = require('../server/providers');

function testConfig(overrides = {}) {
  return {
    port: 0,
    host: '127.0.0.1',
    provider: 'hf',
    hfToken: 'test-token',
    defaultModel: 'Wan-AI/Wan2.2-T2V-A14B',
    customEndpoint: '',
    customAuthHeader: 'Authorization',
    customAuthValue: '',
    customBodyTemplate: '',
    outputDir: fs.mkdtempSync(path.join(os.tmpdir(), 'realframe-test-')),
    maxConcurrent: 1,
    jobTimeoutMs: 5000,
    retainJobs: 10,
    ...overrides,
  };
}

/** Start a server on an ephemeral port and return a base URL plus a stop hook. */
async function withServer(config, fn) {
  const server = createServer(config);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(base, server);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test('GET /api/config reports capability without leaking the token', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/api/config`);
    assert.strictEqual(res.status, 200);
    const body = await res.json();

    assert.strictEqual(body.hasToken, true);
    assert.ok(!JSON.stringify(body).includes('test-token'), 'token must not be serialized to clients');
    assert.ok(body.models.length > 0);
    assert.ok(body.presets.length > 0);
    assert.ok(body.negativeGroups.length > 0);
  });
});

test('POST /api/compile returns a compiled prompt', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/api/compile`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: 'a man crossing a street', preset: 'documentary', intensity: 2 }),
    });
    assert.strictEqual(res.status, 200);
    const body = await res.json();
    assert.match(body.prompt, /^a man crossing a street,/);
    assert.ok(body.negativePrompt.length > 0);
    assert.strictEqual(body.preset.key, 'documentary');
  });
});

test('POST /api/compile rejects an empty subject', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/api/compile`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: '' }),
    });
    assert.strictEqual(res.status, 400);
    assert.match((await res.json()).error, /subject/i);
  });
});

test('POST /api/analyze flags anti-patterns', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/api/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: 'an 8k hyperrealistic masterpiece of a woman' }),
    });
    const body = await res.json();
    assert.ok(body.antiPatterns.length >= 3);
    assert.strictEqual(body.human, true);
  });
});

test('malformed JSON returns 400 rather than crashing', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/api/compile`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{not json',
    });
    assert.strictEqual(res.status, 400);
  });
});

test('unknown API routes 404 and static traversal is refused', async () => {
  await withServer(testConfig(), async (base) => {
    assert.strictEqual((await fetch(`${base}/api/nope`)).status, 404);

    // Encoded traversal — the URL parser normalizes it, so it lands outside public/.
    const res = await fetch(`${base}/..%2f..%2fserver%2fconfig.js`);
    assert.ok(res.status === 403 || res.status === 404, `expected refusal, got ${res.status}`);
  });
});

test('the frontend is served', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/`);
    assert.strictEqual(res.status, 200);
    assert.match(res.headers.get('content-type'), /text\/html/);
    assert.match(await res.text(), /RealFrame/);
  });
});

test('an unknown job id is a 404, and video ids are validated', async () => {
  await withServer(testConfig(), async (base) => {
    assert.strictEqual((await fetch(`${base}/api/jobs/nope`)).status, 404);
    assert.strictEqual((await fetch(`${base}/api/video/..%2f..%2fetc%2fpasswd`)).status, 400);
  });
});

test('generate enqueues a job and the job runs to completion', async () => {
  const config = testConfig();
  const fakeVideo = Buffer.from('fake-mp4-bytes-for-testing');

  // Stand in for the provider so no network call happens.
  const original = providers.generate;
  providers.generate = async () => fakeVideo;

  try {
    await withServer(config, async (base) => {
      const res = await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'a bus pulling away from a stop', intensity: 2 }),
      });
      assert.strictEqual(res.status, 202);
      const { id, compiled } = await res.json();
      assert.ok(id);
      assert.match(compiled.prompt, /bus pulling away/);

      // Poll until the queue finishes.
      let job;
      for (let i = 0; i < 50; i += 1) {
        job = await (await fetch(`${base}/api/jobs/${id}`)).json();
        if (job.status === 'done' || job.status === 'error') break;
        await new Promise((r) => setTimeout(r, 50));
      }

      assert.strictEqual(job.status, 'done', `job failed: ${job.error}`);
      assert.strictEqual(job.videoUrl, `/api/video/${id}`);

      const videoRes = await fetch(`${base}${job.videoUrl}`);
      assert.strictEqual(videoRes.status, 200);
      assert.strictEqual(videoRes.headers.get('content-type'), 'video/mp4');
      assert.strictEqual(Buffer.from(await videoRes.arrayBuffer()).toString(), fakeVideo.toString());

      // Range requests must work so the player can seek.
      const rangeRes = await fetch(`${base}${job.videoUrl}`, { headers: { Range: 'bytes=0-3' } });
      assert.strictEqual(rangeRes.status, 206);
      assert.strictEqual(Buffer.from(await rangeRes.arrayBuffer()).length, 4);
    });
  } finally {
    providers.generate = original;
  }
});

test('a failing provider marks the job as errored with the message', async () => {
  const original = providers.generate;
  providers.generate = async () => { throw new providers.ProviderError('upstream exploded'); };

  try {
    await withServer(testConfig(), async (base) => {
      const { id } = await (await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'a kite over a field' }),
      })).json();

      let job;
      for (let i = 0; i < 50; i += 1) {
        job = await (await fetch(`${base}/api/jobs/${id}`)).json();
        if (job.status === 'error') break;
        await new Promise((r) => setTimeout(r, 50));
      }
      assert.strictEqual(job.status, 'error');
      assert.match(job.error, /upstream exploded/);
    });
  } finally {
    providers.generate = original;
  }
});

test('jobs never expose the init image or abort controller', async () => {
  const original = providers.generate;
  providers.generate = async () => Buffer.from('x');

  try {
    await withServer(testConfig(), async (base) => {
      await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'a still lake', initImage: 'data:image/png;base64,AAAA' }),
      });

      const { jobs } = await (await fetch(`${base}/api/jobs`)).json();
      assert.strictEqual(jobs.length, 1);
      assert.strictEqual(jobs[0].hasInitImage, true);
      assert.ok(!('initImage' in jobs[0]));
      assert.ok(!('controller' in jobs[0]));
    });
  } finally {
    providers.generate = original;
  }
});
