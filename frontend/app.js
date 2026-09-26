/* JobHuntPA dashboard — vanilla JS, no build step. Talks to backend/app.py. */
'use strict';

const STATUSES = ['to_review', 'shortlisted', 'applied', 'not_interested'];
const STATUS_LABEL = { to_review: 'to review', shortlisted: 'shortlisted', applied: 'applied', not_interested: 'not interested' };
const TAG_FIELDS = ['titles', 'keywords_include', 'keywords_exclude', 'dealbreaker_industries', 'dealbreaker_keywords', 'seek_locations'];

const state = {
  jobs: [],
  details: new Map(), // job id -> full job (with description)
  filter: { q: '', minScore: 0, mode: 'all', maxDist: null, showExcluded: false, showClosed: false, status: 'all', sort: 'score' },
  selected: new Set(),
  profile: null,
  pins: [],
  map: null,
  pinLayers: new Map(),
  jobLayer: null,
  pollTimer: null,
  companies: {}, // company_key -> profile
};

/* ---------- utils ---------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind = '', ms = 5000) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`.trim();
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(url, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(url, {
    ...opts,
    headers: isForm ? opts.headers : { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data && typeof data === 'object' && data.detail ? data.detail : text;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(/T\d\d:\d\d/.test(iso) && !/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? `${iso}Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function openModal(title, text) {
  $('#modal-title').textContent = title;
  $('#modal-text').textContent = text;
  $('#modal-text').hidden = false;
  $('#modal-html').hidden = true;
  $('#modal').hidden = false;
}

function openHtmlModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-html').innerHTML = html;
  $('#modal-html').hidden = false;
  $('#modal-text').hidden = true;
  $('#modal').hidden = false;
}

/* ---------- tabs ---------- */
function initTabs() {
  const show = (name) => {
    $$('.tab').forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
    $$('.tab-panel').forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
    if (name === 'map') requestAnimationFrame(() => { initMap(); state.map?.invalidateSize(); drawJobMarkers(); });
    history.replaceState(null, '', `#${name}`);
  };
  $$('.tab').forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  const initial = location.hash.slice(1);
  show(['jobs', 'search', 'docs', 'map'].includes(initial) ? initial : 'jobs');
}

/* ================= JOBS ================= */
function scoreOf(job) { return job.fit && job.fit.status === 'ok' ? job.fit.score : null; }
function modeOf(job) { return ['remote', 'hybrid', 'onsite'].includes(job.work_mode) ? job.work_mode : 'unknown'; }

async function loadJobs() {
  try {
    state.jobs = await api('/api/jobs');
    const ids = new Set(state.jobs.map((j) => j.id));
    state.selected.forEach((id) => { if (!ids.has(id)) state.selected.delete(id); });
    state.details.clear();
    $('#jobs-error').hidden = true;
  } catch (e) {
    $('#jobs-error').textContent = `Could not load jobs: ${e.message}`;
    $('#jobs-error').hidden = false;
  }
  renderJobs();
  drawJobMarkers();
}

function visibleJobs() {
  const f = state.filter;
  const q = f.q.trim().toLowerCase();
  const list = state.jobs.filter((j) => {
    if (!f.showExcluded && j.excluded_reason) return false;
    if (!f.showClosed && j.closed) return false;
    if (f.status !== 'all' && j.status !== f.status) return false;
    if (f.minScore > 0 && (scoreOf(j) ?? -1) < f.minScore) return false;
    if (f.mode !== 'all' && modeOf(j) !== f.mode) return false;
    if (f.maxDist !== null && (j.distance_km === null || j.distance_km > f.maxDist)) return false;
    if (q) {
      const hay = `${j.company} ${j.title} ${j.location_text} ${j.work_mode} ${j.source}`.toLowerCase();
      if (!q.split(/\s+/).every((t) => hay.includes(t))) return false;
    }
    return true;
  });
  const by = {
    score: (a, b) => (scoreOf(b) ?? -1) - (scoreOf(a) ?? -1) || String(b.first_seen).localeCompare(String(a.first_seen)),
    newest: (a, b) => String(b.posted_at || b.first_seen).localeCompare(String(a.posted_at || a.first_seen)),
    distance: (a, b) => (a.distance_km ?? 1e9) - (b.distance_km ?? 1e9),
  }[f.sort];
  return list.sort(by);
}

function renderStatusChips() {
  const box = $('#status-chips');
  box.innerHTML = '';
  const base = state.jobs.filter((j) => (state.filter.showExcluded || !j.excluded_reason) && (state.filter.showClosed || !j.closed));
  [['all', 'all'], ...STATUSES.map((s) => [s, STATUS_LABEL[s]])].forEach(([val, label]) => {
    const n = val === 'all' ? base.length : base.filter((j) => j.status === val).length;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `chip${state.filter.status === val ? ' active' : ''}`;
    b.innerHTML = `${esc(label)} <span class="n">${n}</span>`;
    b.addEventListener('click', () => { state.filter.status = val; renderJobs(); });
    box.appendChild(b);
  });
}

function scoreBadge(job) {
  const fit = job.fit;
  if (!fit) return `<span class="score none" title="Not scored yet">–</span>`;
  if (fit.status === 'error') return `<span class="score err" title="${esc(fit.error || 'error')}">!</span>`;
  const cls = fit.score >= 75 ? 'high' : fit.score >= 45 ? 'mid' : 'low';
  const stale = fit.stale ? ' stale' : '';
  const title = fit.stale ? 'Scored against older documents' : `Scored ${fmtDate(fit.scored_at)}`;
  return `<span class="score ${cls}${stale}" title="${esc(title)}">${fit.score}${fit.stale ? '*' : ''}</span>`;
}

