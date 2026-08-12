/**
 * UI wiring and orchestration.
 *
 * Deliberate design point: the compiler is the valuable half of this app and it
 * has no dependency on the model. Every observed field is an editable input, so
 * if the model fails to load — old phone, no WebGPU, no connection — the user
 * can still type what is in the photo and get a fully compiled era prompt. The
 * model is an accelerator, not a hard requirement.
 */

import { compile, settingsBlock, OBSERVATION_FIELDS } from './compiler.js';
import { CONTENT_LEVELS, DEFAULT_CONTENT } from './vocab.js';
import { perchanceSettings, STYLE_NOTES } from './perchance.js';
import { ERAS, ERA_IDS, DEFAULT_ERA, INTENSITY_LEVELS, DEFAULT_INTENSITY } from './eras.js';
import {
  MODELS,
  detectCapabilities,
  chooseModel,
  prepareImage,
  unloadVision,
  describeError,
  PASSES,
} from './vision.js';
import {
  createRunner,
  saveCheckpoint,
  loadCheckpoint,
  clearCheckpoint,
  acquireWakeLock,
  releaseWakeLock,
  wakeLockSupported,
} from './runner.js';

const HISTORY_KEY = 'promptforge.history.v1';
const PREFS_KEY = 'promptforge.prefs.v1';
const HISTORY_LIMIT = 12;

const FIELD_LABELS = {
  subject: 'Subject',
  appearance: 'Appearance',
  clothing: 'Clothing',
  action: 'Action',
  pose: 'Pose',
  gaze: 'Gaze',
  setting: 'Setting',
  placement: 'Indoor/outdoor',
  colors: 'Colors',
  lighting: 'Lighting',
  shotType: 'Shot type',
  apparentAge: 'Apparent age',
};

const state = {
  era: DEFAULT_ERA,
  format: null,
  intensity: DEFAULT_INTENSITY,
  style: 'tags',
  periodSubject: false,
  emphasis: true,
  variant: 0,
  content: DEFAULT_CONTENT,
  useNegative: false,
  adultConfirmed: false,
  extra: '',
  observation: {},
  source: null,
  imageDataUrl: null,
  imageCanvas: null,
  pixels: null,
  runner: null,
  resumeIndex: 0,
  modelOverride: null,
  caps: null,
  running: false,
  abort: null,
};

const $ = (id) => document.getElementById(id);

const el = {
  drop: $('drop'),
  dropLabel: $('drop-label'),
  preview: $('preview'),
  cameraBtn: $('camera-btn'),
  galleryBtn: $('gallery-btn'),
  cameraInput: $('camera-input'),
  galleryInput: $('gallery-input'),
  readBtn: $('read-btn'),
  status: $('status'),
  eraSeg: $('era-seg'),
  eraBlurb: $('era-blurb'),
  formatSeg: $('format-seg'),
  intensitySeg: $('intensity-seg'),
  styleSeg: $('style-seg'),
  periodSubject: $('period-subject'),
  contentSeg: $('content-seg'),
  adultConfirmed: $('adult-confirmed'),
  contentHint: $('content-hint'),
  extraTerms: $('extra-terms'),
  emphasis: $('emphasis'),
  fields: $('fields'),
  clearFields: $('clear-fields'),
  variantBtn: $('variant-btn'),
  prompt: $('prompt'),
  negative: $('negative'),
  promptCount: $('prompt-count'),
  negativeCount: $('negative-count'),
  statCfg: $('stat-cfg'),
  statStyle: $('stat-style'),
  statResolution: $('stat-resolution'),
  styleHint: $('style-hint'),
  cfgHint: $('cfg-hint'),
  trimmedNote: $('trimmed-note'),
  negativeBlock: $('negative-block'),
  useNegative: $('use-negative'),
  copyPrompt: $('copy-prompt'),
  copyNegative: $('copy-negative'),
  copyAll: $('copy-all'),
  history: $('history'),
  clearHistory: $('clear-history'),
  settingsBtn: $('settings-btn'),
  settingsPanel: $('settings-panel'),
  modelSelect: $('model-select'),
  modelWhy: $('model-why'),
  unloadBtn: $('unload-btn'),
  offlineState: $('offline-state'),
  resumeBar: $('resume-bar'),
  resumeText: $('resume-text'),
  resumeBtn: $('resume-btn'),
  discardBtn: $('discard-btn'),
  notifyBtn: $('notify-btn'),
  backgroundNote: $('background-note'),
};

