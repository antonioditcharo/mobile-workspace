/**
 * Run orchestration: where the model actually gets driven, and everything that
 * makes an interrupted run survivable.
 *
 * WHAT IS AND IS NOT POSSIBLE HERE
 *
 * True background processing does not exist for a web app on a phone. When the
 * OS backgrounds the app it freezes the renderer process — main thread, workers
 * and all. Service workers are not an escape hatch: they are event-driven, get
 * killed after roughly 30 seconds of inactivity, and are not a place to run a
 * several-hundred-megabyte model. So this module does not pretend to keep
 * working while the app is away. Instead it attacks the three things that
 * actually cost the user time:
 *
 *  1. A screen wake lock, so putting the phone down does not stop the run. This
 *     is the single biggest practical win, because "backgrounded" is usually
 *     just the screen going to sleep.
 *  2. A worker, so the phone stays usable during a run instead of the UI
 *     freezing for minutes.
 *  3. A checkpoint after every pass, so if the OS does freeze or kill the app,
 *     coming back costs one pass instead of the entire image.
 */

import { loadVision, observe, describeError, PASSES } from './vision.js';

const RUN_KEY = 'promptforge.run.v1';
/** A checkpoint older than this is stale enough to be worthless. */
const RUN_TTL_MS = 24 * 60 * 60 * 1000;

/* ------------------------------------------------------------------ *
 * Checkpointing
 * ------------------------------------------------------------------ */

/**
 * Persist partial progress. Stores the downscaled preview rather than the raw
 * pixels: it is a fraction of the size, and re-decoding one JPEG on resume is
 * trivial next to redoing model passes.
 */
export function saveCheckpoint(record) {
  try {
    localStorage.setItem(RUN_KEY, JSON.stringify({ ...record, at: Date.now() }));
  } catch {
    /* storage full or unavailable — checkpointing is a bonus, not a requirement */
  }
}

export function loadCheckpoint() {
  try {
    const raw = localStorage.getItem(RUN_KEY);
    if (!raw) return null;
    const record = JSON.parse(raw);
    if (!record?.dataUrl || typeof record.nextIndex !== 'number') return null;
    if (Date.now() - (record.at || 0) > RUN_TTL_MS) {
      clearCheckpoint();
      return null;
    }
    if (record.nextIndex >= PASSES.length) {
      clearCheckpoint();
      return null;
    }
    return record;
  } catch {
    return null;
  }
}

export function clearCheckpoint() {
  try {
    localStorage.removeItem(RUN_KEY);
  } catch {
    /* nothing useful to do */
  }
}

/* ------------------------------------------------------------------ *
 * Screen wake lock
 * ------------------------------------------------------------------ */

let wakeLock = null;

/**
 * Keep the screen awake for the duration of a run.
 *
 * The OS backgrounds an app when the screen sleeps, so without this a run stops
 * the moment the phone is set down. Best-effort: unsupported on some browsers,
 * and the lock is dropped by the system whenever the page is hidden, so it is
 * re-acquired on becoming visible again.
 */
export async function acquireWakeLock() {
  try {
    if (!('wakeLock' in navigator)) return false;
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener?.('release', () => {
      wakeLock = null;
    });
    return true;
  } catch {
    wakeLock = null;
    return false;
  }
}

export async function releaseWakeLock() {
  try {
    await wakeLock?.release();
  } catch {
    /* already released */
  }
  wakeLock = null;
}

export function hasWakeLock() {
  return Boolean(wakeLock);
}

export function wakeLockSupported() {
  return typeof navigator !== 'undefined' && 'wakeLock' in navigator;
}

/* ------------------------------------------------------------------ *
 * Engine: worker, with an in-page fallback
 * ------------------------------------------------------------------ */

/**
 * Try to run in a worker; fall back to the main thread if that is not possible.
 *
 * The fallback matters. A worker needs a sibling module file, so it is
 * unavailable in the standalone single-file build, and module workers are not
 * universally supported. Falling back keeps a working app in both cases rather
 * than trading a slow run for no run.
 */
export function createWorkerRunner(url = new URL('./worker.js', import.meta.url)) {
  let worker;
  try {
    worker = new Worker(url, { type: 'module' });
  } catch {
    return null;
  }

  const pending = new Map();
  let seq = 0;
  let onProgress = () => {};
  let onPass = () => {};

  worker.addEventListener('message', (event) => {
    const { type, progress, pass } = event.data || {};
    if (type === 'progress') return onProgress(progress);
    if (type === 'pass') return onPass(pass);

    // Resolve whichever request is outstanding; only one runs at a time.
    const entry = pending.get('current');
    if (!entry) return;
    if (type === 'error') {
      pending.delete('current');
      entry.reject(new Error(event.data.message || 'Worker error'));
    } else if (type === entry.expect) {
      pending.delete('current');
      entry.resolve(event.data);
    }
  });

  worker.addEventListener('error', (event) => {
    const entry = pending.get('current');
    if (entry) {
      pending.delete('current');
      entry.reject(new Error(event.message || 'Worker failed to start'));
    }
  });

  function request(message, expect, transfer = []) {
    return new Promise((resolve, reject) => {
      seq += 1;
      pending.set('current', { resolve, reject, expect, id: seq });
      worker.postMessage(message, transfer);
    });
  }

  return {
    kind: 'worker',
    setHandlers(handlers) {
      onProgress = handlers.onProgress || (() => {});
      onPass = handlers.onPass || (() => {});
    },
    async load(choice) {
      await request({ type: 'load', choice }, 'loaded');
    },
    async observe({ pixels, startIndex, observation }) {
      // The pixel buffer is transferred, not copied — it is several megabytes.
      const result = await request(
        { type: 'observe', pixels, startIndex, observation },
        'observed',
        [pixels.data.buffer],
      );
      return { observation: result.observation, failures: result.failures, clipped: result.clipped };
    },
    terminate() {
      worker.terminate();
    },
  };
}

/** Same interface, running on the main thread. */
export function createInlineRunner() {
  let engine = null;
  let handlers = {};

  return {
    kind: 'inline',
    setHandlers(next) {
      handlers = next || {};
    },
    async load(choice) {
      engine = await loadVision(choice, { onProgress: handlers.onProgress });
    },
    async observe({ pixels, startIndex, observation, signal }) {
      if (!engine) throw new Error('Model is not loaded');
      return observe(engine, pixels, {
        startIndex,
        observation,
        signal,
        onProgress: handlers.onProgress,
        onPass: handlers.onPass,
      });
    },
    terminate() {
      engine = null;
    },
  };
}

/**
 * Pick a runner, preferring the worker. Verifies the worker can actually load
 * the model before committing to it, so a broken worker degrades to inline
 * rather than failing the run.
 */
export async function createRunner(choice, handlers, { preferWorker = true } = {}) {
  if (preferWorker) {
    const worker = createWorkerRunner();
    if (worker) {
      worker.setHandlers(handlers);
      try {
        await worker.load(choice);
        return worker;
      } catch (err) {
        worker.terminate();
        handlers.onFallback?.(describeError(err));
      }
    }
  }

  const inline = createInlineRunner();
  inline.setHandlers(handlers);
  await inline.load(choice);
  return inline;
}
