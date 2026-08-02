/**
 * Inference backends.
 *
 * Two paths:
 *
 *   hf     — Hugging Face inference. Routes through router.huggingface.co with a
 *            fallback to the legacy api-inference host, since which one answers
 *            for a given model/provider combination changes over time.
 *
 *   custom — Any HTTP endpoint you control: a local ComfyUI wrapper, a self-
 *            hosted diffusers server, a rented GPU box, a dedicated HF Inference
 *            Endpoint. Nothing sits between you and your own weights on this
 *            path — no third-party terms, no shared queue, no rate limit but
 *            your own hardware.
 *
 * Responses vary by host, so `extractVideo` is deliberately permissive: raw
 * video bytes, base64 in JSON, or a URL to fetch all resolve to a Buffer.
 */

const HF_ROUTER = 'https://router.huggingface.co';
const HF_LEGACY = 'https://api-inference.huggingface.co';

class ProviderError extends Error {
  constructor(message, { status, retryable = false, body } = {}) {
    super(message);
    this.name = 'ProviderError';
    this.status = status;
    this.retryable = retryable;
    this.body = body;
  }
}

const VIDEO_MIME = /^(video\/|application\/octet-stream)/;

const B64_KEYS = ['video', 'video_base64', 'data', 'b64_json', 'output', 'generated_video'];
const URL_KEYS = ['url', 'video_url', 'output_url', 'video'];

/**
 * Browsers hand back start frames as `data:image/png;base64,…`, but inference
 * hosts expect the payload alone. Strip the prefix on the way out.
 */
function toBareBase64(str) {
  if (typeof str !== 'string') return str;
  const comma = str.indexOf(',');
  return str.startsWith('data:') && comma !== -1 ? str.slice(comma + 1) : str;
}

function decodeBase64(str) {
  const payload = str.includes(',') && str.startsWith('data:') ? str.split(',')[1] : str;
  const buf = Buffer.from(payload, 'base64');
  if (!buf.length) throw new ProviderError('Provider returned an empty video payload.');
  return buf;
}

/** Walk a JSON response looking for something that resolves to video bytes. */
async function extractFromJson(json, fetchImpl) {
  const seen = new Set();

  const visit = async (node, depth) => {
    if (!node || depth > 6 || seen.has(node)) return null;
    if (typeof node === 'object') seen.add(node);

    if (typeof node === 'string') {
      if (/^https?:\/\//.test(node) && /\.(mp4|webm|mov|gif)(\?|$)/i.test(node)) {
        const res = await fetchImpl(node);
        if (!res.ok) throw new ProviderError(`Could not download result video (HTTP ${res.status}).`);
        return Buffer.from(await res.arrayBuffer());
      }
      // Long opaque strings are almost certainly base64 video.
      if (node.length > 2048 && /^[A-Za-z0-9+/=\s]+$/.test(node.slice(0, 256))) {
        return decodeBase64(node);
      }
      return null;
    }

    if (Array.isArray(node)) {
      for (const item of node) {
        const hit = await visit(item, depth + 1);
        if (hit) return hit;
      }
      return null;
    }

    if (typeof node === 'object') {
      for (const key of URL_KEYS) {
        const val = node[key];
        if (typeof val === 'string' && /^https?:\/\//.test(val)) {
          const res = await fetchImpl(val);
          if (!res.ok) throw new ProviderError(`Could not download result video (HTTP ${res.status}).`);
          return Buffer.from(await res.arrayBuffer());
        }
      }
      for (const key of B64_KEYS) {
        const val = node[key];
        if (typeof val === 'string' && val.length > 512) return decodeBase64(val);
      }
      for (const val of Object.values(node)) {
        const hit = await visit(val, depth + 1);
        if (hit) return hit;
      }
    }
    return null;
  };

  return visit(json, 0);
}

/** Normalize any successful response into a video Buffer. */
async function extractVideo(res, fetchImpl) {
  const contentType = res.headers.get('content-type') || '';

  if (VIDEO_MIME.test(contentType)) {
    const buf = Buffer.from(await res.arrayBuffer());
    if (!buf.length) throw new ProviderError('Provider returned an empty response body.');
    return buf;
  }

  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    throw new ProviderError(
      `Unexpected response from provider (content-type: ${contentType || 'none'}).`,
      { body: text.slice(0, 500) },
    );
  }

  const found = await extractFromJson(json, fetchImpl);
  if (found) return found;

  throw new ProviderError('Provider response contained no video payload.', {
    body: JSON.stringify(json).slice(0, 500),
  });
}