function renderJobs() {
  renderStatusChips();
  const jobs = visibleJobs();
  const list = $('#jobs-list');
  list.innerHTML = '';
  $('#jobs-empty').hidden = jobs.length !== 0;
  const hidden = state.jobs.length - jobs.length;
  $('#jobs-count').textContent = `${jobs.length} shown${hidden ? ` · ${hidden} hidden by filters` : ''}`;
  jobs.forEach((job) => list.appendChild(renderJobCard(job)));
  renderBulkBar();
}

function renderJobCard(job) {
  const card = document.createElement('article');
  card.className = `card job-card${job.excluded_reason ? ' is-excluded' : ''}${state.selected.has(job.id) ? ' is-selected' : ''}`;
  const mode = modeOf(job);
  const dist = job.distance_km === null ? '' : `<span>📍 ${job.distance_km} km</span>`;
  const flags = [
    job.detail_status === 'summary' ? '<span class="badge flag" title="Only the job board listing summary is available">summary only</span>' : '',
    job.detail_status === 'none' ? '<span class="badge flag" title="Description not fetched yet">no description</span>' : '',
    job.closed ? '<span class="badge flag">closed</span>' : '',
  ].join('');
  const excl = job.excluded_reason ? `<p class="hint">Excluded: ${esc(job.excluded_reason)}</p>` : '';
  const fitErr = job.fit && job.fit.error ? `<p class="hint err-text">Scoring failed: ${esc(job.fit.error.slice(0, 240))}</p>` : '';
  card.innerHTML = `
    <div class="job-top">
      <input type="checkbox" aria-label="Select job" ${state.selected.has(job.id) ? 'checked' : ''} />
      <div class="job-title-row">
        <div class="job-company">${esc(job.company || '—')} ${companyIcon(job)}</div>
        <h3 class="job-title"><a href="${esc(job.url)}" target="_blank" rel="noopener">${esc(job.title || '—')}</a></h3>
        <div class="job-meta">
          <span>${esc(job.location_text || 'location unknown')}</span>
          <span class="badge ${mode}">${mode}</span>${dist}${flags}
        </div>
      </div>
      ${scoreBadge(job)}
    </div>
    <div class="job-sub">
      ${job.salary_text ? `<span>💰 ${esc(job.salary_text)}</span>` : ''}
      <span class="muted">${esc(job.source || '')}${job.posted_at ? ` · posted ${esc(fmtDate(job.posted_at).split(',')[0])}` : ''}</span>
    </div>
    ${excl}${fitErr}
    <div class="job-actions">
      <select aria-label="Application status">
        ${STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}
      </select>
      <button class="btn btn-ghost btn-sm" type="button" data-act="rescore">${job.fit && job.fit.status === 'ok' ? 'Rescore' : 'Score'}</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="desc">Description</button>
    </div>
    <details class="reqs" data-section="fit"><summary>Fit: requirements → evidence</summary><div class="fit-body"></div></details>
    ${gapsSection(job)}`;

  card.querySelector('input[type="checkbox"]').addEventListener('change', (e) => {
    if (e.target.checked) state.selected.add(job.id); else state.selected.delete(job.id);
    card.classList.toggle('is-selected', e.target.checked);
    renderBulkBar();
  });
  const sel = card.querySelector('select');
  sel.addEventListener('change', () => updateJobStatus(job, sel.value, sel));
  card.querySelector('[data-act="rescore"]').addEventListener('click', (e) => rescoreJob(job, e.target));
  card.querySelector('[data-act="desc"]').addEventListener('click', () => showDescription(job));
  card.querySelector('[data-act="company"]')?.addEventListener('click', () => showCompany(job));
  card.querySelector('details[data-section="fit"]').addEventListener('toggle', (e) => {
    if (e.target.open) e.target.querySelector('.fit-body').innerHTML = fitHtml(job);
  });
  card.querySelector('details[data-section="gaps"]')?.addEventListener('toggle', (e) => {
    if (e.target.open) e.target.querySelector('.gaps-body').innerHTML = gapsHtml(job);
  });
  return card;
}

function fitHtml(job) {
  const fit = job.fit;
  if (!fit) return '<p class="muted">Not scored yet.</p>';
  if (fit.status === 'error') return `<p class="hint err-text">${esc(fit.error || 'Scoring failed')}</p>`;
  const reqs = (fit.requirements || []).map((r) => {
    const ev = (r.evidence || []).map((e) => {
      const subs = (e.sub_bullets || []).map((s) => `<li>${esc(s)}</li>`).join('');
      return `<li class="ev">✓ ${esc(e.bullet)}${subs ? `<ul>${subs}</ul>` : ''}</li>`;
    }).join('');
    const tag = r.matched ? '<span class="ok-text">matched</span>' : '<span class="err-text">missing</span>';
    const must = r.must_have === false ? ' <span class="muted">(nice to have)</span>' : '';
    return `<li><div>${esc(r.point)} · ${tag}${must}</div>${ev ? `<ul>${ev}</ul>` : ''}</li>`;
  }).join('') || '<li class="muted">No requirement breakdown.</li>';
  return `${fit.summary ? `<p class="summary">${esc(fit.summary)}</p>` : ''}
    <ul>${reqs}</ul>
    <p class="muted">${esc(fit.model || '')} · ${esc(fmtDate(fit.scored_at))}${fit.stale ? ' · scored against older documents' : ''}</p>`;
}

function missingRequirements(fit) {
  return (fit.requirements || [])
    .filter((r) => !r.matched)
    .sort((a, b) => (b.must_have !== false) - (a.must_have !== false));
}

