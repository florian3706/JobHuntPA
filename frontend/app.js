/* JobHuntPA dashboard — vanilla JS, no build step. Talks to backend/app.py. */
'use strict';

const TAG_FIELDS = ['titles', 'keywords_include', 'keywords_exclude', 'dealbreaker_industries', 'dealbreaker_keywords', 'seek_locations', 'commute_from'];

const state = {
  jobs: [],
  details: new Map(), // job id -> full job (with description)
  filter: { q: '', minScore: 0, mode: 'all', maxDist: null, showExcluded: false, showClosed: false, showHidden: false, status: 'all', sort: 'score' },
  selected: new Set(),
  profile: null,
  pins: [],
  map: null,
  pinLayers: new Map(),
  pinLayer: null,
  jobLayer: null,
  jobMarkers: new Map(), // job id -> its map marker
  mapMode: 'jobs', // 'jobs' (read-only) | 'pins' (edit pins)
  pollTimer: null,
  companies: {}, // company_key -> profile
  ws: 1, // current workspace id
  workspaces: [],
  titleSuggestions: [],
  officeSlack: 0, // temporary extra office days allowed on the Jobs tab (never saved)
  dupes: { suggested: [], auto_merged: [] }, // duplicate ads to check
  chat: null, // the open job chat: { jobIds, jobs, threadId, threads, messages, busy }
};

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
// Every request that returns jobs passes the temporary office-days softening.
const slackQuery = () => (state.officeSlack ? `?office_slack=${state.officeSlack}` : '');

async function loadJobs() {
  try {
    state.jobs = await api(`/api/jobs${slackQuery()}`);
    const ids = new Set(state.jobs.map((j) => j.id));
    state.selected.forEach((id) => { if (!ids.has(id)) state.selected.delete(id); });
    state.details.clear();
    $('#jobs-error').hidden = true;
  } catch (e) {
    $('#jobs-error').textContent = `Could not load jobs: ${e.message}`;
    $('#jobs-error').hidden = false;
  }
  renderJobs();
  loadDuplicates();
}

