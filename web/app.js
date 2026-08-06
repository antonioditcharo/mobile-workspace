/* Filmroom - mobile client.
   Vanilla JS, no build step: the server serves this file as-is, which keeps
   the whole thing debuggable from a phone and dependency-free forever. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  options: null,
  preset: null,
  aspect: null,
  model: null,
  job: null,
  events: null,
  viewer: { items: [], index: 0, source: 'stage' },
  lastSeed: null,
  stage: [],
};

const STORE_KEY = 'filmroom.settings.v1';

const IDEAS = [
  'two friends on a couch mid-laugh, cluttered living room',
  'a man waiting at a bus stop in the rain, hood up',
  'birthday party in a small kitchen, paper hats, cake lit up',
  'girl sitting on the hood of a car at dusk, empty parking lot',
  'family dog asleep on a worn armchair, afternoon light',
  'crowded house party hallway, people talking over each other',
  'kid holding a fish they caught, standing on a dock',
  'woman looking out a train window, condensation on the glass',
  'group photo at a diner booth, plates everywhere',
  'someone blowing out candles, room lit only by the cake',
];

/* --------------------------------------------------------------- helpers */

function toast(msg, ms = 2400) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function saveSettings() {
  const data = {
    preset: state.preset,
    aspect: state.aspect,
    model: state.model,
    steps: +$('#steps').value,
    guidance: +$('#guidance').value,
    batch: +$('#batch').value,
    sampler: $('#sampler').value,
    hires: $('#hires').checked,
    negative: $('#negative').value,
    prompt: $('#prompt').value,
  };
  try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); } catch (_) {}
}

function loadSettings() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; }
  catch (_) { return {}; }
}

/* --------------------------------------------------------------- boot */

async function boot() {
  registerServiceWorker();
  bindUI();

  try {
    state.options = await api('/api/options');
  } catch (err) {
    toast('Cannot reach the server: ' + err.message, 6000);
    setStatus('bad', 'offline');
    return;
  }

  const saved = loadSettings();
  const d = state.options.defaults;

  state.preset = saved.preset || d.preset;
  state.aspect = saved.aspect || d.aspect;
  state.model = saved.model || d.model;

  renderPresets();
  renderAspects();
  renderSamplers(saved.sampler);
  renderModels();

  if (saved.steps) $('#steps').value = saved.steps;
  if (saved.guidance) $('#guidance').value = saved.guidance;
  if (saved.batch) $('#batch').value = saved.batch;
  if (saved.hires) $('#hires').checked = true;
  if (saved.negative) $('#negative').value = saved.negative;
  if (saved.prompt) $('#prompt').value = saved.prompt;
  $('#batch').max = d.max_batch;
  $('#steps').max = d.max_steps;
  syncSliderLabels();

  refreshHealth();
  loadGallery();
  setInterval(refreshHealth, 20000);
}

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
}

/* --------------------------------------------------------------- render */

function renderPresets() {
  const wrap = $('#preset-chips');
  wrap.innerHTML = '';
  state.options.presets.forEach((p) => {
    const btn = document.createElement('button');
    btn.className = 'chip';
    btn.setAttribute('aria-pressed', String(p.id === state.preset));
    btn.innerHTML =
      `<div class="chip-top"><span>${p.emoji}</span>${escapeHtml(p.label)}</div>` +
      `<div class="chip-sub">${escapeHtml(p.blurb)}</div>`;
    btn.onclick = () => selectPreset(p);
    wrap.appendChild(btn);
  });
}

function selectPreset(p) {
  state.preset = p.id;
  // A preset is a complete recipe: sampler, steps, guidance and framing are
  // tuned per look, so selecting one resets those rather than keeping stale
  // values from a different era's settings.
  $('#steps').value = p.steps;
  $('#guidance').value = p.guidance;
  $('#sampler').value = p.sampler;
  state.aspect = p.aspect;
  syncSliderLabels();
  renderPresets();
  renderAspects();
  saveSettings();
}

function renderAspects() {
  const wrap = $('#aspect-chips');
  wrap.innerHTML = '';
  state.options.aspects.forEach((a) => {
    const btn = document.createElement('button');
    btn.className = 'chip';
    btn.setAttribute('aria-pressed', String(a.id === state.aspect));
    btn.innerHTML =
      `<div class="chip-top">${escapeHtml(a.label)}</div>` +
      `<div class="chip-sub">${a.width}×${a.height}</div>`;
    btn.onclick = () => { state.aspect = a.id; renderAspects(); saveSettings(); };
    wrap.appendChild(btn);
  });
}

