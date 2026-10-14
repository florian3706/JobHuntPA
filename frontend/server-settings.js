/* JobHuntPA Settings tab, the cards for the whole of this JobHuntPA (open-source spec §1.4): the AI provider, model,
   key and reasoning levels with the connection test, the AI usage figures, the public address and extra host names,
   the server key and the unencrypted copies left from before encryption. They serve every workspace, and whoever is
   signed in may change them (there are no roles). Loaded by index.html after common.js and app.js; the markup it
   fills is in index.html's Settings tab.

   WCAG 2.2 notes: every field has a label and its hint is tied to it (aria-describedby); results are said in the
   card's status line (role="status") and in toasts; errors go under the field (setFieldError) or in a role="alert"
   box; the usage tables have captions and header scopes and become cards on narrow screens (.stack-table). */
'use strict';

/* ================= LLM SETTINGS (Settings > AI provider) ================= */
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

/* ---------- LLM provider, address, model and key (Settings > AI provider) ---------- */
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

async function testScorer() {
  const out = $('#score-test-status');
  out.textContent = 'Testing…';
  try {
    const r = await api('/api/score/test', { method: 'POST' });
    out.innerHTML = r.ok ? `<span aria-hidden="true">✓ </span>${esc(r.message)}` : `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(r.message)}</span>`;
  } catch (e) { out.innerHTML = `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(e.message)}</span>`; }
}

/* ================= AI USAGE (Settings > AI usage) ================= */
const num = (n) => Number(n || 0).toLocaleString();
const pct = (rate) => (rate === null || rate === undefined ? '–' : `${Math.round(rate * 100)}%`);
const TASK_LABEL = {
  scoring: 'Fit scoring', research: 'Company research', cover_letter: 'Cover letters', chat: 'Job chat',
  concise: 'Concise documents', summary: 'Document summaries', title_suggestions: 'Job title suggestions',
  offices: 'Finding offices', office_from_ad: 'Reading offices from ads', other: 'Other',
};
const taskLabel = (t) => TASK_LABEL[t] || String(t || 'other').replace(/_/g, ' ');

function usageTable(caption, rows) {
  if (!rows.length) return '<p class="muted">No AI requests yet.</p>';
  const heads = ['Task', 'Requests', 'Input tokens', 'Output tokens', 'Cached tokens'];
  const body = rows.map((r) => `<tr role="row"><th role="rowheader" scope="row" data-label="Task">${esc(r.name)}</th>${
    [num(r.f.requests), num(r.f.input), num(r.f.output), `${num(r.f.cached)} (${pct(r.f.cache_hit_rate)})`]
      .map((v, i) => `<td role="cell" data-label="${esc(heads[i + 1])}">${v}</td>`).join('')}</tr>`).join('');
  return `<table class="report stack-table usage-table" role="table"><caption class="visually-hidden">${esc(caption)}</caption>
    <thead role="rowgroup"><tr role="row">${heads.map((h) => `<th role="columnheader" scope="col">${esc(h)}</th>`).join('')}</tr></thead>
    <tbody role="rowgroup">${body}</tbody></table>`;
}

async function loadUsage() {
  const status = $('#usage-status');
  let u;
  try { u = await api('/api/settings/usage'); } catch (e) { setText(status, `Could not load the figures: ${e.message}`); return; }
  setText(status, u.since ? `Counted since ${new Date(u.since).toLocaleDateString([], { dateStyle: 'medium' })}.` : '');
  const tile = (name, f) => `<div class="tile"><dt>${esc(name)}</dt><dd>${num(f.requests)}<span class="tile-hint"> requests,
    ${num(f.input + f.output)} tokens</span></dd></div>`;
  $('#usage-tiles').innerHTML = tile('Last 7 days', u.last_7_days) + tile('Last 30 days', u.last_30_days) + tile('In all', u.totals);
  const rows = (which) => u.by_task.filter((t) => t[which].requests).map((t) => ({ name: taskLabel(t.task), f: t[which] }));
  $('#usage-tables').innerHTML = u.totals.requests ? `
    <h4>By task, last 30 days</h4>${usageTable('AI use by task, last 30 days', rows('last_30_days'))}
    <details class="usage-days"><summary>By task, in all</summary>${usageTable('AI use by task, in all', rows('totals'))}</details>`
    : '<p class="muted">No AI requests yet.</p>';
}

/* ================= PUBLIC ADDRESS AND HOST NAMES (Settings) ================= */
function renderAddress(st) {
  $('#public-url').value = st.public_base_url || '';
  $('#allowed-hosts').value = (st.allowed_hosts || []).join(', ');
}

async function loadAddress() {
  try { renderAddress(await api('/api/settings/address')); }
  catch (e) { setText($('#address-status'), `Could not load the address settings: ${e.message}`); }
}

