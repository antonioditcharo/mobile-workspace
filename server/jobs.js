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

  async #run(job) {
    job.status = STATUS.RUNNING;
    job.startedAt = new Date().toISOString();
    job.controller = new AbortController();
    this.emit('update', job);

    const timer = setTimeout(() => job.controller.abort(), this.config.jobTimeoutMs);

    try {
      const video = await providers.generate(job, this.config, {
        signal: job.controller.signal,
        onRetry: ({ attempt, backoff, error }) => {
          job.attempts.push({ attempt, backoff, error: error.message, at: new Date().toISOString() });
          this.emit('update', job);
        },
      });

      const videoPath = path.join(this.config.outputDir, `${job.id}.mp4`);
      await fsp.writeFile(videoPath, video);

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
      job.status = aborted ? STATUS.CANCELLED : STATUS.ERROR;
      job.error = aborted
        ? 'Cancelled or timed out.'
        : err.message || 'Generation failed for an unknown reason.';
      job.finishedAt = new Date().toISOString();
    } finally {
      clearTimeout(timer);
      delete job.controller;
      this.emit('update', job);
    }
  }
}

module.exports = { JobQueue, STATUS };