function visibleJobs() {
  const f = state.filter;
  const q = f.q.trim().toLowerCase();
  const list = state.jobs.filter((j) => {
    if (!f.showExcluded && j.excluded_reason) return false;
    if (!f.showClosed && j.closed) return false;
    if (!f.showHidden && j.hidden) return false;
    if (f.status !== 'all' && j.status !== f.status) return false;
    if (f.minScore > 0 && (scoreOf(j) ?? -1) < f.minScore) return false;
    if (f.mode !== 'all' && modeOf(j) !== f.mode) return false;
    if (f.maxDist !== null && (j.distance_km === null || j.distance_km > f.maxDist)) return false;
    if (q) {
      const also = (j.also_on || []).map((c) => sourceLabel(c.source)).join(' ');
      const hay = `${j.company} ${j.title} ${j.location_text} ${j.work_mode} ${j.source} ${sourceLabel(j.source)} ${also}`.toLowerCase();
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
  const base = state.jobs.filter((j) => (state.filter.showExcluded || !j.excluded_reason)
    && (state.filter.showClosed || !j.closed) && (state.filter.showHidden || !j.hidden));
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

function renderSoftenControl() {
  const p = state.profile;
  const base = p && p.allow_hybrid ? p.max_office_days : null;
  const field = $('#soften-field');
  field.hidden = base === null || base === undefined || base >= 5;
  if (field.hidden) { state.officeSlack = 0; return; }
  if (base + state.officeSlack > 5) state.officeSlack = 5 - base;
  const opts = [`<option value="0">Your rule: max ${base}</option>`];
  for (let n = 1; base + n <= 5; n++) opts.push(`<option value="${n}">+${n}: up to ${base + n} day${base + n > 1 ? 's' : ''}</option>`);
  $('#office-slack').innerHTML = opts.join('');
  $('#office-slack').value = String(state.officeSlack);
}

function renderSoftenNote() {
  const note = $('#soften-note');
  const base = state.profile?.max_office_days;
  if (!state.officeSlack || base === null || base === undefined) { note.hidden = true; return; }
  const extra = state.jobs.filter((j) => j.softened_reason && !j.closed && !j.hidden);
  const unscored = extra.filter((j) => (!j.fit || j.fit.status === 'error') && j.detail_status !== 'none').length;
  const max = base + state.officeSlack;
  note.innerHTML = `<span>Temporarily allowing hybrid roles with up to <strong>${max}</strong> office days a week
    (your saved rule: ${base}). <strong>${extra.length}</strong> more job${extra.length === 1 ? '' : 's'} shown${unscored ? `, ${unscored} not scored yet` : ''}.</span>
    ${unscored ? '<button class="btn btn-primary btn-sm" type="button" data-act="score-softened">Score them</button>' : ''}
    <button class="btn btn-ghost btn-sm" type="button" data-act="reset-softened">Back to my rule</button>`;
  note.hidden = false;
  note.querySelector('[data-act="score-softened"]')?.addEventListener('click', () => startRun('/api/score/run', { mode: 'pending', office_slack: state.officeSlack }));
  note.querySelector('[data-act="reset-softened"]').addEventListener('click', () => setOfficeSlack(0));
}

function setOfficeSlack(n) {
  state.officeSlack = n;
  $('#office-slack').value = String(n);
  loadJobs();
}

function renderJobs() {
  renderStatusChips();
  renderSoftenNote();
  const jobs = visibleJobs();
  // Bulk actions only ever apply to jobs on screen: drop selections the filters now hide.
  const shown = new Set(jobs.map((j) => j.id));
  state.selected.forEach((id) => { if (!shown.has(id)) state.selected.delete(id); });
  const list = $('#jobs-list');
  list.innerHTML = '';
  $('#jobs-empty').hidden = jobs.length !== 0;
  const hidden = state.jobs.length - jobs.length;
  $('#jobs-count').textContent = `${jobs.length} shown${hidden ? ` · ${hidden} hidden by filters` : ''}`;
  jobs.forEach((job) => list.appendChild(renderJobCard(job)));
  renderBulkBar();
  drawJobMarkers();
}

function renderJobCard(job) {
  const card = document.createElement('article');
  card.dataset.jobId = job.id;
  card.className = `card job-card${job.excluded_reason ? ' is-excluded' : ''}${state.selected.has(job.id) ? ' is-selected' : ''}`;
  const mode = modeOf(job);
  const dist = job.distance_km === null ? '' : `<span>📍 ${job.distance_km} km</span>`;
  const flags = [
    job.detail_status === 'summary' ? '<span class="badge flag" title="Only the job board listing summary is available">summary only</span>' : '',
    job.detail_status === 'none' ? '<span class="badge flag" title="Description not fetched yet">no description</span>' : '',
    job.closed ? '<span class="badge flag">closed</span>' : '',
    job.hidden ? '<span class="badge flag">hidden</span>' : '',
    job.office_days ? `<span class="badge flag" title="Days per week in the office, from the ad">${job.office_days} day${job.office_days > 1 ? 's' : ''} in office</span>` : '',
    job.office_unknown ? `<span class="badge soft" title="${esc(job.recruiter
      ? "A recruiter's ad naming only a city: the client's office isn't known. Open the job to set it, or mark the location OK or too far."
      : 'The ad names only a city, so the location filter assumes the city centre. Fill missing info looks for the office, or open the job to set it.')}">office unknown${job.recruiter ? ' · recruiter' : ''}</span>` : '',
    job.softened_reason ? `<span class="badge soft" title="${esc(`Your saved rule excludes this: ${job.softened_reason}`)}">shown by temporary +${state.officeSlack} office day${state.officeSlack > 1 ? 's' : ''}</span>` : '',
  ].join('');
  const excl = job.excluded_reason ? `<p class="hint">Excluded: ${esc(job.excluded_reason)}</p>` : '';
  const also = (job.also_on || []).map((c) => `<span class="also-item"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}</a>${c.closed ? ' <span class="muted">(closed)</span>' : ''}
    <button class="link split-btn" type="button" data-split="${c.id}" title="Not the same job: show this ad as its own card again">split</button></span>`).join('');
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
      <span class="muted">${esc(sourceLabel(job.source))}${job.posted_at ? ` · posted ${esc(fmtDate(job.posted_at).split(',')[0])}` : ''}</span>
    </div>
    ${officeHtml(job) ? `<div class="job-office">${officeHtml(job)}</div>` : ''}
    ${also ? `<div class="also-on" title="The same job advertised on other sites, merged into this card"><span class="muted">Also on:</span> ${also}</div>` : ''}
    ${excl}${fitErr}
    <div class="job-actions">
      <select aria-label="Application status">
        ${STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}
      </select>
      <button class="btn btn-ghost btn-sm" type="button" data-act="rescore">${job.fit && job.fit.status === 'ok' ? 'Rescore' : 'Score'}</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="desc">Description</button>
      ${job.source === 'seek' && job.detail_status === 'summary' ? '<button class="btn btn-sm" type="button" data-act="upload-ad" title="Replace the listing summary with the full ad from a page you save on SEEK">Upload full ad</button>' : ''}
      <button class="btn btn-ghost btn-sm" type="button" data-act="hide">${job.hidden ? 'Unhide' : 'Hide'}</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="letter">${job.has_cover_letter ? 'Cover letter ✓' : 'Draft cover letter'}</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="chat" title="Ask questions about this job in a chat on the right">Chat</button>
      <a class="btn btn-ghost btn-sm" href="./job.html?id=${job.id}&ws=${state.ws}" target="_blank" rel="noopener" title="Full page in a new tab: the whole ad, office map, commute and company details">Open ↗</a>
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
  card.querySelector('[data-act="letter"]').addEventListener('click', () => showCoverLetter(job));
  card.querySelector('[data-act="chat"]').addEventListener('click', () => openChat([job.id]));
  card.querySelectorAll('[data-split]').forEach((b) => b.addEventListener('click', () => splitCopy(Number(b.dataset.split))));
  card.querySelector('[data-act="hide"]').addEventListener('click', () => setHidden([job.id], !job.hidden));
  card.querySelector('[data-act="upload-ad"]')?.addEventListener('click', () => showUploadAd(job));
  card.querySelector('[data-act="company"]')?.addEventListener('click', () => showCompany(job));
  card.querySelector('details[data-section="fit"]').addEventListener('toggle', (e) => {
    if (e.target.open) e.target.querySelector('.fit-body').innerHTML = fitHtml(job);
  });
  card.querySelector('details[data-section="gaps"]')?.addEventListener('toggle', (e) => {
    if (e.target.open) e.target.querySelector('.gaps-body').innerHTML = gapsHtml(job);
  });
  return card;
}

async function showDescription(job) {
  try {
    const full = state.details.get(job.id) || await api(`/api/jobs/${job.id}${slackQuery()}`);
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
  const selectedShown = vis.filter((id) => state.selected.has(id)).length;
  ['#bulk-select-all', '#select-all-shown'].forEach((sel) => {
    $(sel).checked = vis.length > 0 && selectedShown === vis.length;
    $(sel).indeterminate = selectedShown > 0 && selectedShown < vis.length;
  });
  $('#select-all-shown').disabled = vis.length === 0;
  $('#fill-info').textContent = n ? `Fill missing info (${n} selected)` : 'Fill missing info';
  $('#bulk-unhide').hidden = !state.jobs.some((j) => j.hidden && state.selected.has(j.id));
  $('#bulk-merge').hidden = n < 2;
  $('#bulk-chat').textContent = n === 1 ? 'Chat about this job' : `Chat about ${n} jobs`;
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

function showUploadAd(job) {
  const mac = /Mac/i.test(navigator.platform);
  openHtmlModal(`Full ad: ${job.title} at ${job.company}`, `
    <div class="upload-ad">
      <p>SEEK only lets the app read the short listing summary. To score this job on the full ad:</p>
      <ol>
        <li><a href="${esc(job.url)}" target="_blank" rel="noopener">Open the ad on SEEK ↗</a> and wait for it to load.</li>
        <li>Save the page: press <kbd>${mac ? 'Cmd' : 'Ctrl'}</kbd>+<kbd>S</kbd> (any "Webpage" format works).</li>
        <li>Choose the saved file here:</li>
      </ol>
      <input id="ad-file" type="file" accept=".html,.htm,.mhtml,.mht,text/html" />
      <p id="ad-status" class="muted" aria-live="polite"></p>
    </div>`);
  $('#ad-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const status = $('#ad-status');
    status.textContent = 'Reading the ad…';
    const fd = new FormData();
    fd.append('file', file, file.name);
    try {
      const updated = await api(`/api/jobs/${job.id}/import-page${slackQuery()}`, { method: 'POST', body: fd });
      Object.assign(job, updated);
      state.details.set(job.id, updated);
      if (updated.excluded_reason) {
        status.innerHTML = `Updated. With the full ad this job is now excluded: ${esc(updated.excluded_reason)}.`;
        renderJobs();
        return;
      }
      status.textContent = 'Updated with the full ad. Rescoring…';
      renderJobs();
      try {
        Object.assign(job, await api(`/api/score/${job.id}${slackQuery()}`, { method: 'POST' }));
        status.textContent = `Updated and rescored: ${job.fit?.score ?? '?'}.`;
      } catch (err) {
        status.textContent = `Updated with the full ad, but rescoring failed (${err.message}). Use Rescore on the card later.`;
      }
      renderJobs();
    } catch (err) {
      status.innerHTML = `<span class="err-text">${esc(err.message)}</span>`;
      e.target.value = '';
    }
  });
}

async function setHidden(ids, hidden) {
  try {
    await api('/api/jobs/hidden', { method: 'PATCH', body: JSON.stringify({ ids, hidden }) });
    const set = new Set(ids);
    state.jobs.forEach((j) => { if (set.has(j.id)) j.hidden = hidden; });
    ids.forEach((id) => state.selected.delete(id));
    renderJobs();
    const n = ids.length;
    toast(hidden ? `Hid ${n} job${n > 1 ? 's' : ''}. Tick "Show hidden" to see them again.` : `Unhid ${n} job${n > 1 ? 's' : ''}.`, 'ok', 3500);
  } catch (e) { toast(`Could not ${hidden ? 'hide' : 'unhide'}: ${e.message}`, 'err'); }
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
    const updated = await api(`/api/score/${job.id}${slackQuery()}`, { method: 'POST' });
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
  $('#filter-show-hidden').addEventListener('change', (e) => { state.filter.showHidden = e.target.checked; renderJobs(); });
  $('#bulk-hide').addEventListener('click', () => {
    const ids = Array.from(state.selected);
    if (ids.length > 20 && !confirm(`Hide ${ids.length} jobs?`)) return;
    setHidden(ids, true);
  });
  $('#bulk-unhide').addEventListener('click', () => setHidden(Array.from(state.selected), false));
  $('#bulk-apply').addEventListener('click', bulkUpdateStatus);
  $('#bulk-clear').addEventListener('click', () => { state.selected.clear(); renderJobs(); });
  ['#bulk-select-all', '#select-all-shown'].forEach((sel) => $(sel).addEventListener('change', (e) => {
    visibleJobs().forEach((j) => (e.target.checked ? state.selected.add(j.id) : state.selected.delete(j.id)));
    renderJobs();
  }));
  $('#search-run').addEventListener('click', () => startRun('/api/search/run', {}));
  $('#score-pending').addEventListener('click', () => startRun('/api/score/run', { mode: 'pending', office_slack: state.officeSlack }));
  $('#office-slack').addEventListener('change', (e) => setOfficeSlack(Number(e.target.value)));
  $('#fill-info').addEventListener('click', () => {
    const ids = Array.from(state.selected);
    startRun('/api/companies/research', ids.length ? { mode: 'missing', job_ids: ids } : { mode: 'missing' });
  });
  $('#bulk-chat').addEventListener('click', () => openChat(Array.from(state.selected)));
  $('#bulk-merge').addEventListener('click', mergeSelected);
  $('#dupes-open').addEventListener('click', showDuplicates);
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
    if (run.summary?.offices?.aborted) toast(`Office search stopped: ${run.summary.offices.aborted}`, 'err', 12000);
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
  const du = run.summary?.duplicates;
  if (du && (du.merged || du.suggested)) bits.push(`${du.merged} duplicate ad${du.merged === 1 ? '' : 's'} merged${du.suggested ? `, ${du.suggested} possible duplicate${du.suggested === 1 ? '' : 's'} to check` : ''}`);
  const rs = run.summary?.research;
  if (rs) {
    if (rs.aborted) bits.push('research stopped (see error)');
    else bits.push(`researched ${rs.researched}/${rs.requested} companies${rs.errors ? `, ${rs.errors} failed` : ''}`);
  }
  const of = run.summary?.offices;
  if (of && !of.aborted) {
    const found = of.from_ads + of.from_companies;
    bits.push(`offices found for ${found} job${found === 1 ? '' : 's'}${of.unknown ? `, ${of.unknown} still unknown${of.recruiters ? ` (${of.recruiters} recruiter ads: your call)` : ''}` : ''}`);
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

function showCompany(job) {
  const p = state.companies[job.company_key];
  openHtmlModal(p?.official_name || job.company, companyProfileHtml(p, job.company));
  $('#company-research').addEventListener('click', () => {
    $('#modal').hidden = true;
    startRun('/api/companies/research', { name: job.company });
  });
}

function currentWorkspace() { return state.ws; }

/* ================= COVER LETTERS ================= */
async function showCoverLetter(job) {
  let letter = null;
  try { letter = await api(`/api/jobs/${job.id}/cover-letter`); } catch { letter = null; }
  openHtmlModal(`Cover letter: ${job.title} at ${job.company}`, `
    <div class="letter-editor">
      <label class="field"><span class="field-label">Extra instructions for the draft (optional)</span>
        <textarea id="cl-instructions" rows="2" placeholder="e.g. Mention I can start in two weeks. Keep it under 300 words.">${esc(letter?.instructions || '')}</textarea></label>
      <div class="toolbar-row">
        <button id="cl-draft" class="btn btn-primary btn-sm" type="button">${letter ? 'Redraft' : 'Draft cover letter'}</button>
        <span id="cl-status" class="muted" aria-live="polite"></span>
      </div>
      <textarea id="cl-text" rows="16" placeholder="Your draft appears here. You can edit it before saving.">${esc(letter?.text || '')}</textarea>
      <div class="toolbar-row">
        <button id="cl-save" class="btn btn-sm" type="button">Save edits</button>
        <button id="cl-copy" class="btn btn-ghost btn-sm" type="button">Copy</button>
        <a id="cl-docx" class="btn btn-ghost btn-sm" href="/api/jobs/${job.id}/cover-letter.docx?ws=${state.ws}" ${letter ? '' : 'hidden'}>Download .docx</a>
        <span id="cl-meta" class="muted"></span>
      </div>
      <p class="hint">Drafts use only facts from your documents, but always check them before sending.
        Upload earlier cover letters as "Cover letter" documents and drafts will follow your style.</p>
    </div>`);
  const meta = (l) => {
    if (!l) return '';
    const effort = l.reasoning_effort ? `, reasoning ${l.reasoning_effort}` : '';
    return `${l.edited ? 'Edited' : 'Drafted'} ${fmtDate(l.updated_at)} · ${l.model || ''}${effort}`;
  };
  $('#cl-meta').textContent = meta(letter);
  $('#cl-draft').addEventListener('click', async () => {
    const btn = $('#cl-draft');
    if ($('#cl-text').value.trim() && !confirm('Replace the current letter with a new draft?')) return;
    btn.disabled = true;
    $('#cl-status').textContent = 'Drafting… this can take a minute.';
    try {
      letter = await api(`/api/jobs/${job.id}/cover-letter`, { method: 'POST', body: JSON.stringify({ instructions: $('#cl-instructions').value }) });
      $('#cl-text').value = letter.text;
      $('#cl-status').textContent = '';
      $('#cl-meta').textContent = meta(letter);
      $('#cl-docx').hidden = false;
      btn.textContent = 'Redraft';
      job.has_cover_letter = true;
      renderJobs();
    } catch (e) { $('#cl-status').textContent = `Failed: ${e.message}`; }
    btn.disabled = false;
  });
  $('#cl-save').addEventListener('click', async () => {
    try {
      letter = await api(`/api/jobs/${job.id}/cover-letter`, { method: 'PUT', body: JSON.stringify({ text: $('#cl-text').value }) });
      $('#cl-meta').textContent = meta(letter);
      $('#cl-docx').hidden = false;
      job.has_cover_letter = true;
      toast('Cover letter saved', 'ok', 2000);
    } catch (e) { toast(`Save failed: ${e.message}`, 'err'); }
  });
  $('#cl-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#cl-text').value); toast('Copied', 'ok', 1500); }
    catch { $('#cl-text').select(); toast('Press Ctrl+C to copy', '', 2500); }
  });
}

/* ================= DUPLICATE ADS ================= */
async function loadDuplicates() {
  try { state.dupes = await api('/api/duplicates'); } catch { state.dupes = { suggested: [], auto_merged: [] }; }
  renderDupeNote();
}

function renderDupeNote() {
  const n = state.dupes.suggested.length;
  const m = state.dupes.auto_merged.length;
  const note = $('#dupe-note');
  note.hidden = !n && !m;
  $('#dupes-open').textContent = n ? `Duplicates (${n} to check)` : 'Duplicates';
  if (note.hidden) return;
  const parts = [];
  if (n) parts.push(`<strong>${n}</strong> possible duplicate ad${n === 1 ? '' : 's'} to check`);
  if (m) parts.push(`${m} ad${m === 1 ? ' was' : 's were'} merged automatically`);
  note.innerHTML = `<span>The same job on several sites is shown as one card. ${parts.join(' · ')}.</span>
    <button class="btn btn-sm" type="button" data-act="review">Review</button>`;
  note.querySelector('[data-act="review"]').addEventListener('click', showDuplicates);
}

function dupeSide(j) {
  const flags = [j.closed ? 'closed' : '', j.hidden ? 'hidden' : '', j.excluded_reason ? 'excluded by your filters' : '',
    j.detail_status === 'summary' ? 'listing summary only' : '', j.detail_status === 'none' ? 'no description' : ''].filter(Boolean);
  return `<div class="dupe-side">
    <div class="muted">${esc(sourceLabel(j.source))}${flags.length ? ` · ${esc(flags.join(' · '))}` : ''}</div>
    <a href="${esc(j.url)}" target="_blank" rel="noopener"><strong>${esc(j.title || '—')}</strong></a>
    <div>${esc(j.company || '—')}</div>
    <div class="muted">${esc(j.location_text || 'location not stated')}${j.salary_text ? ` · ${esc(j.salary_text)}` : ''}</div>
    <div class="muted">${esc(STATUS_LABEL[j.status] || j.status)}${j.score !== null && j.score !== undefined ? ` · score ${j.score}` : ''} · first seen ${esc(fmtDate(j.first_seen).split(',')[0])}</div>
    <details><summary>Ad text</summary><div class="dupe-desc">${esc(j.description || '(no description stored)')}</div></details>
  </div>`;
}

function dupePairHtml(p, kind) {
  const buttons = kind === 'suggested'
    ? `<button class="btn btn-primary btn-sm" type="button" data-d="merge">Same job: merge</button>
       <button class="btn btn-sm" type="button" data-d="distinct">Different jobs</button>`
    : `<button class="btn btn-sm" type="button" data-d="confirm">Correct</button>
       <button class="btn btn-sm" type="button" data-d="distinct">Not the same job: split</button>`;
  return `<div class="dupe-pair" data-a="${p.a.id}" data-b="${p.b.id}">
    <div class="dupe-why"><span class="badge ${p.score >= 90 ? 'fit-strong' : p.score >= 75 ? 'fit-good' : 'fit-stretch'}">${p.score}% likely</span>
      ${p.reasons.map((r) => `<span class="badge flag">${esc(r)}</span>`).join('')}</div>
    <div class="dupe-sides">${dupeSide(p.a)}${dupeSide(p.b)}</div>
    <div class="toolbar-row">${buttons}</div>
  </div>`;
}

function renderDupeLists() {
  const { suggested, auto_merged: auto } = state.dupes;
  $('#dupes-suggested').innerHTML = suggested.length ? suggested.map((p) => dupePairHtml(p, 'suggested')).join('')
    : '<p class="muted">Nothing to check.</p>';
  $('#dupes-auto').innerHTML = auto.length ? auto.map((p) => dupePairHtml(p, 'auto')).join('')
    : '<p class="muted">No automatic merges waiting for a check.</p>';
  $('#dupes-n-suggested').textContent = suggested.length;
  $('#dupes-n-auto').textContent = auto.length;
  $('#dupes-confirm-all').hidden = auto.length < 2;
  $$('.dupe-pair [data-d]').forEach((btn) => btn.addEventListener('click', async () => {
    const pair = btn.closest('.dupe-pair');
    pair.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      await decideDuplicate(Number(pair.dataset.a), Number(pair.dataset.b), btn.dataset.d);
      await loadDuplicates();
      renderDupeLists();
      loadJobs();
    } catch (e) {
      toast(`Could not save that: ${e.message}`, 'err');
      pair.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
  }));
}

function decideDuplicate(a, b, decision) {
  return api('/api/duplicates/decide', { method: 'POST', body: JSON.stringify({ a, b, decision }) });
}

function showDuplicates() {
  openHtmlModal('Duplicate job ads', `
    <div class="dupes">
      <p class="hint">When the same job is advertised on several sites, the app shows it as one card with the other sites under
        "Also on". It merges ads it's sure about (same employer, title and place, and matching ad text or a very specific title)
        and asks you about the rest. Your status, cover letter and score carry over to the merged card.</p>
      <div class="toolbar-row">
        <button id="dupes-scan" class="btn btn-sm" type="button">Check for duplicates now</button>
        <span id="dupes-status" class="muted" aria-live="polite"></span>
      </div>
      <h4>Possible duplicates to check (<span id="dupes-n-suggested">0</span>)</h4>
      <div id="dupes-suggested"></div>
      <h4>Merged automatically (<span id="dupes-n-auto">0</span>)</h4>
      <p class="hint">Only merges of jobs in your list are shown. Split any that aren't the same job.</p>
      <button id="dupes-confirm-all" class="btn btn-ghost btn-sm" type="button" hidden>All of these are correct</button>
      <div id="dupes-auto"></div>
    </div>`);
  renderDupeLists();
  $('#dupes-scan').addEventListener('click', async () => {
    const btn = $('#dupes-scan');
    btn.disabled = true;
    $('#dupes-status').textContent = 'Comparing ads…';
    try {
      const r = await api('/api/duplicates/scan', { method: 'POST' });
      $('#dupes-status').textContent = `Merged ${r.merged} ad${r.merged === 1 ? '' : 's'}; ${r.suggested} possible duplicate${r.suggested === 1 ? '' : 's'} to check.`;
      await loadDuplicates();
      renderDupeLists();
      loadJobs();
    } catch (e) { $('#dupes-status').textContent = `Failed: ${e.message}`; }
    btn.disabled = false;
  });
  $('#dupes-confirm-all').addEventListener('click', async () => {
    try {
      for (const p of state.dupes.auto_merged) await decideDuplicate(p.a.id, p.b.id, 'confirm');
    } catch (e) { toast(`Could not save that: ${e.message}`, 'err'); }
    await loadDuplicates();
    renderDupeLists();
  });
}

async function mergeSelected() {
  const ids = Array.from(state.selected);
  if (ids.length < 2) return;
  if (!confirm(`Merge these ${ids.length} ads into one job? Use this when they're the same job advertised on different sites.`)) return;
  try {
    await api('/api/jobs/merge', { method: 'POST', body: JSON.stringify({ ids }) });
    state.selected.clear();
    toast(`Merged ${ids.length} ads into one card. Use "split" under "Also on" to undo.`, 'ok');
    loadJobs();
  } catch (e) { toast(`Merge failed: ${e.message}`, 'err'); }
}

async function splitCopy(id) {
  try {
    await api(`/api/jobs/${id}/unmerge`, { method: 'POST' });
    toast('Split out: it is its own card again and won\'t be merged with that job again.', 'ok');
    loadJobs();
  } catch (e) { toast(`Split failed: ${e.message}`, 'err'); }
}

/* ================= JOB CHAT ================= */
const CHAT_MAX_JOBS = 12;
const CHAT_STARTERS = {
  one: ['What would I actually be doing day to day?', 'Honestly, how well do I fit? What are my biggest gaps?',
    'How should I address my gaps in an interview?', 'What should I ask them in an interview?'],
  many: ['Compare these jobs for me.', 'Which should I apply to first, and why?', 'Which one fits my experience best?'],
};
let chatToken = 0; // bumps when the panel switches jobs, so late answers for old jobs are dropped

function jobLabel(j) { return { id: j.id, title: j.title, company: j.company, url: j.url }; }

async function openChat(ids) {
  const jobIds = [...new Set(ids)].sort((a, b) => a - b);
  if (!jobIds.length) return;
  if (jobIds.length > CHAT_MAX_JOBS) { toast(`Chat about up to ${CHAT_MAX_JOBS} jobs at a time; select fewer.`, 'err'); return; }
  const token = ++chatToken;
  state.chat = {
    jobIds, threadId: null, threads: [], messages: [], busy: false,
    jobs: jobIds.map((id) => state.jobs.find((j) => j.id === id)).filter(Boolean).map(jobLabel),
  };
  $('#chat-panel').hidden = false;
  document.body.classList.add('chat-open');
  setChatStatus('');
  renderChat();
  $('#chat-input').focus();
  try {
    const threads = await api(`/api/chats?job_ids=${jobIds.join(',')}`);
    if (token !== chatToken) return;
    state.chat.threads = threads;
    if (threads.length) await loadChatThread(threads[0].id, token);
    else renderChat();
  } catch (e) { if (token === chatToken) setChatStatus(`Could not load earlier chats: ${e.message}`, true); }
}

async function loadChatThread(id, token = chatToken) {
  const t = await api(`/api/chats/${id}`);
  if (token !== chatToken) return;
  applyThread(t);
  renderChat();
}

function applyThread(t) {
  const c = state.chat;
  c.threadId = t.id;
  c.messages = t.messages;
  if (t.jobs.length) c.jobs = t.jobs;
  const summary = { id: t.id, title: t.title, updated_at: t.updated_at };
  c.threads = [summary, ...c.threads.filter((x) => x.id !== t.id)];
}

function closeChat() {
  chatToken++;
  state.chat = null;
  $('#chat-panel').hidden = true;
  document.body.classList.remove('chat-open');
}

function setChatStatus(text, isError = false) {
  const el = $('#chat-status');
  el.textContent = text;
  el.classList.toggle('err-text', isError);
}

function renderChat() {
  const c = state.chat;
  if (!c) return;
  const n = c.jobIds.length;
  $('#chat-title').textContent = n === 1 ? 'Ask about this job' : `Ask about ${n} jobs`;
  $('#chat-input').placeholder = `Ask anything about ${n === 1 ? 'this job' : 'these jobs'}… (Enter to send, Shift+Enter for a new line)`;
  $('#chat-jobs').innerHTML = c.jobs.map((j, i) => `<button class="chat-job" type="button" data-id="${j.id}" title="Show this job's card">
    ${n > 1 ? `<strong>${i + 1}</strong> ` : ''}${esc(j.title || '—')} · ${esc(j.company || '—')}</button>`).join('');
  $$('#chat-jobs .chat-job').forEach((b) => b.addEventListener('click', () => showJobCard(Number(b.dataset.id))));
  const sel = $('#chat-threads');
  const opts = c.threads.map((t) => `<option value="${t.id}" ${t.id === c.threadId ? 'selected' : ''}>${esc(t.title || 'Chat')} · ${esc(fmtDate(t.updated_at).split(',')[0])}</option>`);
  if (!c.threadId) opts.unshift('<option value="" selected>New chat</option>');
  sel.innerHTML = opts.join('');
  sel.disabled = c.busy || c.threads.length === 0;
  $('#chat-delete').hidden = !c.threadId;
  $('#chat-new').disabled = c.busy || !c.threadId;
  $('#chat-send').disabled = c.busy;
  renderChatMessages();
}

function renderChatMessages() {
  const c = state.chat;
  const box = $('#chat-messages');
  if (!c.messages.length && !c.busy) {
    const starters = CHAT_STARTERS[c.jobIds.length === 1 ? 'one' : 'many'];
    box.innerHTML = `<p class="muted">The model sees ${c.jobIds.length === 1 ? 'the ad' : 'each ad'}, your documents, the fit assessment,
      the company info and any cover-letter draft. Try:</p>
      <div class="chat-starters">${starters.map((q) => `<button class="btn btn-ghost btn-sm chat-starter" type="button">${esc(q)}</button>`).join('')}</div>`;
    box.querySelectorAll('.chat-starter').forEach((b) => b.addEventListener('click', () => sendChat(b.textContent)));
    return;
  }
  box.innerHTML = c.messages.map((m) => {
    if (m.role === 'user') return `<div class="chat-msg user">${esc(m.content)}${m.web_search ? '<div class="chat-meta">🌐 with web search</div>' : ''}</div>`;
    const links = m.sources || [];
    const sources = links.length
      ? `<div class="chat-sources"><span class="muted">${links.some((x) => x.cited) ? 'Sources:' : 'Pages the web search found:'}</span>
          ${links.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title || x.url)}</a>`).join(' · ')}</div>` : '';
    return `<div class="chat-msg assistant">${mdToHtml(m.content)}${sources}
      <div class="chat-meta">${esc(m.model || '')}${m.created_at ? ` · ${esc(fmtDate(m.created_at))}` : ''}
        <button class="link chat-copy" type="button" data-id="${m.id}">Copy</button></div></div>`;
  }).join('') + (c.busy ? '<div class="chat-msg assistant chat-thinking" aria-label="Thinking"><span></span><span></span><span></span></div>' : '');
  box.querySelectorAll('.chat-copy').forEach((b) => b.addEventListener('click', async () => {
    const msg = c.messages.find((m) => String(m.id) === b.dataset.id);
    try { await navigator.clipboard.writeText(msg.content); toast('Copied', 'ok', 1500); } catch { toast('Could not copy', 'err', 2500); }
  }));
  box.scrollTop = box.scrollHeight;
}

async function sendChat(text) {
  const c = state.chat;
  text = String(text || '').trim();
  if (!c || !text || c.busy) return;
  const web = $('#chat-web').checked;
  const token = chatToken;
  c.busy = true;
  c.messages.push({ role: 'user', content: text, web_search: web });
  $('#chat-input').value = '';
  setChatStatus(web ? 'Searching the web… this can take a minute or two.' : 'Thinking…');
  renderChat();
  try {
    const body = JSON.stringify(c.threadId ? { message: text, web_search: web } : { job_ids: c.jobIds, message: text, web_search: web });
    const t = await api(c.threadId ? `/api/chats/${c.threadId}/messages` : '/api/chats', { method: 'POST', body });
    if (token !== chatToken) return;
    applyThread(t);
    setChatStatus('');
  } catch (e) {
    if (token !== chatToken) return;
    c.messages.pop();
    if (!$('#chat-input').value) $('#chat-input').value = text;
    setChatStatus(`Failed: ${e.message}`, true);
  }
  c.busy = false;
  renderChat();
}

function showJobCard(id) {
  const card = document.querySelector(`.job-card[data-job-id="${id}"]`);
  if (!card) { toast('That job is hidden by the current filters.', '', 2500); return; }
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.remove('flash');
  void card.offsetWidth;
  card.classList.add('flash');
}

function initChat() {
  $('#chat-close').addEventListener('click', closeChat);
  $('#chat-form').addEventListener('submit', (e) => { e.preventDefault(); sendChat($('#chat-input').value); });
  $('#chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat($('#chat-input').value); }
  });
  $('#chat-new').addEventListener('click', () => {
    const c = state.chat;
    if (!c || c.busy) return;
    c.threadId = null;
    c.messages = [];
    setChatStatus('');
    renderChat();
    $('#chat-input').focus();
  });
  $('#chat-threads').addEventListener('change', async (e) => {
    if (!e.target.value) return;
    try { await loadChatThread(Number(e.target.value)); } catch (err) { setChatStatus(`Could not load that chat: ${err.message}`, true); }
  });
  $('#chat-delete').addEventListener('click', async () => {
    const c = state.chat;
    if (!c?.threadId || !confirm('Delete this conversation?')) return;
    try {
      await api(`/api/chats/${c.threadId}`, { method: 'DELETE' });
      c.threads = c.threads.filter((t) => t.id !== c.threadId);
      c.threadId = null;
      c.messages = [];
      if (c.threads.length) await loadChatThread(c.threads[0].id); else renderChat();
    } catch (e) { setChatStatus(`Delete failed: ${e.message}`, true); }
  });
}

/* ================= JOB TITLE SUGGESTIONS ================= */
const FIT_LABEL = { strong: 'Strong fit', good: 'Good fit', stretch: 'Stretch' };

function renderTitleSuggestions(titles) {
  $('#titles-list').innerHTML = titles.map((t, i) => `
    <label class="title-row">
      <input type="checkbox" data-i="${i}" ${t.in_search ? 'disabled' : (t.fit !== 'stretch' ? 'checked' : '')} />
      <span class="title-name">${esc(t.title)}</span>
      <span class="badge fit-${t.fit}">${FIT_LABEL[t.fit] || t.fit}</span>
      ${t.in_search ? '<span class="badge flag">already in this search</span>' : ''}
      <span class="muted title-reason">${esc(t.reason)}</span>
    </label>`).join('');
  $('#titles-actions').hidden = !titles.length;
}

function pickedTitles() {
  return $$('#titles-list input[type="checkbox"]:checked').map((cb) => state.titleSuggestions[Number(cb.dataset.i)].title);
}

async function saveTitles(titles, replace) {
  const profile = await api('/api/profile');
  const merged = replace ? titles : [...profile.titles, ...titles.filter((t) => !profile.titles.some((x) => x.toLowerCase() === t.toLowerCase()))];
  await api('/api/profile', { method: 'PUT', body: JSON.stringify({ ...profile, titles: merged }) });
  await api('/api/jobs/refilter', { method: 'POST' });
  await loadSetup();
  loadJobs();
  return merged;
}

function initTitleSuggestions() {
  $('#suggest-titles').addEventListener('click', async () => {
    const btn = $('#suggest-titles');
    btn.disabled = true;
    $('#titles-status').textContent = 'Reading your documents… (about 20 seconds)';
    try {
      const r = await api('/api/documents/suggest-titles', { method: 'POST' });
      state.titleSuggestions = r.titles;
      renderTitleSuggestions(r.titles);
      $('#titles-status').textContent = `${r.titles.length} suggestions. Titles are also used as SEEK search terms.`;
    } catch (e) { $('#titles-status').textContent = `Failed: ${e.message}`; }
    btn.disabled = false;
  });
  $('#titles-merge').addEventListener('click', async () => {
    const picked = pickedTitles();
    if (!picked.length) { toast('Tick at least one title.', 'err'); return; }
    try {
      const all = await saveTitles(picked, false);
      toast(`Added ${picked.length} title(s); this search now has ${all.length}.`, 'ok');
      state.titleSuggestions = state.titleSuggestions.map((t) => (picked.includes(t.title) ? { ...t, in_search: true } : t));
      renderTitleSuggestions(state.titleSuggestions);
    } catch (e) { toast(`Could not add titles: ${e.message}`, 'err'); }
  });
  $('#titles-new').addEventListener('click', async () => {
    const picked = pickedTitles();
    if (!picked.length) { toast('Tick at least one title.', 'err'); return; }
    const data = await workspaceForm('New search with these titles', { create: true, name: `${picked[0]} search` });
    if (!data.name) return;
    try {
      const ws = await api('/api/workspaces', { method: 'POST', body: JSON.stringify(data) });
      await switchWorkspace(ws.id);
      await saveTitles(picked, true);
      toast(`Created "${ws.name}" with ${picked.length} title(s).`, 'ok');
    } catch (e) { toast(`Could not create the search: ${e.message}`, 'err'); }
  });
}

/* ================= LLM SETTINGS ================= */
const LEVEL_LABEL = { none: 'None', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high' };
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
  btn.disabled = true;
  $('#levels-status').textContent = 'Detecting (a few tiny test requests)…';
  try { renderLlmSettings(await api('/api/llm/detect-levels', { method: 'POST' })); }
  catch (e) { $('#levels-status').textContent = `Detection failed: ${e.message}`; }
  btn.disabled = false;
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
    ['titles', 'keywords_include', 'keywords_exclude', 'seek_locations', 'commute_from'].forEach((f) => setTags(f, profile[f]));
    $('#commute-arrive').value = profile.commute_arrive_by || '09:00';
    api('/api/commute/status').then((k) => {
      $('#commute-keys').innerHTML = `Public transport: ${k.transit ? '<span class="ok-text">Transport for NSW ✓</span>'
        : 'needs a free Transport for NSW API key (<code>TFNSW_API_KEY</code>; see the README)'}.
        Driving: ${k.traffic ? '<span class="ok-text">TomTom, with peak traffic ✓</span>'
        : 'without traffic (a free TomTom key, <code>TOMTOM_API_KEY</code>, adds peak-hour traffic)'}.`;
    }).catch(() => {});
    $('#commute-leave').value = profile.commute_leave_at || '17:00';
    setTags('dealbreaker_industries', dealbreakers.industries);
    setTags('dealbreaker_keywords', dealbreakers.keywords);
    $('#salary-floor').value = profile.salary_floor ?? '';
    $('#remote-aus').checked = profile.remote_aus_ok;
    $('#remote-global').checked = profile.remote_global_ok;
    $('#allow-hybrid').checked = profile.allow_hybrid;
    $('#max-office-days').value = profile.max_office_days ?? '';
    $('#allow-onsite').checked = profile.allow_onsite;
    renderSoftenControl();
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
    max_office_days: $('#max-office-days').value === '' ? null : Number($('#max-office-days').value),
    allow_onsite: $('#allow-onsite').checked,
    seek_enabled: $('#seek-enabled').checked,
    seek_locations: getTags('seek_locations'),
    commute_from: getTags('commute_from'),
    commute_arrive_by: $('#commute-arrive').value || '09:00',
    commute_leave_at: $('#commute-leave').value || '17:00',
  };
  const dealbreakers = { industries: getTags('dealbreaker_industries'), keywords: getTags('dealbreaker_keywords') };
  try {
    await Promise.all([
      api('/api/profile', { method: 'PUT', body: JSON.stringify(profile) }),
      api('/api/dealbreakers', { method: 'PUT', body: JSON.stringify(dealbreakers) }),
    ]);
    const { changed } = await api('/api/jobs/refilter', { method: 'POST' });
    status.textContent = `Saved ${new Date().toLocaleTimeString()}${changed ? ` · filters re-applied to ${changed} job(s)` : ''}`;
    const slack = state.officeSlack;
    // A new saved rule starts from scratch: the softening was relative to the old one.
    if (state.profile?.max_office_days !== profile.max_office_days || state.profile?.allow_hybrid !== profile.allow_hybrid) state.officeSlack = 0;
    state.profile = { ...state.profile, ...profile };
    renderSoftenControl();
    if (changed || slack !== state.officeSlack) loadJobs();
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
  ['#remote-aus', '#remote-global', '#allow-hybrid', '#allow-onsite', '#seek-enabled', '#max-office-days', '#commute-arrive', '#commute-leave'].forEach((s) => $(s).addEventListener('change', saveSetup));
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
    const failing = s.configured && s.scored === 0 && s.errors > 0;
    badge.className = `api-status ${!s.configured || failing ? 'is-fail' : s.scored ? 'is-ok' : 'is-unknown'}`;
    badge.textContent = !s.configured ? '● LLM not configured'
      : failing ? '● scorer: failing (Search setup → Test connection)'
        : `● ${s.model}${s.scored ? '' : ' (not verified yet)'}`;
    badge.title = `${s.scored} scored, ${s.errors} failed`;
    $('#scorer-info').innerHTML = `
      ${s.problem ? `<span class="err-text">${esc(s.problem)}</span><br>` : ''}
      Model <strong>${esc(s.model || '(LLM_MODEL not set)')}</strong> at ${esc(s.base_url || '(LLM_BASE_URL not set)')}${s.reasoning_effort ? ` · reasoning effort ${esc(s.reasoning_effort)}` : ''}<br>
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
// Two modes: 'jobs' (read-only: jobs + pins with radii, jobs in view listed below)
// and 'pins' (click to drop pins, drag to move, pins listed below).
const PIN_HUE = { home: 'special', hybrid: 'info', onsite: 'warn' }; // theme slots
const pinColour = (kind) => themeColor(PIN_HUE[kind] || 'accent');
const MAP_LIST_MAX = 200;

function storedMapMode() {
  try { return localStorage.getItem('jobhunt.mapMode') === 'pins' ? 'pins' : 'jobs'; } catch { return 'jobs'; }
}

function initMap() {
  if (state.map) return;
  if (typeof L === 'undefined') { $('#pins-error').textContent = 'Map library failed to load (offline?).'; $('#pins-error').hidden = false; return; }
  state.map = L.map('map').setView([-33.87, 151.21], 9);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);
  state.pinLayer = L.layerGroup().addTo(state.map);
  state.jobLayer = L.layerGroup().addTo(state.map);
  state.map.on('click', (e) => { if (state.mapMode === 'pins') addPinAt(e); });
  state.map.on('moveend', () => { if (state.mapMode === 'jobs') renderMapJobs(); else if ($('#pins-in-view').checked) renderPinsList(); });
  $('#pin-radius').addEventListener('input', (e) => { $('#pin-radius-output').textContent = `${e.target.value} km`; });
  $('#pins-in-view').addEventListener('change', renderPinsList);
  $('#map-mode-jobs').addEventListener('click', () => setMapMode('jobs'));
  $('#map-mode-pins').addEventListener('click', () => setMapMode('pins'));
  setMapMode(storedMapMode(), false);
  loadPins();
}

function setMapMode(mode, redraw = true) {
  state.mapMode = mode;
  try { localStorage.setItem('jobhunt.mapMode', mode); } catch { /* private mode */ }
  const edit = mode === 'pins';
  [['#map-mode-jobs', !edit], ['#map-mode-pins', edit]].forEach(([sel, on]) => {
    $(sel).classList.toggle('active', on);
    $(sel).setAttribute('aria-pressed', String(on));
  });
  $('#map-hint-jobs').hidden = edit;
  $('#map-hint-pins').hidden = !edit;
  $('#pin-controls').hidden = !edit;
  $('#pins-list').hidden = !edit;
  $('#map-jobs').hidden = edit;
  $('#map').classList.toggle('editing', edit);
  if (!redraw) return;
  drawPins();
  if (edit) renderPinsList(); else renderMapJobs();
}

/* Jobs on the map: the ones the Jobs tab's filters show, grouped by place
   (many ads that only name a city share one point). */
function mapJobs() { return visibleJobs().filter((j) => j.lat !== null && j.lng !== null); }

function jobLinkHtml(j) {
  return `<a href="./job.html?id=${j.id}&ws=${state.ws}" target="_blank" rel="noopener" title="Full page in a new tab">Open ↗</a>`;
}

function drawJobMarkers() {
  if (!state.map || !state.jobLayer) return;
  state.jobLayer.clearLayers();
  state.jobMarkers = new Map();
  const groups = new Map();
  mapJobs().forEach((j) => {
    const key = `${j.lat.toFixed(4)},${j.lng.toFixed(4)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(j);
  });
  groups.forEach((jobs, key) => {
    const kept = jobs.some((j) => !j.excluded_reason);
    const n = jobs.length;
    const marker = L.circleMarker([jobs[0].lat, jobs[0].lng], {
      radius: Math.min(16, 5 + 2.5 * Math.log2(n)), weight: 1, color: themeColor(kept ? 'ok' : 'quiet'), fillOpacity: 0.75,
    });
    const rows = jobs.slice(0, 40).map((j) => `<li><b>${esc(j.title)}</b> · ${esc(j.company || '')}
      ${scoreOf(j) !== null ? ` · ${scoreOf(j)}` : ''}${j.excluded_reason ? ' <i>(excluded)</i>' : ''}<br>${jobLinkHtml(j)}
      · <a href="${esc(j.url)}" target="_blank" rel="noopener">ad</a></li>`).join('');
    marker.bindPopup(`<div class="map-popup"><div class="muted">${esc(jobs[0].location_text || '')}${n > 1 ? ` · ${n} jobs` : ''}</div>
      <ul>${rows}</ul>${n > 40 ? `<div class="muted">…and ${n - 40} more: see the list under the map.</div>` : ''}</div>`, { maxWidth: 320, maxHeight: 300 });
    if (n > 1) marker.bindTooltip(String(n), { permanent: true, direction: 'center', className: 'map-count' });
    marker.addTo(state.jobLayer);
    jobs.forEach((j) => state.jobMarkers.set(j.id, marker));
  });
  renderMapLegend();
  renderMapJobs();
}

function renderMapLegend() {
  const all = visibleJobs().length;
  const onMap = mapJobs().length;
  $('#map-legend').innerHTML = `<span class="dot" style="--c:var(--ok)"></span> job you'd consider
    <span class="dot" style="--c:var(--quiet)"></span> excluded
    ${Object.entries(PIN_HUE).map(([k, hue]) => `<span class="ring" style="--c:var(--${hue})"></span> ${k} pin`).join(' ')}
    · ${onMap} of ${all} filtered job${all === 1 ? '' : 's'} on the map${all > onMap ? ` (${all - onMap} remote or without a location)` : ''}`;
}

function renderMapJobs() {
  const box = $('#map-jobs');
  if (!state.map || state.mapMode !== 'jobs') return;
  const bounds = state.map.getBounds();
  const inView = mapJobs().filter((j) => bounds.contains([j.lat, j.lng]));
  if (!inView.length) {
    box.innerHTML = '<div class="empty">No jobs in this part of the map. Zoom out, or change the Jobs tab filters.</div>';
    return;
  }
  const shown = inView.slice(0, MAP_LIST_MAX);
  box.innerHTML = `<div class="map-jobs-head"><strong>${inView.length} job${inView.length === 1 ? '' : 's'} in view</strong>
      <span class="muted">sorted like the Jobs tab${inView.length > MAP_LIST_MAX ? `; first ${MAP_LIST_MAX} shown, zoom in for the rest` : ''}</span></div>
    ${shown.map((j) => `<div class="map-job-row" data-id="${j.id}">
      ${scoreBadge(j)}
      <div class="map-job-main">
        <div><a class="map-job-title" href="./job.html?id=${j.id}&ws=${state.ws}" target="_blank" rel="noopener" title="Open the full page in a new tab">${esc(j.title || '—')}</a>
          <span class="muted">· ${esc(j.company || '—')}</span></div>
        <div class="muted">${esc(j.location_text || '')}${j.office_unknown ? ' · office unknown' : ''}${j.distance_km !== null ? ` · 📍 ${j.distance_km} km` : ''}
          · <span class="badge ${modeOf(j)}">${modeOf(j)}</span> · ${esc(STATUS_LABEL[j.status] || j.status)}${j.excluded_reason ? ` · excluded: ${esc(j.excluded_reason)}` : ''}</div>
      </div>
      <button class="btn btn-ghost btn-sm" type="button" data-act="show" title="Show it on the map">Show</button>
      <a class="btn btn-ghost btn-sm" href="./job.html?id=${j.id}&ws=${state.ws}" target="_blank" rel="noopener">Open ↗</a>
    </div>`).join('')}`;
  box.querySelectorAll('[data-act="show"]').forEach((b) => b.addEventListener('click', () => {
    const marker = state.jobMarkers.get(Number(b.closest('.map-job-row').dataset.id));
    if (!marker) return;
    // The list sits below the map: bring the map back on screen first.
    $('#map').scrollIntoView({ block: 'center' });
    state.map.panTo(marker.getLatLng());
    marker.openPopup();
  }));
}

/* Pins: draggable markers in edit mode; small dots with their radius otherwise. */
function drawPin(p) {
  const edit = state.mapMode === 'pins';
  const colour = pinColour(p.kind);
  const circle = L.circle([p.lat, p.lng], { radius: p.radius_km * 1000, weight: 1, color: colour, fillOpacity: edit ? 0.12 : 0.06, interactive: false });
  const marker = edit
    ? L.marker([p.lat, p.lng], { draggable: true })
    : L.circleMarker([p.lat, p.lng], { radius: 4, weight: 2, color: colour, fillOpacity: 1 });
  marker.bindPopup(`<b>${esc(p.label)}</b><br>${esc(p.kind)} pin · ${p.radius_km} km`);
  if (edit) {
    marker.on('dragend', async () => {
      const ll = marker.getLatLng();
      circle.setLatLng(ll);
      await savePin(p, { lat: ll.lat, lng: ll.lng });
    });
  }
  circle.addTo(state.pinLayer);
  marker.addTo(state.pinLayer);
  state.pinLayers.set(p.id, { marker, circle });
}

function drawPins() {
  if (!state.map) return;
  state.pinLayer.clearLayers();
  state.pinLayers.clear();
  state.pins.forEach(drawPin);
}

async function loadPins() {
  try {
    state.pins = await api('/api/pins');
    drawPins();
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
  try {
    Object.assign(p, await api(`/api/pins/${p.id}`, { method: 'PUT', body: JSON.stringify(patch) }));
    const old = state.pinLayers.get(p.id);
    if (old) { old.marker.remove(); old.circle.remove(); }
    drawPin(p);
    renderPinsList();
    pinsChanged();
  } catch (e) { toast(`Pin update failed: ${e.message}`, 'err'); }
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
  if (state.mapMode !== 'pins') return;
  const inViewOnly = $('#pins-in-view').checked && state.map;
  const bounds = state.map?.getBounds();
  const pins = inViewOnly ? state.pins.filter((p) => bounds.contains([p.lat, p.lng])) : state.pins;
  box.innerHTML = '';
  if (!state.pins.length) { box.innerHTML = '<div class="empty">No pins yet. Click the map to drop one.</div>'; return; }
  if (inViewOnly) {
    box.insertAdjacentHTML('beforeend', `<div class="muted">${pins.length} of ${state.pins.length} pins in view</div>`);
    if (!pins.length) return;
  }
  pins.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'pin-row';
    const huge = p.radius_km >= 1000 ? '<span class="err-text" title="Ignored by the location filter: a radius this large covers everywhere. Remote roles are handled by the Remote toggles in Search setup.">⚠ ignored by location filter (radius ≥ 1000 km)</span>' : '';
    row.innerHTML = `
      <span class="pin-kind" style="border-color:var(--${PIN_HUE[p.kind] || 'border'})">${esc(p.kind)}</span>
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
    row.querySelector('[data-act="locate"]').addEventListener('click', () => { state.map.setView([p.lat, p.lng], 14); state.pinLayers.get(p.id)?.marker.openPopup(); });
    row.querySelector('[data-act="del"]').addEventListener('click', () => deletePin(p));
    box.appendChild(row);
  });
}

/* ---------- boot ---------- */
/* ================= WORKSPACES ================= */
function storedWorkspace() {
  try { return Number(localStorage.getItem('jobhunt.ws')) || 1; } catch { return 1; }
}

async function loadWorkspaces() {
  state.workspaces = await api('/api/workspaces');
  if (!state.workspaces.some((w) => w.id === state.ws)) state.ws = state.workspaces[0]?.id ?? 1;
  $('#ws-select').innerHTML = state.workspaces
    .map((w) => `<option value="${w.id}" ${w.id === state.ws ? 'selected' : ''}>${esc(w.name)}</option>`).join('');
  $('#ws-delete').disabled = state.workspaces.length <= 1;
}

async function switchWorkspace(id) {
  state.ws = Number(id);
  state.officeSlack = 0;
  try { localStorage.setItem('jobhunt.ws', String(state.ws)); } catch { /* private mode */ }
  clearTimeout(state.pollTimer);
  state.selected.clear();
  state.details.clear();
  state.titleSuggestions = [];
  $('#titles-list').innerHTML = '';
  $('#titles-actions').hidden = true;
  $('#titles-status').textContent = '';
  $('#run-status').textContent = '';
  $('#run-report').hidden = true;
  ['#search-run', '#score-pending', '#fill-info'].forEach((s) => { $(s).disabled = false; });
  $('#run-progress').hidden = true;
  closeChat();
  state.dupes = { suggested: [], auto_merged: [] };
  await loadWorkspaces();
  loadAll();
}

function loadAll() {
  loadLlmSettings();
  loadCompanies();
  loadJobs();
  loadSetup();
  loadSources();
  loadDocs();
  loadScorerStatus();
  loadLatestRun();
  if (state.map) loadPins();
}

function workspaceForm(title, { name = '', create = false } = {}) {
  const current = state.workspaces.find((w) => w.id === state.ws);
  openHtmlModal(title, `
    <form id="ws-form" class="ws-form">
      <label class="field"><span class="field-label">Name</span>
        <input id="ws-name" type="text" maxlength="80" value="${esc(name)}" required /></label>
      ${create ? `
      <label class="check"><input id="ws-copy" type="checkbox" /> Copy search settings, company sources and pins from "${esc(current?.name || '')}"</label>
      <label class="check"><input id="ws-copy-docs" type="checkbox" /> Also copy its documents</label>
      <p class="muted">Jobs, scores and runs always start empty. Company profiles are shared by all workspaces.</p>` : ''}
      <button class="btn btn-primary" type="submit">${create ? 'Create workspace' : 'Save'}</button>
    </form>`);
  $('#ws-name').focus();
  return new Promise((resolve) => {
    $('#ws-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const out = { name: $('#ws-name').value.trim() };
      if (create && $('#ws-copy').checked) { out.copy_from = state.ws; out.copy_documents = $('#ws-copy-docs').checked; }
      $('#modal').hidden = true;
      resolve(out);
    });
  });
}

function initWorkspaces() {
  $('#ws-select').addEventListener('change', (e) => switchWorkspace(e.target.value));
  $('#ws-new').addEventListener('click', async () => {
    const data = await workspaceForm('New workspace', { create: true });
    if (!data.name) return;
    try {
      const ws = await api('/api/workspaces', { method: 'POST', body: JSON.stringify(data) });
      toast(`Created "${ws.name}"`, 'ok');
      await switchWorkspace(ws.id);
    } catch (e) { toast(`Could not create workspace: ${e.message}`, 'err'); }
  });
  $('#ws-rename').addEventListener('click', async () => {
    const current = state.workspaces.find((w) => w.id === state.ws);
    const data = await workspaceForm('Rename workspace', { name: current?.name || '' });
    if (!data.name) return;
    try { await api(`/api/workspaces/${state.ws}`, { method: 'PATCH', body: JSON.stringify({ name: data.name }) }); await loadWorkspaces(); }
    catch (e) { toast(`Rename failed: ${e.message}`, 'err'); }
  });
  $('#ws-delete').addEventListener('click', async () => {
    const current = state.workspaces.find((w) => w.id === state.ws);
    if (!confirm(`Delete workspace "${current?.name}" with all its jobs, scores, documents, pins, sources and settings? This can't be undone.`)) return;
    try {
      await api(`/api/workspaces/${state.ws}`, { method: 'DELETE' });
      const next = state.workspaces.find((w) => w.id !== state.ws);
      toast(`Deleted "${current?.name}"`, 'ok');
      await switchWorkspace(next.id);
    } catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  state.ws = storedWorkspace();
  try { await loadWorkspaces(); } catch (e) { toast(`Could not load workspaces: ${e.message}`, 'err'); }
  initWorkspaces();
  initLlmSettings();
  initTitleSuggestions();
  initTabs();
  initJobs();
  initSetup();
  initDocs();
  initChat();
  initThemePicker($('#theme-select'));
  // Map markers are drawn in script, so they take the new theme's colours here.
  document.addEventListener('themechange', () => { if (state.map) { drawPins(); drawJobMarkers(); } });
  $('#source-add').addEventListener('click', addSource);
  $('#source-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addSource(); } });
  $('#score-test').addEventListener('click', testScorer);
  $('#score-stale').addEventListener('click', () => startRun('/api/score/run', { mode: 'stale' }));
  $('#score-all').addEventListener('click', () => { if (confirm('Rescore every job? This makes one API call per job.')) startRun('/api/score/run', { mode: 'all' }); });
  $('#modal-close').addEventListener('click', () => { $('#modal').hidden = true; });
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('#modal').hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#modal').hidden = true; });
  loadAll();
});