/* ------------------------------------------------------------------ *
 * Status helpers
 * ------------------------------------------------------------------ */

function setStatus(message, kind = '', progress = null) {
  if (!message) {
    el.status.className = '';
    el.status.textContent = '';
    return;
  }
  el.status.className = `show ${kind}`.trim();
  el.status.textContent = message;
  if (progress !== null) {
    const bar = document.createElement('progress');
    if (progress >= 0) {
      bar.max = 100;
      bar.value = progress;
    }
    el.status.appendChild(bar);
  }
}

/* ------------------------------------------------------------------ *
 * Segmented controls
 * ------------------------------------------------------------------ */

function buildSegment(container, options, current, onPick) {
  container.textContent = '';
  for (const option of options) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = option.label;
    button.setAttribute('aria-pressed', String(option.value === current));
    button.addEventListener('click', () => onPick(option.value));
    container.appendChild(button);
  }
}

function renderEraControls() {
  buildSegment(
    el.eraSeg,
    ERA_IDS.map((id) => ({ value: id, label: ERAS[id].label })),
    state.era,
    (value) => {
      state.era = value;
      state.format = null; // fall back to the new era's default
      renderEraControls();
      recompile();
      savePrefs();
    },
  );

  const era = ERAS[state.era];
  el.eraBlurb.textContent = era.blurb;

  const formats = Object.keys(era.formats);
  const activeFormat = state.format && era.formats[state.format] ? state.format : era.defaultFormat;
  buildSegment(
    el.formatSeg,
    formats.map((key) => ({ value: key, label: era.formats[key].label || key })),
    activeFormat,
    (value) => {
      state.format = value;
      renderEraControls();
      recompile();
      savePrefs();
    },
  );
}

function renderIntensity() {
  buildSegment(
    el.intensitySeg,
    Object.entries(INTENSITY_LEVELS).map(([key, level]) => ({ value: key, label: level.label })),
    state.intensity,
    (value) => {
      state.intensity = value;
      renderIntensity();
      recompile();
      savePrefs();
    },
  );
}

function renderStyle() {
  buildSegment(
    el.styleSeg,
    [
      { value: 'tags', label: 'Tag style' },
      { value: 'natural', label: 'Natural language' },
    ],
    state.style,
    (value) => {
      state.style = value;
      renderStyle();
      recompile();
      savePrefs();
    },
  );
}

function renderContent() {
  buildSegment(
    el.contentSeg,
    Object.entries(CONTENT_LEVELS).map(([key, level]) => ({ value: key, label: level.label })),
    state.content,
    (value) => {
      state.content = value;
      renderContent();
      recompile();
      savePrefs();
    },
  );
  el.adultConfirmed.checked = state.adultConfirmed;
  // The affirmation is only meaningful for levels that require it.
  el.adultConfirmed.disabled = !CONTENT_LEVELS[state.content]?.requiresAdult;
}

/* ------------------------------------------------------------------ *
 * Observation fields
 * ------------------------------------------------------------------ */

function renderFields() {
  el.fields.textContent = '';
  for (const field of OBSERVATION_FIELDS) {
    const row = document.createElement('div');
    row.className = 'field';

    const label = document.createElement('label');
    label.textContent = FIELD_LABELS[field] || field;
    label.htmlFor = `field-${field}`;

    const input = document.createElement('input');
    input.id = `field-${field}`;
    input.type = 'text';
    input.value = state.observation[field] || '';
    input.placeholder = '—';
    input.autocomplete = 'off';
    input.addEventListener('input', () => {
      state.observation[field] = input.value;
      recompile();
    });

    row.append(label, input);
    el.fields.appendChild(row);
  }
}

function fillFields() {
  for (const field of OBSERVATION_FIELDS) {
    const input = document.getElementById(`field-${field}`);
    if (input) input.value = state.observation[field] || '';
  }
}

/* ------------------------------------------------------------------ *
 * Compile + render output
 * ------------------------------------------------------------------ */

let lastResult = null;

