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
  loadVision,
  unloadVision,
  observe,
  describeError,
  PASSES,
} from './vision.js';

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
  adultConfirmed: false,
  extra: '',
  observation: {},
  source: null,
  imageDataUrl: null,
  imageCanvas: null,
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
  });
  lastResult = result;

  el.prompt.value = result.prompt;
  el.negative.value = result.negative;
  el.promptCount.textContent = `${result.prompt.length} chars`;
  el.negativeCount.textContent = `${result.negativeList.length} terms`;
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
    state.imageCanvas = prepared.canvas; // what inference actually reads
    state.source = prepared.original;

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

async function runVision() {
  if (state.running) {
    state.abort?.abort();
    return;
  }
  if (!state.imageCanvas) return;

  state.running = true;
  state.abort = new AbortController();
  el.readBtn.textContent = 'Cancel';

  try {
    const caps = await ensureCaps();
    const choice = chooseModel(caps, state.modelOverride);

    setStatus(`Loading ${choice.label} (${choice.approxDownload} first time)…`, '', -1);
    const engine = await loadVision(choice, {
      onProgress: (p) => {
        if (p.phase === 'download' && p.total) {
          const pct = Math.round((p.loaded / p.total) * 100);
          setStatus(`Downloading ${p.file || 'model'} — ${pct}%`, '', pct);
        } else if (p.phase === 'library') {
          setStatus('Loading runtime…', '', -1);
        } else if (p.phase === 'processor') {
          setStatus('Loading processor…', '', -1);
        } else if (p.phase === 'model') {
          setStatus('Initialising model…', '', -1);
        }
      },
    });

    const { observation, failures } = await observe(engine, state.imageCanvas, {
      signal: state.abort.signal,
      onProgress: ({ index, total, field }) => {
        setStatus(
          `Reading image — ${FIELD_LABELS[field] || field} (${index + 1}/${total})`,
          '',
          Math.round((index / total) * 100),
        );
      },
    });

    state.observation = { ...state.observation, ...observation };
    fillFields();
    const result = recompile();
    saveHistory(result);

    if (failures.length) {
      setStatus(
        `Done, but ${failures.length} of ${PASSES.length} questions failed. Edit any blank field by hand.`,
        'err',
      );
    } else {
      setStatus('Done. Check the fields, then copy the prompt.', 'ok');
    }
  } catch (err) {
    setStatus(describeError(err), 'err');
  } finally {
    state.running = false;
    state.abort = null;
    el.readBtn.textContent = 'Read image';
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
      observation: state.observation,
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
      fillFields();
      recompile();
      setStatus(`Loaded forge ${index + 1} from history.`, 'ok');
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
    await unloadVision();
    setStatus('Model unloaded from memory.', 'ok');
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

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('./sw.js').catch(() => {
        /* offline install is a bonus, not a requirement */
      });
    });
  }
}

boot();