/** Turn a non-2xx response into a ProviderError with an actionable message. */
async function toError(res, url) {
  const text = await res.text().catch(() => '');
  let detail = text.slice(0, 400);
  try {
    const parsed = JSON.parse(text);
    detail = parsed.error?.message || parsed.error || parsed.message || detail;
    if (typeof detail === 'object') detail = JSON.stringify(detail);
  } catch { /* keep the raw text */ }

  const status = res.status;
  let message = `Provider returned HTTP ${status}: ${detail || 'no detail'}`;
  let retryable = false;

  if (status === 401 || status === 403) {
    message = `Authentication failed (HTTP ${status}). Check HF_TOKEN — it needs "Make calls to Inference Providers" permission. Detail: ${detail}`;
  } else if (status === 404) {
    message = `Model or endpoint not found at ${url}. The model may not be served by any provider on your account. Detail: ${detail}`;
  } else if (status === 429) {
    message = `Rate limited or out of credits (HTTP 429). Detail: ${detail}`;
    retryable = true;
  } else if (status === 503) {
    message = `Model is loading or temporarily unavailable (HTTP 503). Detail: ${detail}`;
    retryable = true;
  } else if (status >= 500) {
    retryable = true;
  }

  return new ProviderError(message, { status, retryable, body: detail });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Hugging Face path. Tries the router first, then the legacy host, so a model
 * that has moved between the two still resolves without config changes.
 */
async function callHuggingFace(job, config, { fetchImpl = fetch, signal } = {}) {
  if (!config.hfToken) {
    throw new ProviderError('No HF_TOKEN set. Add one to .env — see README for how to create it.');
  }

  const payload = {
    inputs: job.prompt,
    parameters: {
      negative_prompt: job.negativePrompt || undefined,
      ...job.params,
    },
    options: { wait_for_model: true, use_cache: false },
  };
  if (job.initImage) {
    payload.parameters.image = toBareBase64(job.initImage);
  }

  const candidates = [
    `${HF_ROUTER}/hf-inference/models/${job.model}`,
    `${HF_LEGACY}/models/${job.model}`,
  ];

  let lastError = null;

  for (const url of candidates) {
    try {
      const res = await fetchImpl(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${config.hfToken}`,
          'Content-Type': 'application/json',
          Accept: 'video/mp4, application/json',
        },
        body: JSON.stringify(payload),
        signal,
      });

      if (!res.ok) {
        lastError = await toError(res, url);
        // A 404 means "wrong host, try the next one"; anything else is real.
        if (lastError.status === 404) continue;
        throw lastError;
      }

      return await extractVideo(res, (u) => fetchImpl(u, {
        headers: { Authorization: `Bearer ${config.hfToken}` },
        signal,
      }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      lastError = err instanceof ProviderError ? err : new ProviderError(err.message);
      if (lastError.status && lastError.status !== 404) throw lastError;
    }
  }

  throw lastError || new ProviderError('No Hugging Face endpoint responded.');
}

/**
 * Custom endpoint path. Your URL, your auth header, your model.
 * The request body mirrors the HF shape so the same UI drives both, but
 * `CUSTOM_BODY_TEMPLATE` lets you reshape it for an endpoint that expects
 * something else.
 */
async function callCustom(job, config, { fetchImpl = fetch, signal } = {}) {
  if (!config.customEndpoint) {
    throw new ProviderError('Provider is set to "custom" but CUSTOM_ENDPOINT is not configured.');
  }

  let body = {
    inputs: job.prompt,
    prompt: job.prompt,
    negative_prompt: job.negativePrompt || undefined,
    model: job.model || undefined,
    parameters: { negative_prompt: job.negativePrompt || undefined, ...job.params },
    ...job.params,
  };
  if (job.initImage) {
    // Self-hosted wrappers vary on which they accept, so send both shapes.
    body.image = toBareBase64(job.initImage);
    body.image_data_url = job.initImage;
    body.parameters.image = body.image;
  }

  if (config.customBodyTemplate) {
    try {
      const rendered = config.customBodyTemplate
        .replace(/\{\{prompt\}\}/g, JSON.stringify(job.prompt).slice(1, -1))
        .replace(/\{\{negative_prompt\}\}/g, JSON.stringify(job.negativePrompt || '').slice(1, -1))
        .replace(/\{\{model\}\}/g, job.model || '');
      body = JSON.parse(rendered);
    } catch (err) {
      throw new ProviderError(`CUSTOM_BODY_TEMPLATE is not valid JSON after substitution: ${err.message}`);
    }
  }

  const headers = { 'Content-Type': 'application/json', Accept: 'video/mp4, application/json' };
  if (config.customAuthHeader && config.customAuthValue) {
    headers[config.customAuthHeader] = config.customAuthValue;
  }

  const res = await fetchImpl(config.customEndpoint, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) throw await toError(res, config.customEndpoint);
  return extractVideo(res, (u) => fetchImpl(u, { signal }));
}

/**
 * Run a job against the configured provider, retrying the errors that are
 * worth retrying (cold model, rate limit, upstream 5xx).
 */
async function generate(job, config, opts = {}) {
  const provider = job.provider || config.provider || 'hf';
  const call = provider === 'custom' ? callCustom : callHuggingFace;
  const maxAttempts = opts.maxAttempts ?? 3;

  let lastError;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      return await call(job, config, opts);
    } catch (err) {
      lastError = err;
      if (err.name === 'AbortError') throw err;
      if (!(err instanceof ProviderError) || !err.retryable || attempt === maxAttempts) throw err;
      const backoff = 2000 * 2 ** (attempt - 1);
      if (opts.onRetry) opts.onRetry({ attempt, backoff, error: err });
      await sleep(backoff);
    }
  }
  throw lastError;
}

module.exports = { generate, callHuggingFace, callCustom, extractVideo, toBareBase64, ProviderError };