function renderSamplers(saved) {
  const sel = $('#sampler');
  sel.innerHTML = '';
  state.options.samplers.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.label;
    sel.appendChild(opt);
  });
  const preset = state.options.presets.find((p) => p.id === state.preset);
  sel.value = saved || (preset ? preset.sampler : 'dpmpp_2m_karras');
}

function renderModels() {
  const wrap = $('#model-list');
  wrap.innerHTML = '';
  state.options.models.forEach((m) => {
    const btn = document.createElement('button');
    btn.className = 'model';
    btn.setAttribute('aria-pressed', String(m.id === state.model));
    const badges = [
      m.recommended ? '<span class="badge warn">recommended</span>' : '',
      m.loaded ? '<span class="badge on">loaded</span>' : '',
      m.cached ? '<span class="badge">on disk</span>'
               : '<span class="badge">will download</span>',
      m.heavy ? '<span class="badge">slow on 4GB</span>' : '',
    ].join('');
    btn.innerHTML =
      `<div class="model-body">` +
      `<div class="model-name">${escapeHtml(m.label)}${badges}</div>` +
      `<div class="model-sub">${escapeHtml(m.blurb)}</div></div>`;
    btn.onclick = () => { state.model = m.id; renderModels(); saveSettings(); };
    wrap.appendChild(btn);
  });
}

