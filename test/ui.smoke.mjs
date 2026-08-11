/**
 * Browser smoke test.
 *
 * Drives the real UI in Chromium and asserts the whole chain works: boot,
 * era/format/intensity controls, editable observation fields, live recompiling,
 * copy buttons, history, and the standalone single-file build.
 *
 * The vision model is deliberately NOT exercised — the model host is not
 * reachable from the build sandbox, and the app is designed so the compiler
 * works without it. That path has to be verified on a real phone.
 *
 * Playwright is resolved from wherever it happens to be installed so the repo
 * itself stays dependency-free. If it cannot be found the test skips rather
 * than fails.
 *
 * Usage: node test/ui.smoke.mjs
 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = join(fileURLToPath(new URL('.', import.meta.url)), '..');
const require = createRequire(import.meta.url);

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
  '.png': 'image/png',
};

/* ------------------------------------------------------------------ *
 * Harness
 * ------------------------------------------------------------------ */

function loadPlaywright() {
  const candidates = ['playwright', 'playwright-core'];
  try {
    const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
    candidates.push(join(globalRoot, 'playwright'), join(globalRoot, 'playwright-core'));
  } catch {
    /* npm not available; fall through to the bare specifiers */
  }
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      /* try the next one */
    }
  }
  return null;
}

function startServer() {
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://localhost');
      const rel = normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, '');
      const filePath = join(root, rel === '/' ? 'index.html' : rel);
      const body = await readFile(filePath);
      res.writeHead(200, {
        'Content-Type': MIME[extname(filePath)] || 'application/octet-stream',
        'Service-Worker-Allowed': '/',
      });
      res.end(body);
    } catch {
      res.writeHead(404).end('not found');
    }
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

let passed = 0;
const failures = [];

function check(label, condition, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  ok   ${label}`);
  } else {
    failures.push(`${label}${detail ? ` — ${detail}` : ''}`);
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`);
  }
}

/* ------------------------------------------------------------------ *
 * Page helpers
 * ------------------------------------------------------------------ */

const segButton = (page, containerId, label) =>
  page.locator(`#${containerId} button`, { hasText: label }).first();

async function readState(page) {
  return page.evaluate(() => ({
    prompt: document.getElementById('prompt').value,
    negative: document.getElementById('negative').value,
    cfg: document.getElementById('stat-cfg').textContent,
    steps: document.getElementById('stat-steps').textContent,
    aspect: document.getElementById('stat-aspect').textContent,
    eras: [...document.querySelectorAll('#era-seg button')].map((b) => b.textContent),
    formats: [...document.querySelectorAll('#format-seg button')].map((b) => b.textContent),
    pressedEra: document
      .querySelector('#era-seg button[aria-pressed="true"]')
      ?.textContent,
    fieldCount: document.querySelectorAll('#fields input').length,
  }));
}

/* ------------------------------------------------------------------ *
 * Main
 * ------------------------------------------------------------------ */

