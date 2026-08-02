/**
 * HTTP client with no request timeout.
 *
 * Node's built-in fetch (undici) defaults to a 300 second headersTimeout. A
 * video model does not send response headers until the whole clip is rendered,
 * and on modest hardware — or while the weights are still downloading — that
 * takes far longer than five minutes. The request dies with a bare
 * "fetch failed" that looks like a network fault but is really a clock.
 *
 * Undici's dispatcher is not configurable from the standard library, so this
 * wraps node:http/node:https directly and presents the parts of the fetch
 * Response interface the rest of the app uses. That keeps call sites and tests
 * unchanged while removing the ceiling.
 */

const http = require('node:http');
const https = require('node:https');
const { URL } = require('node:url');

const MAX_REDIRECTS = 5;

class HttpError extends Error {
  constructor(message, code) {
    super(message);
    this.name = 'HttpError';
    this.code = code;
  }
}

/** Minimal fetch-Response shape over a collected Buffer. */
function makeResponse(status, headers, body, url) {
  const lower = {};
  for (const [k, v] of Object.entries(headers)) {
    lower[k.toLowerCase()] = Array.isArray(v) ? v.join(', ') : v;
  }
  return {
    ok: status >= 200 && status < 300,
    status,
    url,
    headers: { get: (name) => lower[String(name).toLowerCase()] ?? null },
    arrayBuffer: async () => body,
    text: async () => body.toString('utf8'),
    json: async () => JSON.parse(body.toString('utf8')),
  };
}

/**
 * Perform a request. Mirrors the fetch signature closely enough to stand in
 * for it: `request(url, { method, headers, body, signal })`.
 *
 * `timeoutMs` defaults to 0, meaning no limit — the caller is expected to
 * enforce its own deadline via `signal`, which the job queue already does.
 */
function request(url, options = {}, redirectsLeft = MAX_REDIRECTS) {
  const {
    method = 'GET',
    headers = {},
    body = null,
    signal = null,
    timeoutMs = 0,
  } = options;

  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(Object.assign(new Error('Aborted'), { name: 'AbortError' }));
      return;
    }

    let target;
    try {
      target = new URL(url);
    } catch {
      reject(new HttpError(`Invalid URL: ${url}`, 'ERR_INVALID_URL'));
      return;
    }

    const transport = target.protocol === 'https:' ? https : http;
    const payload = body == null
      ? null
      : (Buffer.isBuffer(body) ? body : Buffer.from(String(body)));

    const sendHeaders = { ...headers };
    if (payload && sendHeaders['Content-Length'] == null) {
      sendHeaders['Content-Length'] = String(payload.length);
    }

    const req = transport.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || (target.protocol === 'https:' ? 443 : 80),
        path: `${target.pathname}${target.search}`,
        method,
        headers: sendHeaders,
      },
      (res) => {
        // Follow redirects so provider result URLs resolve.
        const location = res.headers.location;
        if (location && res.statusCode >= 300 && res.statusCode < 400 && redirectsLeft > 0) {
          res.resume();
          const next = new URL(location, target).toString();
          // A redirected POST becomes a GET, matching browser behaviour for 303
          // and the common practice for 301/302.
          const nextMethod = res.statusCode === 307 || res.statusCode === 308 ? method : 'GET';
          const nextBody = nextMethod === method ? body : null;
          resolve(request(next, { ...options, method: nextMethod, body: nextBody }, redirectsLeft - 1));
          return;
        }

        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          cleanup();
          resolve(makeResponse(res.statusCode, res.headers, Buffer.concat(chunks), url));
        });
        res.on('error', (err) => {
          cleanup();
          reject(err);
        });
      },
    );

    const onAbort = () => {
      req.destroy();
      const err = new Error('Aborted');
      err.name = 'AbortError';
      reject(err);
    };

    function cleanup() {
      if (signal) signal.removeEventListener('abort', onAbort);
    }

    if (signal) signal.addEventListener('abort', onAbort, { once: true });

    // Explicitly disable the socket timeout. Without this, a long generation
    // looks identical to a hung connection.
    req.setTimeout(timeoutMs);

    req.on('timeout', () => {
      if (timeoutMs > 0) {
        req.destroy(new HttpError(`Request timed out after ${timeoutMs}ms`, 'ETIMEDOUT'));
      }
    });

    req.on('error', (err) => {
      cleanup();
      if (err.name === 'AbortError') return; // already rejected
      reject(describe(err, target));
    });

    if (payload) req.write(payload);
    req.end();
  });
}

/** Turn a socket-level failure into something a user can act on. */
function describe(err, target) {
  const where = `${target.hostname}:${target.port || (target.protocol === 'https:' ? 443 : 80)}`;
  switch (err.code) {
    case 'ECONNREFUSED':
      return new HttpError(
        `Nothing is listening at ${where}. If this is the local GPU server, check that its window is still running.`,
        err.code,
      );
    case 'ECONNRESET':
      return new HttpError(
        `The connection to ${where} was closed before a reply arrived. If this is the local GPU server, it likely ran out of memory — check its window for the error.`,
        err.code,
      );
    case 'ENOTFOUND':
      return new HttpError(`Could not resolve ${target.hostname}.`, err.code);
    case 'ETIMEDOUT':
      return new HttpError(`Timed out connecting to ${where}.`, err.code);
    default:
      return err;
  }
}

module.exports = { request, HttpError };
