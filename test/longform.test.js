const test = require('node:test');
const assert = require('node:assert');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');

const catalog = require('../server/catalog');
const ffmpeg = require('../server/ffmpeg');
const providers = require('../server/providers');
const { createServer } = require('../server/index');

/* ---------- segment planning ---------- */

test('a short clip needs one pass', () => {
  const plan = catalog.planSegments({ model: 'Wan-AI/Wan2.2-T2V-A14B', durationSeconds: 5, fps: 16 });
  assert.strictEqual(plan.segments, 1);
  assert.strictEqual(plan.chainable, true);
});

test('20 seconds is chained across segments', () => {
  const plan = catalog.planSegments({ model: 'Wan-AI/Wan2.2-T2V-A14B', durationSeconds: 20, fps: 16 });
  // 320 frames at a ceiling of 81 per pass.
  assert.strictEqual(plan.segments, 4);
  assert.ok(plan.framesPerSegment <= 81);
  assert.ok(plan.actualSeconds >= 19, `expected ~20s, got ${plan.actualSeconds}`);
  assert.strictEqual(plan.continuation, 'Wan-AI/Wan2.2-I2V-A14B');
});

test('frames are spread evenly rather than leaving a stub segment', () => {
  const plan = catalog.planSegments({ model: 'Wan-AI/Wan2.2-T2V-A14B', durationSeconds: 10, fps: 16 });
  assert.strictEqual(plan.segments, 2);
  // 160 frames over 2 passes is 80 each, not 81 + 79.
  assert.strictEqual(plan.framesPerSegment, 80);
});

test('a model with no image-to-video counterpart cannot be extended', () => {
  const plan = catalog.planSegments({ model: 'tencent/HunyuanVideo', durationSeconds: 20, fps: 24 });
  assert.strictEqual(plan.segments, 1);
  assert.strictEqual(plan.chainable, false);
  assert.match(plan.reason, /cannot be extended/);
});

test('duration is clamped to the supported ceiling', () => {
  const plan = catalog.planSegments({ model: 'Wan-AI/Wan2.2-T2V-A14B', durationSeconds: 600, fps: 16 });
  assert.ok(plan.actualSeconds <= catalog.MAX_DURATION_SECONDS + 1);
});

test('quality presets increase step count monotonically', () => {
  const order = ['draft', 'standard', 'high', 'max'];
  for (let i = 1; i < order.length; i += 1) {
    assert.ok(
      catalog.QUALITY[order[i]].steps > catalog.QUALITY[order[i - 1]].steps,
      `${order[i]} should use more steps than ${order[i - 1]}`,
    );
    assert.ok(catalog.QUALITY[order[i]].timeFactor > catalog.QUALITY[order[i - 1]].timeFactor);
  }
});

/* ---------- ffmpeg ---------- */

const hasFfmpeg = (() => {
  try {
    execFileSync('ffmpeg', ['-version'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
})();

/** Build a real test clip: N seconds of a moving pattern at a known size. */
function makeClip(dir, name, seconds, color) {
  const out = path.join(dir, name);
  execFileSync('ffmpeg', [
    '-y', '-f', 'lavfi',
    '-i', `color=c=${color}:s=64x64:d=${seconds}:r=10`,
    '-pix_fmt', 'yuv420p', out,
  ], { stdio: 'ignore' });
  return out;
}

test('lastFrameDataUrl extracts a usable PNG', { skip: !hasFfmpeg }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rf-ff-'));
  const clip = makeClip(dir, 'a.mp4', 1, 'red');

  const dataUrl = await ffmpeg.lastFrameDataUrl(clip);
  assert.match(dataUrl, /^data:image\/png;base64,/);

  const bytes = Buffer.from(dataUrl.split(',')[1], 'base64');
  // PNG magic number.
  assert.deepStrictEqual([...bytes.subarray(0, 4)], [0x89, 0x50, 0x4e, 0x47]);
  assert.ok(bytes.length > 100);
});

test('concat joins segments and drops the duplicated seam frame', { skip: !hasFfmpeg }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rf-ff-'));
  const a = makeClip(dir, 'a.mp4', 1, 'red');
  const b = makeClip(dir, 'b.mp4', 1, 'blue');
  const out = path.join(dir, 'joined.mp4');

  await ffmpeg.concat([a, b], out);
  assert.ok(fs.existsSync(out));

  const seconds = await ffmpeg.duration(out);
  // Two 1s clips minus one trimmed frame at 10fps.
  assert.ok(seconds > 1.5 && seconds < 2.1, `expected ~1.9s, got ${seconds}`);
});

test('concat with a single segment just copies it', { skip: !hasFfmpeg }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rf-ff-'));
  const a = makeClip(dir, 'a.mp4', 1, 'green');
  const out = path.join(dir, 'one.mp4');

  await ffmpeg.concat([a], out);
  assert.deepStrictEqual(fs.readFileSync(out), fs.readFileSync(a));
});

