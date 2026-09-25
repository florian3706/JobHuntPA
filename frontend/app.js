/* JobHuntPA dashboard — vanilla JS, no build step.
 *
 * Primary contract (task spec — these exact calls are made first):
 *   GET    /api/jobs                      -> Job[]
 *   PATCH  /api/jobs/{id}/status          body { status }
 *   PATCH  /api/jobs/status               body { ids: string[], status }
 *   GET    /api/search-setup              -> SearchSetup
 *   PUT    /api/search-setup              body SearchSetup
 *   GET    /api/documents                 -> Document[]
 *   POST   /api/documents                 multipart field "file"
 *   DELETE /api/documents/{id}
 *   GET    /api/documents/{id}/preview    -> file bytes (iframe)
 *   GET    /api/pins                      -> Pin[]
 *   POST   /api/pins                      body { lat, lng, radius_km, kind, label }
 *   PUT    /api/pins/{id}
 *   DELETE /api/pins/{id}
 *
 * Real-backend fallbacks (backend/ is READ-ONLY; observed shapes):
 *   /api/docs (+ /{id}/text)              docs router (prefix /api/docs)
 *   /api/profile + /api/dealbreakers      search profile split endpoints
 *   /api/pins                             identical paths (DELETE → 204)
 *   Job: { id, company, title, location_text, work_mode (remote_aus|
 *          remote_global|hybrid|onsite|unknown), salary_text/min/max,
 *          url, distance_km, status, excluded_reason, … }
 *   FitResult evidence_json: { score, requirements:
 *          [{ point, matched, evidence: [{ bullet, sub_bullets[] }] }],
 *          gaps[], summary }
 * The normalizers below accept BOTH shapes.
 */

'use strict';

const API = {
  jobs: '/api/jobs',
  jobStatus: (id) => `/api/jobs/${encodeURIComponent(id)}/status`,
  jobsBulkStatus: '/api/jobs/status',
  searchSetup: '/api/search-setup',
  profile: '/api/profile',
  dealbreakers: '/api/dealbreakers',
  documents: '/api/documents',
  document: (id) => `/api/documents/${encodeURIComponent(id)}`,
  documentPreview: (id) => `/api/documents/${encodeURIComponent(id)}/preview`,
  // real backend (backend/docs.py, prefix /api/docs)
  docsAlt: '/api/docs',
  docAlt: (id) => `/api/docs/${encodeURIComponent(id)}`,
  docAltText: (id) => `/api/docs/${encodeURIComponent(id)}/text`,
  pins: '/api/pins',
  pin: (id) => `/api/pins/${encodeURIComponent(id)}`,
  sources: '/api/sources',
  source: (id) => `/api/sources/${encodeURIComponent(id)}`,
  sourceTest: (id) => `/api/sources/${encodeURIComponent(id)}/test`,
  searchRun: '/api/search/run',
  score: (id) => `/api/score/${encodeURIComponent(id)}`,
  scoreTest: '/api/score/test',
};

const STATUSES = ['to_review', 'applied', 'shortlisted', 'not_interested'];

const state = {
  jobs: [],
  filter: { q: '', minScore: 0, mode: 'all', maxDist: null, hideExcluded: true, status: 'all' },
  selected: new Set(),
  searchSetup: null,
  searchRaw: null, // { profile, dealbreakers } from real backend (to preserve remote_global_ok)
  docs: [],
  docsSource: 'documents', // 'documents' | 'docs'
  pins: [],
  map: null,
  markers: new Map(), // pinId|tmpId -> { marker, circle, data }
  sources: [],
};

/* ---------- utils ---------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, kind = '') {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`.trim();
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${opts.method || 'GET'} ${url} → ${res.status} ${text.slice(0, 300)}`);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  const text = await res.text();
  if (!text) return null;
  if (ct.includes('application/json')) {
    try { return JSON.parse(text); } catch { return text; }
  }
  try { return JSON.parse(text); } catch { return text; }
}

/** GET primary, fall back to alternate on network/HTTP error. */
async function getWithFallback(primary, fallback) {
  try {
    return { data: await apiFetch(primary), source: 'primary' };
  } catch (e1) {
    if (!fallback) throw e1;
    const data = await apiFetch(fallback);
    return { data, source: 'fallback' };
  }
}

function setApiStatus(ok) {
  const el = $('#api-status');
  if (ok === null) { el.className = 'api-status is-unknown'; el.textContent = '● backend: checking…'; }
  else if (ok) { el.className = 'api-status is-ok'; el.textContent = '● backend: connected'; }
  else { el.className = 'api-status is-fail'; el.textContent = '● backend: unreachable'; }
}

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------- tabs ---------- */
function initTabs() {
  const btns = $$('.tab');
  const panels = { jobs: $('#tab-jobs'), search: $('#tab-search'), docs: $('#tab-docs'), map: $('#tab-map') };
  function show(name) {
    btns.forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
    Object.entries(panels).forEach(([k, p]) => { p.hidden = k !== name; });
    if (name === 'map') requestAnimationFrame(() => { initMap(); state.map && state.map.invalidateSize(); });
    location.hash = name;
  }
  btns.forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  const initial = (location.hash || '#jobs').slice(1);
  if (panels[initial]) show(initial);
}

/* ================= JOBS ================= */
function normalizeMode(raw) {
  const m = String(raw ?? 'unknown').toLowerCase().trim();
  if (['remote_aus', 'remote_global', 'remote-global', 'remote'].includes(m)) return 'remote';
  if (m === 'hybrid') return 'hybrid';
  if (m === 'onsite' || m === 'on-site' || m === 'on site') return 'onsite';
  if (['remote_aus_ok', 'remote_global_ok'].includes(m)) return 'remote';
  return 'unknown';
}
function displayMode(raw) {
  const m = String(raw ?? '').toLowerCase();
  if (m === 'remote_aus') return 'remote·aus';
  if (m === 'remote_global') return 'remote·global';
  return normalizeMode(raw);
}

function normRequirement(x) {
  if (typeof x === 'string') return { bullet: x, evidence: [], gaps: [] };
  const o = x || {};
  // scorer shape: { point, matched, evidence: [{ bullet, sub_bullets[] }] }
  const bullet = o.bullet ?? o.text ?? o.title ?? o.requirement ?? o.point ?? '';
  let evidence = o.evidence ?? o.matches ?? o.proof ?? o.details ?? [];
  if (!Array.isArray(evidence)) evidence = [];
  evidence = evidence.map((e) => {
    if (typeof e === 'string') return { bullet: e, sub: [] };
    const eo = e || {};
    return { bullet: eo.bullet ?? eo.text ?? String(eo ?? ''), sub: eo.sub_bullets ?? eo.subs ?? eo.children ?? [] };
  }).filter((e) => String(e.bullet || '').trim());
  // unmatched scorer entries carry evidence:[] — keep gaps from entry or top-level
  let gaps = o.gaps ?? o.missing ?? o.gap ?? [];
  if (!Array.isArray(gaps)) gaps = gaps ? [gaps] : [];
  const matched = o.matched ?? o.is_matched ?? (evidence.length > 0 ? true : undefined);
  return { bullet: String(bullet), evidence, gaps: gaps.map(String), matched };
}