async function saveAddress(e) {
  e.preventDefault();
  const err = $('#address-error');
  err.hidden = true;
  setFieldError($('#public-url'), '');
  setFieldError($('#allowed-hosts'), '');
  const url = $('#public-url').value.trim().replace(/\/+$/, '');
  if (url && !/^https?:\/\//i.test(url)) {
    setFieldError($('#public-url'), 'Start the address with https:// (http:// only for localhost), like https://jobs.example.com.');
    return;
  }
  const names = $('#allowed-hosts').value.split(/[\s,]+/).map((n) => n.trim()).filter(Boolean);
  if (names.some((n) => n.includes('/') || n.includes(':'))) {
    setFieldError($('#allowed-hosts'), 'Give the names alone, like pi.tail1234.ts.net: no http://, port or path.');
    return;
  }
  const btn = $('#address-save');
  setBusy(btn, true);
  setText($('#address-status'), 'Saving…');
  try {
    // It widens which addresses and origins the app accepts, so it asks for the password if it was entered a while ago.
    renderAddress(await withRecentAuth(() => api('/api/settings/address', { method: 'PUT', body: JSON.stringify({ public_base_url: url, allowed_hosts: names }) })));
    setText($('#address-status'), 'Saved. It works straight away.');
  } catch (ex) {
    setText($('#address-status'), '');
    showError(err, `Not saved: ${ex.message}`);
  }
  setBusy(btn, false);
}

/* ================= SERVER KEY AND UNENCRYPTED COPIES (Settings) ================= */
async function rotateKey() {
  if (!confirm('Replace the server key? JobHuntPA moves the keys for background searches to a new server key and drops '
    + 'the old one. Nobody is signed out. If it can\'t move everything, the old key is kept and nothing is lost. You may '
    + 'be asked for your password.')) return;
  const btn = $('#rotate-key');
  setBusy(btn, true);
  setText($('#rotate-status'), 'Replacing the key…');
  try {
    const out = await withRecentAuth(() => api('/api/settings/server-key/rotate', { method: 'POST', body: '{}' }));
    if (out) setText($('#rotate-status'), `Done. ${num(out.rewrapped)} ${out.rewrapped === 1 ? 'key was' : 'keys were'} moved to the new server key.`);
    else setText($('#rotate-status'), 'Not replaced.');
  } catch (e) { setText($('#rotate-status'), `Not replaced: ${e.message}`); }
  setBusy(btn, false);
}

async function loadPlaintext() {
  let p;
  try { p = await api('/api/settings/leftovers'); } catch { return; }
  const card = $('#plain-card');
  card.hidden = !p.leftovers.length;
  if (!p.leftovers.length) return;
  setText($('#plain-summary'), `${p.leftovers.length === 1 ? 'A file' : `${p.leftovers.length} files`} from before encryption still ${p.leftovers.length === 1 ? 'holds' : 'hold'} your data unencrypted.`);
  $('#plain-details').innerHTML = `<p class="hint">Backups made before encryption and the old page cache. You only need the backup to go back to an older version of JobHuntPA. Delete the <code>.bak-*</code> files you don't need: they also hold keys from before, and anyone who gets them with the key file can read what they held.</p>
    <ul class="leftover-files">${p.leftovers.map((f) => `<li><code>${esc(f.path)}</code> (${esc(fmtBytes(f.bytes))})</li>`).join('')}</ul>`;
  $('#plain-delete').hidden = false;
}

async function deletePlaintext() {
  const btn = $('#plain-delete');
  if (!confirm('Delete the unencrypted files? They are overwritten and removed. Without the backup, you can\'t go back to an older version of JobHuntPA.')) return;
  setBusy(btn, true);
  setText($('#plain-status'), 'Deleting…');
  try {
    const out = await withRecentAuth(() => api('/api/settings/leftovers/delete', { method: 'POST', body: '{}' }));
    if (out) {
      setText($('#plain-status'), out.leftovers.length ? `${out.leftovers.length} file(s) could not be deleted. Check the data folder's permissions.` : `Deleted ${num(out.deleted.length)} file(s).`);
      await loadPlaintext();
    } else setText($('#plain-status'), 'Not deleted.');
  } catch (e) { setText($('#plain-status'), `Not deleted: ${e.message}`); }
  setBusy(btn, false);
  ($('#plain-delete').hidden ? $('#plain-summary') : $('#plain-delete')).focus?.();
}

/* ================= boot ================= */
document.addEventListener('DOMContentLoaded', async () => {
  if (!$('#llm-card')) return;
  initLlmSettings();
  initLlmProvider();
  $('#score-test').addEventListener('click', testScorer);
  $('#address-card').addEventListener('submit', saveAddress);
  $('#rotate-key').addEventListener('click', rotateKey);
  $('#plain-delete').addEventListener('click', deletePlaintext);
  try { if (!(await loadSession())) return; } catch { return; } // (app.js shows why; signed out goes to sign-in)
  loadLlmProvider();
  loadLlmSettings();
  loadUsage();
  loadAddress();
  loadPlaintext();
});