/* ---------- end to end ---------- */

function testConfig(overrides = {}) {
  return {
    port: 0,
    host: '127.0.0.1',
    provider: 'hf',
    hfToken: 'test-token',
    hfProvider: 'auto',
    defaultModel: 'Wan-AI/Wan2.2-T2V-A14B',
    customEndpoint: '',
    customAuthHeader: 'Authorization',
    customAuthValue: '',
    customBodyTemplate: '',
    outputDir: fs.mkdtempSync(path.join(os.tmpdir(), 'realframe-long-')),
    maxConcurrent: 1,
    jobTimeoutMs: 20000,
    retainJobs: 10,
    ...overrides,
  };
}

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

async function waitFor(base, id, states = ['done', 'error']) {
  for (let i = 0; i < 200; i += 1) {
    const job = await (await fetch(`${base}/api/jobs/${id}`)).json();
    if (states.includes(job.status)) return job;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error('job did not settle');
}

test('a long request generates each segment and stitches them', { skip: !hasFfmpeg }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rf-src-'));
  const clip = fs.readFileSync(makeClip(dir, 'src.mp4', 1, 'red'));

  const calls = [];
  const original = providers.generate;
  providers.generate = async (job) => {
    calls.push({ model: job.model, hasImage: Boolean(job.initImage), frames: job.params.num_frames });
    return clip;
  };

  const config = testConfig();

  try {
    await withServer(config, async (base) => {
      const res = await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          subject: 'a train crossing a bridge',
          durationSeconds: 20,
          quality: 'standard',
        }),
      });
      assert.strictEqual(res.status, 202);
      const { id, plan } = await res.json();
      assert.strictEqual(plan.segments, 4);

      const job = await waitFor(base, id);
      assert.strictEqual(job.status, 'done', `failed: ${job.error}`);

      // First pass is text-to-video; the rest continue from a frame.
      assert.strictEqual(calls.length, 4);
      assert.strictEqual(calls[0].hasImage, false);
      assert.strictEqual(calls[0].model, 'Wan-AI/Wan2.2-T2V-A14B');
      for (const call of calls.slice(1)) {
        assert.strictEqual(call.hasImage, true, 'continuation passes need a start frame');
        assert.strictEqual(call.model, 'Wan-AI/Wan2.2-I2V-A14B');
      }

      // The stitched file must actually be four segments long, not one.
      const finalPath = path.join(config.outputDir, `${id}.mp4`);
      const seconds = await ffmpeg.duration(finalPath);
      assert.ok(seconds > 3.5, `expected ~4s of stitched video, got ${seconds}`);

      // Intermediate segment files are cleaned up.
      const leftovers = fs.readdirSync(config.outputDir).filter((f) => f.includes('.seg'));
      assert.deepStrictEqual(leftovers, []);
    });
  } finally {
    providers.generate = original;
  }
});

test('quality sets the step count sent to the provider', async () => {
  let seen = null;
  const original = providers.generate;
  providers.generate = async (job) => {
    seen = job.params.num_inference_steps;
    return Buffer.from('x');
  };

  try {
    await withServer(testConfig(), async (base) => {
      const { id } = await (await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'a quiet street', quality: 'max', durationSeconds: 3 }),
      })).json();
      await waitFor(base, id);
      assert.strictEqual(seen, catalog.QUALITY.max.steps);
    });
  } finally {
    providers.generate = original;
  }
});

test('an explicit step count overrides the quality preset', async () => {
  let seen = null;
  const original = providers.generate;
  providers.generate = async (job) => {
    seen = job.params.num_inference_steps;
    return Buffer.from('x');
  };

  try {
    await withServer(testConfig(), async (base) => {
      const { id } = await (await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          subject: 'a quiet street',
          quality: 'draft',
          params: { num_inference_steps: 41 },
        }),
      })).json();
      await waitFor(base, id);
      assert.strictEqual(seen, 41);
    });
  } finally {
    providers.generate = original;
  }
});

/* ---------- deletion ---------- */

test('deleting a finished render removes it and its files', async () => {
  const config = testConfig();

  const original = providers.generate;
  providers.generate = async () => Buffer.from('video-bytes');

  try {
    await withServer(config, async (base) => {
      const { id } = await (await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'a boat on a lake' }),
      })).json();
      await waitFor(base, id);

      assert.ok(fs.existsSync(path.join(config.outputDir, `${id}.mp4`)));

      const res = await fetch(`${base}/api/jobs/${id}`, { method: 'DELETE' });
      assert.strictEqual(res.status, 200);
      assert.strictEqual((await res.json()).action, 'deleted');

      assert.strictEqual((await (await fetch(`${base}/api/jobs`)).json()).jobs.length, 0);
      assert.ok(!fs.existsSync(path.join(config.outputDir, `${id}.mp4`)));
      assert.ok(!fs.existsSync(path.join(config.outputDir, `${id}.json`)));
    });
  } finally {
    providers.generate = original;
  }
});