function recompile() {
  const result = compile(state.observation, {
    era: state.era,
    format: state.format,
    intensity: state.intensity,
    style: state.style,
    periodSubject: state.periodSubject,
    emphasis: state.emphasis,
    variant: state.variant,
    source: state.source,
    content: state.content,
    adultConfirmed: state.adultConfirmed,
    extra: state.extra,
    useNegative: state.useNegative,
  });
  lastResult = result;

  el.prompt.value = result.prompt;
  el.negative.value = result.negative;
  // Token counts, not character counts: CLIP's 75-token context is the limit
  // that actually decides what the image model sees.
  const b = result.budget;
  const badge = (el2, tokens) => {
    el2.textContent = `${tokens}/${b.limit} tokens`;
    el2.className = 'count' + (tokens > b.limit ? ' over' : tokens >= b.limit - 4 ? ' tight' : '');
  };
  badge(el.promptCount, b.promptTokens);
  badge(el.negativeCount, b.negativeTokens);
  el.negativeBlock.style.display = result.useNegative ? '' : 'none';
  el.copyNegative.style.display = result.useNegative ? '' : 'none';

  const trimmed = [];
  if (b.droppedFromPrompt.length) {
    trimmed.push(`Trimmed from prompt to fit: ${b.droppedFromPrompt.join(', ')}.`);
  }
  if (b.droppedFromNegative.length) {
    trimmed.push(`${b.droppedFromNegative.length} lower-priority negative terms dropped.`);
  }
  if (b.promptOverBudget) {
    trimmed.push('Still over budget — the image model may ignore the tail.');
  }
  el.trimmedNote.textContent = trimmed.join(' ');
  el.trimmedNote.className = b.promptOverBudget ? 'hint warn' : 'hint';
  const perchance = perchanceSettings(result);
  el.statCfg.textContent = perchance.guidanceScale;
  el.statStyle.textContent = perchance.style;
  el.statResolution.classList.add('small');
  el.statResolution.textContent = perchance.resolution.value;

  el.styleHint.textContent = result.suppressedQualityTags ? STYLE_NOTES : '';
  el.styleHint.style.display = result.suppressedQualityTags ? '' : 'none';

  // Surface exactly why a requested content level was not applied, rather than
  // silently downgrading it.
  if (result.contentBlocked) {
    el.contentHint.textContent = result.contentReason;
    el.contentHint.className = 'hint warn';
  } else {
    el.contentHint.className = 'hint';
    el.contentHint.textContent =
      result.content === 'sfw'
        ? 'Safe: nudity is added to the negative prompt.'
        : `${CONTENT_LEVELS[result.content].label}: nudity permitted, anatomy support added.`;
  }

  const [lo, hi] = result.cfg.range;
  el.cfgHint.textContent =
    `This era works best at guidance ${lo}–${hi} (Perchance accepts ${perchance.guidanceRange[0]}–${perchance.guidanceRange[1]}). ` +
    `Lower reads more like a real photo; higher starts to look AI-generated. ` +
    `Ideal framing is ${result.aspect}, so pick ${perchance.resolution.label}. ` +
    `Steps aren't a Perchance control — ${perchance.advisorySteps} is only a hint for other tools.`;

  return result;
}

/* ------------------------------------------------------------------ *
 * Image selection
 * ------------------------------------------------------------------ */

async function handleFile(file) {
  if (!file) return;
  try {
    setStatus('Preparing image…');
    const prepared = await prepareImage(file);
    state.imageDataUrl = prepared.dataUrl; // preview only
    state.imageCanvas = prepared.canvas; // retained so pixels can be rebuilt
    state.pixels = prepared.pixels; // transferred to the worker for inference
    state.source = prepared.original;
    state.resumeIndex = 0;
    clearCheckpoint();
    hideResume();

    el.preview.src = prepared.dataUrl;
    el.preview.classList.add('show');
    el.drop.classList.add('has-image');
    el.dropLabel.textContent = `${prepared.original.width}×${prepared.original.height}`;
    el.readBtn.disabled = false;
    setStatus('Ready. Tap "Read image".', 'ok');
    recompile();
  } catch (err) {
    setStatus(`Could not read that image: ${err.message}`, 'err');
  }
}

/* ------------------------------------------------------------------ *
 * Vision run
 * ------------------------------------------------------------------ */

async function ensureCaps() {
  if (!state.caps) {
    state.caps = await detectCapabilities();
    updateModelWhy();
  }
  return state.caps;
}

function updateModelWhy() {
  if (!state.caps) return;
  const choice = chooseModel(state.caps, state.modelOverride);
  const bits = [choice.label, choice.device === 'webgpu' ? 'GPU' : 'CPU', choice.approxDownload];
  el.modelWhy.textContent = `${bits.join(' · ')} — ${choice.why}`;
}

