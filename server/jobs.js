/**
 * Job queue.
 *
 * Video generation runs for minutes, well past any sane HTTP timeout, so
 * requests enqueue a job and the client polls. State lives in memory; finished
 * videos and their metadata are written to disk so they survive a restart even
 * though the queue itself does not.
 */

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');

const providers = require('./providers');
const ffmpeg = require('./ffmpeg');

const STATUS = {
  QUEUED: 'queued',
  RUNNING: 'running',
  DONE: 'done',
  ERROR: 'error',
  CANCELLED: 'cancelled',
};

class JobQueue extends EventEmitter {
  constructor(config) {
    super();
    this.config = config;
    this.jobs = new Map();
    this.pending = [];
    this.active = 0;
    fs.mkdirSync(config.outputDir, { recursive: true });
    this.#loadFromDisk();
  }

  /** Restore previously completed jobs so the gallery survives a restart. */
  #loadFromDisk() {
    let entries = [];
    try {
      entries = fs.readdirSync(this.config.outputDir).filter((f) => f.endsWith('.json'));
    } catch {
      return;
    }
    for (const file of entries) {
      try {
        const meta = JSON.parse(fs.readFileSync(path.join(this.config.outputDir, file), 'utf8'));
        if (meta && meta.id) {
          const videoPath = path.join(this.config.outputDir, `${meta.id}.mp4`);
          if (fs.existsSync(videoPath)) this.jobs.set(meta.id, meta);
        }
      } catch { /* skip unreadable metadata */ }
    }
  }

  create(spec) {
    const id = crypto.randomUUID();
    const job = {
      id,
      status: STATUS.QUEUED,
      createdAt: new Date().toISOString(),
      startedAt: null,
      finishedAt: null,
      provider: spec.provider,
      model: spec.model,
      subject: spec.subject,
      prompt: spec.prompt,
      negativePrompt: spec.negativePrompt,
      params: spec.params || {},
      initImage: spec.initImage || null,
      compiled: spec.compiled || null,
      plan: spec.plan || null,
      quality: spec.quality || null,
      targetFps: spec.targetFps || null,
      error: null,
      videoUrl: null,
      attempts: [],
    };
    this.jobs.set(id, job);
    this.pending.push(id);
    this.emit('update', job);
    this.#drain();
    this.#prune();
    return job;
  }

  get(id) {
    return this.jobs.get(id) || null;
  }

  list() {
    return [...this.jobs.values()]
      .sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
      .map((j) => this.#publicView(j));
  }

  cancel(id) {
    const job = this.jobs.get(id);
    if (!job) return null;
    if (job.status === STATUS.QUEUED) {
      this.pending = this.pending.filter((p) => p !== id);
      job.status = STATUS.CANCELLED;
      job.finishedAt = new Date().toISOString();
      this.emit('update', job);
    } else if (job.status === STATUS.RUNNING && job.controller) {
      job.controller.abort();
    }
    return this.#publicView(job);
  }

  /** Strip internals that should not cross the API boundary. */
  #publicView(job) {
    const { controller, initImage, ...rest } = job;
    return { ...rest, hasInitImage: Boolean(initImage) };
  }

  #prune() {
    const limit = this.config.retainJobs;
    const all = [...this.jobs.values()].sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
    for (const job of all.slice(limit)) {
      this.jobs.delete(job.id);
    }
  }

  #drain() {
    while (this.active < this.config.maxConcurrent && this.pending.length) {
      const id = this.pending.shift();
      const job = this.jobs.get(id);
      if (!job || job.status !== STATUS.QUEUED) continue;
      this.active += 1;
      this.#run(job).finally(() => {
        this.active -= 1;
        this.#drain();
      });
    }
  }

  /**
   * Generate one segment. The first is produced from the prompt; later ones
   * resume from the previous segment's final frame via image-to-video.
   */
  async #runSegment(job, index, startFrame) {
    const spec = {
      ...job,
      model: index === 0 ? job.model : (job.plan?.continuation || job.model),
      initImage: index === 0 ? job.initImage : startFrame,
      params: { ...job.params, num_frames: job.plan?.framesPerSegment || job.params.num_frames },
    };

    return providers.generate(spec, this.config, {
      signal: job.controller.signal,
      onRetry: ({ attempt, backoff, error }) => {
        job.attempts.push({
          attempt, backoff, segment: index + 1, error: error.message, at: new Date().toISOString(),
        });
        this.emit('update', job);
      },
    });
  }

  async #run(job) {
    job.status = STATUS.RUNNING;
    job.startedAt = new Date().toISOString();
    job.controller = new AbortController();
    this.emit('update', job);

    // Chained jobs need proportionally longer before the timeout bites.
    // A configured 0 means no deadline at all; the job then runs until it
    // finishes, fails, or is cancelled from the UI.
    const segmentCount = job.plan?.segments || 1;
    const timeout = this.config.jobTimeoutMs > 0
      ? this.config.jobTimeoutMs * Math.max(1, segmentCount)
      : 0;
    job.timedOut = false;
    const timer = timeout > 0
      ? setTimeout(() => {
        job.timedOut = true;
        job.controller.abort();
      }, timeout)
      : null;

    const scratch = [];

    try {
      const videoPath = path.join(this.config.outputDir, `${job.id}.mp4`);

      if (segmentCount === 1) {
        const video = await this.#runSegment(job, 0, null);
        await fsp.writeFile(videoPath, video);
      } else {
        // Build the clip a segment at a time, carrying the last frame forward.
        let startFrame = job.initImage || null;

        for (let i = 0; i < segmentCount; i += 1) {
          job.segmentProgress = { current: i + 1, total: segmentCount };
          this.emit('update', job);

          const video = await this.#runSegment(job, i, startFrame);
          const segPath = path.join(this.config.outputDir, `${job.id}.seg${i}.mp4`);
          await fsp.writeFile(segPath, video);
          scratch.push(segPath);

          if (i < segmentCount - 1) {
            startFrame = await ffmpeg.lastFrameDataUrl(segPath);
          }
        }

        job.segmentProgress = { current: segmentCount, total: segmentCount, stitching: true };
        this.emit('update', job);
        await ffmpeg.concat(scratch, videoPath);
      }

      // Interpolate last, once the whole clip exists — doing it per segment
      // would smooth each piece but leave the joins at the original rate.
      if (job.targetFps && job.targetFps > (job.params.fps || 16)) {
        job.segmentProgress = { interpolating: true, to: job.targetFps };
        this.emit('update', job);

        const smooth = path.join(this.config.outputDir, `${job.id}.smooth.mp4`);
        try {
          await ffmpeg.interpolate(videoPath, smooth, job.targetFps);
          await fsp.rename(smooth, videoPath);
          job.interpolatedTo = job.targetFps;
        } catch (err) {
          // The clip itself is fine; only the smoothing failed.
          job.warning = `Generated successfully, but interpolation to `
            + `${job.targetFps}fps failed: ${err.message}`;
          await fsp.unlink(smooth).catch(() => {});
        }
      }

      const video = await fsp.readFile(videoPath);

      job.status = STATUS.DONE;
      job.finishedAt = new Date().toISOString();
      job.videoUrl = `/api/video/${job.id}`;
      job.sizeBytes = video.length;

      await fsp.writeFile(
        path.join(this.config.outputDir, `${job.id}.json`),
        JSON.stringify(this.#publicView(job), null, 2),
      );
    } catch (err) {
      const aborted = err.name === 'AbortError';
      job.status = aborted && !job.timedOut ? STATUS.CANCELLED : STATUS.ERROR;

      if (aborted && job.timedOut) {
        const minutes = Math.round(timeout / 60000);
        job.error = `Gave up after ${minutes} minutes. On a first local run most of that is `
          + 'the model download — check the GPU server window, and if it is still working, '
          + 'raise JOB_TIMEOUT_MS in .env and try again once the download finishes.';
      } else if (aborted) {
        job.error = 'Cancelled.';
      } else {
        job.error = err.message || 'Generation failed for an unknown reason.';
      }
      job.finishedAt = new Date().toISOString();
    } finally {
      clearTimeout(timer);
      delete job.controller;
      delete job.segmentProgress;
      // Intermediate segments are only needed until the stitch completes.
      await Promise.all(scratch.map((p) => fsp.unlink(p).catch(() => {})));
      this.emit('update', job);
    }
  }

  /**
   * Remove a job and everything it wrote. Running jobs are cancelled first so
   * the file handles are released before the unlink.
   */
  async remove(id) {
    const job = this.jobs.get(id);
    if (!job) return false;

    if (job.status === STATUS.RUNNING || job.status === STATUS.QUEUED) {
      this.cancel(id);
    }

    this.jobs.delete(id);
    this.pending = this.pending.filter((p) => p !== id);

    await Promise.all([
      fsp.unlink(path.join(this.config.outputDir, `${id}.mp4`)).catch(() => {}),
      fsp.unlink(path.join(this.config.outputDir, `${id}.json`)).catch(() => {}),
    ]);

    this.emit('removed', id);
    return true;
  }

  /** Delete every finished job. Running work is left alone. */
  async clear() {
    const finished = [...this.jobs.values()].filter(
      (j) => j.status !== STATUS.RUNNING && j.status !== STATUS.QUEUED,
    );
    for (const job of finished) {
      await this.remove(job.id);
    }
    return finished.length;
  }
}

module.exports = { JobQueue, STATUS };
