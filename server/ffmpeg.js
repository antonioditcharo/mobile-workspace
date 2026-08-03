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

/**
 * Look for a bundled ffmpeg inside the local server's Python environment.
 *
 * The environment lives outside the project folder so it survives updates, so
 * both locations have to be searched — checking only the in-project one meant
 * interpolation and stitching silently went missing on an up-to-date install.
 */
function findBundled() {
  const envRoots = [
    path.join(ROOT, 'local', '.venv'),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'realframe-venv'),
    process.env.HOME && path.join(process.env.HOME, '.local', 'share', 'realframe-venv'),
  ].filter(Boolean);

  const roots = [];
  for (const envRoot of envRoots) {
    roots.push(path.join(envRoot, 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries'));
    roots.push(path.join(envRoot, 'lib', 'site-packages', 'imageio_ffmpeg', 'binaries'));

    // Linux/mac venvs bury site-packages under a python version directory.
    try {
      for (const entry of fs.readdirSync(path.join(envRoot, 'lib'))) {
        roots.push(path.join(envRoot, 'lib', entry, 'site-packages', 'imageio_ffmpeg', 'binaries'));
      }
    } catch { /* not this one */ }
  }

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

/**
 * Raise the frame rate by synthesising intermediate frames.
 *
 * Video models generate at 8-16fps because every frame costs GPU memory and
 * time, and the result reads as choppy however good the individual frames are.
 * Interpolating afterwards is far cheaper than generating more frames: it runs
 * on the CPU, costs no VRAM, and does not constrain clip length.
 *
 * `mci` is motion-compensated interpolation — it estimates where things moved
 * and synthesises the in-between, rather than blending or duplicating frames.
 * Slower than the alternatives and worth it; blending just looks like a smear.
 */
async function interpolate(inputPath, outputPath, targetFps, { onProgress } = {}) {
  const bin = await locate();
  if (!bin) throw new Error('ffmpeg not available.');

  const filter = `minterpolate=fps=${targetFps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1`;

  if (onProgress) onProgress(`Interpolating to ${targetFps}fps`);

  await run(bin, [
    '-y', '-i', inputPath,
    '-vf', filter,
    '-c:v', 'libx264',
    '-pix_fmt', 'yuv420p',
    '-crf', '17',
    '-preset', 'medium',
    outputPath,
  ], { timeoutMs: 1800000 });

  return outputPath;
}

/** Frames per second, or null if it cannot be read. */
async function frameRate(videoPath) {
  const bin = await locate();
  if (!bin) return null;
  try {
    const { stderr } = await run(bin, ['-i', videoPath]).catch((e) => ({ stderr: e.stderr || '' }));
    const m = /,\s*([\d.]+)\s*fps/.exec(stderr || '');
    return m ? parseFloat(m[1]) : null;
  } catch {
    return null;
  }
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

module.exports = { locate, reset, lastFrameDataUrl, concat, interpolate, duration, frameRate };