test('deleting a running render cancels rather than deleting, unless purged', async () => {
  const original = providers.generate;
  // Never resolves on its own, so the job stays running.
  providers.generate = (job, config, { signal }) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => {
      const err = new Error('aborted');
      err.name = 'AbortError';
      reject(err);
    });
  });

  try {
    await withServer(testConfig(), async (base) => {
      const { id } = await (await fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'a long exposure' }),
      })).json();

      await new Promise((r) => setTimeout(r, 100));

      const res = await fetch(`${base}/api/jobs/${id}`, { method: 'DELETE' });
      assert.strictEqual((await res.json()).action, 'cancelled');

      // Still listed, just cancelled.
      const after = await waitFor(base, id, ['cancelled', 'error', 'done']);
      assert.strictEqual(after.status, 'cancelled');

      // Purge removes it outright.
      const purged = await fetch(`${base}/api/jobs/${id}?purge=true`, { method: 'DELETE' });
      assert.strictEqual((await purged.json()).action, 'deleted');
      assert.strictEqual((await (await fetch(`${base}/api/jobs`)).json()).jobs.length, 0);
    });
  } finally {
    providers.generate = original;
  }
});

test('clear removes finished renders but leaves running ones', async () => {
  const original = providers.generate;
  let hold = null;

  providers.generate = (job, config, { signal }) => {
    if (job.subject === 'slow') {
      return new Promise((_, reject) => {
        hold = reject;
        signal.addEventListener('abort', () => {
          const err = new Error('aborted');
          err.name = 'AbortError';
          reject(err);
        });
      });
    }
    return Promise.resolve(Buffer.from('done-bytes'));
  };

  try {
    await withServer(testConfig({ maxConcurrent: 2 }), async (base) => {
      const post = (subject) => fetch(`${base}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject }),
      }).then((r) => r.json());

      const fast = await post('a finished clip');
      await post('slow');
      await waitFor(base, fast.id);

      const res = await fetch(`${base}/api/jobs/all`, { method: 'DELETE' });
      assert.strictEqual((await res.json()).removed, 1);

      const { jobs } = await (await fetch(`${base}/api/jobs`)).json();
      assert.strictEqual(jobs.length, 1);
      assert.strictEqual(jobs[0].status, 'running');
    });
  } finally {
    if (hold) hold(Object.assign(new Error('cleanup'), { name: 'AbortError' }));
    providers.generate = original;
  }
});

test('JOB_TIMEOUT_MS=0 removes the deadline entirely', async () => {
  const { loadConfig } = require('../server/config');
  const previous = process.env.JOB_TIMEOUT_MS;

  try {
    process.env.JOB_TIMEOUT_MS = '0';
    assert.strictEqual(loadConfig().jobTimeoutMs, 0);

    // A job under a zero timeout must not be aborted on its own.
    const original = providers.generate;
    let sawAbort = false;
    providers.generate = (job, config, { signal }) => new Promise((resolve) => {
      signal.addEventListener('abort', () => { sawAbort = true; });
      setTimeout(() => resolve(Buffer.from('slow-but-finished')), 400);
    });

    try {
      await withServer(testConfig({ jobTimeoutMs: 0 }), async (base) => {
        const { id } = await (await fetch(`${base}/api/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subject: 'a long slow shot' }),
        })).json();

        const job = await waitFor(base, id);
        assert.strictEqual(job.status, 'done');
        assert.strictEqual(sawAbort, false, 'no timeout should have fired');
      });
    } finally {
      providers.generate = original;
    }
  } finally {
    if (previous === undefined) delete process.env.JOB_TIMEOUT_MS;
    else process.env.JOB_TIMEOUT_MS = previous;
  }
});

test('an unset JOB_TIMEOUT_MS still gets the default hour', () => {
  const { loadConfig } = require('../server/config');
  const previous = process.env.JOB_TIMEOUT_MS;
  delete process.env.JOB_TIMEOUT_MS;
  try {
    assert.strictEqual(loadConfig().jobTimeoutMs, 3600000);
  } finally {
    if (previous !== undefined) process.env.JOB_TIMEOUT_MS = previous;
  }
});

test('deleting an unknown job is a 404', async () => {
  await withServer(testConfig(), async (base) => {
    const res = await fetch(`${base}/api/jobs/does-not-exist`, { method: 'DELETE' });
    assert.strictEqual(res.status, 404);
  });
});