function normJob(raw) {
  const r = raw || {};
  const id = r.id ?? r.job_id ?? r.uuid ?? Math.random().toString(36).slice(2);
  const modeRaw = r.work_mode ?? r.mode ?? r.workMode ?? 'unknown';
  // score may live on the job or inside an embedded fit result
  const fit = r.fit ?? r.fit_result ?? r.score_result ?? null;
  let requirements = r.requirements ?? r.criteria ?? r.matches ?? fit?.requirements ?? [];
  if (!Array.isArray(requirements)) requirements = [];
  let gaps = r.gaps ?? fit?.gaps ?? [];
  if (!Array.isArray(gaps)) gaps = [];
  const summary = r.summary ?? fit?.summary ?? r.fit_summary ?? '';
  const scoreRaw = r.score ?? r.match_score ?? fit?.score ?? 0;
  return {
    id: String(id),
    company: r.company ?? r.company_name ?? '—',
    title: r.title ?? r.job_title ?? '—',
    location: r.location ?? r.location_text ?? r.city ?? '—',
    mode: normalizeMode(modeRaw),
    modeLabel: displayMode(modeRaw),
    distanceKm: numOrNull(r.distance_km ?? r.distance ?? r.distanceKm),
    salary: r.salary ?? r.salary_text ?? r.compensation ?? fmtSalary(r),
    sourceUrl: r.source_url ?? r.url ?? r.link ?? null,
    sourceLabel: r.source ?? (r.source_url || r.url ? 'source' : null),
    score: Number(scoreRaw) || 0,
    status: STATUSES.includes(r.status) ? r.status : 'to_review',
    excluded: Boolean(r.excluded ?? r.is_excluded ?? r.excluded_reason),
    excludedReason: r.excluded_reason ?? r.excludedReason ?? '',
    industry: r.industry ?? '',
    requirements: requirements.map(normRequirement),
    gaps: gaps.map(String),
    summary: String(summary || ''),
    _raw: r,
  };
}

function numOrNull(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function fmtSalary(r) {
  const mn = r.salary_min ?? r.salaryMin, mx = r.salary_max ?? r.salaryMax;
  if (mn != null || mx != null) {
    const f = (n) => (n == null || n === '' ? '' : `${Number(n).toLocaleString()}€`);
    const t = [f(mn), f(mx)].filter(Boolean).join(' – ');
    if (t) return r.salary_text ? `${r.salary_text} (${t})` : t;
  }
  return '—';
}

function scoreClass(s) { return s >= 75 ? 'high' : s >= 45 ? 'mid' : 'low'; }

async function loadJobs() {
  const errBox = $('#jobs-error');
  errBox.hidden = true;
  try {
    const data = await apiFetch(API.jobs);
    const arr = Array.isArray(data) ? data : (data.jobs ?? data.items ?? data.results ?? []);
    state.jobs = arr.map(normJob).sort((a, b) => b.score - a.score);
    const ids = new Set(state.jobs.map((j) => j.id));
    state.selected.forEach((id) => { if (!ids.has(id)) state.selected.delete(id); });
    setApiStatus(true);
  } catch (e) {
    setApiStatus(false);
    errBox.textContent = `Could not load jobs: ${e.message}\nIs the backend running? Expected GET ${API.jobs} → Job[].`;
    errBox.hidden = false;
    state.jobs = [];
  }
  renderJobs();
}

function filteredJobs() {
  const f = state.filter;
  const q = f.q.trim().toLowerCase();
  return state.jobs
    .filter((j) => (f.status === 'all' ? true : j.status === f.status))
    .filter((j) => j.score >= f.minScore)
    .filter((j) => (f.mode === 'all' ? true : j.mode === f.mode))
    .filter((j) => (f.maxDist === null ? true : (j.distanceKm === null ? false : j.distanceKm <= f.maxDist)))
    .filter((j) => (!f.hideExcluded ? true : !j.excluded))
    .filter((j) => {
      if (!q) return true;
      const hay = `${j.company} ${j.title} ${j.location} ${j.salary} ${j.mode} ${j.modeLabel} ${j.industry} ${j.requirements.map((r) => r.bullet).join(' ')}`.toLowerCase();
      return q.split(/\s+/).every((tok) => hay.includes(tok));
    })
    .sort((a, b) => b.score - a.score);
}

function renderStatusChips() {
  const box = $('#status-chips');
  box.innerHTML = '';
  const counts = { all: state.jobs.length };
  STATUSES.forEach((s) => { counts[s] = state.jobs.filter((j) => j.status === s).length; });
  const defs = [['all', 'All'], ...STATUSES.map((s) => [s, s])];
  defs.forEach(([val, label]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `chip${state.filter.status === val ? ' active' : ''}`;
    b.innerHTML = `${esc(label)} <span class="n">${counts[val] ?? 0}</span>`;
    b.addEventListener('click', () => { state.filter.status = val; renderJobs(); });
    box.appendChild(b);
  });
}

function renderJobs() {
  renderStatusChips();
  const list = $('#jobs-list');
  const jobs = filteredJobs();
  list.innerHTML = '';
  $('#jobs-empty').hidden = jobs.length !== 0;
  $('#jobs-count').textContent = `${jobs.length} / ${state.jobs.length} shown · sorted by score ↓`;

  jobs.forEach((job) => {
    const card = document.createElement('article');
    card.className = `card job-card${job.excluded ? ' is-excluded' : ''}${state.selected.has(job.id) ? ' is-selected' : ''}`;
    card.dataset.id = job.id;

    const dist = job.distanceKm === null ? 'n/a' : `${job.distanceKm} km`;
    const src = job.sourceUrl
      ? `<a href="${esc(job.sourceUrl)}" target="_blank" rel="noopener">↗ ${esc(job.sourceLabel || 'source')}</a>`
      : `<span class="muted">no link</span>`;

    const reqItems = job.requirements.length ? job.requirements.map((r) => {
      const ev = (r.evidence || []).map((e) => {
        const subs = (e.sub || []).map((s) => `<li>${esc(s)}</li>`).join('');
        return `<li class="ev">✓ ${esc(e.bullet)}${subs ? `<ul>${subs}</ul>` : ''}</li>`;
      }).join('');
      const gaps = (r.gaps || []).map((g) => `<li class="gap">✕ gap: ${esc(g)}</li>`).join('');
      const sub = (ev || gaps) ? `<ul>${ev}${gaps}</ul>` : '';
      const mark = r.matched === true ? ' · matched' : r.matched === false ? ' · missing' : '';
      return `<li><div>${esc(r.bullet || '—')}<span class="muted">${esc(mark)}</span></div>${sub}</li>`;
    }).join('') : '<li class="muted">No requirement breakdown available.</li>';
    const topGaps = (job.gaps || []).map((g) => `<li class="gap">✕ ${esc(g)}</li>`).join('');
    const gapsBlock = topGaps ? `<div class="gaps-block"><strong>Gaps</strong><ul>${topGaps}</ul></div>` : '';
    const summaryBlock = job.summary ? `<p class="summary">${esc(job.summary)}</p>` : '';
    const exclBlock = job.excluded && job.excludedReason ? `<p class="hint">Excluded: ${esc(job.excludedReason)}</p>` : '';

    card.innerHTML = `
      <div class="job-top">
        <input type="checkbox" aria-label="Select job" ${state.selected.has(job.id) ? 'checked' : ''} />
        <div class="job-title-row">
          <div class="job-company">${esc(job.company)}${job.excluded ? ' · excluded' : ''}</div>
          <h3 class="job-title">${esc(job.title)}</h3>
          <div class="job-meta">
            <span>${esc(job.location)}</span>
            <span class="badge ${esc(job.mode)}" title="raw: ${esc(job._raw?.work_mode ?? job.mode)}">${esc(job.modeLabel)}</span>
            <span>📍 ${esc(dist)}</span>
          </div>
        </div>
        <span class="score ${scoreClass(job.score)}" title="Match score">${esc(String(job.score))}</span>
      </div>
      <div class="job-sub"><span>💰 ${esc(job.salary || '—')}</span><span>${src}</span></div>
      ${exclBlock}
      <div class="job-actions">
        <select aria-label="Application status">
          ${STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
        <button class="btn btn-ghost btn-sm" type="button" data-act="details">Requirements (${job.requirements.length})</button>
        <button class="btn btn-ghost btn-sm" type="button" data-act="rescore">Rescore</button>
      </div>
      <details class="reqs">
        <summary>Requirements → evidence + gaps</summary>
        <ul>${reqItems}</ul>
        ${gapsBlock}
        ${summaryBlock}
      </details>`;

    const checkbox = card.querySelector('input[type="checkbox"]');
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selected.add(job.id); else state.selected.delete(job.id);
      card.classList.toggle('is-selected', checkbox.checked);
      renderBulkBar();
    });

    const select = card.querySelector('select');
    select.addEventListener('change', () => updateJobStatus(job, select.value, select));

    card.querySelector('[data-act="details"]').addEventListener('click', () => {
      const d = card.querySelector('details');
      d.open = !d.open;
    });

    card.querySelector('[data-act="rescore"]').addEventListener('click', () => rescoreJob(job.id));

    list.appendChild(card);
  });

  renderBulkBar();
}

