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

const { request: httpRequest } = require('./httpclient');

const HF_ROUTER = 'https://router.huggingface.co';
const HF_LEGACY = 'https://api-inference.huggingface.co';
const HF_MODEL_API = 'https://huggingface.co/api/models';

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
 * Ask the Hub which inference providers actually serve a model.
 *
 * `hf-inference` is Hugging Face's own serverless pool and it does not host
 * large video models — those live on partner providers (fal.ai, Replicate,
 * Novita) that the router forwards to. Rather than hardcode a guess that goes
 * stale, look the mapping up and route accordingly.
 *
 * Returns [{ provider, providerId, status, task }], or [] if the lookup fails
 * (offline, rate limited, private model) so callers can fall back.
 */
async function fetchProviderMapping(model, config, { fetchImpl = httpRequest, signal, strict = false } = {}) {
  const url = `${HF_MODEL_API}/${model}?expand[]=inferenceProviderMapping`;
  const headers = config.hfToken ? { Authorization: `Bearer ${config.hfToken}` } : {};

  let json;
  try {
    const res = await fetchImpl(url, { headers, signal });
    if (!res.ok) {
      // `strict` is for the probe, which must tell "no providers" apart from
      // "could not ask". Routing wants the quiet fallback instead.
      if (strict) throw await toError(res, url);
      return [];
    }
    json = await res.json();
  } catch (err) {
    if (strict) throw err;
    return [];
  }

  const mapping = json?.inferenceProviderMapping;
  if (!mapping) return [];

  // The Hub has returned this as both a keyed object and an array; accept either.
  const rows = Array.isArray(mapping)
    ? mapping
    : Object.entries(mapping).map(([provider, v]) => ({ provider, ...v }));

  return rows
    .filter((r) => r && (r.provider || r.providerId))
    .map((r) => ({
      provider: r.provider,
      providerId: r.providerId || model,
      status: r.status || 'unknown',
      task: r.task || null,
    }));
}

/**
 * Build the request for a specific provider. Each partner exposes its own path
 * and body shape through the router, so they cannot share one format.
 */
function buildProviderRequest(provider, providerId, job) {
  const image = job.initImage ? toBareBase64(job.initImage) : null;
  const params = job.params || {};

  switch (provider) {
    case 'replicate':
      return {
        url: `${HF_ROUTER}/replicate/v1/models/${providerId}/predictions`,
        // Ask Replicate to hold the connection instead of returning a job to poll.
        headers: { Prefer: 'wait' },
        body: {
          input: {
            prompt: job.prompt,
            negative_prompt: job.negativePrompt || undefined,
            ...(job.initImage ? { image: job.initImage } : {}),
            ...params,
          },
        },
      };

    case 'hf-inference':
      return {
        url: `${HF_ROUTER}/hf-inference/models/${providerId}`,
        headers: {},
        body: {
          inputs: job.prompt,
          parameters: {
            negative_prompt: job.negativePrompt || undefined,
            ...(image ? { image } : {}),
            ...params,
          },
          options: { wait_for_model: true, use_cache: false },
        },
      };

    // fal-ai, novita, and the rest take a flat body at the provider root.
    default:
      return {
        url: `${HF_ROUTER}/${provider}/${providerId}`,
        headers: {},
        body: {
          prompt: job.prompt,
          negative_prompt: job.negativePrompt || undefined,
          ...(job.initImage ? { image_url: job.initImage } : {}),
          ...params,
        },
      };
  }
}

/**
 * Hugging Face path. Resolves which provider serves the model, then falls back
 * to the legacy inference host if nothing is mapped.
 */
async function callHuggingFace(job, config, opts = {}) {
  const { fetchImpl = httpRequest, signal } = opts;

  if (!config.hfToken) {
    throw new ProviderError('No HF_TOKEN set. Add one to .env — see README for how to create it.');
  }

  // Work out which providers can actually serve this model.
  const forced = job.hfProvider || config.hfProvider;
  let candidates = [];

  if (forced && forced !== 'auto') {
    candidates = [{ provider: forced, providerId: job.model, status: 'forced' }];
  } else {
    const mapping = await fetchProviderMapping(job.model, config, { fetchImpl, signal });
    // Live providers first; hf-inference last since it rarely hosts video.
    candidates = mapping
      .filter((m) => m.status !== 'error')
      .sort((a, b) => {
        if (a.status === 'live' && b.status !== 'live') return -1;
        if (b.status === 'live' && a.status !== 'live') return 1;
        if (a.provider === 'hf-inference') return 1;
        if (b.provider === 'hf-inference') return -1;
        return 0;
      });

    if (!candidates.length) {
      // No mapping available — try the historical hosts before giving up.
      candidates = [{ provider: 'hf-inference', providerId: job.model, status: 'assumed' }];
    }
  }

  const authHeaders = {
    Authorization: `Bearer ${config.hfToken}`,
    'Content-Type': 'application/json',
    Accept: 'video/mp4, application/json',
  };

  let lastError = null;
  const tried = [];

  for (const candidate of candidates) {
    const req = buildProviderRequest(candidate.provider, candidate.providerId, job);
    tried.push(candidate.provider);

    try {
      const res = await fetchImpl(req.url, {
        method: 'POST',
        headers: { ...authHeaders, ...req.headers },
        body: JSON.stringify(req.body),
        signal,
      });

      if (!res.ok) {
        lastError = await toError(res, req.url);
        // "Wrong door" answers mean try the next provider; real errors stop here.
        if (lastError.status === 404 || lastError.status === 400) continue;
        throw lastError;
      }

      return await extractVideo(res, (u) => fetchImpl(u, {
        headers: { Authorization: `Bearer ${config.hfToken}` },
        signal,
      }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      lastError = err instanceof ProviderError ? err : new ProviderError(err.message);
      if (lastError.status && lastError.status !== 404 && lastError.status !== 400) throw lastError;
    }
  }

  // Last resort: the legacy inference host, which predates provider routing.
  try {
    const res = await fetchImpl(`${HF_LEGACY}/models/${job.model}`, {
      method: 'POST',
      headers: authHeaders,
      body: JSON.stringify({
        inputs: job.prompt,
        parameters: {
          negative_prompt: job.negativePrompt || undefined,
          ...(job.initImage ? { image: toBareBase64(job.initImage) } : {}),
          ...job.params,
        },
        options: { wait_for_model: true, use_cache: false },
      }),
      signal,
    });
    if (res.ok) {
      return await extractVideo(res, (u) => fetchImpl(u, {
        headers: { Authorization: `Bearer ${config.hfToken}` },
        signal,
      }));
    }
  } catch (err) {
    if (err.name === 'AbortError') throw err;
  }

  const summary = tried.length ? tried.join(', ') : 'none';
  throw new ProviderError(
    `No provider served ${job.model} (tried: ${summary}). `
    + `Use the "Check availability" button to see which providers carry this model, `
    + `then set HF_PROVIDER in .env or pick a different model. `
    + `Last error: ${lastError ? lastError.message : 'none'}`,
    { status: lastError?.status, body: lastError?.body },
  );
}

/**
 * Custom endpoint path. Your URL, your auth header, your model.
 * The request body mirrors the HF shape so the same UI drives both, but
 * `CUSTOM_BODY_TEMPLATE` lets you reshape it for an endpoint that expects
 * something else.
 */
async function callCustom(job, config, { fetchImpl = httpRequest, signal } = {}) {
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

module.exports = {
  generate,
  callHuggingFace,
  callCustom,
  extractVideo,
  toBareBase64,
  fetchProviderMapping,
  buildProviderRequest,
  ProviderError,
};