function gapsSection(job) {
  const fit = job.fit;
  if (!fit || fit.status !== 'ok') return '';
  const n = (fit.gaps || []).length || missingRequirements(fit).length;
  if (!n) return '';
  return `<details class="reqs gaps" data-section="gaps"><summary>Gaps (${n})</summary><div class="gaps-body"></div></details>`;
}

function gapsHtml(job) {
  const fit = job.fit;
  const gaps = (fit.gaps || []).map((g) => `<li class="gap">✕ ${esc(g)}</li>`).join('');
  const missing = missingRequirements(fit).map((r) =>
    `<li class="gap">${esc(r.point)}${r.must_have === false ? ' <span class="muted">(nice to have)</span>' : ' <span class="muted">(must have)</span>'}</li>`).join('');
  return `${gaps ? `<ul>${gaps}</ul>` : ''}
    ${missing ? `<p class="muted gaps-sub">Requirements your documents don't evidence:</p><ul>${missing}</ul>` : ''}`;
}

async function showDescription(job) {
  try {
    const full = state.details.get(job.id) || await api(`/api/jobs/${job.id}`);
    state.details.set(job.id, full);
    const note = full.detail_status === 'summary' ? '[Listing summary only; open the job link for the full ad]\n\n' : '';
    openModal(`${full.title} — ${full.company}`, note + (full.description || '(no description stored)'));
  } catch (e) { toast(`Could not load job: ${e.message}`, 'err'); }
}

function renderBulkBar() {
  const n = state.selected.size;
  $('#bulk-bar').hidden = n === 0;
  $('#bulk-count').textContent = `${n} selected`;
  const vis = visibleJobs().map((j) => j.id);
  $('#bulk-select-all').checked = vis.length > 0 && vis.every((id) => state.selected.has(id));
}

async function updateJobStatus(job, next, selectEl) {
  const prev = job.status;
  job.status = next;
  renderStatusChips();
  try {
    await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: next }) });
  } catch (e) {
    job.status = prev;
    selectEl.value = prev;
    renderStatusChips();
    toast(`Status update failed: ${e.message}`, 'err');
  }
}

async function bulkUpdateStatus() {
  const ids = Array.from(state.selected);
  const status = $('#bulk-status').value;
  try {
    await api('/api/jobs/status', { method: 'PATCH', body: JSON.stringify({ ids, status }) });
    state.jobs.forEach((j) => { if (state.selected.has(j.id)) j.status = status; });
    state.selected.clear();
    renderJobs();
    toast(`Updated ${ids.length} job(s)`, 'ok');
  } catch (e) { toast(`Bulk update failed: ${e.message}`, 'err'); }
}

async function rescoreJob(job, btn) {
  btn.disabled = true;
  btn.textContent = 'Scoring…';
  try {
    const updated = await api(`/api/score/${job.id}`, { method: 'POST' });
    Object.assign(job, updated);
    toast(`${job.title}: ${updated.fit?.score ?? '?'}`, 'ok');
  } catch (e) {
    toast(`Scoring failed: ${e.message}`, 'err', 9000);
    await loadJobs();
    return;
  }
  renderJobs();
}

function initJobs() {
  $('#filter-search').addEventListener('input', debounce((e) => { state.filter.q = e.target.value; renderJobs(); }, 150));
  $('#filter-min-score').addEventListener('input', (e) => { state.filter.minScore = Number(e.target.value); $('#score-output').textContent = e.target.value; renderJobs(); });
  $('#filter-mode').addEventListener('change', (e) => { state.filter.mode = e.target.value; renderJobs(); });
  $('#sort-by').addEventListener('change', (e) => { state.filter.sort = e.target.value; renderJobs(); });
  $('#filter-max-dist').addEventListener('input', (e) => { const v = e.target.value === '' ? null : Number(e.target.value); state.filter.maxDist = Number.isFinite(v) ? v : null; renderJobs(); });
  $('#filter-show-excluded').addEventListener('change', (e) => { state.filter.showExcluded = e.target.checked; renderJobs(); });
  $('#filter-show-closed').addEventListener('change', (e) => { state.filter.showClosed = e.target.checked; renderJobs(); });
  $('#bulk-apply').addEventListener('click', bulkUpdateStatus);
  $('#bulk-clear').addEventListener('click', () => { state.selected.clear(); renderJobs(); });
  $('#bulk-select-all').addEventListener('change', (e) => {
    visibleJobs().forEach((j) => (e.target.checked ? state.selected.add(j.id) : state.selected.delete(j.id)));
    renderJobs();
  });
  $('#search-run').addEventListener('click', () => startRun('/api/search/run', {}));
  $('#score-pending').addEventListener('click', () => startRun('/api/score/run', { mode: 'pending' }));
  $('#fill-info').addEventListener('click', () => startRun('/api/companies/research', { mode: 'missing' }));
}

/* ================= BACKGROUND RUNS ================= */
const RUN_LABEL = { search: 'Search', score: 'Scoring', research: 'Company research' };
async function startRun(url, body) {
  try {
    const run = await api(url, { method: 'POST', body: JSON.stringify(body) });
    if (run.already_running) toast('A run is already in progress; showing its progress.');
    watchRun(run.id);
  } catch (e) { toast(e.message, 'err', 9000); }
}

function watchRun(id) {
  clearTimeout(state.pollTimer);
  ['#search-run', '#score-pending', '#fill-info'].forEach((s) => { $(s).disabled = true; });
  $('#run-progress').hidden = false;
  const tick = async () => {
    let run;
    try { run = await api(`/api/runs/${id}`); } catch (e) { state.pollTimer = setTimeout(tick, 3000); return; }
    const p = run.progress || {};
    const pct = p.total ? Math.round((100 * (p.done || 0)) / p.total) : null;
    $('#run-progress .progress-bar').style.width = pct === null ? '100%' : `${pct}%`;
    $('#run-progress').classList.toggle('indeterminate', pct === null);
    $('#run-status').textContent = `${RUN_LABEL[run.kind] || 'Working'}: ${run.stage || run.state}…`;
    if (run.state === 'queued' || run.state === 'running') { state.pollTimer = setTimeout(tick, 1200); return; }
    finishRun(run);
  };
  tick();
}