function renderBulkBar() {
  const bar = $('#bulk-bar');
  const n = state.selected.size;
  bar.hidden = n === 0;
  $('#bulk-count').textContent = `${n} selected`;
  const all = $('#bulk-select-all');
  const visibleIds = filteredJobs().map((j) => j.id);
  all.checked = visibleIds.length > 0 && visibleIds.every((id) => state.selected.has(id));
}

async function updateJobStatus(job, next, selectEl) {
  const prev = job.status;
  if (prev === next) return;
  job.status = next; // optimistic
  renderStatusChips();
  try {
    // Primary contract: PATCH /api/jobs/{id}/status { status }
    await apiFetch(API.jobStatus(job.id), { method: 'PATCH', body: JSON.stringify({ status: next }) });
    toast(`✓ ${job.company} — ${job.title} → ${next}`, 'ok');
  } catch (e) {
    job.status = prev; // rollback
    if (selectEl) selectEl.value = prev;
    renderStatusChips();
    toast(`Status update failed, rolled back: ${e.message}`, 'err');
  }
}

async function bulkUpdateStatus() {
  const ids = Array.from(state.selected);
  const status = $('#bulk-status').value;
  if (!ids.length) return;
  const prevMap = new Map(state.jobs.filter((j) => ids.includes(j.id)).map((j) => [j.id, j.status]));
  state.jobs.forEach((j) => { if (prevMap.has(j.id)) j.status = status; });
  renderJobs();
  try {
    // Primary contract: PATCH /api/jobs/status { ids, status }
    await apiFetch(API.jobsBulkStatus, { method: 'PATCH', body: JSON.stringify({ ids, status }) });
    toast(`✓ Updated ${ids.length} job(s) → ${status}`, 'ok');
    state.selected.clear();
    renderJobs();
  } catch (e) {
    state.jobs.forEach((j) => { if (prevMap.has(j.id)) j.status = prevMap.get(j.id); });
    renderJobs();
    toast(`Bulk update failed, rolled back: ${e.message}`, 'err');
  }
}

async function rescoreJob(id) {
  try {
    toast(`Rescoring job ${id}…`);
    const res = await apiFetch(API.score(id), { method: 'POST', body: JSON.stringify({}) });
    const summary = String(res?.summary ?? res?.result?.summary ?? '');
    const score = res?.score ?? res?.result?.score;
    if (summary.startsWith('ERROR:') || summary.startsWith('NO_KEY')) {
      toast(`Rescore ${id}: ${summary.slice(0, 300)}`, 'err');
    } else {
      toast(`✓ Rescored ${id} → ${score ?? '?'}`, 'ok');
    }
    await loadJobs();
  } catch (e) {
    toast(`Rescore failed: ${e.message}`, 'err');
  }
}

