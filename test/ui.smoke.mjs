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

// Derived rather than hard-coded, so adding an observation pass doesn't break this.
const { OBSERVATION_FIELDS } = await import('../src/compiler.js');
const OBSERVATION_FIELD_COUNT = OBSERVATION_FIELDS.length;
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
    style: document.getElementById('stat-style').textContent,
    resolution: document.getElementById('stat-resolution').textContent,
    styleHint: document.getElementById('style-hint').textContent,
    cfgHint: document.getElementById('cfg-hint').textContent,
    promptCount: document.getElementById('prompt-count').textContent,
    negativeCount: document.getElementById('negative-count').textContent,
    trimmedNote: document.getElementById('trimmed-note').textContent,
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
    check(
      'one input per observation field',
      s.fieldCount === OBSERVATION_FIELD_COUNT,
      `${s.fieldCount} inputs vs ${OBSERVATION_FIELD_COUNT} fields`,
    );
    check('an era-only prompt compiles with no image', s.prompt.length > 40, s.prompt);
    check('negative prompt is populated', s.negative.length > 60);
    check('1990s Perchance settings shown', s.cfg === '5.5' && s.resolution === '768x512',
      `${s.cfg}/${s.resolution}`);
    check('art style is the neutral one', s.style === 'none', s.style);
    check('the style trap is warned about', /8k|HDR|masterpiece/i.test(s.styleHint), s.styleHint);
    check('steps are marked as not a Perchance control', /Steps aren't a Perchance control/i.test(s.cfgHint),
      s.cfgHint);
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
    check('era settings update', s.cfg === '6' && s.resolution === '768x512',
      `${s.cfg}/${s.resolution}`);
    check('formats swap to the new era', s.formats.some((f) => /Digital compact/i.test(f)),
      s.formats.join('|'));
    check('early-2000s artifacts appear', /JPEG compression/i.test(s.prompt), s.prompt);
    check('negative drops jpeg artifacts for this era',
      !/\bjpeg artifacts\b/i.test(s.negative), s.negative);

    await segButton(page, 'era-seg', '1980s').click();
    s = await readState(page);
    check('1980s selected', s.cfg === '5', s.cfg);

    await segButton(page, 'format-seg', 'Instant').click();
    s = await readState(page);
    check('instant format maps to the square resolution', s.resolution === '768x768', s.resolution);
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

    /* -------------------------------------------------- content level */
    console.log('\nContent level & minor safeguard');
    await page.fill('#field-subject', 'a woman');
    await page.fill('#field-apparentAge', 'adult');
    s = await readState(page);
    check('safe is the default', /nsfw|nude/i.test(s.negative), s.negative.slice(0, 80));

    await segButton(page, 'content-seg', 'Explicit').click();
    let hint = await page.textContent('#content-hint');
    check('adult level blocked without affirmation', /confirm/i.test(hint || ''), hint || '');
    s = await readState(page);
    check('blocked level still negates nudity', /\bnude\b/i.test(s.negative), s.negative.slice(0, 80));

    await page.check('#adult-confirmed');
    s = await readState(page);
    hint = await page.textContent('#content-hint');
    check('affirmation enables the level', !/confirm/i.test(hint || ''), hint || '');
    check('nudity negatives lifted', !/\bnude\b/i.test(s.negative), s.negative.slice(0, 100));
    check('anatomy support added', /anatomically correct/i.test(s.prompt), s.prompt);
    check('era framing kept for adult content', /boudoir|private/i.test(s.prompt), s.prompt);
    check('era realism still applies', !/masterpiece|\b8k\b/i.test(s.prompt), s.prompt);

    // The safeguard must hold in the real UI, not just in the compiler.
    await page.fill('#field-subject', 'a child');
    s = await readState(page);
    hint = await page.textContent('#content-hint');
    check('minor subject blocks adult content', /disabled/i.test(hint || ''), hint || '');
    check('blocked output falls back to safe', /\bnude\b/i.test(s.negative), s.negative.slice(0, 80));
    check('no anatomy tags when blocked', !/anatomically correct/i.test(s.prompt), s.prompt);

    await page.fill('#field-subject', 'a woman');
    await page.fill('#field-apparentAge', 'child');
    s = await readState(page);
    hint = await page.textContent('#content-hint');
    check('model age read blocks adult content', /does not read as an adult/i.test(hint || ''), hint || '');

    // Reset for the remaining checks.
    await page.fill('#field-apparentAge', 'adult');
    await page.uncheck('#adult-confirmed');
    await segButton(page, 'content-seg', 'Safe').click();
    await page.fill('#field-subject', 'a man');

    /* -------------------------------------------------- pose & gaze */
    console.log('\nPose & gaze');
    // Subtle intensity so the prompt has budget room: this checks normalisation,
    // not trimming, and gaze is deliberately one of the first things trimmed.
    await segButton(page, 'intensity-seg', 'Subtle').click();
    await page.fill('#field-clothing', '');
    await page.fill('#field-pose', 'sitting');
    await page.fill('#field-gaze', 'camera');
    s = await readState(page);
    check('pose is normalised', s.prompt.includes('seated'), s.prompt);
    check('gaze is normalised', s.prompt.includes('at the camera'), s.prompt);
    await page.fill('#extra-terms', 'holding a coffee mug');
    s = await readState(page);
    check('extra terms reach the prompt', s.prompt.includes('holding a coffee mug'), s.prompt);
    await page.fill('#extra-terms', '');
    await page.fill('#field-pose', '');
    await page.fill('#field-gaze', '');
    await page.fill('#field-clothing', 'a plaid shirt');
    await segButton(page, 'intensity-seg', 'Medium').click();

    /* -------------------------------------------------- token budget */
    console.log('\nToken budget');
    s = await readState(page);
    check('prompt token counter is shown against the limit',
      /^\d+\/75 tokens$/.test(s.promptCount), s.promptCount);
    check('negative token counter is shown', /^\d+\/75 tokens$/.test(s.negativeCount), s.negativeCount);
    const budgetState = await page.evaluate(() => {
      const n = (t) => Number((t || '').split('/')[0]);
      return {
        prompt: n(document.getElementById('prompt-count').textContent),
        negative: n(document.getElementById('negative-count').textContent),
        note: document.getElementById('trimmed-note').textContent,
      };
    });
    check('prompt fits CLIP context', budgetState.prompt <= 75, String(budgetState.prompt));
    check('negative fits CLIP context', budgetState.negative <= 75, String(budgetState.negative));
    check('the era apparatus survived the budget', /Kodak|point-and-shoot/.test(s.prompt), s.prompt);
    check('nudity negatives survived the budget', /\bnude\b/.test(s.negative), s.negative.slice(0, 90));

    // Force a very long observation and confirm it is trimmed rather than overflowing.
    await page.fill('#field-appearance',
      'long wavy brown hair, side-swept bangs, warm brown eyes, a small silver nose stud, lightly freckled cheeks, faint smile lines, a thin gold chain');
    await page.fill('#field-pose',
      'seated on a low stone wall, leaning back on both hands, head tilted slightly to one side, shoulders relaxed');
    s = await readState(page);
    const overflow = await page.evaluate(() => ({
      prompt: Number((document.getElementById('prompt-count').textContent || '').split('/')[0]),
      note: document.getElementById('trimmed-note').textContent,
    }));
    check('a very long observation is still trimmed to fit', overflow.prompt <= 75, String(overflow.prompt));
    check('what was trimmed is disclosed', /Trimmed from prompt/i.test(overflow.note), overflow.note);
    check('the era apparatus still survives a long observation',
      /Kodak|point-and-shoot/.test(s.prompt), s.prompt);
    await page.fill('#field-appearance', '');
    await page.fill('#field-pose', '');

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
    check('copy-everything includes the Perchance checklist', /Guidance scale:/.test(clipAll), clipAll.slice(0, 80));
    check('copy-everything names the resolution', /Resolution: \d{3,4}x\d{3,4}/.test(clipAll), clipAll);
    check('copy-everything warns about the art style', /Art style: none/.test(clipAll), clipAll);
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

    /* -------------------------------------------------- background handling */
    console.log('\nBackground handling');

    // The worker only touches the network when told to load a model, so its
    // construction and whole import graph can be verified without the model host.
    const workerReady = await page.evaluate(
      () =>
        new Promise((resolve) => {
          let w;
          const done = (v) => {
            try {
              w?.terminate();
            } catch {}
            resolve(v);
          };
          try {
            w = new Worker('./src/worker.js', { type: 'module' });
          } catch (e) {
            return done(`construct threw: ${e.message}`);
          }
          w.onmessage = (e) => done(e.data?.type === 'ready' ? 'ready' : `unexpected: ${e.data?.type}`);
          w.onerror = (e) => done(`worker error: ${e.message}`);
          setTimeout(() => done('timeout'), 8000);
        }),
    );
    check('inference worker starts and reports ready', workerReady === 'ready', String(workerReady));

    check(
      'the background limitation is stated, not implied away',
      /resume|progress is saved/i.test((await page.textContent('#background-note')) || ''),
      (await page.textContent('#background-note')) || '',
    );

    const resumeHidden = await page.evaluate(
      () => !document.getElementById('resume-bar').classList.contains('show'),
    );
    check('no resume prompt when there is nothing to resume', resumeHidden);

    // Plant an interrupted run and reload, as an OS kill mid-read would leave it.
    await page.evaluate(() => {
      const c = document.createElement('canvas');
      c.width = 32;
      c.height = 24;
      const ctx = c.getContext('2d');
      ctx.fillStyle = '#888';
      ctx.fillRect(0, 0, 32, 24);
      localStorage.setItem(
        'promptforge.run.v1',
        JSON.stringify({
          dataUrl: c.toDataURL('image/jpeg', 0.9),
          source: { width: 32, height: 24 },
          observation: { subject: 'a cyclist', pose: 'seated' },
          nextIndex: 3,
          total: 12,
          at: Date.now(),
        }),
      );
    });
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#era-seg button');
    await page.waitForTimeout(300);

    const resumeShown = await page.evaluate(() => ({
      shown: document.getElementById('resume-bar').classList.contains('show'),
      text: document.getElementById('resume-text').textContent,
      subject: document.getElementById('field-subject').value,
      preview: document.getElementById('preview').classList.contains('show'),
    }));
    check('an interrupted run offers to resume', resumeShown.shown, JSON.stringify(resumeShown));
    check('resume reports how far it got', /3 of \d+/.test(resumeShown.text), resumeShown.text);
    check('partial answers survive the interruption', resumeShown.subject === 'a cyclist', resumeShown.subject);
    check('the photo is restored too', resumeShown.preview);

    const restoredPrompt = await page.inputValue('#prompt');
    check('a partial run still compiles a usable prompt', /cyclist/.test(restoredPrompt), restoredPrompt);

    await page.click('#discard-btn');
    const discarded = await page.evaluate(() => ({
      hidden: !document.getElementById('resume-bar').classList.contains('show'),
      cleared: localStorage.getItem('promptforge.run.v1') === null,
    }));
    check('discarding hides the prompt and clears the checkpoint', discarded.hidden && discarded.cleared,
      JSON.stringify(discarded));

    // A stale checkpoint should be dropped rather than offered.
    await page.evaluate(() => {
      localStorage.setItem(
        'promptforge.run.v1',
        JSON.stringify({ dataUrl: 'data:,', observation: {}, nextIndex: 2, at: Date.now() - 48 * 3600 * 1000 }),
      );
    });
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#era-seg button');
    await page.waitForTimeout(200);
    const staleDropped = await page.evaluate(
      () => !document.getElementById('resume-bar').classList.contains('show'),
    );
    check('a day-old checkpoint is not offered', staleDropped);

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