function finishRun(run) {
  ['#search-run', '#score-pending', '#fill-info'].forEach((s) => { $(s).disabled = false; });
  $('#run-progress').hidden = true;
  if (run.state === 'failed') {
    $('#run-status').textContent = `Run failed: ${run.error}`;
    toast(`Run failed: ${run.error}`, 'err', 10000);
  } else {
    $('#run-status').textContent = runSummaryText(run);
    const sc = run.summary?.scoring || run.summary?.research;
    if (sc?.aborted) toast(`Stopped: ${sc.aborted}`, 'err', 12000);
    else if (sc?.first_error) toast(`Some failed: ${sc.first_error}`, 'err', 9000);
  }
  renderRunReport(run);
  loadCompanies();
  loadJobs();
  loadScorerStatus();
  loadSources();
}

function runSummaryText(run) {
  const bits = [];
  const t = run.summary?.search?.totals;
  if (t) bits.push(`${t.listed} listed, ${t.new} new, ${t.kept} kept, ${t.excluded} excluded${t.closed ? `, ${t.closed} closed` : ''}${t.details_deferred ? `, ${t.details_deferred} descriptions deferred to next run` : ''}`);
  const sc = run.summary?.scoring;
  if (sc) {
    if (sc.skipped && typeof sc.skipped === 'string') bits.push(`scoring skipped: ${sc.skipped}`);
    else if (sc.aborted) bits.push('scoring stopped (see error)');
    else if (sc.requested !== undefined) bits.push(`scored ${sc.scored}/${sc.unique_postings ?? sc.requested} unique postings${sc.errors ? `, ${sc.errors} failed` : ''}`);
  }
  const rs = run.summary?.research;
  if (rs) {
    if (rs.aborted) bits.push('research stopped (see error)');
    else bits.push(`researched ${rs.researched}/${rs.requested} companies${rs.errors ? `, ${rs.errors} failed` : ''}`);
  }
  return `${RUN_LABEL[run.kind] || 'Run'} finished ${fmtDate(run.finished_at)}: ${bits.join(' · ')}`;
}

function renderRunReport(run) {
  const per = run.summary?.search?.per_source;
  if (!per) return;
  const rows = per.map((s) => `
    <tr>
      <td><strong>${esc(s.label)}</strong><div class="muted">${esc(s.method || '')}</div></td>
      <td>${s.listed}</td><td>${s.new}</td><td>${s.details_fetched}${s.details_deferred ? ` (+${s.details_deferred} next run)` : ''}</td>
      <td>${s.kept}</td><td>${s.excluded}</td><td>${s.closed}</td>
      <td>${s.error ? `<span class="err-text">${esc(s.error)}</span>` : '✓'}${(s.notes || []).length ? `<div class="muted">${(s.notes || []).map(esc).join('<br>')}</div>` : ''}</td>
    </tr>`).join('');
  $('#run-report-body').innerHTML = `<table class="report"><thead><tr><th>Source</th><th>Listed</th><th>New</th><th>Descriptions fetched</th><th>Kept</th><th>Excluded</th><th>Closed</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`;
  $('#run-report').hidden = false;
}

async function loadLatestRun() {
  try {
    const run = await api('/api/runs/latest');
    if (!run) return;
    if (run.state === 'queued' || run.state === 'running') { watchRun(run.id); return; }
    $('#run-status').textContent = run.state === 'failed' ? `Last run failed: ${run.error}` : runSummaryText(run);
    renderRunReport(run);
  } catch { /* first launch */ }
}

/* ================= COMPANY PROFILES ================= */
async function loadCompanies() {
  try { state.companies = await api('/api/companies'); } catch { state.companies = {}; }
  renderJobs();
}

function companyIcon(job) {
  if (!job.company) return '';
  const p = state.companies[job.company_key];
  let cls = 'none', title = 'No company profile yet (Fill missing info)';
  if (p?.status === 'done') {
    const n = p.controversies.length;
    cls = n ? 'warn' : 'ok';
    title = n ? `${n} controversy item(s) found: click for details` : 'Company profile';
  } else if (p?.status === 'error') { cls = 'err'; title = 'Research failed: click for details'; }
  else if (p?.status === 'skipped') { cls = 'none'; title = 'Not a researchable company'; }
  const count = p?.status === 'done' && p.controversies.length ? `<sup>${p.controversies.length}</sup>` : '';
  return `<button type="button" class="info-btn ${cls}" data-act="company" title="${esc(title)}" aria-label="Company info">ⓘ${count}</button>`;
}