async function runVision({ resume = false } = {}) {
  if (state.running) {
    state.abort?.abort();
    return;
  }
  // The buffer is transferred to the worker, so it may be detached from an
  // earlier run. Rebuild from the retained canvas rather than silently doing
  // nothing when the button is tapped.
  if (!state.pixels && state.imageCanvas) state.pixels = pixelsFromCanvas(state.imageCanvas);
  if (!state.pixels) {
    setStatus('Could not read that image any more — pick the photo again.', 'err');
    return;
  }

  state.running = true;
  state.abort = new AbortController();
  el.readBtn.textContent = 'Cancel';
  hideResume();

  // Keep the screen awake: the OS backgrounds the app when the screen sleeps,
  // which is what actually stops a run when the phone is set down.
  const locked = await acquireWakeLock();
  let interrupted = false;

  try {
    const caps = await ensureCaps();
    const choice = chooseModel(caps, state.modelOverride);

    const startIndex = resume ? state.resumeIndex : 0;
    const carried = resume ? { ...state.observation } : {};

    const handlers = {
      onProgress: (p) => {
        if (!p) return;
        if (p.phase === 'download' && p.total) {
          const pct = Math.round((p.loaded / p.total) * 100);
          setStatus(`Downloading ${p.file || 'model'} — ${pct}%`, '', pct);
        } else if (p.phase === 'library') {
          setStatus('Loading runtime…', '', -1);
        } else if (p.phase === 'processor') {
          setStatus('Loading processor…', '', -1);
        } else if (p.phase === 'model') {
          setStatus('Initialising model…', '', -1);
        } else if (typeof p.index === 'number') {
          setStatus(
            `Reading image — ${FIELD_LABELS[p.field] || p.field} (${p.index + 1}/${p.total})` +
              (locked ? '' : ' · keep the screen on'),
            '',
            Math.round((p.index / p.total) * 100),
          );
        }
      },
      // Checkpoint after every pass, and show each answer as it lands so a run
      // that gets killed still leaves visible, usable work behind.
      onPass: ({ field, value, index, total }) => {
        state.observation[field] = value;
        const input = document.getElementById(`field-${field}`);
        if (input) input.value = value || '';
        recompile();
        state.resumeIndex = index + 1;
        saveCheckpoint({
          dataUrl: state.imageDataUrl,
          source: state.source,
          observation: state.observation,
          nextIndex: index + 1,
          total,
        });
      },
      onFallback: (reason) => {
        // Worth surfacing: it explains why the UI is about to become sluggish.
        setStatus(`Background worker unavailable, running in page. ${reason}`, '', -1);
      },
    };

    setStatus(`Loading ${choice.label} (${choice.approxDownload} first time)…`, '', -1);
    if (!state.runner) {
      state.runner = await createRunner(choice, handlers, { preferWorker: true });
    } else {
      state.runner.setHandlers(handlers);
    }

    const { observation, failures, clipped = [] } = await state.runner.observe({
      pixels: state.pixels,
      startIndex,
      observation: carried,
      signal: state.abort.signal,
    });

    state.observation = { ...state.observation, ...observation };
    state.resumeIndex = PASSES.length;
    clearCheckpoint();
    fillFields();
    const result = recompile();
    saveHistory(result);

    if (failures.length) {
      setStatus(
        `Done, but ${failures.length} of ${PASSES.length} questions failed. Edit any blank field by hand.`,
        'err',
      );
    } else if (clipped.length) {
      // The answer was trimmed back to a clean boundary rather than left dangling,
      // but say so — a field that clips every time wants a bigger model budget.
      const names = clipped.map((f) => FIELD_LABELS[f] || f).join(', ');
      setStatus(`Done. ${names} ran to the length limit and were trimmed cleanly.`, 'ok');
    } else {
      setStatus('Done. Check the fields, then copy the prompt.', 'ok');
    }
    notifyDone(failures.length);
  } catch (err) {
    interrupted = true;
    setStatus(describeError(err), 'err');
    // A partial run is still worth resuming.
    if (state.resumeIndex > 0 && state.resumeIndex < PASSES.length) showResume();
  } finally {
    // The pixel buffer is transferred to the worker, so it cannot be reused for
    // a second run — rebuild it from the canvas that stayed on this thread.
    if (state.imageCanvas) state.pixels = pixelsFromCanvas(state.imageCanvas);
    state.running = false;
    state.abort = null;
    el.readBtn.textContent = 'Read image';
    await releaseWakeLock();
    if (!interrupted) hideResume();
  }
}