async function testScorer() {
  const status = $('#score-test-status');
  if (status) status.textContent = 'Testing scorer…';
  try {
    const res = await apiFetch(API.scoreTest, { method: 'POST', body: JSON.stringify({}) });
    const summary = String(res?.summary ?? res?.result?.summary ?? JSON.stringify(res).slice(0, 300));
    const ok = res?.ok !== false && !summary.startsWith('ERROR:') && !summary.startsWith('NO_KEY');
    toast(`Scorer test: ${summary.slice(0, 300)}`, ok ? 'ok' : 'err');
    if (status) status.textContent = `Done ✓ ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    toast(`Scorer test failed: ${e.message}`, 'err');
    if (status) status.textContent = 'Test failed';
  }
}

function initJobs() {
  $('#filter-search').addEventListener('input', debounce((e) => {
    state.filter.q = e.target.value; renderJobs();
  }, 150));
  $('#filter-min-score').addEventListener('input', (e) => {
    state.filter.minScore = Number(e.target.value);
    $('#score-output').textContent = e.target.value;
    renderJobs();
  });
  $('#filter-mode').addEventListener('change', (e) => { state.filter.mode = e.target.value; renderJobs(); });
  $('#filter-max-dist').addEventListener('input', (e) => {
    const v = e.target.value === '' ? null : Number(e.target.value);
    state.filter.maxDist = Number.isFinite(v) ? v : null;
    renderJobs();
  });
  $('#filter-hide-excluded').addEventListener('change', (e) => { state.filter.hideExcluded = e.target.checked; renderJobs(); });
  $('#jobs-reload').addEventListener('click', loadJobs);
  $('#bulk-apply').addEventListener('click', bulkUpdateStatus);
  $('#bulk-clear').addEventListener('click', () => { state.selected.clear(); renderJobs(); });
  $('#bulk-select-all').addEventListener('change', (e) => {
    const vis = filteredJobs().map((j) => j.id);
    if (e.target.checked) vis.forEach((id) => state.selected.add(id));
    else vis.forEach((id) => state.selected.delete(id));
    renderJobs();
  });
}

/* ================= SEARCH SETUP =================
 * Unified UI model:
 *   { titles[], keywords_include[], keywords_exclude[], salary_floor,
 *     allow_remote, allow_hybrid, allow_onsite,
 *     dealbreakers: { industries[], keywords[] } }
 * Primary: PUT /api/search-setup. Fallback: PUT /api/profile +
 * PUT /api/dealbreakers (real backend split).
 */
const SEARCH_DEFAULTS = {
  titles: [], keywords_include: [], keywords_exclude: [],
  salary_floor: null, allow_remote: true, allow_hybrid: true, allow_onsite: true,
  dealbreakers: { industries: [], keywords: [] },
};

function tagInputs() { return $$('.tag-input'); }

function getTagValues(field) {
  const wrap = document.querySelector(`.tag-input[data-field="${field}"] .tags`);
  if (!wrap) return [];
  return Array.from(wrap.querySelectorAll('.tag')).map((t) => t.dataset.value);
}

function setTagValues(field, values) {
  const wrap = document.querySelector(`.tag-input[data-field="${field}"] .tags`);
  if (!wrap) return;
  wrap.innerHTML = '';
  (values || []).forEach((v) => addTag(field, String(v), false));
}

function addTag(field, value, triggerSave = true) {
  value = String(value || '').trim();
  if (!value) return;
  const wrap = document.querySelector(`.tag-input[data-field="${field}"] .tags`);
  if (!wrap) return;
  const exists = Array.from(wrap.querySelectorAll('.tag')).some((t) => t.dataset.value.toLowerCase() === value.toLowerCase());
  if (exists) return;
  const el = document.createElement('span');
  el.className = 'tag';
  el.dataset.value = value;
  el.innerHTML = `<span>${esc(value)}</span><button type="button" aria-label="Remove ${esc(value)}">×</button>`;
  el.querySelector('button').addEventListener('click', () => { el.remove(); scheduleSearchSave(); });
  wrap.appendChild(el);
  if (triggerSave) scheduleSearchSave();
}

function collectSearchSetup() {
  return {
    titles: getTagValues('titles'),
    keywords_include: getTagValues('keywords_include'),
    keywords_exclude: getTagValues('keywords_exclude'),
    salary_floor: $('#salary-floor').value === '' ? null : Number($('#salary-floor').value),
    allow_remote: $('#allow-remote').checked,
    allow_hybrid: $('#allow-hybrid').checked,
    allow_onsite: $('#allow-onsite').checked,
    dealbreakers: {
      industries: getTagValues('dealbreakers_industries'),
      keywords: getTagValues('dealbreakers_keywords'),
    },
  };
}

function fillSearchSetup(s) {
  const d = { ...SEARCH_DEFAULTS, ...(s || {}) };
  d.dealbreakers = { ...SEARCH_DEFAULTS.dealbreakers, ...((s && s.dealbreakers) || {}) };
  setTagValues('titles', d.titles ?? []);
  setTagValues('keywords_include', d.keywords_include ?? []);
  setTagValues('keywords_exclude', d.keywords_exclude ?? []);
  $('#salary-floor').value = d.salary_floor ?? '';
  $('#allow-remote').checked = d.allow_remote ?? true;
  $('#allow-hybrid').checked = d.allow_hybrid ?? true;
  $('#allow-onsite').checked = d.allow_onsite ?? true;
  setTagValues('dealbreakers_industries', d.dealbreakers.industries ?? []);
  setTagValues('dealbreakers_keywords', d.dealbreakers.keywords ?? []);
}

/** Map real-backend GET /api/profile + /api/dealbreakers → unified model. */
function fromRealBackend(profile, dealbreakers) {
  const p = profile || {};
  const db = dealbreakers || {};
  return {
    titles: p.titles ?? p.job_titles ?? [],
    keywords_include: p.keywords_include ?? p.include ?? [],
    keywords_exclude: p.keywords_exclude ?? p.exclude ?? [],
    salary_floor: p.salary_floor ?? p.salaryFloor ?? null,
    // real backend splits remote into aus/global; UI has one remote toggle (OR)
    allow_remote: (p.remote_aus_ok ?? p.allow_remote ?? true) || (p.remote_global_ok ?? false),
    allow_hybrid: p.allow_hybrid ?? true,
    allow_onsite: p.allow_onsite ?? true,
    dealbreakers: {
      industries: db.industries ?? [],
      keywords: db.keywords ?? [],
    },
  };
}

async function loadSearchSetup() {
  const box = $('#search-error');
  box.hidden = true;
  // 1) primary contract
  try {
    const data = await apiFetch(API.searchSetup);
    state.searchSetup = data;
    state.searchRaw = null;
    fillSearchSetup(data);
    $('#search-save-status').textContent = 'Loaded ✓ (/api/search-setup)';
    return;
  } catch (e1) {
    // 2) real-backend split endpoints
    try {
      const [profile, dealbreakers] = await Promise.all([
        apiFetch(API.profile), apiFetch(API.dealbreakers),
      ]);
      state.searchRaw = { profile, dealbreakers };
      const unified = fromRealBackend(profile, dealbreakers);
      state.searchSetup = unified;
      fillSearchSetup(unified);
      $('#search-save-status').textContent = 'Loaded ✓ (/api/profile + /api/dealbreakers)';
      return;
    } catch (e2) {
      box.textContent = `Could not load search setup: ${e1.message}\n(fallback ${API.profile} also failed: ${e2.message}). You can still edit — saving will retry.`;
      box.hidden = false;
      fillSearchSetup(SEARCH_DEFAULTS);
      $('#search-save-status').textContent = 'Not on server yet (will create on save)';
    }
  }
}

const scheduleSearchSave = debounce(async () => {
  const status = $('#search-save-status');
  const payload = collectSearchSetup();
  status.textContent = 'Saving…';
  const errors = [];
  // 1) primary contract first (acceptance requirement)
  try {
    const saved = await apiFetch(API.searchSetup, { method: 'PUT', body: JSON.stringify(payload) });
    state.searchSetup = saved ?? payload;
    status.textContent = `Saved ✓ ${new Date().toLocaleTimeString()} (/api/search-setup)`;
  } catch (e) {
    errors.push(`search-setup: ${e.message}`);
  }
  // 2) real-backend split endpoints (best-effort mirror so both backends stay in sync)
  try {
    const keepGlobal = state.searchRaw?.profile?.remote_global_ok ?? false;
    const profilePayload = {
      titles: payload.titles,
      keywords_include: payload.keywords_include,
      keywords_exclude: payload.keywords_exclude,
      salary_floor: payload.salary_floor,
      remote_aus_ok: payload.allow_remote,
      remote_global_ok: keepGlobal,
    };
    const dealPayload = {
      industries: payload.dealbreakers.industries,
      keywords: payload.dealbreakers.keywords,
    };
    const [p, d] = await Promise.all([
      apiFetch(API.profile, { method: 'PUT', body: JSON.stringify(profilePayload) }),
      apiFetch(API.dealbreakers, { method: 'PUT', body: JSON.stringify(dealPayload) }),
    ]);
    state.searchRaw = { profile: p, dealbreakers: d };
    if (errors.length) status.textContent = `Saved ✓ ${new Date().toLocaleTimeString()} (/api/profile + /api/dealbreakers)`;
  } catch (e) {
    errors.push(`profile/dealbreakers: ${e.message}`);
  }
  if (errors.length === 2 || (errors.length === 1 && !status.textContent.startsWith('Saved'))) {
    status.textContent = 'Save failed — will retry on next change';
    toast(`Search setup save failed: ${errors.join(' | ')}`, 'err');
  } else if (errors.length) {
    // one path succeeded; note the other quietly
    console.warn('Search save partial:', errors.join(' | '));
  }
}, 600);

function initSearchSetup() {
  tagInputs().forEach((wrap) => {
    const input = wrap.querySelector('input');
    const field = wrap.dataset.field;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        input.value.split(',').forEach((v) => addTag(field, v));
        input.value = '';
      } else if (e.key === 'Backspace' && !input.value) {
        wrap.querySelector('.tags .tag:last-child')?.remove();
        scheduleSearchSave();
      }
    });
    input.addEventListener('blur', () => {
      if (input.value.trim()) { addTag(field, input.value.trim()); input.value = ''; }
    });
  });
  $('#salary-floor').addEventListener('input', scheduleSearchSave);
  ['allow-remote', 'allow-hybrid', 'allow-onsite'].forEach((id) => {
    document.getElementById(id).addEventListener('change', scheduleSearchSave);
  });
  $('#search-form').addEventListener('submit', (e) => e.preventDefault());
  $('#search-reload').addEventListener('click', loadSearchSetup);
}

/* ================= DOCUMENTS =================
 * Primary: /api/documents (+ /{id}/preview). Fallback: /api/docs
 * (+ /{id}/text for preview, since the real backend stores extracted
 * text rather than serving raw bytes).
 */
function normDoc(d, source) {
  return {
    id: String(d.id ?? d.filename ?? d.name),
    name: d.filename ?? d.name ?? d.title ?? 'document',
    size: d.size ?? d.size_bytes ?? null,
    created: d.created_at ?? d.uploaded_at ?? d.created ?? null,
    mime: d.mime ?? d.content_type ?? d.filetype ?? '',
    kind: d.kind ?? '',
    textLen: d.text_len ?? null,
    source,
    _raw: d,
  };
}

function docPreviewUrl(doc) {
  if (doc.source === 'docs') return API.docAltText(doc.id);
  return doc._raw.preview_url || doc._raw.file_url || doc._raw.url || API.documentPreview(doc.id);
}

async function loadDocs() {
  const err = $('#docs-error');
  err.hidden = true;
  try {
    const { data, source } = await getWithFallback(API.documents, API.docsAlt);
    const arr = Array.isArray(data) ? data : (data.documents ?? data.items ?? []);
    state.docsSource = source === 'fallback' ? 'docs' : 'documents';
    state.docs = arr.map((d) => normDoc(d, state.docsSource));
  } catch (e) {
    err.textContent = `Could not load documents: ${e.message}\nTried GET ${API.documents} then GET ${API.docsAlt}.`;
    err.hidden = false;
    state.docs = [];
  }
  renderDocs();
}

function renderDocs() {
  const list = $('#docs-list');
  list.innerHTML = '';
  $('#docs-empty').hidden = state.docs.length !== 0;
  state.docs.forEach((doc) => {
    const row = document.createElement('div');
    row.className = 'doc-row';
    const size = doc.size ? ` · ${(Number(doc.size) / 1024).toFixed(1)} KB` : '';
    const tlen = doc.textLen != null ? ` · ${doc.textLen} chars` : '';
    const date = doc.created ? ` · ${esc(new Date(doc.created).toLocaleString())}` : '';
    const kind = doc.kind ? ` · ${esc(doc.kind)}` : '';
    row.innerHTML = `
      <span class="doc-name">${esc(doc.name)}</span>
      <span class="doc-meta">${esc(doc.mime || '')}${size}${tlen}${kind}${date}</span>
      <span class="spacer"></span>
      <button class="btn btn-ghost btn-sm" data-act="preview" type="button">Preview</button>
      <button class="btn btn-danger btn-sm" data-act="delete" type="button">Delete</button>`;
    row.querySelector('[data-act="preview"]').addEventListener('click', () => openPreview(doc));
    row.querySelector('[data-act="delete"]').addEventListener('click', () => deleteDoc(doc, row));
    list.appendChild(row);
  });
}

async function openPreview(doc) {
  $('#preview-title').textContent = doc.name;
  const frame = $('#preview-frame');
  let pre = $('#preview-text');
  frame.hidden = false;
  if (pre) pre.hidden = true;
  if (doc.source === 'docs') {
    // real backend: extracted text as JSON { id, filename, text }
    try {
      const data = await apiFetch(API.docAltText(doc.id));
      const text = typeof data === 'string' ? data : (data.text ?? JSON.stringify(data));
      if (!pre) {
        pre = document.createElement('pre');
        pre.id = 'preview-text';
        pre.className = 'preview-text';
        frame.after(pre);
      }
      pre.textContent = text || '(no extracted text)';
      pre.hidden = false;
      frame.hidden = true;
      frame.src = 'about:blank';
    } catch (e) {
      toast(`Preview failed: ${e.message}`, 'err');
      return;
    }
  } else {
    frame.src = docPreviewUrl(doc);
  }
  $('#preview-modal').hidden = false;
}

function initDocs() {
  $('#preview-close').addEventListener('click', () => {
    $('#preview-modal').hidden = true;
    $('#preview-frame').src = 'about:blank';
  });
  $('#preview-modal').addEventListener('click', (e) => {
    if (e.target.id === 'preview-modal') $('#preview-close').click();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#preview-modal').hidden) $('#preview-close').click();
  });
  $('#upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = $('#upload-input');
    const status = $('#upload-status');
    if (!input.files.length) { toast('Choose a PDF / ODF / DOCX file first.', 'err'); return; }
    // real backend allows: .pdf .docx .odt .ods .odp (spec: PDF/ODF/DOCX)
    const allowed = /\.(pdf|odt|ods|odp|docx?)$/i;
    for (const f of input.files) {
      if (!allowed.test(f.name)) { toast(`Skipped ${f.name}: only PDF / ODF / DOCX allowed.`, 'err'); continue; }
      status.textContent = `Uploading ${f.name}…`;
      // primary contract first
      let ok = false, lastErr = '';
      try {
        const fd = new FormData();
        fd.append('file', f, f.name);
        const res = await fetch(API.documents, { method: 'POST', body: fd });
        if (!res.ok) throw new Error(`POST ${API.documents} → ${res.status} ${(await res.text()).slice(0, 200)}`);
        ok = true;
      } catch (err) { lastErr = err.message; }
      if (!ok) {
        // real-backend fallback (extra "kind" form field)
        try {
          const fd = new FormData();
          fd.append('file', f, f.name);
          fd.append('kind', 'cv');
          const res = await fetch(API.docsAlt, { method: 'POST', body: fd });
          if (!res.ok) throw new Error(`POST ${API.docsAlt} → ${res.status} ${(await res.text()).slice(0, 200)}`);
          ok = true;
        } catch (err) { lastErr += ` | ${err.message}`; }
      }
      toast(ok ? `✓ Uploaded ${f.name}` : `Upload failed for ${f.name}: ${lastErr}`, ok ? 'ok' : 'err');
    }
    input.value = '';
    status.textContent = '';
    loadDocs();
  });
}

async function deleteDoc(doc, rowEl) {
  if (!confirm(`Delete "${doc.name}"?`)) return;
  rowEl.style.opacity = '0.5';
  const primary = doc.source === 'docs' ? API.docAlt(doc.id) : API.document(doc.id);
  const fallback = doc.source === 'docs' ? API.document(doc.id) : API.docAlt(doc.id);
  try {
    try { await apiFetch(primary, { method: 'DELETE' }); }
    catch { await apiFetch(fallback, { method: 'DELETE' }); }
    state.docs = state.docs.filter((d) => d.id !== doc.id);
    renderDocs();
    toast(`✓ Deleted ${doc.name}`, 'ok');
  } catch (e) {
    rowEl.style.opacity = '';
    toast(`Delete failed: ${e.message}`, 'err');
  }
}

/* ================= MAP ================= */
function initMap() {
  if (state.map) return;
  if (typeof L === 'undefined') {
    $('#pins-error').textContent = 'Leaflet failed to load (CDN unreachable). Check network.';
    $('#pins-error').hidden = false;
    return;
  }
  const map = L.map('map').setView([-33.87, 151.21], 10); // Sydney default (AU market)
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  state.map = map;
  map.on('click', onMapClick);
  $('#pin-radius').addEventListener('input', (e) => {
    $('#pin-radius-output').textContent = `${e.target.value} km`;
  });
  $('#pins-reload').addEventListener('click', loadPins);
  loadPins();
}

function pinPopupHtml(p) {
  return `<b>${esc(p.label || p.kind || 'pin')}</b><br/>${esc(p.kind || '')} · ${esc(String(p.radius_km ?? '?'))} km`;
}

function drawPin(p) {
  const key = p.id ?? p._tmp;
  const old = state.markers.get(key);
  if (old) { state.map.removeLayer(old.marker); state.map.removeLayer(old.circle); }
  const marker = L.marker([p.lat, p.lng], { draggable: true }).addTo(state.map);
  marker.bindPopup(pinPopupHtml(p));
  const circle = L.circle([p.lat, p.lng], { radius: Number(p.radius_km ?? 25) * 1000 }).addTo(state.map);
  marker.on('dragend', async () => {
    const ll = marker.getLatLng();
    p.lat = ll.lat; p.lng = ll.lng;
    circle.setLatLng(ll);
    await savePin(p, true);
    renderPinsList();
  });
  state.markers.set(key, { marker, circle, data: p });
}

async function onMapClick(e) {
  const kind = $('#pin-kind').value;
  const radiusKm = Number($('#pin-radius').value);
  const label = $('#pin-label').value.trim() || kind;
  const pin = { _tmp: `tmp-${Date.now()}`, lat: e.latlng.lat, lng: e.latlng.lng, radius_km: radiusKm, kind, label };
  drawPin(pin);
  renderPinsList();
  try {
    // POST /api/pins { lat, lng, radius_km, kind, label } (both contracts agree)
    const saved = await apiFetch(API.pins, {
      method: 'POST',
      body: JSON.stringify({ lat: pin.lat, lng: pin.lng, radius_km: pin.radius_km, kind: pin.kind, label: pin.label || null }),
    });
    const s = saved?.pin ?? saved ?? {};
    const oldM = state.markers.get(pin._tmp);
    if (oldM) { state.map.removeLayer(oldM.marker); state.map.removeLayer(oldM.circle); state.markers.delete(pin._tmp); }
    const merged = { id: String(s.id ?? s.pin_id ?? Date.now()), ...pin, ...s, lat: Number(s.lat ?? pin.lat), lng: Number(s.lng ?? pin.lng) };
    delete merged._tmp;
    state.pins.push(merged);
    drawPin(merged);
    renderPinsList();
    toast('✓ Pin saved', 'ok');
  } catch (err) {
    toast(`Pin save failed (kept locally, drag to retry): ${err.message}`, 'err');
    state.pins.push(pin);
    renderPinsList();
  }
}

async function loadPins() {
  const err = $('#pins-error');
  err.hidden = true;
  try {
    const data = await apiFetch(API.pins);
    const arr = Array.isArray(data) ? data : (data.pins ?? data.items ?? []);
    state.pins = arr.map((r) => ({
      id: String(r.id ?? r.pin_id ?? `${r.lat},${r.lng}`),
      lat: Number(r.lat ?? r.latitude),
      lng: Number(r.lng ?? r.lon ?? r.longitude),
      radius_km: Number(r.radius_km ?? r.radiusKm ?? r.radius ?? r.max_km ?? 25),
      kind: ['home', 'hybrid', 'onsite'].includes(r.kind) ? r.kind : 'home',
      label: r.label ?? r.name ?? '',
    })).filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lng));
    state.markers.forEach(({ marker, circle }) => { state.map.removeLayer(marker); state.map.removeLayer(circle); });
    state.markers.clear();
    state.pins.forEach(drawPin);
    if (state.pins.length) {
      const b = L.latLngBounds(state.pins.map((p) => [p.lat, p.lng]));
      state.map.fitBounds(b.pad(0.4));
    }
  } catch (e) {
    err.textContent = `Could not load pins: ${e.message}\nExpected GET ${API.pins} → Pin[]. You can still click the map to create pins.`;
    err.hidden = false;
  }
  renderPinsList();
}

async function savePin(p, silent = false) {
  if (!p.id) { // unsaved temp pin — re-POST
    try {
      const saved = await apiFetch(API.pins, {
        method: 'POST',
        body: JSON.stringify({ lat: p.lat, lng: p.lng, radius_km: p.radius_km, kind: p.kind, label: p.label || null }),
      });
      p.id = String(saved?.id ?? saved?.pin?.id ?? Date.now());
      if (!silent) toast('✓ Pin saved', 'ok');
    } catch (e) { if (!silent) toast(`Pin save failed: ${e.message}`, 'err'); }
    return;
  }
  try {
    // PUT /api/pins/{id} (real backend uses PUT for partial update)
    await apiFetch(API.pin(p.id), {
      method: 'PUT',
      body: JSON.stringify({ lat: p.lat, lng: p.lng, radius_km: p.radius_km, kind: p.kind, label: p.label }),
    });
    const m = state.markers.get(p.id);
    if (m) { m.circle.setRadius(Number(p.radius_km) * 1000); m.marker.setPopupContent(pinPopupHtml(p)); }
    if (!silent) toast('✓ Pin updated', 'ok');
  } catch (e) { if (!silent) toast(`Pin update failed: ${e.message}`, 'err'); }
}

async function deletePin(p) {
  if (p.id && !String(p.id).startsWith('tmp')) {
    try { await apiFetch(API.pin(p.id), { method: 'DELETE' }); } // 204 handled in apiFetch
    catch (e) { toast(`Pin delete failed: ${e.message}`, 'err'); return; }
  }
  const key = p.id ?? p._tmp;
  const m = state.markers.get(key);
  if (m && state.map) { state.map.removeLayer(m.marker); state.map.removeLayer(m.circle); state.markers.delete(key); }
  state.pins = state.pins.filter((x) => x !== p);
  renderPinsList();
  toast('✓ Pin deleted', 'ok');
}

function renderPinsList() {
  const box = $('#pins-list');
  box.innerHTML = '';
  if (!state.pins.length && state.markers.size === 0) {
    box.innerHTML = '<div class="empty">No pins yet — click anywhere on the map to drop one.</div>';
    return;
  }
  state.pins.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'pin-row';
    row.innerHTML = `
      <span class="pin-kind">${esc(p.kind)}</span>
      <strong>${esc(p.label || 'Untitled pin')}</strong>
      <span class="muted">${Number(p.lat).toFixed(4)}, ${Number(p.lng).toFixed(4)}</span>
      <label>Radius <input type="number" min="1" max="200" step="1" value="${esc(String(p.radius_km ?? 25))}" style="width:70px" /> km</label>
      <select aria-label="Pin kind">
        ${['home', 'hybrid', 'onsite'].map((k) => `<option value="${k}" ${p.kind === k ? 'selected' : ''}>${k}</option>`).join('')}
      </select>
      <span class="spacer" style="flex:1"></span>
      <button class="btn btn-primary btn-sm" data-act="save" type="button">Save</button>
      <button class="btn btn-ghost btn-sm" data-act="locate" type="button">Locate</button>
      <button class="btn btn-danger btn-sm" data-act="del" type="button">Delete</button>`;
    const radiusInput = row.querySelector('input[type="number"]');
    const kindSel = row.querySelector('select');
    radiusInput.addEventListener('change', () => {
      p.radius_km = Number(radiusInput.value) || p.radius_km;
      const m = state.markers.get(p.id ?? p._tmp);
      if (m) m.circle.setRadius(Number(p.radius_km) * 1000);
    });
    kindSel.addEventListener('change', () => { p.kind = kindSel.value; });
    row.querySelector('[data-act="save"]').addEventListener('click', async () => {
      p.radius_km = Number(radiusInput.value) || p.radius_km;
      p.kind = kindSel.value;
      await savePin(p);
      renderPinsList();
    });
    row.querySelector('[data-act="locate"]').addEventListener('click', () => {
      state.map.setView([p.lat, p.lng], Math.max(state.map.getZoom(), 11));
      state.markers.get(p.id ?? p._tmp)?.marker.openPopup();
    });
    row.querySelector('[data-act="del"]').addEventListener('click', () => deletePin(p));
    box.appendChild(row);
  });
}

/* ================= SOURCES + RUN SEARCH =================
 *   GET    /api/sources               -> Source[]
 *   POST   /api/sources               body { url }
 *   PATCH  /api/sources/{id}          body { enabled }
 *   DELETE /api/sources/{id}
 *   POST   /api/sources/{id}/test     -> { ok, … }
 *   POST   /api/search/run            body {} -> { fetched, excluded,
 *          inserted, updated, per_source }
 */
function normSource(raw) {
  const r = raw || {};
  return {
    id: String(r.id ?? r.source_id ?? r.url ?? Math.random().toString(36).slice(2)),
    url: r.url ?? r.source_url ?? r.link ?? '',
    enabled: (r.enabled ?? r.is_enabled ?? r.active ?? true) !== false,
    label: r.label ?? r.name ?? '',
    _raw: r,
  };
}

async function loadSources() {
  const err = $('#sources-error');
  if (err) err.hidden = true;
  try {
    const data = await apiFetch(API.sources);
    const arr = Array.isArray(data) ? data : (data.sources ?? data.items ?? data.results ?? []);
    state.sources = arr.map(normSource);
  } catch (e) {
    if (err) {
      err.textContent = `Could not load sources: ${e.message}\nExpected GET ${API.sources} → Source[].`;
      err.hidden = false;
    }
    state.sources = [];
  }
  renderSourcesList();
}

function renderSourcesList() {
  const box = $('#sources-list');
  if (!box) return;
  box.innerHTML = '';
  if (!state.sources.length) {
    box.innerHTML = '<div class="empty">No sources yet — add a job-board URL above.</div>';
    return;
  }
  state.sources.forEach((src) => {
    const row = document.createElement('div');
    row.className = 'doc-row';
    row.dataset.id = src.id;
    const label = src.label ? ` · ${esc(src.label)}` : '';
    row.innerHTML = `
      <input type="checkbox" aria-label="Enable source" ${src.enabled ? 'checked' : ''} />
      <span class="doc-name">${esc(src.url || '(no url)')}</span>
      <span class="doc-meta">${src.enabled ? 'enabled' : 'disabled'}${label}</span>
      <span class="spacer"></span>
      <button class="btn btn-ghost btn-sm" data-act="test" type="button">Test</button>
      <button class="btn btn-danger btn-sm" data-act="delete" type="button">Delete</button>`;
    row.querySelector('input[type="checkbox"]').addEventListener('change', (e) => {
      toggleSource(src.id, e.target.checked);
    });
    row.querySelector('[data-act="test"]').addEventListener('click', () => testSource(src.id));
    row.querySelector('[data-act="delete"]').addEventListener('click', () => deleteSource(src.id));
    box.appendChild(row);
  });
}

async function addSource(url) {
  const input = $('#source-url');
  const value = String(url ?? input?.value ?? '').trim();
  if (!value) { toast('Enter a source URL first.', 'err'); return; }
  try {
    await apiFetch(API.sources, { method: 'POST', body: JSON.stringify({ url: value }) });
    toast(`✓ Source added: ${value}`, 'ok');
    if (input && url === undefined) input.value = '';
    await loadSources();
  } catch (e) {
    toast(`Add source failed: ${e.message}`, 'err');
  }
}

async function toggleSource(id, enabled) {
  const src = state.sources.find((s) => s.id === String(id));
  const prev = src ? src.enabled : !enabled;
  if (src) { src.enabled = enabled; renderSourcesList(); }
  try {
    await apiFetch(API.source(id), { method: 'PATCH', body: JSON.stringify({ enabled }) });
    toast(`✓ Source ${enabled ? 'enabled' : 'disabled'}`, 'ok');
  } catch (e) {
    if (src) { src.enabled = prev; renderSourcesList(); }
    toast(`Toggle source failed, rolled back: ${e.message}`, 'err');
  }
}

async function deleteSource(id) {
  const src = state.sources.find((s) => s.id === String(id));
  if (!confirm(`Delete source "${src?.url || id}"?`)) return;
  try {
    await apiFetch(API.source(id), { method: 'DELETE' });
    state.sources = state.sources.filter((s) => s.id !== String(id));
    renderSourcesList();
    toast('✓ Source deleted', 'ok');
  } catch (e) {
    toast(`Delete source failed: ${e.message}`, 'err');
  }
}

async function testSource(id) {
  try {
    const res = await apiFetch(API.sourceTest(id), { method: 'POST', body: JSON.stringify({}) });
    const detail = typeof res === 'string' ? res : (res?.message ?? res?.detail ?? JSON.stringify(res ?? { ok: true }));
    toast(`Source test: ${detail}`, 'ok');
  } catch (e) {
    toast(`Source test failed: ${e.message}`, 'err');
  }
}

async function runSearch() {
  const btn = $('#search-run');
  const status = $('#search-run-status');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Searching…';
  // Snapshot current Search setup UI so the backend filters with what the user sees.
  let setup = null;
  try {
    if (typeof collectSearchSetup === 'function') setup = collectSearchSetup();
  } catch { setup = null; }
  if (!setup || typeof setup !== 'object') {
    const s = state.searchSetup || {};
    setup = {
      titles: s.titles ?? [],
      keywords_include: s.keywords_include ?? [],
      keywords_exclude: s.keywords_exclude ?? [],
      salary_floor: s.salary_floor ?? null,
      allow_remote: s.allow_remote ?? true,
      allow_hybrid: s.allow_hybrid ?? true,
      allow_onsite: s.allow_onsite ?? true,
      dealbreakers: s.dealbreakers ?? { industries: [], keywords: [] },
      home_location: s.home_location,
      location: s.location,
    };
  }
  const dealbreakers = {
    industries: setup.dealbreakers?.industries ?? [],
    keywords: setup.dealbreakers?.keywords ?? [],
  };
  const allowedModes = [
    setup.allow_remote ? 'remote' : null,
    setup.allow_hybrid ? 'hybrid' : null,
    setup.allow_onsite ? 'onsite' : null,
  ].filter(Boolean);
  const location = state.searchRaw?.profile?.home_location
    ?? setup.home_location ?? setup.location ?? null;
  const payload = {
    titles: setup.titles ?? [],
    keywords_include: setup.keywords_include ?? [],
    keywords_exclude: setup.keywords_exclude ?? [],
    salary_floor: setup.salary_floor ?? null,
    allow_remote: setup.allow_remote ?? true,
    allow_hybrid: setup.allow_hybrid ?? true,
    allow_onsite: setup.allow_onsite ?? true,
    allowed_modes: allowedModes,
    dealbreakers,
    pins: state.pins,
    adapters: { seek: { location: location || null, max_pages: 1 } },
    sources: 'auto',
  };
  try {
    const res = await apiFetch(API.searchRun, { method: 'POST', body: JSON.stringify(payload) });
    const r = res || {};
    const fetched = Number(r.fetched ?? 0);
    const excluded = Number(r.excluded ?? 0);
    const inserted = Number(r.inserted ?? 0);
    const updated = Number(r.updated ?? 0);
    const per = r.per_source ?? r.perSource ?? r.sources ?? r.per_adapter ?? null;
    let perText = '';
    if (Array.isArray(per)) {
      perText = per.map((p) => {
        if (typeof p === 'string') return p;
        const o = p || {};
        const count = o.count ?? o.fetched ?? o.jobs ?? null;
        const excl = o.excluded ?? o.excluded_count ?? null;
        const label = o.label ?? o.source ?? o.url ?? o.name ?? '?';
        let piece = count !== null ? `${label}: ${count} fetched` : `${label}: ${JSON.stringify(o)}`;
        if (excl !== null) piece += `, ${excl} excluded`;
        if (o.error) piece += ` (error: ${o.error})`;
        return piece;
      }).join(' · ');
    } else if (per && typeof per === 'object') {
      perText = Object.entries(per).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
    }
    const applied = r.applied_filters ?? r.appliedFilters ?? null;
    let appliedText = '';
    if (applied && typeof applied === 'object') {
      appliedText = Object.entries(applied).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') || '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v ?? '—'))}`).join(' · ');
    } else {
      const bits = [];
      if (payload.titles.length) bits.push(`titles: ${payload.titles.join(', ')}`);
      if (payload.keywords_include.length) bits.push(`+${payload.keywords_include.join(', +')}`);
      if (payload.keywords_exclude.length) bits.push(`-${payload.keywords_exclude.join(', -')}`);
      if (payload.salary_floor !== null && payload.salary_floor !== undefined && payload.salary_floor !== '') bits.push(`floor ${payload.salary_floor}`);
      bits.push(`modes: ${payload.allowed_modes.join('/') || 'none'}`);
      if (dealbreakers.industries.length || dealbreakers.keywords.length) {
        bits.push(`dealbreakers: ${[...dealbreakers.industries, ...dealbreakers.keywords].join(', ')}`);
      }
      bits.push(`${payload.pins.length} pin(s)`);
      appliedText = bits.join(' · ');
    }
    const summary = `Search done: ${fetched} fetched, ${inserted} new, ${updated} updated (${excluded} excluded) | filters: ${appliedText}${perText ? ` — ${perText}` : ''}`;
    toast(summary, 'ok');
    // Scorer summary: scored ok/total + first error if any.
    try {
      const scores = r.scores ?? {};
      const errs = r.errors ?? {};
      const total = Object.keys(scores).length;
      const okCount = Number(r.scored ?? (total - Object.keys(errs).length));
      let firstErr = Object.values(errs)[0];
      if (!firstErr) {
        for (const v of Object.values(scores)) {
          if (v && typeof v === 'object' && v.error) { firstErr = v.error; break; }
        }
      }
      if (total > 0 || firstErr) {
        const errPart = firstErr ? ` — ${String(firstErr).slice(0, 300)}` : '';
        toast(`scored ${okCount}/${total}${errPart}`, firstErr ? 'err' : 'ok');
      }
    } catch { /* toast best-effort */ }
    if (fetched === 0) {
      toast('No jobs fetched — add a source URL in Search setup → Sources, then run again.', 'err');
    }
    if (status) status.textContent = `Done: ${fetched} fetched, ${inserted} new (${excluded} excluded) ✓ ${new Date().toLocaleTimeString()}`;
    await loadJobs();
  } catch (e) {
    toast(`Search failed: ${e.message}`, 'err');
    if (status) status.textContent = 'Search failed';
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initSources() {
  $('#source-add')?.addEventListener('click', () => addSource());
  $('#source-url')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addSource(); }
  });
  $('#search-run')?.addEventListener('click', runSearch);
  $('#score-test')?.addEventListener('click', testScorer);
}

/* ---------- boot ---------- */
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initJobs();
  initSearchSetup();
  initDocs();
  initSources();
  loadJobs();
  loadSearchSetup();
  loadDocs();
  loadSources();
  if ((location.hash || '#jobs') === '#map') initMap();
});