function linkList(sources) {
  return (sources || []).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>`).join(' · ');
}

function showCompany(job) {
  const p = state.companies[job.company_key];
  const researchBtn = `<button id="company-research" class="btn btn-sm" type="button">${p ? 'Research again' : 'Research this company'}</button>`;
  let body;
  if (!p) {
    body = `<p class="muted">Not researched yet. <strong>Fill missing info</strong> researches every company in your list, or research just this one:</p>${researchBtn}`;
  } else if (p.status === 'error') {
    body = `<p class="err-text">Research failed: ${esc(p.error)}</p>${researchBtn}`;
  } else if (p.status === 'skipped') {
    body = `<p class="muted">"${esc(p.name)}" isn't an identifiable organisation (for example an anonymous advertiser).</p>${researchBtn}`;
  } else {
    const cons = p.controversies.length
      ? `<ul class="controversies">${p.controversies.map((c) => `
          <li><strong>${esc(c.title)}</strong>${c.year ? ` <span class="muted">(${esc(c.year)})</span>` : ''}
            <div>${esc(c.summary)}</div><div class="sources">${linkList(c.sources)}</div></li>`).join('')}</ul>`
      : '';
    body = `
      ${p.is_recruiter ? '<p class="notice">This is a recruitment agency. The employer behind the ad is usually not disclosed; ask the recruiter who the client is.</p>' : ''}
      <dl class="profile">
        <dt>How they make money</dt><dd>${esc(p.business_model || 'Unknown')}</dd>
        <dt>Ownership</dt><dd>${esc(p.ownership || 'Unknown')}</dd>
        <dt>Headquarters</dt><dd>${esc(p.headquarters || 'Unknown')}</dd>
        <dt>Controversies</dt><dd>${esc(p.controversy_note || (p.controversies.length ? '' : 'None found.'))}${cons}</dd>
      </dl>
      ${p.sources.length ? `<p class="sources"><span class="muted">Sources:</span> ${linkList(p.sources)}</p>` : ''}
      <p class="muted">Researched ${esc(fmtDate(p.researched_at))} by ${esc(p.model || 'Muse Spark')} with web search.
        Only links the search actually returned are shown${p.unverified_dropped ? `; ${p.unverified_dropped} unverifiable link(s) and their claims were removed` : ''}.
        Automated research can be wrong: check the linked articles.</p>
      ${researchBtn}`;
  }
  openHtmlModal(p?.official_name || job.company, body);
  $('#company-research').addEventListener('click', () => {
    $('#modal').hidden = true;
    startRun('/api/companies/research', { name: job.company });
  });
}

/* ================= SEARCH SETUP ================= */
function tagWrap(field) { return document.querySelector(`.tag-input[data-field="${field}"] .tags`); }
function getTags(field) { return Array.from(tagWrap(field).querySelectorAll('.tag')).map((t) => t.dataset.value); }
function setTags(field, values) { tagWrap(field).innerHTML = ''; (values || []).forEach((v) => addTag(field, v, false)); }

function addTag(field, value, save = true) {
  value = String(value || '').trim();
  if (!value || getTags(field).some((v) => v.toLowerCase() === value.toLowerCase())) return;
  const el = document.createElement('span');
  el.className = 'tag';
  el.dataset.value = value;
  el.innerHTML = `<span>${esc(value)}</span><button type="button" aria-label="Remove ${esc(value)}">×</button>`;
  el.querySelector('button').addEventListener('click', () => { el.remove(); saveSetup(); });
  tagWrap(field).appendChild(el);
  if (save) saveSetup();
}

async function loadSetup() {
  try {
    const [profile, dealbreakers] = await Promise.all([api('/api/profile'), api('/api/dealbreakers')]);
    state.profile = profile;
    ['titles', 'keywords_include', 'keywords_exclude', 'seek_locations'].forEach((f) => setTags(f, profile[f]));
    setTags('dealbreaker_industries', dealbreakers.industries);
    setTags('dealbreaker_keywords', dealbreakers.keywords);
    $('#salary-floor').value = profile.salary_floor ?? '';
    $('#remote-aus').checked = profile.remote_aus_ok;
    $('#remote-global').checked = profile.remote_global_ok;
    $('#allow-hybrid').checked = profile.allow_hybrid;
    $('#allow-onsite').checked = profile.allow_onsite;
    $('#seek-enabled').checked = profile.seek_enabled;
    $('#search-error').hidden = true;
  } catch (e) {
    $('#search-error').textContent = `Could not load search setup: ${e.message}`;
    $('#search-error').hidden = false;
  }
}

const saveSetup = debounce(async () => {
  const status = $('#search-save-status');
  status.textContent = 'Saving…';
  const floor = $('#salary-floor').value;
  const profile = {
    titles: getTags('titles'),
    keywords_include: getTags('keywords_include'),
    keywords_exclude: getTags('keywords_exclude'),
    salary_floor: floor === '' ? null : Number(floor),
    remote_aus_ok: $('#remote-aus').checked,
    remote_global_ok: $('#remote-global').checked,
    allow_hybrid: $('#allow-hybrid').checked,
    allow_onsite: $('#allow-onsite').checked,
    seek_enabled: $('#seek-enabled').checked,
    seek_locations: getTags('seek_locations'),
  };
  const dealbreakers = { industries: getTags('dealbreaker_industries'), keywords: getTags('dealbreaker_keywords') };
  try {
    await Promise.all([
      api('/api/profile', { method: 'PUT', body: JSON.stringify(profile) }),
      api('/api/dealbreakers', { method: 'PUT', body: JSON.stringify(dealbreakers) }),
    ]);
    const { changed } = await api('/api/jobs/refilter', { method: 'POST' });
    status.textContent = `Saved ${new Date().toLocaleTimeString()}${changed ? ` · filters re-applied to ${changed} job(s)` : ''}`;
    if (changed) loadJobs();
  } catch (e) {
    status.textContent = 'Save failed';
    toast(`Saving search setup failed: ${e.message}`, 'err');
  }
}, 700);

