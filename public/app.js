/* RealFrame frontend. No framework — the surface is small enough not to need one. */

const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  compiled: null,
  activeGroups: new Set(),
  initImage: null,
  pollTimer: null,
  compileTimer: null,
};

const INTENSITY_HINTS = [
  'Off — your text passes through untouched, negative prompt only.',
  'Light — camera body and basic lighting.',
  'Moderate — adds optical character and depth of field.',
  'Strong — full capture stack plus subject detail.',
  'Maximum — every imperfection layer, including capture faults.',
];

/* ---------- helpers ---------- */

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    // A browser-level failure means RealFrame itself is unreachable, which is
    // a different problem from a generation failing. Say which.
    setConnectionLost(true);
    throw new Error('Lost contact with RealFrame. Is the window running npm start still open?');
  }
  setConnectionLost(false);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function setConnectionLost(lost) {
  if (state.connectionLost === lost) return;
  state.connectionLost = lost;
  const chip = $('status-chip');
  if (lost) {
    state.lastChip = { text: chip.textContent, cls: chip.className };
    chip.textContent = 'RealFrame not responding';
    chip.className = 'status warn';
  } else if (state.lastChip) {
    chip.textContent = state.lastChip.text;
    chip.className = state.lastChip.cls;
  }
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function showError(message) {
  const el = $('composer-error');
  el.textContent = message;
  el.classList.toggle('hidden', !message);
}

/* ---------- boot ---------- */

async function boot() {
  try {
    state.config = await api('/api/config');
  } catch {
    $('status-chip').textContent = 'server unreachable';
    $('status-chip').className = 'status warn';
    return;
  }

  const cfg = state.config;

  // Status chip reflects whether generation can actually succeed right now.
  const chip = $('status-chip');
  if (cfg.provider === 'custom') {
    chip.textContent = cfg.hasCustomEndpoint ? 'custom endpoint ready' : 'custom endpoint not configured';
    chip.className = `status ${cfg.hasCustomEndpoint ? 'ok' : 'warn'}`;
  } else {
    chip.textContent = cfg.hasToken ? 'hugging face ready' : 'no HF_TOKEN — add one to .env';
    chip.className = `status ${cfg.hasToken ? 'ok' : 'warn'}`;
  }

  // Presets
  const presetSel = $('preset');
  presetSel.innerHTML = cfg.presets
    .map((p) => `<option value="${p.key}">${p.label}</option>`)
    .join('');
  presetSel.value = 'documentary';
  updatePresetSummary();

  // Models
  $('model-list').innerHTML = cfg.models
    .map((m) => `<option value="${m.id}">${m.label} · ${m.kind} — ${m.notes}</option>`)
    .join('');
  $('model').value = cfg.defaultModel;
  $('provider').value = cfg.provider;
  applyModelDefaults();

  // Negative groups — all on by default.
  const groupBox = $('negative-groups');
  groupBox.innerHTML = cfg.negativeGroups
    .map((g) => `<button type="button" class="chip on" data-group="${g.key}" title="${g.terms.slice(0, 6).join(', ')}…">${g.key}</button>`)
    .join('');
  // Quality presets
  $('quality').innerHTML = Object.entries(cfg.quality)
    .map(([key, q]) => `<option value="${key}">${q.label} — ${q.steps} steps</option>`)
    .join('');
  $('quality').value = 'standard';
  $('num_inference_steps').value = cfg.quality.standard.steps;
  updateDurationHint();
  updateQualityHint();

  cfg.negativeGroups.forEach((g) => state.activeGroups.add(g.key));
  groupBox.addEventListener('click', (e) => {
    const btn = e.target.closest('.chip');
    if (!btn) return;
    const key = btn.dataset.group;
    if (state.activeGroups.has(key)) state.activeGroups.delete(key);
    else state.activeGroups.add(key);
    btn.classList.toggle('on');
    scheduleCompile();
  });

  wireEvents();
  refreshJobs();
}

function updatePresetSummary() {
  const preset = state.config.presets.find((p) => p.key === $('preset').value);
  $('preset-summary').textContent = preset ? `${preset.summary} — ${preset.camera}.` : '';
}

/**
 * Adopt the parameters a local endpoint reports for whatever it loaded.
 * Without this the fields carry defaults for the hosted model named in the
 * dropdown, which is a different architecture entirely — that mismatch is what
 * sends a Wan frame count and guidance value to a Stable Diffusion model.
 */
function applyLocalDefaults(info) {
  const defaults = info.defaults || {};
  state.localInfo = info;

  for (const [key, value] of Object.entries(defaults)) {
    const input = $(key);
    if (input && value != null) {
      input.value = value;
      delete input.dataset.touched;
    }
  }

  $('model-note').textContent = `Running locally: ${info.label || info.model}. `
    + 'The model box above is only a label on this backend.';

  updateDurationHint();
  updateQualityHint();
}

function applyModelDefaults() {
  const model = state.config.models.find((m) => m.id === $('model').value);
  $('model-note').textContent = model ? model.notes : 'Custom model id — parameters passed through as entered.';

  // Switching to a text-to-video model while a start frame is loaded would
  // silently drop the frame, so say so.
  const note = $('init-image-note');
  if (state.initImage && model && model.kind !== 'image-to-video') {
    note.textContent = `Warning: ${model.label} is text-to-video and will ignore the start frame.`;
    note.classList.remove('hidden');
  } else if (state.initImage && model) {
    note.textContent = `Using ${model.label}.`;
    note.classList.remove('hidden');
  }

  if (!model) return;
  for (const [key, value] of Object.entries(model.defaultParams)) {
    const input = $(key);
    if (input && !input.dataset.touched) input.value = value;
  }
}

/* ---------- prompt compilation ---------- */

function currentOptions() {
  return {
    subject: $('subject').value,
    preset: $('preset').value,
    intensity: Number($('intensity').value),
    extraNegative: $('extra-negative').value,
    negativeGroups: [...state.activeGroups],
  };
}

/**
 * Explain what a requested length actually costs: how many passes it takes,
 * and roughly how much longer than a single clip.
 */
function updateDurationHint() {
  const seconds = Number($('duration').value);
  const model = state.config.models.find((m) => m.id === $('model').value);
  const hint = $('duration-hint');

  $('duration-value').textContent = `${seconds}s`;

  // A local endpoint knows its own ceiling, which depends on its GPU and the
  // frame size — the hosted catalog cannot speak for it.
  const local = state.localInfo;
  if (local && local.max_frames) {
    const fps = Number($('fps').value) || local.defaults?.fps || 16;
    const maxSeconds = local.max_frames / fps;
    const size = `${local.defaults?.width}x${local.defaults?.height}`;

    if (seconds <= maxSeconds + 0.05) {
      hint.textContent = `One pass on your GPU — up to ${maxSeconds.toFixed(1)}s at ${size}.`;
      hint.className = 'hint';
    } else {
      hint.innerHTML = `<span class="warn-text">Your GPU tops out at `
        + `${maxSeconds.toFixed(1)}s at ${size}</span>, so this will come back shorter. `
        + 'A smaller frame buys more time: restart the GPU server with '
        + '<code>LOCAL_WIDTH=320 LOCAL_HEIGHT=192</code> for roughly '
        + `${(local.max_frames * (local.defaults.width * local.defaults.height) / (320 * 192) / fps).toFixed(1)}s.`;
    }
    return;
  }

  if (!model) {
    hint.textContent = 'Custom model — length is passed through as requested.';
    return;
  }

  const fps = Number($('fps').value) || model.defaultParams.fps || 16;
  const segments = Math.max(1, Math.ceil((seconds * fps) / model.maxFrames));

  if (segments === 1) {
    hint.textContent = `One pass. ${model.label} handles this length directly.`;
    hint.className = 'hint';
    return;
  }

  if (!model.continuation) {
    hint.innerHTML = `<span class="bad">${escapeHtml(model.label)} cannot be extended — `
      + `it has no image-to-video counterpart. Length will be capped at `
      + `${(model.maxFrames / fps).toFixed(1)}s. Try Wan or LTX.</span>`;
    return;
  }

  if (!state.config.hasFfmpeg) {
    hint.innerHTML = '<span class="bad">Needs ffmpeg to join segments, and none was found. '
      + 'Setting up the local GPU server installs one.</span>';
    return;
  }

  hint.innerHTML = `<span class="warn-text">${segments} chained passes</span> — each continues from `
    + `the last frame of the one before. Roughly ${segments}× the time of a single clip, and detail `
    + `drifts a little at each join.`;
}

function updateQualityHint() {
  const q = state.config.quality[$('quality').value];
  if (!q) return;
  const seconds = Number($('duration').value);
  const model = state.config.models.find((m) => m.id === $('model').value);
  const fps = Number($('fps').value) || model?.defaultParams.fps || 16;
  const segments = model ? Math.max(1, Math.ceil((seconds * fps) / model.maxFrames)) : 1;
  const factor = (q.timeFactor * segments).toFixed(1);
  $('quality-hint').textContent = `${q.summary} ${q.steps} steps, about ${factor}× a standard single clip.`;
}

async function compile() {
  const subject = $('subject').value.trim();
  if (!subject) {
    $('compiled-prompt').textContent = 'Describe a shot to see the compiled prompt.';
    $('compiled-negative').textContent = '';
    $('score-after').textContent = '—';
    $('score-delta').textContent = '';
    $('compile-notes').innerHTML = '';
    $('analysis').classList.add('hidden');
    state.compiled = null;
    return;
  }

  try {
    const result = await api('/api/compile', { method: 'POST', body: currentOptions() });
    state.compiled = result;

    $('compiled-prompt').textContent = result.prompt;
    $('compiled-negative').textContent = result.negativePrompt || '(none)';
    $('score-after').textContent = result.score.after;

    const delta = result.score.after - result.score.before;
    $('score-delta').innerHTML = delta > 0
      ? `<span class="up">+${delta}</span> from your raw description (${result.score.before})`
      : `unchanged from your raw description (${result.score.before})`;

    $('compile-notes').innerHTML = result.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('');

    // Warnings come from the *raw* subject, so they tell the user what to fix
    // in their own text rather than in the generated modifiers.
    const analysis = $('analysis');
    if (result.warnings.length) {
      analysis.innerHTML = `<h4>Terms working against realism</h4><ul>${
        result.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')
      }</ul>`;
      analysis.classList.remove('hidden');
    } else {
      analysis.classList.add('hidden');
    }
    showError('');
  } catch (err) {
    showError(err.message);
  }
}

const scheduleCompile = debounce(compile, 250);

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ---------- generation ---------- */

async function generate() {
  const subject = $('subject').value.trim();
  if (!subject) {
    showError('Describe the shot first.');
    return;
  }

  const btn = $('generate');
  btn.disabled = true;
  btn.textContent = 'Queueing…';

  const params = {};
  for (const key of ['num_frames', 'fps', 'guidance_scale', 'num_inference_steps', 'seed']) {
    const raw = $(key).value;
    if (raw !== '') params[key] = Number(raw);
  }

  try {
    await api('/api/generate', {
      method: 'POST',
      body: {
        ...currentOptions(),
        provider: $('provider').value,
        model: $('model').value,
        durationSeconds: Number($('duration').value),
        quality: $('quality').value,
        params,
        initImage: state.initImage,
      },
    });
    showError('');
    refreshJobs();
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate';
  }
}

/* ---------- job list ---------- */

function jobCard(job) {
  const meta = [
    job.model?.split('/').pop(),
    job.params?.num_frames ? `${job.params.num_frames}f` : null,
    job.params?.fps ? `${job.params.fps}fps` : null,
    job.compiled?.preset?.label,
  ].filter(Boolean);

  let media;
  if (job.status === 'done') {
    media = `<video src="${job.videoUrl}" controls loop muted playsinline preload="metadata"></video>`;
  } else if (job.status === 'error') {
    media = `<div class="job-state error"><div class="job-error-detail">${escapeHtml(job.error || 'Failed.')}</div></div>`;
  } else if (job.status === 'cancelled') {
    media = '<div class="job-state">Cancelled.</div>';
  } else {
    let label = job.status === 'running' ? 'Generating — this takes minutes' : 'Queued';
    const p = job.segmentProgress;
    if (p) {
      label = p.stitching
        ? `Joining ${p.total} segments`
        : `Generating segment ${p.current} of ${p.total}`;
    }
    const retry = job.attempts?.length ? ` (retry ${job.attempts.length})` : '';
    media = `<div class="job-state"><span class="spinner"></span>${label}${retry}…</div>`;
  }

  if (job.plan?.segments > 1) meta.push(`${job.plan.segments} segments`);
  if (job.plan?.actualSeconds) meta.push(`${job.plan.actualSeconds}s`);
  if (job.quality) meta.push(job.quality);

  const actions = [];
  if (job.status === 'done') {
    actions.push(`<a class="link" href="${job.videoUrl}" download="realframe-${job.id.slice(0, 8)}.mp4">download</a>`);
  }
  if (job.status === 'queued' || job.status === 'running') {
    actions.push(`<button class="link" data-cancel="${job.id}">cancel</button>`);
  }
  actions.push(`<button class="link" data-reuse="${job.id}">reuse prompt</button>`);
  actions.push(`<button class="link danger" data-delete="${job.id}">delete</button>`);

  return `
    <article class="job" data-id="${job.id}">
      ${media}
      <div class="job-body">
        <p class="job-subject">${escapeHtml(job.subject || '')}</p>
        <div class="job-meta">${meta.map((m) => `<span>${escapeHtml(m)}</span>`).join('')}</div>
      </div>
      <div class="job-actions">${actions.join('')}</div>
    </article>`;
}

async function refreshJobs() {
  let jobs;
  try {
    ({ jobs } = await api('/api/jobs'));
  } catch {
    // api() has already flagged the connection; keep polling so the page
    // recovers on its own once the server is back.
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(refreshJobs, 3000);
    return;
  }

  const box = $('jobs');
  if (!jobs.length) {
    box.innerHTML = '<p class="empty">Nothing generated yet.</p>';
  } else {
    // Rebuilding wholesale would restart playing videos, so only redraw when
    // the set of cards or their states actually changed.
    const signature = jobs.map((j) => `${j.id}:${j.status}:${j.attempts?.length || 0}`).join('|');
    if (box.dataset.signature !== signature) {
      box.dataset.signature = signature;
      box.innerHTML = jobs.map(jobCard).join('');
    }
  }

  $('clear-all').classList.toggle('hidden', !jobs.some(
    (j) => j.status !== 'running' && j.status !== 'queued',
  ));

  const busy = jobs.some((j) => j.status === 'queued' || j.status === 'running');
  clearTimeout(state.pollTimer);
  if (busy) state.pollTimer = setTimeout(refreshJobs, 2500);
  state.jobs = jobs;
}

/* ---------- events ---------- */

function wireEvents() {
  $('subject').addEventListener('input', scheduleCompile);
  $('extra-negative').addEventListener('input', scheduleCompile);

  $('preset').addEventListener('change', () => {
    updatePresetSummary();
    compile();
  });

  $('intensity').addEventListener('input', (e) => {
    const v = Number(e.target.value);
    $('intensity-value').textContent = v;
    $('intensity-hint').textContent = INTENSITY_HINTS[v];
    scheduleCompile();
  });

  $('duration').addEventListener('input', () => {
    updateDurationHint();
    updateQualityHint();
  });

  // Quality is a shortcut for step count, so write it into the visible field
  // rather than applying it invisibly at the server.
  $('quality').addEventListener('change', () => {
    const q = state.config.quality[$('quality').value];
    if (q) {
      const steps = $('num_inference_steps');
      steps.value = q.steps;
      delete steps.dataset.touched;
    }
    updateQualityHint();
  });

  $('model').addEventListener('change', () => {
    applyModelDefaults();
    updateDurationHint();
    updateQualityHint();
  });
  $('provider').addEventListener('change', () => {
    const custom = $('provider').value === 'custom';
    $('model-note').textContent = custom
      ? 'Sent to CUSTOM_ENDPOINT. Model id is passed through; your endpoint decides what it means.'
      : (state.config.models.find((m) => m.id === $('model').value)?.notes || '');
  });

  // Mark numeric params as user-edited so model defaults stop overwriting them.
  for (const key of ['num_frames', 'fps', 'guidance_scale', 'num_inference_steps']) {
    $(key).addEventListener('input', (e) => { e.target.dataset.touched = '1'; });
  }

  $('init-image').addEventListener('change', async (e) => {
    const file = e.target.files?.[0];
    const note = $('init-image-note');

    if (!file) {
      state.initImage = null;
      note.textContent = '';
      note.classList.add('hidden');
      return;
    }

    const reader = new FileReader();
    reader.onload = () => { state.initImage = reader.result; };
    reader.readAsDataURL(file);

    // A start frame is meaningless to a text-to-video model, so move to an
    // image-to-video one rather than letting the upload silently do nothing.
    const current = state.config.models.find((m) => m.id === $('model').value);
    if (current && current.kind === 'image-to-video') {
      note.textContent = `Using ${current.label}.`;
      note.classList.remove('hidden');
      return;
    }

    const i2v = state.config.models.find((m) => m.kind === 'image-to-video');
    if (i2v) {
      $('model').value = i2v.id;
      applyModelDefaults();
      note.textContent = `Switched to ${i2v.label} — the selected model does not accept a start frame.`;
    } else {
      note.textContent = 'Warning: the selected model is text-to-video and will ignore this start frame.';
    }
    note.classList.remove('hidden');
  });

  $('generate').addEventListener('click', generate);

  $('clear-all').addEventListener('click', async () => {
    const finished = (state.jobs || []).filter(
      (j) => j.status !== 'running' && j.status !== 'queued',
    ).length;
    if (!window.confirm(`Delete ${finished} finished render${finished === 1 ? '' : 's'}? Video files are removed from disk.`)) return;
    await api('/api/jobs/all', { method: 'DELETE' });
    $('jobs').dataset.signature = '';
    refreshJobs();
  });

  $('probe').addEventListener('click', async () => {
    const out = $('probe-result');
    out.textContent = 'Checking…';
    try {
      const model = encodeURIComponent($('model').value);
      const provider = $('provider').value;
      const info = await api(`/api/probe?model=${model}&provider=${provider}`);

      // Custom backend: the endpoint reports what it actually loaded, which
      // has nothing to do with the model named in the box above.
      if (info.local) {
        if (!info.reachable) {
          out.innerHTML = `<span class="bad">${escapeHtml(info.hint)}</span>`;
          return;
        }
        applyLocalDefaults(info);
        const bits = [info.hint];
        if (info.offload && info.offload !== 'unknown') bits.push(`offload: ${info.offload}`);
        out.innerHTML = `<span class="good">${escapeHtml(bits.join(' · '))}</span>`
          + '<br>Parameters below set to suit it.';
        return;
      }

      if (!info.providers.length) {
        out.innerHTML = `<span class="bad">${escapeHtml(info.hint)}</span>`;
        return;
      }
      const rows = info.providers
        .map((p) => `${p.provider}${p.status === 'live' ? '' : ` (${p.status})`}`)
        .join(', ');
      out.innerHTML = `<span class="good">Served by: ${escapeHtml(rows)}</span>`;
    } catch (err) {
      out.innerHTML = `<span class="bad">${escapeHtml(err.message)}</span>`;
    }
  });

  $('copy-prompt').addEventListener('click', () => {
    if (state.compiled) copy(state.compiled.prompt, $('copy-prompt'));
  });

  document.addEventListener('click', (e) => {
    const copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      copy($(copyBtn.dataset.copy).textContent, copyBtn);
      return;
    }

    const cancelBtn = e.target.closest('[data-cancel]');
    if (cancelBtn) {
      api(`/api/jobs/${cancelBtn.dataset.cancel}`, { method: 'DELETE' }).then(refreshJobs);
      return;
    }

    const deleteBtn = e.target.closest('[data-delete]');
    if (deleteBtn) {
      const id = deleteBtn.dataset.delete;
      const job = state.jobs?.find((j) => j.id === id);
      const active = job && (job.status === 'running' || job.status === 'queued');
      const message = active
        ? 'This render is still running. Cancel it and delete it?'
        : 'Delete this render? The video file is removed from disk.';
      if (!window.confirm(message)) return;

      api(`/api/jobs/${id}?purge=true`, { method: 'DELETE' }).then(() => {
        // Force a redraw — the signature check would otherwise see no change.
        $('jobs').dataset.signature = '';
        refreshJobs();
      });
      return;
    }

    const reuseBtn = e.target.closest('[data-reuse]');
    if (reuseBtn) {
      const job = state.jobs?.find((j) => j.id === reuseBtn.dataset.reuse);
      if (!job) return;
      $('subject').value = job.subject || '';
      if (job.compiled?.preset?.key) $('preset').value = job.compiled.preset.key;
      if (typeof job.compiled?.intensity === 'number') {
        $('intensity').value = job.compiled.intensity;
        $('intensity-value').textContent = job.compiled.intensity;
        $('intensity-hint').textContent = INTENSITY_HINTS[job.compiled.intensity];
      }
      updatePresetSummary();
      compile();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  });

  // Ctrl/Cmd+Enter generates from anywhere in the composer.
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') generate();
  });
}

async function copy(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const original = btn.textContent;
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = original; }, 1200);
  } catch { /* clipboard unavailable */ }
}

boot();