/** Re-read pixels from the retained canvas after a transfer emptied the buffer. */
function pixelsFromCanvas(canvas) {
  try {
    const ctx = canvas.getContext('2d');
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
    return { data: data.data, width: canvas.width, height: canvas.height, channels: 4 };
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ *
 * Interruption handling
 * ------------------------------------------------------------------ */

function showResume() {
  el.resumeBar.classList.add('show');
  el.resumeText.textContent = `Unfinished read — ${state.resumeIndex} of ${PASSES.length} questions done.`;
}

function hideResume() {
  el.resumeBar.classList.remove('show');
}

/**
 * Offer to resume a run that a previous session did not finish.
 *
 * This is the honest answer to "keep working while I'm away": the OS will freeze
 * the app, so instead of pretending otherwise, the work already done is kept and
 * the run picks up where it stopped.
 */
async function restoreCheckpoint() {
  const record = loadCheckpoint();
  if (!record) return;

  state.observation = { ...record.observation };
  state.resumeIndex = record.nextIndex;
  state.source = record.source || null;
  state.imageDataUrl = record.dataUrl;

  el.preview.src = record.dataUrl;
  el.preview.classList.add('show');
  el.drop.classList.add('has-image');
  el.dropLabel.textContent = 'restored from an unfinished read';

  // Rebuild the pixels the run needs from the stored preview.
  try {
    const blob = await (await fetch(record.dataUrl)).blob();
    const prepared = await prepareImage(blob);
    state.imageCanvas = prepared.canvas;
    state.pixels = prepared.pixels;
    el.readBtn.disabled = false;
  } catch {
    /* preview still shows; the user can re-pick the photo */
  }

  fillFields();
  recompile();
  showResume();
}

/**
 * Notify on completion when the app is not in the foreground, so a long run does
 * not require watching it.
 */
function notifyDone(failureCount) {
  try {
    if (!('Notification' in window)) return;
    if (Notification.permission !== 'granted') return;
    if (!document.hidden) return;
    const body = failureCount
      ? `Prompt ready, ${failureCount} question(s) failed.`
      : 'Your prompt is ready.';
    new Notification('Prompt Forge', { body, icon: './icons/icon-192.png', tag: 'forge-done' });
  } catch {
    /* notifications are a convenience */
  }
}

/* ------------------------------------------------------------------ *
 * Clipboard
 * ------------------------------------------------------------------ */

async function copyText(text, button) {
  const original = button.textContent;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback for browsers that gate the async clipboard API.
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.setAttribute('readonly', '');
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      document.execCommand('copy');
      scratch.remove();
    }
    button.textContent = '✓ Copied';
  } catch {
    button.textContent = 'Copy failed — select manually';
  }
  setTimeout(() => {
    button.textContent = original;
  }, 1600);
}

/* ------------------------------------------------------------------ *
 * Persistence
 * ------------------------------------------------------------------ */

function savePrefs() {
  try {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        era: state.era,
        format: state.format,
        intensity: state.intensity,
        style: state.style,
        periodSubject: state.periodSubject,
        emphasis: state.emphasis,
        content: state.content,
        useNegative: state.useNegative,
        modelOverride: state.modelOverride,
      }),
    );
  } catch {
    /* storage may be unavailable in private mode */
  }
}

function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    if (saved.era && ERAS[saved.era]) state.era = saved.era;
    if (saved.format) state.format = saved.format;
    if (saved.intensity && INTENSITY_LEVELS[saved.intensity]) state.intensity = saved.intensity;
    if (saved.style === 'tags' || saved.style === 'natural') state.style = saved.style;
    state.periodSubject = Boolean(saved.periodSubject);
    state.emphasis = saved.emphasis !== false;
    if (saved.content && CONTENT_LEVELS[saved.content]) state.content = saved.content;
    state.useNegative = saved.useNegative === true;
    if (saved.modelOverride && MODELS[saved.modelOverride]) {
      state.modelOverride = saved.modelOverride;
    }
  } catch {
    /* ignore malformed prefs */
  }
}

function readHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
  } catch {
    return [];
  }
}