function initSetup() {
  $$('.tag-input').forEach((wrap) => {
    const input = wrap.querySelector('input');
    const field = wrap.dataset.field;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        input.value.split(',').forEach((v) => addTag(field, v));
        input.value = '';
      } else if (e.key === 'Backspace' && !input.value) {
        wrap.querySelector('.tags .tag:last-child')?.remove();
        saveSetup();
      }
    });
    input.addEventListener('blur', () => { if (input.value.trim()) { addTag(field, input.value); input.value = ''; } });
  });
  ['#salary-floor'].forEach((s) => $(s).addEventListener('input', saveSetup));
  ['#remote-aus', '#remote-global', '#allow-hybrid', '#allow-onsite', '#seek-enabled'].forEach((s) => $(s).addEventListener('change', saveSetup));
  $('#search-form').addEventListener('submit', (e) => e.preventDefault());
  $('#import-form').addEventListener('submit', importPages);
}

async function importPages(e) {
  e.preventDefault();
  const files = Array.from($('#import-input').files);
  if (!files.length) { toast('Choose saved .html pages first.', 'err'); return; }
  const status = $('#import-status');
  let total = { imported: 0, new: 0, kept: 0 };
  for (const f of files) {
    status.textContent = `Importing ${f.name}…`;
    const fd = new FormData();
    fd.append('file', f, f.name);
    try {
      const r = await api('/api/import/page', { method: 'POST', body: fd });
      total = { imported: total.imported + r.imported, new: total.new + r.new, kept: total.kept + r.kept };
    } catch (err) { toast(`${f.name}: ${err.message}`, 'err', 8000); }
  }
  status.textContent = `Imported ${total.imported} job(s): ${total.new} new, ${total.kept} pass your filters`;
  $('#import-input').value = '';
  loadJobs();
}

/* ---------- sources ---------- */
async function loadSources() {
  try {
    const sources = await api('/api/sources');
    renderSources(sources);
    $('#sources-error').hidden = true;
  } catch (e) {
    $('#sources-error').textContent = `Could not load sources: ${e.message}`;
    $('#sources-error').hidden = false;
  }
}

function renderSources(sources) {
  const box = $('#sources-list');
  box.innerHTML = sources.length ? '' : '<div class="empty">No company sources yet.</div>';
  sources.forEach((src) => {
    const row = document.createElement('div');
    row.className = 'doc-row source-row';
    const last = src.last_run
      ? `${src.last_error ? `<span class="err-text">✕ ${esc(src.last_error)}</span>` : `✓ ${src.last_count} listed`} · ${esc(fmtDate(src.last_run))}${src.last_method ? `<br>${esc(src.last_method)}` : ''}`
      : 'not run yet';
    row.innerHTML = `
      <input type="checkbox" aria-label="Enabled" ${src.enabled ? 'checked' : ''} />
      <div class="source-main">
        <input class="source-label" type="text" value="${esc(src.label)}" aria-label="Company name" />
        <a class="doc-meta" href="${esc(src.url)}" target="_blank" rel="noopener">${esc(src.url)}</a>
        <div class="doc-meta">${last}</div>
        <div class="doc-meta test-result"></div>
      </div>
      <button class="btn btn-ghost btn-sm" data-act="test" type="button">Test</button>
      <button class="btn btn-danger btn-sm" data-act="delete" type="button">Delete</button>`;
    row.querySelector('input[type="checkbox"]').addEventListener('change', (e) => patchSource(src.id, { enabled: e.target.checked }));
    row.querySelector('.source-label').addEventListener('change', (e) => patchSource(src.id, { label: e.target.value }));
    row.querySelector('[data-act="test"]').addEventListener('click', (e) => testSource(src, e.target, row.querySelector('.test-result')));
    row.querySelector('[data-act="delete"]').addEventListener('click', () => deleteSource(src));
    box.appendChild(row);
  });
}

async function addSource() {
  const url = $('#source-url').value.trim();
  if (!url) { toast('Enter a careers page URL first.', 'err'); return; }
  try {
    await api('/api/sources', { method: 'POST', body: JSON.stringify({ url, label: $('#source-label').value.trim() || null }) });
    $('#source-url').value = '';
    $('#source-label').value = '';
    loadSources();
  } catch (e) { toast(`Add source failed: ${e.message}`, 'err'); }
}

async function patchSource(id, body) {
  try { await api(`/api/sources/${id}`, { method: 'PATCH', body: JSON.stringify(body) }); toast('Source updated', 'ok', 2000); }
  catch (e) { toast(`Update failed: ${e.message}`, 'err'); loadSources(); }
}

async function deleteSource(src) {
  if (!confirm(`Delete source "${src.label}"? Its jobs stay in the list.`)) return;
  try { await api(`/api/sources/${src.id}`, { method: 'DELETE' }); loadSources(); }
  catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
}

async function testSource(src, btn, out) {
  btn.disabled = true;
  out.textContent = 'Testing (this can take a minute: crawl delays apply)…';
  try {
    const r = await api(`/api/sources/${src.id}/test`, { method: 'POST' });
    out.innerHTML = r.ok
      ? `✓ ${r.count} postings via ${esc(r.method)}<br>${r.sample.map((s) => `• ${esc(s.title)}${s.location ? ` (${esc(s.location)})` : ''}`).join('<br>')}`
      : `<span class="err-text">✕ ${esc(r.error)}</span>`;
    if ((r.notes || []).length) out.innerHTML += `<br><span class="muted">${r.notes.map(esc).join('<br>')}</span>`;
  } catch (e) { out.innerHTML = `<span class="err-text">✕ ${esc(e.message)}</span>`; }
  btn.disabled = false;
}

