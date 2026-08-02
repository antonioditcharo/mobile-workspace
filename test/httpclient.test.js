const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');

const { request } = require('../server/httpclient');

/** Spin up a throwaway server and hand its base URL to the test. */
async function withServer(handler, fn) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(base);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    server.closeAllConnections?.();
  }
}

test('GET returns a fetch-shaped response', async () => {
  await withServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ hello: 'world' }));
  }, async (base) => {
    const res = await request(base);
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.status, 200);
    assert.strictEqual(res.headers.get('content-type'), 'application/json');
    assert.strictEqual(res.headers.get('Content-Type'), 'application/json', 'header lookup is case-insensitive');
    assert.deepStrictEqual(await res.json(), { hello: 'world' });
  });
});

test('POST sends a JSON body and sets Content-Length', async () => {
  let seen = null;
  let contentLength = null;

  await withServer((req, res) => {
    const chunks = [];
    contentLength = req.headers['content-length'];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen = JSON.parse(Buffer.concat(chunks).toString());
      res.writeHead(200, { 'Content-Type': 'video/mp4' });
      res.end(Buffer.from('mp4-bytes'));
    });
  }, async (base) => {
    const res = await request(base, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: 'a street at night' }),
    });
    assert.deepStrictEqual(seen, { prompt: 'a street at night' });
    assert.ok(Number(contentLength) > 0);
    assert.strictEqual(Buffer.from(await res.arrayBuffer()).toString(), 'mp4-bytes');
  });
});

test('non-2xx is returned rather than thrown', async () => {
  await withServer((req, res) => {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end('{"error":"boom"}');
  }, async (base) => {
    const res = await request(base);
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.status, 500);
    assert.strictEqual(await res.text(), '{"error":"boom"}');
  });
});

/**
 * The regression this module exists for: Node's built-in fetch gives up after
 * 300s waiting for headers, which is shorter than a real video generation.
 * A full-length reproduction would take five minutes, so this asserts the
 * mechanism — the client does not impose a deadline of its own — using a delay
 * long enough to catch any accidental short timeout.
 */
test('a slow response is not cut off by a client-side timeout', async () => {
  await withServer((req, res) => {
    setTimeout(() => {
      res.writeHead(200, { 'Content-Type': 'video/mp4' });
      res.end(Buffer.from('late-but-complete'));
    }, 1200);
  }, async (base) => {
    const started = Date.now();
    const res = await request(base, { method: 'POST', body: '{}' });
    const waited = Date.now() - started;
    assert.ok(waited >= 1100, `should have waited for the slow reply, waited ${waited}ms`);
    assert.strictEqual(Buffer.from(await res.arrayBuffer()).toString(), 'late-but-complete');
  });
});

test('no socket timeout is set by default', async () => {
  // A server that holds the connection open past any default Node would apply
  // to sockets, then replies.
  await withServer((req, res) => {
    req.socket.setKeepAlive(true);
    setTimeout(() => {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('ok');
    }, 800);
  }, async (base) => {
    const res = await request(base, { timeoutMs: 0 });
    assert.strictEqual(await res.text(), 'ok');
  });
});

test('an explicit timeout is honoured when asked for', async () => {
  await withServer((req, res) => {
    setTimeout(() => res.end('too late'), 3000);
  }, async (base) => {
    await assert.rejects(
      () => request(base, { timeoutMs: 200 }),
      (err) => {
        assert.match(err.message, /timed out/i);
        return true;
      },
    );
  });
});

test('an abort signal cancels in flight', async () => {
  await withServer((req, res) => {
    setTimeout(() => res.end('never read'), 5000);
  }, async (base) => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 150);

    await assert.rejects(
      () => request(base, { signal: controller.signal }),
      (err) => {
        assert.strictEqual(err.name, 'AbortError');
        return true;
      },
    );
  });
});

test('an already-aborted signal fails immediately', async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    () => request('http://127.0.0.1:1/', { signal: controller.signal }),
    (err) => err.name === 'AbortError',
  );
});

test('redirects are followed', async () => {
  await withServer((req, res) => {
    if (req.url === '/start') {
      res.writeHead(302, { Location: '/final' });
      res.end();
      return;
    }
    res.writeHead(200, { 'Content-Type': 'video/mp4' });
    res.end(Buffer.from('redirected-body'));
  }, async (base) => {
    const res = await request(`${base}/start`);
    assert.strictEqual(res.status, 200);
    assert.strictEqual(Buffer.from(await res.arrayBuffer()).toString(), 'redirected-body');
  });
});

test('a refused connection explains itself in plain language', async () => {
  // Port 1 is reserved and nothing listens there.
  await assert.rejects(
    () => request('http://127.0.0.1:1/generate', { method: 'POST', body: '{}' }),
    (err) => {
      assert.match(err.message, /Nothing is listening/);
      assert.match(err.message, /local GPU server/);
      return true;
    },
  );
});

test('a connection dropped mid-request points at the likely cause', async () => {
  await withServer((req, res) => {
    // Simulate the server dying partway through, as an out-of-memory kill would.
    req.socket.destroy();
  }, async (base) => {
    await assert.rejects(
      () => request(base, { method: 'POST', body: '{}' }),
      (err) => {
        assert.match(err.message, /closed before a reply|socket hang up/i);
        return true;
      },
    );
  });
});

test('an invalid URL is reported clearly', async () => {
  await assert.rejects(() => request('not-a-url'), /Invalid URL/);
});