function saveHistory(result) {
  try {
    const entries = readHistory();
    entries.unshift({
      at: Date.now(),
      era: result.eraLabel,
      format: result.formatLabel,
      prompt: result.prompt,
      negative: result.negative,
      observation: { ...state.observation },
      // The full recipe. Without this, "Load" restored what the model saw but not
      // the settings that shaped it, so a result you liked could not be rebuilt.
      recipe: {
        era: state.era,
        format: state.format,
        intensity: state.intensity,
        style: state.style,
        periodSubject: state.periodSubject,
        emphasis: state.emphasis,
        variant: state.variant,
        content: state.content,
        adultConfirmed: state.adultConfirmed,
        extra: state.extra,
        useNegative: state.useNegative,
      },
    });
    localStorage.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, HISTORY_LIMIT)));
    renderHistory();
  } catch {
    /* non-fatal */
  }
}

function renderHistory() {
  const entries = readHistory();
  el.history.textContent = '';
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = 'Nothing yet.';
    el.history.appendChild(empty);
    return;
  }
  entries.forEach((entry, index) => {
    const row = document.createElement('div');
    row.className = 'history-item';

    const text = document.createElement('p');
    text.textContent = `${entry.era} · ${entry.prompt.slice(0, 70)}`;

    const restore = document.createElement('button');
    restore.type = 'button';
    restore.textContent = 'Load';
    restore.addEventListener('click', () => {
      state.observation = { ...(entry.observation || {}) };

      // Restore the settings too, so the prompt can actually be reproduced.
      const recipe = entry.recipe || {};
      if (recipe.era && ERAS[recipe.era]) state.era = recipe.era;
      if (recipe.format !== undefined) state.format = recipe.format;
      if (recipe.intensity && INTENSITY_LEVELS[recipe.intensity]) {
        state.intensity = recipe.intensity;
      }
      if (recipe.style === 'tags' || recipe.style === 'natural') state.style = recipe.style;
      if (recipe.content && CONTENT_LEVELS[recipe.content]) state.content = recipe.content;
      if (typeof recipe.variant === 'number') state.variant = recipe.variant;
      state.periodSubject = Boolean(recipe.periodSubject);
      state.emphasis = recipe.emphasis !== false;
      state.adultConfirmed = Boolean(recipe.adultConfirmed);
      state.useNegative = Boolean(recipe.useNegative);
      state.extra = recipe.extra || '';

      // Push all of it back into the controls.
      el.periodSubject.checked = state.periodSubject;
      el.emphasis.checked = state.emphasis;
      el.useNegative.checked = state.useNegative;
      el.extraTerms.value = state.extra;
      renderEraControls();
      renderIntensity();
      renderStyle();
      renderContent();
      fillFields();

      const result = recompile();
      const faithful = !entry.recipe || result.prompt === entry.prompt;
      setStatus(
        faithful
          ? `Restored forge ${index + 1} exactly.`
          : `Restored forge ${index + 1}, but the prompt differs — presets have changed since it was saved.`,
        faithful ? 'ok' : 'err',
      );
      savePrefs();
    });

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.textContent = 'Copy';
    copy.addEventListener('click', () => copyText(entry.prompt, copy));

    row.append(text, restore, copy);
    el.history.appendChild(row);
  });
}

/* ------------------------------------------------------------------ *
 * Settings
 * ------------------------------------------------------------------ */

