#!/usr/bin/env node
/**
 * RealFrame server — static frontend plus a small JSON API.
 * No runtime dependencies; Node 18+ provides everything used here.
 */

const http = require('node:http');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

const { loadConfig, ROOT } = require('./config');
const { JobQueue } = require('./jobs');
const realism = require('./realism');
const catalog = require('./catalog');
const providers = require('./providers');
const ffmpeg = require('./ffmpeg');

const PUBLIC_DIR = path.join(ROOT, 'public');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

const MAX_BODY = 24 * 1024 * 1024; // room for a base64 init image

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error('Request body too large.'), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(Object.assign(new Error('Request body is not valid JSON.'), { status: 400 }));
      }
    });
    req.on('error', reject);
  });
}

async function serveStatic(req, res, urlPath) {
  const rel = urlPath === '/' ? 'index.html' : urlPath.replace(/^\/+/, '');
  const filePath = path.join(PUBLIC_DIR, rel);

  // Refuse anything that escapes the public directory.
  if (!filePath.startsWith(PUBLIC_DIR)) {
    sendJson(res, 403, { error: 'Forbidden' });
    return;
  }

  try {
    const stat = await fsp.stat(filePath);
    if (!stat.isFile()) throw new Error('not a file');
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(filePath)] || 'application/octet-stream',
      'Content-Length': stat.size,
    });
    fs.createReadStream(filePath).pipe(res);
  } catch {
    sendJson(res, 404, { error: 'Not found' });
  }
}

/** Range-aware video streaming so the player can seek and scrub. */
async function serveVideo(res, req, config, id) {
  if (!/^[a-f0-9-]{36}$/i.test(id)) {
    sendJson(res, 400, { error: 'Invalid job id.' });
    return;
  }
  const filePath = path.join(config.outputDir, `${id}.mp4`);
  let stat;
  try {
    stat = await fsp.stat(filePath);
  } catch {
    sendJson(res, 404, { error: 'No video for that job.' });
    return;
  }

  const range = req.headers.range;
  if (range) {
    const match = /bytes=(\d*)-(\d*)/.exec(range);
    if (match) {
      const start = match[1] ? parseInt(match[1], 10) : 0;
      const end = match[2] ? parseInt(match[2], 10) : stat.size - 1;
      if (start >= stat.size || end >= stat.size || start > end) {
        res.writeHead(416, { 'Content-Range': `bytes */${stat.size}` });
        res.end();
        return;
      }
      res.writeHead(206, {
        'Content-Type': 'video/mp4',
        'Content-Length': end - start + 1,
        'Content-Range': `bytes ${start}-${end}/${stat.size}`,
        'Accept-Ranges': 'bytes',
      });
      fs.createReadStream(filePath, { start, end }).pipe(res);
      return;
    }
  }

  res.writeHead(200, {
    'Content-Type': 'video/mp4',
    'Content-Length': stat.size,
    'Accept-Ranges': 'bytes',
  });
  fs.createReadStream(filePath).pipe(res);
}