async function refreshHealth() {
  try {
    const h = await api('/api/health');
    const hw = h.hardware;
    if (h.stub) setStatus('stub', 'stub mode');
    else if (hw.ready && hw.device === 'cuda') setStatus('ok', hw.gpu_name || 'gpu');
    else if (hw.ready) setStatus('stub', hw.device);
    else setStatus('bad', 'no backend');

    $('#output-path').textContent = h.output_dir;

    const rows = [
      ['Device', hw.device],
      ['GPU', hw.gpu_name || '—'],
      ['VRAM', hw.vram_gb ? hw.vram_gb + ' GB' : '—'],
      ['PyTorch', hw.torch || 'not installed'],
      ['diffusers', hw.diffusers || 'not installed'],
      ['Long prompts', hw.compel ? 'enabled (compel)' : 'TRUNCATED - install compel'],
      ['Model', h.pipeline.loaded || 'none loaded'],
      ['Status', hw.detail || h.pipeline.detail || '—'],
      ['Phone URL', h.lan_url],
    ];
    $('#hw-info').innerHTML = rows
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${escapeHtml(String(v))}</dd></div>`)
      .join('');
  } catch (_) {
    setStatus('bad', 'offline');
  }
}

function setStatus(kind, text) {
  $('#status-chip').dataset.state = kind;
  $('#status-text').textContent = text;
}

/* --------------------------------------------------------------- generate */

async function generate() {
  const body = {
    prompt: $('#prompt').value,
    negative: $('#negative').value,
    preset: state.preset,
    model: state.model,
    aspect: state.aspect,
    steps: +$('#steps').value,
    guidance: +$('#guidance').value,
    sampler: $('#sampler').value,
    seed: +$('#seed').value,
    batch: +$('#batch').value,
    hires: $('#hires').checked,
  };

  saveSettings();
  setGenerating(true);
  state.stage = [];
  $('#stage-images').innerHTML = '';
  $('#stage-empty').hidden = true;
  setProgress(0, 'Submitting…');

  let res;
  try {
    res = await api('/api/generate', { method: 'POST', body: JSON.stringify(body) });
  } catch (err) {
    setGenerating(false);
    toast('Failed: ' + err.message, 5000);
    return;
  }

  state.job = res.job.id;
  listenToJob(res.job.id);
}

function listenToJob(jobId) {
  if (state.events) state.events.close();
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  state.events = es;

  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);

    if (ev.type === 'progress') {
      setProgress(ev.progress, ev.message);
    } else if (ev.type === 'status') {
      setProgress(ev.progress ?? 0, ev.message);
      if (ev.status === 'error') {
        toast('Error: ' + (ev.error || 'unknown'), 7000);
        finishJob();
      }
    } else if (ev.type === 'image') {
      addStageImage(ev.image);
      state.lastSeed = ev.image.seed;
    } else if (ev.type === 'end') {
      finishJob();
      loadGallery();
    }
  };

  es.onerror = () => {
    // The browser retries automatically; only give up once the job is done.
    if (!state.job) es.close();
  };
}

function finishJob() {
  state.job = null;
  if (state.events) { state.events.close(); state.events = null; }
  setGenerating(false);
  $('#stage-progress').hidden = true;
}

function addStageImage(image) {
  state.stage.push(image);
  const img = document.createElement('img');
  img.src = image.preview || image.url;
  img.alt = '';
  img.onclick = () => openViewer(state.stage, state.stage.indexOf(image), 'stage');
  $('#stage-images').appendChild(img);
  $('#stage-empty').hidden = true;
}

function setProgress(fraction, message) {
  $('#stage-progress').hidden = false;
  const pct = Math.round((fraction || 0) * 100);
  $('#progress-pct').textContent = pct + '%';
  $('#ring-fg').style.strokeDashoffset = String(327 - (327 * pct) / 100);
  $('#progress-msg').textContent = message || '';
}

function setGenerating(on) {
  $('#btn-generate').disabled = on;
  $('#btn-generate').textContent = on ? 'Working…' : 'Generate';
}

/* --------------------------------------------------------------- gallery */

async function loadGallery() {
  let data;
  try { data = await api('/api/gallery'); } catch (_) { return; }
  const grid = $('#gallery-grid');
  grid.innerHTML = '';
  $('#gallery-empty').hidden = data.images.length > 0;

  data.images.forEach((item, i) => {
    const img = document.createElement('img');
    img.src = item.thumb;
    img.loading = 'lazy';
    img.alt = '';
    img.onclick = () => openViewer(data.images, i, 'gallery');
    grid.appendChild(img);
  });
}

/* --------------------------------------------------------------- viewer */

function openViewer(items, index, source) {
  state.viewer = { items, index, source };
  const wrap = $('#viewer-img-wrap');
  wrap.innerHTML = '';
  items.forEach((item) => {
    const img = document.createElement('img');
    img.src = item.url;
    img.alt = '';
    wrap.appendChild(img);
  });
  $('#viewer').hidden = false;
  $('#viewer-info').hidden = true;
  requestAnimationFrame(() => {
    wrap.scrollLeft = index * wrap.clientWidth;
    updateViewerCount();
  });
  wrap.onscroll = () => {
    const i = Math.round(wrap.scrollLeft / Math.max(wrap.clientWidth, 1));
    if (i !== state.viewer.index) { state.viewer.index = i; updateViewerCount(); }
  };
}

function updateViewerCount() {
  const { items, index } = state.viewer;
  $('#viewer-count').textContent = `${index + 1} / ${items.length}`;
}

function currentImage() {
  return state.viewer.items[state.viewer.index];
}

async function saveToPhone() {
  const item = currentImage();
  if (!item) return;
  // Content-Disposition on the server makes Android Chrome write it straight
  // to /Downloads rather than opening it in a tab.
  const a = document.createElement('a');
  a.href = item.url + '?download=1';
  a.download = item.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('Saved to Downloads');
}

async function shareImage() {
  const item = currentImage();
  if (!item) return;
  try {
    const blob = await (await fetch(item.url)).blob();
    const file = new File([blob], item.filename, { type: 'image/png' });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file] });
      return;
    }
    toast('Sharing not supported here - use Save');
  } catch (err) {
    if (err && err.name !== 'AbortError') toast('Share failed');
  }
}

function reuseSettings() {
  const item = currentImage();
  const meta = (item && item.meta) || {};
  if (!meta.preset) { toast('No settings stored for this image'); return; }

  $('#prompt').value = meta.prompt || '';
  $('#negative').value = meta.negative || '';
  $('#steps').value = meta.steps || 30;
  $('#guidance').value = meta.guidance || 6;
  $('#seed').value = meta.seed ?? -1;
  $('#hires').checked = !!meta.hires;
  state.preset = meta.preset;
  if (meta.model) state.model = meta.model;
  $('#sampler').value = meta.sampler || 'dpmpp_2m_karras';

  const match = state.options.aspects.find(
    (a) => a.width === meta.width && a.height === meta.height
  );
  if (match) state.aspect = match.id;

  syncSliderLabels();
  renderPresets();
  renderAspects();
  renderModels();
  saveSettings();
  closeViewer();
  switchView('create');
  toast('Settings restored');
}

async function deleteImage() {
  const item = currentImage();
  if (!item) return;
  if (!confirm('Delete this image from the laptop?')) return;
  try {
    await api(`/api/images/${encodeURIComponent(item.filename)}`, { method: 'DELETE' });
    toast('Deleted');
    closeViewer();
    loadGallery();
  } catch (err) {
    toast('Delete failed: ' + err.message);
  }
}

function showInfo() {
  const panel = $('#viewer-info');
  if (!panel.hidden) { panel.hidden = true; return; }
  const meta = (currentImage() || {}).meta || {};
  panel.innerHTML =
    `<h4>Prompt sent</h4><pre>${escapeHtml(meta.full_prompt || '—')}</pre>` +
    `<h4>Negative sent</h4><pre>${escapeHtml(meta.full_negative || '—')}</pre>` +
    `<h4>Settings</h4><pre>${escapeHtml(
      [
        `preset: ${meta.preset_label || meta.preset || '—'}`,
        `model: ${meta.model || '—'}`,
        `size: ${meta.width || '?'}×${meta.height || '?'}`,
        `steps: ${meta.steps} · guidance: ${meta.guidance}`,
        `sampler: ${meta.sampler || '—'}`,
        `seed: ${meta.seed}`,
        `detail pass: ${meta.hires ? 'yes' : 'no'}`,
      ].join('\n')
    )}</pre>`;
  panel.hidden = false;
}

function closeViewer() { $('#viewer').hidden = true; }

/* --------------------------------------------------------------- misc UI */

function syncSliderLabels() {
  $('#steps-val').textContent = $('#steps').value;
  $('#guidance-val').textContent = (+$('#guidance').value).toFixed(1);
  $('#batch-val').textContent = $('#batch').value;
}

function switchView(name) {
  $$('.view').forEach((v) => v.classList.remove('active'));
  $(`#view-${name}`).classList.add('active');
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  window.scrollTo(0, 0);
  if (name === 'gallery') loadGallery();
  if (name === 'settings') refreshHealth();
}

async function peekPrompt() {
  try {
    const res = await api('/api/preview-prompt', {
      method: 'POST',
      body: JSON.stringify({
        prompt: $('#prompt').value,
        negative: $('#negative').value,
        preset: state.preset,
      }),
    });
    $('#peek-pos').textContent = res.prompt;
    $('#peek-neg').textContent = res.negative;
    $('#peek').hidden = false;
  } catch (err) {
    toast('Could not build prompt: ' + err.message);
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function bindUI() {
  $$('.tab').forEach((tab) => {
    tab.onclick = () => switchView(tab.dataset.view);
  });

  $('#btn-generate').onclick = generate;

  $('#btn-cancel').onclick = async () => {
    if (!state.job) return;
    try { await api(`/api/jobs/${state.job}/cancel`, { method: 'POST' }); } catch (_) {}
    toast('Cancelling…');
  };

  $('#btn-dice').onclick = () => {
    $('#prompt').value = IDEAS[Math.floor(Math.random() * IDEAS.length)];
    saveSettings();
  };

  ['steps', 'guidance', 'batch'].forEach((id) => {
    $('#' + id).oninput = () => { syncSliderLabels(); saveSettings(); };
  });
  ['sampler', 'hires', 'negative', 'prompt', 'seed'].forEach((id) => {
    $('#' + id).onchange = saveSettings;
  });

  $('#btn-reuse-seed').onclick = () => {
    if (state.lastSeed == null) { toast('No seed yet'); return; }
    $('#seed').value = state.lastSeed;
    toast('Seed ' + state.lastSeed);
  };

  $('#btn-prompt-peek').onclick = peekPrompt;
  $('#peek-close').onclick = () => { $('#peek').hidden = true; };
  $('#peek').onclick = (e) => { if (e.target.id === 'peek') $('#peek').hidden = true; };

  $('#viewer-close').onclick = closeViewer;
  $('#viewer-info-btn').onclick = showInfo;
  $('#btn-save').onclick = saveToPhone;
  $('#btn-share').onclick = shareImage;
  $('#btn-reuse').onclick = reuseSettings;
  $('#btn-delete').onclick = deleteImage;

  $('#btn-refresh-gallery').onclick = loadGallery;
  $('#status-chip').onclick = () => switchView('settings');

  $('#btn-unload').onclick = async () => {
    try {
      await api('/api/models/unload', { method: 'POST' });
      toast('GPU memory freed');
      state.options.models = (await api('/api/models')).models;
      renderModels();
      refreshHealth();
    } catch (err) { toast(err.message); }
  };

  let installEvent = null;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    installEvent = e;
    $('#btn-install').hidden = false;
  });
  $('#btn-install').onclick = async () => {
    if (!installEvent) return;
    installEvent.prompt();
    installEvent = null;
    $('#btn-install').hidden = true;
  };

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeViewer(); $('#peek').hidden = true; }
  });
}

boot();