/* ---------- scorer ---------- */
async function loadScorerStatus() {
  const badge = $('#scorer-status');
  try {
    const s = await api('/api/score/status');
    const failing = s.has_key && s.scored === 0 && s.errors > 0;
    badge.className = `api-status ${!s.has_key || failing ? 'is-fail' : s.scored ? 'is-ok' : 'is-unknown'}`;
    badge.textContent = !s.has_key ? '● scorer: no API key'
      : failing ? '● scorer: failing (Search setup → Test connection)'
        : `● ${s.model}${s.scored ? '' : ' (not verified yet)'}`;
    badge.title = `${s.scored} scored, ${s.errors} failed`;
    $('#scorer-info').innerHTML = `
      Model <strong>${esc(s.model)}</strong>${s.model_known ? '' : ' <span class="err-text">(not a known Muse Spark model id)</span>'} at ${esc(s.base_url)} ·
      key: ${s.has_key ? esc(s.key_source) : '<span class="err-text">missing</span>'} · reasoning effort ${esc(s.reasoning_effort)}<br>
      ${s.scored} scored · ${s.pending} waiting to be scored · ${s.stale} stale (documents changed) · ${s.errors} failed ·
      profile ${s.profile_chars.toLocaleString()} characters`;
  } catch (e) {
    badge.className = 'api-status is-fail';
    badge.textContent = '● backend unreachable';
  }
}

async function testScorer() {
  const out = $('#score-test-status');
  out.textContent = 'Testing…';
  try {
    const r = await api('/api/score/test', { method: 'POST' });
    out.innerHTML = r.ok ? `✓ ${esc(r.message)}` : `<span class="err-text">✕ ${esc(r.message)}</span>`;
  } catch (e) { out.innerHTML = `<span class="err-text">✕ ${esc(e.message)}</span>`; }
}

/* ================= DOCUMENTS ================= */
async function loadDocs() {
  try {
    const docs = await api('/api/documents');
    renderDocs(docs);
    $('#docs-error').hidden = true;
  } catch (e) {
    $('#docs-error').textContent = `Could not load documents: ${e.message}`;
    $('#docs-error').hidden = false;
  }
}

function renderDocs(docs) {
  const list = $('#docs-list');
  list.innerHTML = '';
  $('#docs-empty').hidden = docs.length !== 0;
  docs.forEach((doc) => {
    const row = document.createElement('div');
    row.className = 'doc-row';
    row.innerHTML = `
      <span class="doc-name">${esc(doc.filename)}</span>
      <span class="doc-meta">${doc.text_len.toLocaleString()} chars · ${esc(fmtDate(doc.created_at))}</span>
      <span class="spacer"></span>
      <select aria-label="Document type">
        <option value="resume" ${doc.kind === 'resume' ? 'selected' : ''}>Resume / CV</option>
        <option value="cover_letter" ${doc.kind === 'cover_letter' ? 'selected' : ''}>Cover letter</option>
        <option value="other" ${doc.kind === 'other' ? 'selected' : ''}>Other (supporting)</option>
      </select>
      <label class="check"><input type="checkbox" ${doc.use_for_scoring ? 'checked' : ''} /> use for scoring</label>
      <button class="btn btn-ghost btn-sm" data-act="preview" type="button">Text</button>
      <button class="btn btn-danger btn-sm" data-act="delete" type="button">Delete</button>`;
    row.querySelector('select').addEventListener('change', (e) => patchDoc(doc.id, { kind: e.target.value }));
    row.querySelector('input[type="checkbox"]').addEventListener('change', (e) => patchDoc(doc.id, { use_for_scoring: e.target.checked }));
    row.querySelector('[data-act="preview"]').addEventListener('click', async () => {
      try { const d = await api(`/api/documents/${doc.id}/text`); openModal(d.filename, d.text || '(no text)'); }
      catch (e) { toast(e.message, 'err'); }
    });
    row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
      if (!confirm(`Delete "${doc.filename}"?`)) return;
      try { await api(`/api/documents/${doc.id}`, { method: 'DELETE' }); loadDocs(); loadScorerStatus(); }
      catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
    });
    list.appendChild(row);
  });
}