function renderModelOptions() {
  for (const [key, model] of Object.entries(MODELS)) {
    const option = document.createElement('option');
    option.value = key;
    option.textContent = `${model.label} — ${model.approxDownload} · ${model.note}`;
    el.modelSelect.appendChild(option);
  }
  el.modelSelect.value = state.modelOverride || '';
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

function wire() {
  el.cameraBtn.addEventListener('click', () => el.cameraInput.click());
  el.galleryBtn.addEventListener('click', () => el.galleryInput.click());
  el.cameraInput.addEventListener('change', (e) => handleFile(e.target.files?.[0]));
  el.galleryInput.addEventListener('change', (e) => handleFile(e.target.files?.[0]));
  el.readBtn.addEventListener('click', runVision);

  el.periodSubject.addEventListener('change', () => {
    state.periodSubject = el.periodSubject.checked;
    recompile();
    savePrefs();
  });
  el.emphasis.addEventListener('change', () => {
    state.emphasis = el.emphasis.checked;
    recompile();
    savePrefs();
  });

  el.useNegative.addEventListener('change', () => {
    state.useNegative = el.useNegative.checked;
    recompile();
    savePrefs();
  });

  el.adultConfirmed.addEventListener('change', () => {
    state.adultConfirmed = el.adultConfirmed.checked;
    recompile();
  });
  el.extraTerms.addEventListener('input', () => {
    state.extra = el.extraTerms.value;
    recompile();
  });

  el.clearFields.addEventListener('click', () => {
    state.observation = {};
    fillFields();
    recompile();
  });
  el.variantBtn.addEventListener('click', () => {
    state.variant += 1;
    recompile();
  });

  el.copyPrompt.addEventListener('click', () => copyText(el.prompt.value, el.copyPrompt));
  el.copyNegative.addEventListener('click', () => copyText(el.negative.value, el.copyNegative));
  el.copyAll.addEventListener('click', () => {
    // Respect any manual edits made in the textareas.
    const edited = {
      ...lastResult,
      prompt: el.prompt.value,
      negative: el.negative.value,
    };
    copyText(settingsBlock(edited), el.copyAll);
    // Also record it: someone who types the fields by hand never runs the model,
    // and would otherwise never build up any history.
    saveHistory(edited);
  });

  el.clearHistory.addEventListener('click', () => {
    try {
      localStorage.removeItem(HISTORY_KEY);
    } catch {
      /* ignore */
    }
    renderHistory();
  });

  el.settingsBtn.addEventListener('click', () => {
    el.settingsPanel.open = !el.settingsPanel.open;
    if (el.settingsPanel.open) el.settingsPanel.scrollIntoView({ behavior: 'smooth' });
  });

  el.modelSelect.addEventListener('change', async () => {
    state.modelOverride = el.modelSelect.value || null;
    await unloadVision();
    updateModelWhy();
    savePrefs();
    setStatus('Model changed. It will load on the next read.', 'ok');
  });

  el.unloadBtn.addEventListener('click', async () => {
    state.runner?.terminate();
    state.runner = null;
    await unloadVision();
    setStatus('Model unloaded from memory.', 'ok');
  });

  el.resumeBtn.addEventListener('click', () => runVision({ resume: true }));
  el.discardBtn.addEventListener('click', () => {
    clearCheckpoint();
    state.resumeIndex = 0;
    hideResume();
  });

  el.notifyBtn.addEventListener('click', async () => {
    if (!('Notification' in window)) {
      setStatus('This browser does not support notifications.', 'err');
      return;
    }
    const permission = await Notification.requestPermission();
    setStatus(
      permission === 'granted'
        ? "Notifications on. You'll get one when a read finishes while you're away."
        : 'Notification permission was not granted.',
      permission === 'granted' ? 'ok' : 'err',
    );
  });

  // Report interruptions honestly rather than leaving a silently stalled bar.
  document.addEventListener('visibilitychange', async () => {
    if (document.hidden) return;
    if (state.running) {
      // The system drops a screen wake lock whenever the page is hidden.
      await acquireWakeLock();
    } else if (state.resumeIndex > 0 && state.resumeIndex < PASSES.length) {
      showResume();
    }
  });

  const updateOnline = () => {
    el.offlineState.textContent = navigator.onLine ? 'online' : 'offline (cached)';
  };
  window.addEventListener('online', updateOnline);
  window.addEventListener('offline', updateOnline);
  updateOnline();
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

function boot() {
  loadPrefs();
  el.periodSubject.checked = state.periodSubject;
  el.emphasis.checked = state.emphasis;
  el.useNegative.checked = state.useNegative;

  renderEraControls();
  renderIntensity();
  renderStyle();
  renderContent();
  renderFields();
  renderModelOptions();
  renderHistory();
  wire();
  recompile();

  // Capability detection is cheap and makes the settings panel honest up front.
  ensureCaps().catch(() => {
    el.modelWhy.textContent = 'Could not probe device capability; auto-detect will retry.';
  });

  // Say plainly what happens when the app goes away, rather than implying it
  // keeps working.
  el.backgroundNote.textContent = wakeLockSupported()
    ? 'Reading keeps the screen awake so it continues while the phone sits. Switching apps pauses it — progress is saved after every question and you can resume.'
    : 'Keep this screen on while reading. Switching apps or the screen sleeping pauses it — progress is saved after every question and you can resume.';

  // Offer to pick up an interrupted run from a previous session.
  restoreCheckpoint();

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('./sw.js').catch(() => {
        /* offline install is a bonus, not a requirement */
      });
    });
  }
}

boot();
