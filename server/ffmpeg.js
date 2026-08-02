/**
 * ffmpeg helpers for long-form video.
 *
 * No open video model generates 20 seconds in one pass — they top out around
 * 5 seconds before coherence collapses and VRAM runs out. Long clips are built
 * by chaining: generate a segment, take its final frame, continue from that
 * frame with image-to-video, then stitch. This module owns the frame surgery.
 *
 * ffmpeg is located rather than required: if the local GPU server has been set
 * up, its Python environment already ships a binary, so most users have one
 * without installing anything.
 */

const { execFile } = require('node:child_process');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const { ROOT } = require('./config');

let cached;

function run(bin, args, { timeoutMs = 120000 } = {}) {
  return new Promise((resolve, reject) => {
    execFile(bin, args, { timeout: timeoutMs, maxBuffer: 1 << 26 }, (err, stdout, stderr) => {
      if (err) {
        err.stderr = stderr;
        reject(err);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

/** Look for a bundled ffmpeg inside the local server's Python environment. */
function findBundled() {
  const roots = [
    path.join(ROOT, 'local', '.venv', 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries'),
    path.join(ROOT, 'local', '.venv', 'lib', 'site-packages', 'imageio_ffmpeg', 'binaries'),
  ];

  // Linux/mac venvs bury site-packages under a python version directory.
  const posixLib = path.join(ROOT, 'local', '.venv', 'lib');
  try {
    for (const entry of fs.readdirSync(posixLib)) {
      roots.push(path.join(posixLib, entry, 'site-packages', 'imageio_ffmpeg', 'binaries'));
    }
  } catch { /* no venv yet */ }

  for (const dir of roots) {
    try {
      const hit = fs.readdirSync(dir).find((f) => f.startsWith('ffmpeg-'));
      if (hit) return path.join(dir, hit);
    } catch { /* not this one */ }
  }
  return null;
}

/**
 * Resolve an ffmpeg binary, or null. Cached after the first successful probe.
 */
async function locate() {
  if (cached !== undefined) return cached;

  const candidates = [
    process.env.FFMPEG_PATH,
    'ffmpeg',
    findBundled(),
  ].filter(Boolean);

  for (const bin of candidates) {
    try {
      await run(bin, ['-version'], { timeoutMs: 10000 });
      cached = bin;
      return cached;
    } catch { /* try the next */ }
  }

  cached = null;
  return cached;
}

/** Forget the cached probe — used by tests. */
function reset() {
  cached = undefined;
}

/**
 * Grab the last frame of a video as a PNG data URL, ready to feed straight
 * back in as the next segment's start frame.
 */
async function lastFrameDataUrl(videoPath) {
  const bin = await locate();
  if (!bin) throw new Error('ffmpeg not available.');

  const out = path.join(os.tmpdir(), `realframe-frame-${process.pid}-${Date.now()}.png`);
  try {
    // Seek from the end, then take one frame.
    await run(bin, ['-y', '-sseof', '-0.5', '-i', videoPath, '-vframes', '1', '-q:v', '2', out]);
    const buf = await fsp.readFile(out);
    return `data:image/png;base64,${buf.toString('base64')}`;
  } finally {
    fsp.unlink(out).catch(() => {});
  }
}

/**
 * Stitch segments into one file.
 *
 * Every segment after the first opens on the frame that ended the previous one,
 * so that duplicate is trimmed — otherwise each join shows a visible hitch.
 */
async function concat(segmentPaths, outPath) {
  const bin = await locate();
  if (!bin) throw new Error('ffmpeg not available.');

  if (segmentPaths.length === 1) {
    await fsp.copyFile(segmentPaths[0], outPath);
    return outPath;
  }

  const args = ['-y'];
  for (const p of segmentPaths) args.push('-i', p);

  const parts = segmentPaths.map((_, i) => (
    i === 0
      ? `[0:v]setpts=PTS-STARTPTS[v0];`
      : `[${i}:v]trim=start_frame=1,setpts=PTS-STARTPTS[v${i}];`
  )).join('');

  const inputs = segmentPaths.map((_, i) => `[v${i}]`).join('');
  const filter = `${parts}${inputs}concat=n=${segmentPaths.length}:v=1:a=0[out]`;

  args.push(
    '-filter_complex', filter,
    '-map', '[out]',
    '-c:v', 'libx264',
    '-pix_fmt', 'yuv420p',
    '-crf', '17',
    '-preset', 'medium',
    outPath,
  );

  await run(bin, args, { timeoutMs: 600000 });
  return outPath;
}

/** Duration in seconds, or null if it cannot be read. */
async function duration(videoPath) {
  const bin = await locate();
  if (!bin) return null;
  try {
    const { stderr } = await run(bin, ['-i', videoPath]).catch((e) => ({ stderr: e.stderr || '' }));
    const m = /Duration:\s*(\d+):(\d+):(\d+\.?\d*)/.exec(stderr || '');
    if (!m) return null;
    return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
  } catch {
    return null;
  }
}

module.exports = { locate, reset, lastFrameDataUrl, concat, duration };
