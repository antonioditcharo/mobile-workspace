/**
 * Inference worker.
 *
 * Moves model loading and the observation passes off the main thread. Two
 * reasons, in order of how much they matter:
 *
 *  1. The UI stays responsive. Eight-plus generate() calls on the main thread
 *     lock the page for the entire run — on a CPU-only phone that is minutes of
 *     frozen interface.
 *  2. A worker is more likely to survive a brief hide than main-thread work, and
 *     it is the only structure in which resuming a partial run is clean.
 *
 * It does NOT give true background execution: when the OS backgrounds the app it
 * freezes the whole renderer, workers included. Nothing in a browser can avoid
 * that, which is why the page also checkpoints every pass and can resume.
 *
 * Falls back to in-page inference if this worker cannot start — see app.js.
 */

import { loadVision, observe, describeError } from './vision.js';

let engine = null;

function post(type, payload = {}) {
  self.postMessage({ type, ...payload });
}

self.addEventListener('message', async (event) => {
  const message = event.data || {};

  try {
    if (message.type === 'load') {
      engine = await loadVision(message.choice, {
        onProgress: (progress) => post('progress', { progress }),
      });
      post('loaded');
      return;
    }

    if (message.type === 'observe') {
      if (!engine) throw new Error('Model is not loaded');
      const { observation, failures, clipped } = await observe(engine, message.pixels, {
        startIndex: message.startIndex || 0,
        observation: message.observation || {},
        onProgress: (progress) => post('progress', { progress }),
        onPass: (pass) => post('pass', { pass }),
      });
      post('observed', { observation, failures, clipped });
      return;
    }

    throw new Error(`Unknown message type: ${message.type}`);
  } catch (err) {
    post('error', { message: describeError(err), raw: String(err?.message || err) });
  }
});

post('ready');
