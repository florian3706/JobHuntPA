/* JobHuntPA admin panel, Server section: the AI provider, model, key and reasoning levels, the connection test, and
   the commute API keys (accounts design §7 and §14.8). They are global for everyone who uses this JobHuntPA and
   were on the dashboard's Settings tab before the admin panel existed. Loaded by admin.html after common.js; the
   markup it fills is in admin.html. */
'use strict';

/* ================= LLM SETTINGS (admin panel > Server) ================= */
const LEVEL_LABEL = { none: 'None', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max' };
let levelsAutoDetected = false;

function renderLlmSettings(st) {
  const levels = st.levels;
  $$('.reasoning-settings select').forEach((sel) => {
    const task = sel.dataset.task;
    const current = st.reasoning[task] || '';
    const defLabel = st.env_default ? `Model default (${LEVEL_LABEL[st.env_default] || st.env_default} from .env)` : 'Model default';
    const opts = (levels || []).map((l) => `<option value="${l}" ${l === current ? 'selected' : ''}>${LEVEL_LABEL[l] || l}</option>`);
    if (current && levels && !levels.includes(current)) {
      opts.push(`<option value="${current}" selected>${LEVEL_LABEL[current] || current} (not supported by this model)</option>`);
    }
    sel.innerHTML = `<option value="" ${current ? '' : 'selected'}>${esc(defLabel)}</option>${opts.join('')}`;
    sel.disabled = !levels;
  });
  const status = $('#levels-status');
  if (!st.configured) status.textContent = 'Configure the LLM first.';
  else if (levels === null) status.textContent = 'Not detected yet for this model.';
  else if (!levels.length) status.textContent = `${st.model} doesn't accept a reasoning level; it always uses its default.`;
  else status.textContent = `${st.model} accepts: ${levels.map((l) => LEVEL_LABEL[l] || l).join(', ')}.`;
}

async function loadLlmSettings() {
  try {
    const st = await api('/api/llm');
    renderLlmSettings(st);
    if (st.configured && st.levels === null && !levelsAutoDetected) {
      levelsAutoDetected = true;
      detectLevels();
    }
  } catch (e) { $('#levels-status').textContent = `Could not load LLM settings: ${e.message}`; }
}

async function detectLevels() {
  const btn = $('#detect-levels');
  setBusy(btn, true);
  $('#levels-status').textContent = 'Detecting (a few tiny test requests)…';
  try { renderLlmSettings(await api('/api/llm/detect-levels', { method: 'POST' })); }
  catch (e) { $('#levels-status').textContent = `Detection failed: ${e.message}`; }
  setBusy(btn, false);
  btn.classList.remove('btn-primary');
}

function initLlmSettings() {
  $('#detect-levels').addEventListener('click', detectLevels);
  $$('.reasoning-settings select').forEach((sel) => sel.addEventListener('change', async () => {
    try {
      renderLlmSettings(await api('/api/llm/reasoning', { method: 'PUT', body: JSON.stringify({ [sel.dataset.task]: sel.value }) }));
      toast('Reasoning level saved', 'ok', 1500);
    } catch (e) { toast(`Save failed: ${e.message}`, 'err'); }
  }));
}

/* ---------- LLM provider, address, model and key (admin panel > Server) ---------- */
// The server never sends the saved key back: only whether one is set and its last characters.
let llmCfg = null; // the provider, address, model and key state (/api/settings/llm), with the provider presets
const llmPreset = (id) => llmCfg?.presets.find((p) => p.id === id);
const originOf = (url) => { try { return new URL(url).origin; } catch { return null; } };

function renderLlmProvider(st) {
  llmCfg = st;
  $('#llm-provider').innerHTML = st.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
  $('#llm-provider').value = st.provider;
  $('#llm-base-url').value = st.base_url;
  $('#llm-model').value = st.model;
  $('#llm-key').value = '';
  updateLlmProviderInfo();
}

// Everything under the fields that depends on the chosen provider (and on whether the address moved).
function updateLlmProviderInfo() {
  const st = llmCfg;
  if (!st) return;
  const preset = llmPreset($('#llm-provider').value) || st.presets[st.presets.length - 1];
  $('#llm-models').innerHTML = preset.models.map((m) => `<option value="${esc(m)}"></option>`).join('');
  $('#llm-model').placeholder = preset.models.length ? `e.g. ${preset.models[0]}` : 'Type the model name';
  $('#llm-base-url').placeholder = preset.base_url || 'https://api.example.com/v1';
  const link = preset.key_url
    ? ` <a href="${esc(preset.key_url)}" target="_blank" rel="noopener">${preset.key_required ? 'Get a key' : 'Get Ollama'}${NEW_TAB}</a>` : '';
  $('#llm-note').innerHTML = `${esc(preset.note)}${link}`;
  const web = $('#llm-web-notice');
  web.hidden = preset.web_search;
  web.textContent = preset.web_search ? '' : `${preset.label} can't search the web, so company research, finding offices and "Search the web" in job chats `
    + 'won\'t work with it. Scoring, cover letters, job titles and job chat will.';
  const key = $('#llm-key');
  key.placeholder = preset.key_required ? 'Paste the API key' : 'Not needed (optional)';
  $('#llm-key-state').innerHTML = st.key.set
    ? `<span class="ok-text">Saved</span>${st.key.hint ? ` <span class="muted">(ends in ${esc(st.key.hint)})</span>` : ''}`
    : `<span class="muted">${preset.key_required ? 'Not set' : 'No key needed for this provider'}</span>`;
  // Moving to another server needs the key typed again; say so before the server has to refuse.
  const moved = originOf($('#llm-base-url').value.trim()) !== originOf(st.base_url);
  const hint = $('#llm-key-hint');
  hint.hidden = !(moved && preset.key_required && !key.value.trim());
  hint.textContent = hint.hidden ? '' : 'The address changed to another server, so paste its API key again. '
    + 'The key saved for the old address is never sent to a new one.';
}

function onLlmProviderChange() {
  const st = llmCfg;
  const preset = llmPreset($('#llm-provider').value);
  const saved = preset.id === st.provider; // back to what is saved: restore its address and model
  $('#llm-base-url').value = saved ? st.base_url : preset.base_url;
  $('#llm-model').value = saved ? st.model : (preset.models[0] || '');
  $('#llm-error').hidden = true;
  updateLlmProviderInfo();
  if (!$('#llm-base-url').value) $('#llm-base-url').focus(); // Custom: the address is up to you
  else if (!$('#llm-model').value) $('#llm-model').focus();
  else if (preset.key_required && !saved) $('#llm-key').focus();
}

async function loadLlmProvider() {
  try { renderLlmProvider(await api('/api/settings/llm')); }
  catch (e) { $('#llm-save-status').textContent = `Could not load the LLM provider settings: ${e.message}`; }
}

async function saveLlmProvider(e) {
  e.preventDefault();
  const err = $('#llm-error');
  const body = { base_url: $('#llm-base-url').value.trim(), model: $('#llm-model').value.trim() };
  const key = $('#llm-key').value.trim(); // pasted keys often carry a space or line break; the server accepts none
  if (key) body.api_key = key;
  err.hidden = true;
  setFieldError($('#llm-model'), '');
  setFieldError($('#llm-base-url'), '');
  if (!body.model) setFieldError($('#llm-model'), 'Type the model name, for example the first one the list suggests.', { focus: !!body.base_url });
  if (!body.base_url) setFieldError($('#llm-base-url'), 'Type the API address, starting with https:// (for Ollama, http://localhost:11434/v1).');
  else if (!originOf(body.base_url)) {
    setFieldError($('#llm-base-url'), 'That isn\'t a web address. It starts with https:// (or http:// for a server on this computer).');
    return;
  }
  if (!body.base_url || !body.model) return;
  const before = llmCfg;
  const btn = $('#llm-save');
  setBusy(btn, true);
  $('#llm-save-status').textContent = 'Saving…';
  try {
    const st = await api('/api/settings/llm', { method: 'PUT', body: JSON.stringify(body) });
    renderLlmProvider(st);
    const changed = !before || before.base_url !== st.base_url || before.model !== st.model;
    levelsAutoDetected = true; // the user is asked to run Detect, rather than it running by itself
    await loadLlmSettings(); // levels for this model
    $('#llm-save-status').textContent = changed
      ? 'Saved. Reasoning levels differ per model: run "Detect supported levels" below.' : 'Saved.';
    if (changed) $('#detect-levels').classList.add('btn-primary');
    toast('LLM settings saved', 'ok', 2500);
  } catch (ex) {
    $('#llm-save-status').textContent = '';
    showError(err, `Not saved: ${ex.message}`);
  }
  setBusy(btn, false);
}

function initLlmProvider() {
  $('#llm-provider').addEventListener('change', onLlmProviderChange);
  ['#llm-base-url', '#llm-key'].forEach((s) => $(s).addEventListener('input', updateLlmProviderInfo));
  $('#llm-form').addEventListener('submit', saveLlmProvider);
}

/* ================= API KEYS (admin panel > Server) ================= */
// The server never sends a saved key back, only whether one is set and its last characters.
const KEY_LABEL = { tfnsw: 'Transport for NSW', tomtom: 'TomTom' };
const keyField = (name) => document.querySelector(`.key-field[data-key="${name}"]`);

function renderCommuteKeys(st) {
  Object.keys(KEY_LABEL).forEach((name) => {
    const info = st[name] || { set: false, hint: null };
    const box = keyField(name);
    box.querySelector('.key-state').innerHTML = info.set
      ? `<span class="ok-text">Saved</span>${info.hint ? ` <span class="muted">(ends in ${esc(info.hint)})</span>` : ''}`
      : '<span class="muted">Not set</span>';
    box.querySelector('.key-clear').disabled = !info.set;
  });
}

async function loadCommuteKeys() {
  try { renderCommuteKeys(await api('/api/settings/keys')); }
  catch (e) { $('#keys-test-status').textContent = `Could not load the key settings: ${e.message}`; }
}

async function putCommuteKey(name, value) {
  const box = keyField(name);
  const save = box.querySelector('.key-save');
  const clear = box.querySelector('.key-clear');
  const couldClear = !clear.disabled;
  setBusy(save, true);
  setBusy(clear, true);
  try {
    renderCommuteKeys(await api('/api/settings/keys', { method: 'PUT', body: JSON.stringify({ [name]: value }) })); // also sets Clear
    box.querySelector('.key-input').value = '';
    $('#keys-test-status').textContent = '';
    toast(value ? `${KEY_LABEL[name]} key saved` : `${KEY_LABEL[name]} key removed`, 'ok', 2500);
  } catch (e) {
    clear.disabled = !couldClear;
    setBusy(clear, false);
    toast(`Saving the ${KEY_LABEL[name]} key failed: ${e.message}`, 'err', 8000);
  }
  setBusy(save, false);
  setBusy(clear, false);
  if (document.activeElement === document.body || document.activeElement?.disabled) box.querySelector('.key-input').focus();
}

async function testCommuteKeys() {
  const btn = $('#keys-test');
  const out = $('#keys-test-status');
  setBusy(btn, true);
  out.textContent = 'Testing…';
  try {
    const r = await api('/api/settings/keys/test', { method: 'POST' });
    const lines = Object.keys(KEY_LABEL).map((name) => (r[name]
      ? (r[name].ok ? `<span class="ok-text">${KEY_LABEL[name]}: works<span aria-hidden="true"> ✓</span></span>` : `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(r[name].error || `${KEY_LABEL[name]}: failed`)}</span>`)
      : `${KEY_LABEL[name]}: not set`));
    out.innerHTML = lines.join(' · ');
  } catch (e) { out.innerHTML = `<span class="err-text">Test failed: ${esc(e.message)}</span>`; }
  setBusy(btn, false);
}

function initCommuteKeys() {
  Object.keys(KEY_LABEL).forEach((name) => {
    const box = keyField(name);
    const input = box.querySelector('.key-input');
    box.addEventListener('submit', (e) => {
      e.preventDefault();
      const value = input.value.trim(); // pasted keys often carry a space or line break; the server accepts none inside
      if (!value) { setFieldError(input, `Paste the ${KEY_LABEL[name]} key first: copy it from the website named below.`); return; }
      setFieldError(input, '');
      putCommuteKey(name, value);
    });
    box.querySelector('.key-clear').addEventListener('click', async (e) => {
      const clear = e.currentTarget;
      if (await confirmDialog({ title: `Remove the ${KEY_LABEL[name]} key?`, ok: 'Remove key', returnTo: () => clear,
        text: 'Commute times stop working until you save a key again.' })) putCommuteKey(name, '');
    });
  });
  $('#keys-test').addEventListener('click', testCommuteKeys);
}


async function testScorer() {
  const out = $('#score-test-status');
  out.textContent = 'Testing…';
  try {
    const r = await api('/api/score/test', { method: 'POST' });
    out.innerHTML = r.ok ? `<span aria-hidden="true">✓ </span>${esc(r.message)}` : `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(r.message)}</span>`;
  } catch (e) { out.innerHTML = `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(e.message)}</span>`; }
}