function createServer(config = loadConfig()) {
  const queue = new JobQueue(config);

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const { pathname } = url;

    try {
      // ---- API ----
      if (pathname === '/api/config' && req.method === 'GET') {
        sendJson(res, 200, {
          provider: config.provider,
          defaultModel: config.defaultModel,
          hasToken: Boolean(config.hfToken),
          hasCustomEndpoint: Boolean(config.customEndpoint),
          models: catalog.MODELS,
          quality: catalog.QUALITY,
          maxDuration: catalog.MAX_DURATION_SECONDS,
          hasFfmpeg: await ffmpeg.locate() !== null,
          presets: realism.listPresets(),
          negativeGroups: realism.listNegativeGroups(),
        });
        return;
      }

      // Compile a prompt without generating — powers the live preview.
      if (pathname === '/api/compile' && req.method === 'POST') {
        const body = await readBody(req);
        try {
          sendJson(res, 200, realism.compilePrompt(body));
        } catch (err) {
          sendJson(res, 400, { error: err.message });
        }
        return;
      }

      // Which providers actually serve a model. Runs from the user's machine,
      // which can reach the Hub, and reports exactly what routing will see.
      if (pathname === '/api/probe' && req.method === 'GET') {
        const model = url.searchParams.get('model') || config.defaultModel;

        // On the custom backend the hosted catalog is irrelevant — ask the
        // endpoint itself what it is running, so the UI can show the real
        // model and apply parameters that suit it.
        if ((url.searchParams.get('provider') || config.provider) === 'custom') {
          if (!config.customEndpoint) {
            sendJson(res, 200, {
              local: true,
              reachable: false,
              hint: 'PROVIDER=custom but CUSTOM_ENDPOINT is not set in .env.',
            });
            return;
          }
          const healthUrl = config.customEndpoint.replace(/\/generate\/?$/, '/health');
          try {
            const info = await providers.probeCustom(healthUrl, config);
            sendJson(res, 200, {
              local: true,
              reachable: true,
              ...info,
              hint: `Local: ${info.label || info.model}`
                + (info.gpu?.name ? ` on ${info.gpu.name} (${info.gpu.vram_gb} GB)` : ''),
            });
          } catch (err) {
            sendJson(res, 200, {
              local: true,
              reachable: false,
              hint: `Could not reach the local GPU server at ${healthUrl}. `
                + `Is its window still running? (${err.message})`,
            });
          }
          return;
        }

        try {
          const mapping = await providers.fetchProviderMapping(model, config, { strict: true });
          sendJson(res, 200, {
            model,
            configuredProvider: config.hfProvider,
            providers: mapping,
            reachable: true,
            hint: mapping.length
              ? `Routing will try: ${mapping.map((m) => m.provider).join(', ')}`
              : 'No providers serve this model. Pick a different one, or check that your token has the "Make calls to Inference Providers" permission.',
          });
        } catch (err) {
          sendJson(res, 200, {
            model,
            providers: [],
            reachable: false,
            hint: `Could not reach huggingface.co: ${err.message}`,
          });
        }
        return;
      }

      if (pathname === '/api/analyze' && req.method === 'POST') {
        const body = await readBody(req);
        const text = String(body.subject || '');
        sendJson(res, 200, {
          ...realism.scorePrompt(text),
          antiPatterns: realism.findAntiPatterns(text),
          human: realism.looksHuman(text),
        });
        return;
      }

      if (pathname === '/api/generate' && req.method === 'POST') {
        const body = await readBody(req);
        let compiled;
        try {
          compiled = realism.compilePrompt(body);
        } catch (err) {
          sendJson(res, 400, { error: err.message });
          return;
        }

        const model = body.model || config.defaultModel;
        const params = { ...catalog.defaultParamsFor(model), ...(body.params || {}) };

        // Quality sets the step count unless the user typed one explicitly.
        const quality = catalog.QUALITY[body.quality] ? body.quality : null;
        if (quality && (body.params || {}).num_inference_steps == null) {
          params.num_inference_steps = catalog.QUALITY[quality].steps;
        }

        const plan = catalog.planSegments({
          model,
          durationSeconds: body.durationSeconds,
          fps: params.fps,
        });

        if (plan.segments > 1) {
          if (!(await ffmpeg.locate())) {
            sendJson(res, 400, {
              error: 'Clips longer than one segment need ffmpeg to join them, and none was found. '
                + 'Set it up via local/start-local-gpu.bat (its Python environment ships one), '
                + 'install ffmpeg, or point FFMPEG_PATH at a binary.',
            });
            return;
          }
          params.num_frames = plan.framesPerSegment;
        }

        const job = queue.create({
          provider: body.provider || config.provider,
          model,
          subject: body.subject,
          prompt: body.usePrompt || compiled.prompt,
          negativePrompt: body.useNegativePrompt ?? compiled.negativePrompt,
          params,
          initImage: body.initImage || null,
          compiled,
          plan,
          quality,
        });

        sendJson(res, 202, { id: job.id, compiled, plan });
        return;
      }

      if (pathname === '/api/jobs' && req.method === 'GET') {
        sendJson(res, 200, { jobs: queue.list() });
        return;
      }

      // Delete every finished job. Declared before the :id route so "all"
      // is not read as an id.
      if (pathname === '/api/jobs/all' && req.method === 'DELETE') {
        const removed = await queue.clear();
        sendJson(res, 200, { removed });
        return;
      }

      const jobMatch = /^\/api\/jobs\/([^/]+)$/.exec(pathname);
      if (jobMatch) {
        const id = jobMatch[1];
        const job = queue.get(id);
        if (!job) {
          sendJson(res, 404, { error: 'Unknown job.' });
          return;
        }

        if (req.method === 'DELETE') {
          // Cancel what is still running; delete what has finished. `purge`
          // forces removal either way.
          const purge = url.searchParams.get('purge') === 'true';
          const active = job.status === 'running' || job.status === 'queued';

          if (active && !purge) {
            sendJson(res, 200, { action: 'cancelled', job: queue.cancel(id) });
            return;
          }
          await queue.remove(id);
          sendJson(res, 200, { action: 'deleted', id });
          return;
        }

        sendJson(res, 200, queue.list().find((j) => j.id === id));
        return;
      }

      const videoMatch = /^\/api\/video\/([^/]+)$/.exec(pathname);
      if (videoMatch && req.method === 'GET') {
        await serveVideo(res, req, config, videoMatch[1]);
        return;
      }

      if (pathname.startsWith('/api/')) {
        sendJson(res, 404, { error: 'Unknown endpoint.' });
        return;
      }

      // ---- Static ----
      if (req.method === 'GET') {
        await serveStatic(req, res, pathname);
        return;
      }

      sendJson(res, 405, { error: 'Method not allowed.' });
    } catch (err) {
      sendJson(res, err.status || 500, { error: err.message || 'Internal error.' });
    }
  });

  server.queue = queue;
  return server;
}

if (require.main === module) {
  const config = loadConfig();
  const server = createServer(config);
  server.listen(config.port, config.host, () => {
    console.log(`\n  RealFrame running at http://localhost:${config.port}`);
    console.log(`  provider: ${config.provider}`);
    if (config.provider === 'hf' && !config.hfToken) {
      console.log('  ⚠  No HF_TOKEN set — generation will fail until you add one to .env');
    }
    if (config.provider === 'custom' && !config.customEndpoint) {
      console.log('  ⚠  PROVIDER=custom but CUSTOM_ENDPOINT is empty');
    }
    console.log(`  outputs: ${config.outputDir}\n`);
  });
}

module.exports = { createServer };