async function patchDoc(id, body) {
  try {
    await api(`/api/documents/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
    toast('Saved. Existing scores are now marked stale.', 'ok', 3000);
    loadScorerStatus();
    loadJobs();
  } catch (e) { toast(`Update failed: ${e.message}`, 'err'); loadDocs(); }
}

function initDocs() {
  $('#upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const files = Array.from($('#upload-input').files);
    if (!files.length) { toast('Choose a file first.', 'err'); return; }
    for (const f of files) {
      $('#upload-status').textContent = `Uploading ${f.name}…`;
      const fd = new FormData();
      fd.append('file', f, f.name);
      fd.append('kind', $('#upload-kind').value);
      try { await api('/api/documents', { method: 'POST', body: fd }); toast(`Uploaded ${f.name}`, 'ok'); }
      catch (err) { toast(`${f.name}: ${err.message}`, 'err', 8000); }
    }
    $('#upload-status').textContent = '';
    $('#upload-input').value = '';
    loadDocs();
    loadScorerStatus();
  });
}

/* ================= MAP ================= */
function initMap() {
  if (state.map) return;
  if (typeof L === 'undefined') { $('#pins-error').textContent = 'Map library failed to load (offline?).'; $('#pins-error').hidden = false; return; }
  state.map = L.map('map').setView([-33.87, 151.21], 9);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);
  state.jobLayer = L.layerGroup().addTo(state.map);
  state.map.on('click', addPinAt);
  $('#pin-radius').addEventListener('input', (e) => { $('#pin-radius-output').textContent = `${e.target.value} km`; });
  loadPins();
}

function drawJobMarkers() {
  if (!state.map || !state.jobLayer) return;
  state.jobLayer.clearLayers();
  state.jobs.filter((j) => j.lat !== null && j.lng !== null && !j.closed).forEach((j) => {
    L.circleMarker([j.lat, j.lng], { radius: 5, weight: 1, color: j.excluded_reason ? '#6b7788' : '#22c07a', fillOpacity: 0.7 })
      .bindPopup(`<b>${esc(j.title)}</b><br>${esc(j.company)}<br>${esc(j.location_text)}${j.excluded_reason ? `<br><i>${esc(j.excluded_reason)}</i>` : ''}`)
      .addTo(state.jobLayer);
  });
}

function drawPin(p) {
  const old = state.pinLayers.get(p.id);
  if (old) { old.marker.remove(); old.circle.remove(); }
  const marker = L.marker([p.lat, p.lng], { draggable: true }).addTo(state.map)
    .bindPopup(`<b>${esc(p.label)}</b><br>${esc(p.kind)} · ${p.radius_km} km`);
  const circle = L.circle([p.lat, p.lng], { radius: p.radius_km * 1000, weight: 1 }).addTo(state.map);
  marker.on('dragend', async () => {
    const ll = marker.getLatLng();
    circle.setLatLng(ll);
    await savePin(p, { lat: ll.lat, lng: ll.lng });
  });
  state.pinLayers.set(p.id, { marker, circle });
}

async function loadPins() {
  try {
    state.pins = await api('/api/pins');
    state.pinLayers.forEach(({ marker, circle }) => { marker.remove(); circle.remove(); });
    state.pinLayers.clear();
    state.pins.forEach(drawPin);
    const sane = state.pins.filter((p) => p.radius_km < 1000);
    if (sane.length) state.map.fitBounds(L.latLngBounds(sane.map((p) => [p.lat, p.lng])).pad(0.3));
    $('#pins-error').hidden = true;
  } catch (e) {
    $('#pins-error').textContent = `Could not load pins: ${e.message}`;
    $('#pins-error').hidden = false;
  }
  renderPinsList();
  drawJobMarkers();
}

async function addPinAt(e) {
  const kind = $('#pin-kind').value;
  const body = { lat: e.latlng.lat, lng: e.latlng.lng, radius_km: Number($('#pin-radius').value), kind, label: $('#pin-label').value.trim() || kind };
  try { await api('/api/pins', { method: 'POST', body: JSON.stringify(body) }); await loadPins(); pinsChanged(); }
  catch (err) { toast(`Pin save failed: ${err.message}`, 'err'); }
}

async function savePin(p, patch) {
  try { Object.assign(p, await api(`/api/pins/${p.id}`, { method: 'PUT', body: JSON.stringify(patch) })); drawPin(p); renderPinsList(); pinsChanged(); }
  catch (e) { toast(`Pin update failed: ${e.message}`, 'err'); }
}

async function deletePin(p) {
  try { await api(`/api/pins/${p.id}`, { method: 'DELETE' }); await loadPins(); pinsChanged(); }
  catch (e) { toast(`Pin delete failed: ${e.message}`, 'err'); }
}

const pinsChanged = debounce(async () => {
  try { const { changed } = await api('/api/jobs/refilter', { method: 'POST' }); if (changed) { toast(`Filters re-applied: ${changed} job(s) changed`, 'ok'); loadJobs(); } }
  catch (e) { toast(`Re-filtering failed: ${e.message}`, 'err'); }
}, 800);

function renderPinsList() {
  const box = $('#pins-list');
  box.innerHTML = state.pins.length ? '' : '<div class="empty">No pins yet. Click the map to drop one.</div>';
  state.pins.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'pin-row';
    const huge = p.radius_km >= 1000 ? '<span class="err-text" title="Ignored by the location filter: a radius this large covers everywhere. Remote roles are handled by the Remote toggles in Search setup.">⚠ ignored by location filter (radius ≥ 1000 km)</span>' : '';
    row.innerHTML = `
      <span class="pin-kind">${esc(p.kind)}</span>
      <strong>${esc(p.label)}</strong>
      <span class="muted">${p.lat.toFixed(4)}, ${p.lng.toFixed(4)}</span>
      <label>Radius <input type="number" min="0.1" step="0.5" value="${p.radius_km}" style="width:90px" /> km</label>
      <select aria-label="Pin kind">${['home', 'hybrid', 'onsite'].map((k) => `<option ${p.kind === k ? 'selected' : ''}>${k}</option>`).join('')}</select>
      ${huge}
      <span style="flex:1"></span>
      <button class="btn btn-ghost btn-sm" data-act="locate" type="button">Locate</button>
      <button class="btn btn-danger btn-sm" data-act="del" type="button">Delete</button>`;
    row.querySelector('input').addEventListener('change', (e) => savePin(p, { radius_km: Number(e.target.value) }));
    row.querySelector('select').addEventListener('change', (e) => savePin(p, { kind: e.target.value }));
    row.querySelector('[data-act="locate"]').addEventListener('click', () => { state.map.setView([p.lat, p.lng], 12); state.pinLayers.get(p.id)?.marker.openPopup(); });
    row.querySelector('[data-act="del"]').addEventListener('click', () => deletePin(p));
    box.appendChild(row);
  });
}

/* ---------- boot ---------- */
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initJobs();
  initSetup();
  initDocs();
  $('#source-add').addEventListener('click', addSource);
  $('#source-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addSource(); } });
  $('#score-test').addEventListener('click', testScorer);
  $('#score-stale').addEventListener('click', () => startRun('/api/score/run', { mode: 'stale' }));
  $('#score-all').addEventListener('click', () => { if (confirm('Rescore every job? This makes one API call per job.')) startRun('/api/score/run', { mode: 'all' }); });
  $('#modal-close').addEventListener('click', () => { $('#modal').hidden = true; });
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('#modal').hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#modal').hidden = true; });
  loadCompanies();
  loadJobs();
  loadSetup();
  loadSources();
  loadDocs();
  loadScorerStatus();
  loadLatestRun();
});