async function main() {
  const playwright = loadPlaywright();
  if (!playwright) {
    console.log('SKIP: playwright not installed — cannot run the browser smoke test.');
    process.exit(0);
  }

  const { server, port } = await startServer();
  const base = `http://127.0.0.1:${port}`;
  const executablePath = process.env.CHROMIUM_PATH || '/opt/pw-browsers/chromium';

  const browser = await playwright.chromium.launch({
    executablePath,
    args: ['--no-sandbox'],
  });

  const context = await browser.newContext({
    viewport: { width: 390, height: 844 }, // phone-sized, as intended
    permissions: ['clipboard-read', 'clipboard-write'],
  });

  const consoleErrors = [];
  context.on('weberror', (e) => consoleErrors.push(e.error().message));

  const page = await context.newPage();
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(err.message));

  try {
    /* -------------------------------------------------- boot */
    console.log('\nBoot & initial render');
    await page.goto(`${base}/index.html`, { waitUntil: 'load' });
    await page.waitForSelector('#era-seg button');

    let s = await readState(page);
    check('four era options render', s.eras.length === 4, s.eras.join('|'));
    check('1990s is the default era', s.pressedEra === '1990s', String(s.pressedEra));
    check('formats render for the era', s.formats.length === 3, s.formats.join('|'));
    check('one input per observation field', s.fieldCount === 9, String(s.fieldCount));
    check('an era-only prompt compiles with no image', s.prompt.length > 40, s.prompt);
    check('negative prompt is populated', s.negative.length > 60);
    check('1990s CFG/steps/aspect shown', s.cfg === '5.5' && s.steps === '28' && s.aspect === '3:2',
      `${s.cfg}/${s.steps}/${s.aspect}`);
    check('film stock appears in the prompt', s.prompt.includes('Kodak Gold 200'), s.prompt);
    check(
      'quality boilerplate is suppressed',
      !/masterpiece|ultra detailed|\b8k\b/i.test(s.prompt),
      s.prompt,
    );

    /* -------------------------------------------------- editable fields */
    console.log('\nEditable observation fields');
    await page.fill('#field-subject', 'a man');
    await page.fill('#field-clothing', 'a plaid shirt');
    await page.fill('#field-setting', 'a kitchen');
    s = await readState(page);
    check('typed subject reaches the prompt', s.prompt.includes('man'), s.prompt);
    check('subject is weighted', s.prompt.includes('(man:1.2)'), s.prompt);
    check('typed setting is normalised', s.prompt.includes('domestic kitchen'), s.prompt);

    /* -------------------------------------------------- era switching */
    console.log('\nEra switching');
    await segButton(page, 'era-seg', 'Early 2000s').click();
    s = await readState(page);
    check('era settings update', s.cfg === '6' && s.steps === '26' && s.aspect === '4:3',
      `${s.cfg}/${s.steps}/${s.aspect}`);
    check('formats swap to the new era', s.formats.some((f) => /Digital compact/i.test(f)),
      s.formats.join('|'));
    check('early-2000s artifacts appear', /JPEG compression/i.test(s.prompt), s.prompt);
    check('negative drops jpeg artifacts for this era',
      !/\bjpeg artifacts\b/i.test(s.negative), s.negative);

    await segButton(page, 'era-seg', '1980s').click();
    s = await readState(page);
    check('1980s selected', s.cfg === '5' && s.steps === '30', `${s.cfg}/${s.steps}`);

    await segButton(page, 'format-seg', 'Instant').click();
    s = await readState(page);
    check('instant format forces a square aspect', s.aspect === '1:1', s.aspect);
    check('polaroid appears in the prompt', /Polaroid/i.test(s.prompt), s.prompt);

    /* -------------------------------------------------- intensity */
    console.log('\nIntensity & toggles');
    await segButton(page, 'era-seg', '1990s').click();
    await segButton(page, 'intensity-seg', 'Subtle').click();
    const subtle = (await readState(page)).prompt;
    await segButton(page, 'intensity-seg', 'Heavy').click();
    const heavy = (await readState(page)).prompt;
    check('heavy emits a longer prompt than subtle', heavy.length > subtle.length,
      `${subtle.length} vs ${heavy.length}`);

    await page.check('#period-subject');
    s = await readState(page);
    // The observed shirt suppresses specific era garments, so the generic era
    // marker is what should appear. The indoor kitchen setting does allow decor.
    check('period-subject adds era styling', /1990s clothing/i.test(s.prompt), s.prompt);
    check('period-subject respects observed clothing', !/windbreaker/i.test(s.prompt), s.prompt);
    await page.uncheck('#period-subject');

    await page.uncheck('#emphasis');
    s = await readState(page);
    check('emphasis toggle removes weighting', !s.prompt.includes('(man:1.2)'));
    await page.check('#emphasis');

    /* -------------------------------------------------- scene awareness */
    console.log('\nScene awareness');
    await page.fill('#field-setting', 'a beach by the ocean');
    await page.fill('#field-placement', 'outdoors');
    await page.fill('#field-lighting', 'sunny');
    await page.fill('#field-shotType', 'close-up shot');
    s = await readState(page);
    check('outdoor photo drops the camera flash', !/on-camera flash/i.test(s.prompt), s.prompt);
    check('outdoor photo drops the wall shadow', !/shadow on the wall/i.test(s.prompt), s.prompt);
    await page.check('#period-subject');
    s = await readState(page);
    check('outdoor photo drops indoor decor', !/popcorn ceiling|beige carpet/i.test(s.prompt), s.prompt);
    check('close-up drops out-of-frame garments', !/sneakers|baggy jeans/i.test(s.prompt), s.prompt);
    await page.uncheck('#period-subject');

    // Restore the indoor observation for the remaining checks.
    await page.fill('#field-setting', 'a kitchen');
    await page.fill('#field-placement', '');
    await page.fill('#field-lighting', 'harsh flash');
    await page.fill('#field-shotType', '');

    /* -------------------------------------------------- natural language */
    console.log('\nPrompt style');
    await segButton(page, 'style-seg', 'Natural language').click();
    s = await readState(page);
    check('natural style restores the article', s.prompt.includes('of a man'), s.prompt);
    check('natural style has no weight syntax', !s.prompt.includes(':1.2)'), s.prompt);
    check('no doubled prepositions', !/\bin\s+in\b|\bwearing\s+wearing\b/.test(s.prompt), s.prompt);
    await segButton(page, 'style-seg', 'Tag style').click();

    /* -------------------------------------------------- reroll */
    console.log('\nVariant reroll');
    const before = (await readState(page)).prompt;
    await page.click('#variant-btn');
    const after = (await readState(page)).prompt;
    check('reroll changes the film/camera choice', before !== after);

    /* -------------------------------------------------- copy + history */
    console.log('\nCopy & history');
    await page.click('#copy-prompt');
    await page.waitForTimeout(150);
    const copyLabel = await page.textContent('#copy-prompt');
    check('copy button confirms', /copied/i.test(copyLabel || ''), copyLabel || '');

    const clip = await page.evaluate(() => navigator.clipboard.readText());
    check('clipboard holds the prompt', clip.includes('man'), clip.slice(0, 60));

    await page.click('#copy-all');
    await page.waitForTimeout(150);
    const clipAll = await page.evaluate(() => navigator.clipboard.readText());
    check('copy-everything includes settings', /Guidance \/ CFG:/.test(clipAll), clipAll.slice(0, 80));
    check('copy-everything includes the negative prompt', /Negative prompt:/.test(clipAll));

    await page.click('details >> nth=0'); // open "Recent forges"
    await page.waitForTimeout(100);
    const historyRows = await page.locator('.history-item').count();
    check('manual forge is saved to history', historyRows >= 1, String(historyRows));

    /* -------------------------------------------------- persistence */
    console.log('\nPreference persistence');
    await segButton(page, 'era-seg', 'Early 2000s').click();
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#era-seg button');
    s = await readState(page);
    check('era choice survives a reload', s.pressedEra === 'Early 2000s', String(s.pressedEra));

    /* -------------------------------------------------- service worker */
    console.log('\nPWA');
    const swReady = await page
      .waitForFunction(() => navigator.serviceWorker.getRegistration().then((r) => !!r), {
        timeout: 5000,
      })
      .then(() => true)
      .catch(() => false);
    check('service worker registers', swReady);

    const manifest = await page.evaluate(async () => {
      const res = await fetch('./manifest.webmanifest');
      return res.ok ? res.json() : null;
    });
    check('manifest is served and valid JSON', manifest?.name === 'Perchance Prompt Forge');
    check('manifest declares three icons', manifest?.icons?.length === 3);

    /* -------------------------------------------------- standalone bundle */
    console.log('\nStandalone single-file build');
    const standalone = await context.newPage();
    const standaloneErrors = [];
    standalone.on('pageerror', (err) => standaloneErrors.push(err.message));
    await standalone.goto(`${base}/dist/prompt-forge.html`, { waitUntil: 'load' });
    await standalone.waitForSelector('#era-seg button');
    const bundlePrompt = await standalone.inputValue('#prompt');
    check('bundle boots and compiles', bundlePrompt.length > 40, bundlePrompt);
    await standalone.fill('#field-subject', 'a dog');
    const bundleAfter = await standalone.inputValue('#prompt');
    check('bundle recompiles on input', bundleAfter.includes('dog'), bundleAfter);
    check('bundle throws no errors', standaloneErrors.length === 0, standaloneErrors.join('; '));
    await standalone.close();

    /* -------------------------------------------------- console hygiene */
    console.log('\nConsole');
    const realErrors = consoleErrors.filter(
      // Model CDN is unreachable from the sandbox; that is expected here.
      (e) => !/cdn\.jsdelivr|transformers|Failed to fetch dynamically/i.test(e),
    );
    check('no unexpected console errors', realErrors.length === 0, realErrors.join('; '));
  } finally {
    await browser.close();
    server.close();
  }

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    console.log('\nFailures:');
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(`smoke test crashed: ${err.stack || err.message}`);
  process.exit(1);
});
