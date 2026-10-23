/* JobHuntPA dashboard — vanilla JS, no build step. Talks to backend/app.py. */
'use strict';

const TAG_FIELDS = ['titles', 'keywords_include', 'keywords_exclude', 'dealbreaker_industries', 'dealbreaker_keywords', 'seek_locations'];

const state = {
  jobs: [],
  jobsStatus: 'loading', // 'loading' (nothing to show yet) | 'ready' | 'error' (first load failed)
  jobsRefreshing: false, // a reload is on its way; the list on screen stays up meanwhile
  jobsToken: 0, // bumped by every loadJobs, so only the newest answer is shown
  loadedOmit: 'excluded,hidden', // what the loaded list left out ("Show excluded"/"Show hidden" fetch them)
  omittedCount: 0, // how many jobs it left out
  details: new Map(), // job id -> full job (with description)
  filter: { q: '', minScore: 0, mode: 'all', maxDist: null, showExcluded: false, showClosed: false, showHidden: false, status: 'all', sort: 'score' },
  selected: new Set(),
  includeTouched: new Set(), // jobs whose "Always include" was set here: their cards keep it, ticked or not
  profile: null,
  pins: [],
  startingPoints: [], // Search setup > Commute: where the job page's Google Maps links start (besides home pins)
  setupPins: [], // the pins a starting point can be made from
  map: null,
  pinLayers: new Map(),
  pinLayer: null,
  jobLayer: null,
  jobMarkers: new Map(), // job id -> its map marker
  mapMode: 'jobs', // 'jobs' (read-only) | 'pins' (edit pins)
  pollTimer: null,
  watching: false, // a run is being followed by watchRun
  watchToken: 0, // bumped by every watchRun and workspace switch, so an older poll loop stops
  lastRun: null, // { ws, id } of the newest run this page has shown
  schedule: null, // this workspace's scheduled-search settings, as the server last returned them
  companies: {}, // company_key -> profile
  ws: 0, // current workspace id (0 until known: the server then uses the account's first)
  workspaces: [],
  titleSuggestions: [],
  officeSlack: 0, // temporary extra office days allowed on the Jobs tab (never saved)
  dupes: { suggested: [], auto_merged: [] }, // duplicate ads to check
  chat: null, // the open job chat: { jobIds, jobs, threadId, threads, messages, busy }
  // Documents tab: the list, which rows are open, their texts, unsaved edits ("id:field"), status lines
  // ("id:area") and actions under way ("id:act"), all for workspace `ws`.
  docs: { ws: null, list: [], open: new Set(), text: new Map(), drafts: new Map(), status: new Map(), busy: new Set() },
  // Cover letter blocks (Documents tab): the list in letter order, for workspace `blocksWs`, and the one being edited.
  blocks: [],
  blocksWs: null,
  blockEdit: null,
};

/* ---------- the dialog (#modal, a native <dialog>) ----------
   showModal() moves focus in, keeps it there (the page behind is inert) and closes on Escape. When it closes,
   focus goes back to the control that opened it, or to the same control redrawn meanwhile (a job card's
   buttons are redrawn when its job changes). */
let modalReturn = null;

function focusFinder(el) {
  if (!el || el === document.body) return () => null;
  const act = el.dataset?.act;
  const jobId = el.closest?.('[data-job-id]')?.dataset.jobId;
  const id = el.id;
  return () => {
    if (el.isConnected && !el.closest('[hidden]')) return el;
    if (id && document.getElementById(id)) return document.getElementById(id);
    if (act && jobId) return document.querySelector(`.job-card[data-job-id="${jobId}"] [data-act="${act}"]`);
    return null;
  };
}

function showModal() {
  const dlg = $('#modal');
  $('#modal-html').scrollTop = 0;
  $('#modal-text').scrollTop = 0;
  if (dlg.open) return;
  modalReturn = focusFinder(document.activeElement);
  dlg.showModal();
}

function closeModal() {
  if ($('#modal').open) $('#modal').close();
}

function openModal(title, text) {
  $('#modal-title').textContent = title;
  $('#modal-text').textContent = text;
  $('#modal-text').hidden = false;
  $('#modal-html').hidden = true;
  $('#modal-html').innerHTML = '';
  showModal();
}

function openHtmlModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-html').innerHTML = html;
  $('#modal-html').hidden = false;
  $('#modal-text').hidden = true;
  showModal();
}

function initModal() {
  const dlg = $('#modal');
  $('#modal-close').addEventListener('click', closeModal);
  // A click on the backdrop lands on the <dialog> itself (its box fills it otherwise).
  dlg.addEventListener('click', (e) => { if (e.target === dlg) closeModal(); });
  dlg.addEventListener('close', () => {
    $('#modal-html').innerHTML = ''; // nothing of the old content stays reachable
    const back = modalReturn?.();
    modalReturn = null;
    (back || $('#main')).focus();
  });
}

/* ---------- tabs (ARIA tabs pattern: arrow keys, Home and End move between them) ---------- */
const TAB_TITLE = { jobs: 'Jobs', search: 'Search setup', docs: 'Documents', map: 'Map', settings: 'Settings' };
let currentTab = 'jobs';

function updateTitle() {
  const ws = state.workspaces.find((w) => w.id === state.ws);
  document.title = [TAB_TITLE[currentTab], ws?.name, 'JobHuntPA'].filter(Boolean).join(' · ');
}

function initTabs() {
  const tabs = $$('.tab');
  const names = tabs.map((b) => b.dataset.tab);
  const show = (name, { focus = null } = {}) => {
    const leaving = $(`#tab-${currentTab}`);
    const hadFocus = leaving && leaving !== $(`#tab-${name}`) && leaving.contains(document.activeElement);
    currentTab = name;
    tabs.forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    });
    $$('.tab-panel').forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
    if (name === 'map') requestAnimationFrame(() => { initMap(); state.map?.invalidateSize(); drawJobMarkers(); });
    if ((name === 'settings' || name === 'jobs') && state.schedule) loadSchedule({ controls: false });
    history.replaceState(null, '', `#${name}`);
    updateTitle();
    if (focus === 'tab') $(`#tab-btn-${name}`).focus();
    // A link inside the old panel (e.g. one to the Settings tab) switched tabs: focus would be lost in a hidden panel.
    else if (focus === 'panel' || hadFocus) $(`#tab-${name}`).focus();
  };
  tabs.forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  $('.tabs').addEventListener('keydown', (e) => {
    const i = names.indexOf(currentTab);
    const next = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: names.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    show(names[(next + names.length) % names.length], { focus: 'tab' });
  });
  // #tab in the address picks the tab at load; a link to one (<a href="#settings">) or an edited address switches to it.
  const fromHash = () => { const name = location.hash.slice(1); if (names.includes(name)) show(name); };
  window.addEventListener('hashchange', fromHash);
  // The skip link: to the open tab's panel, without changing the address (that picks the tab).
  $('.skip-link').addEventListener('click', (e) => { e.preventDefault(); $(`#tab-${currentTab}`).focus(); });
  show(names.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'jobs');
}

/* ================= JOBS ================= */
// Every request that returns jobs passes the temporary office-days softening.
const slackQuery = () => (state.officeSlack ? `?office_slack=${state.officeSlack}` : '');

// A big list takes a while (the Pi, a search running): the first load shows a loading screen, and a
// reload keeps the list up and says it's updating. An answer for an older request, or for the
// workspace you've just left, is dropped. Excluded and hidden jobs (usually most of them) are only
// sent on demand, while "Show excluded" / "Show hidden" is ticked; otherwise only their number comes along.
const includeFor = () => [state.filter.showExcluded && 'excluded', state.filter.showHidden && 'hidden'].filter(Boolean);

async function loadJobs() {
  const token = ++state.jobsToken;
  const ws = state.ws;
  const include = includeFor();
  const omit = ['excluded', 'hidden'].filter((k) => !include.includes(k)).join(',');
  state.jobsRefreshing = state.jobsStatus === 'ready';
  if (state.jobsStatus === 'error') state.jobsStatus = 'loading';
  renderJobs();
  let jobs, omittedCount;
  try {
    const q = new URLSearchParams();
    if (include.length) q.set('include', include.join(','));
    if (state.officeSlack) q.set('office_slack', state.officeSlack);
    const res = await apiResponse(`/api/jobs?${q}`);
    jobs = res.data;
    omittedCount = Number(res.headers.get('X-Omitted-Count')) || 0;
  } catch (e) {
    if (token !== state.jobsToken || ws !== state.ws) return;
    state.jobsRefreshing = false;
    if (state.jobsStatus === 'loading') state.jobsStatus = 'error';
    showError($('#jobs-error'), `Could not load jobs: ${e.message}. Reload the page to try again.`);
    renderJobs();
    return;
  }
  if (token !== state.jobsToken || ws !== state.ws) return;
  state.jobs = jobs;
  state.loadedOmit = omit;
  state.omittedCount = omittedCount;
  state.jobsStatus = 'ready';
  state.jobsRefreshing = false;
  const ids = new Set(state.jobs.map((j) => j.id));
  state.selected.forEach((id) => { if (!ids.has(id)) state.selected.delete(id); });
  state.details.clear();
  $('#jobs-error').hidden = true;
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
  const focused = box.contains(document.activeElement) ? document.activeElement : null;
  box.innerHTML = '';
  const base = state.jobs.filter((j) => (state.filter.showExcluded || !j.excluded_reason)
    && (state.filter.showClosed || !j.closed) && (state.filter.showHidden || !j.hidden));
  [['all', 'all'], ...STATUSES.map((s) => [s, STATUS_LABEL[s]])].forEach(([val, label]) => {
    const n = val === 'all' ? base.length : base.filter((j) => j.status === val).length;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `chip${state.filter.status === val ? ' active' : ''}`;
    b.dataset.status = val;
    b.setAttribute('aria-pressed', String(state.filter.status === val));
    b.innerHTML = state.jobsStatus === 'ready' ? `${esc(label)} <span class="n">${n}</span>` : esc(label); // no "0" while loading
    b.addEventListener('click', () => { state.filter.status = val; renderJobs(); });
    box.appendChild(b);
  });
  // The chips are redrawn: keep keyboard focus on the same one.
  if (focused?.dataset.status) box.querySelector(`[data-status="${focused.dataset.status}"]`)?.focus();
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

const LOADING_JOBS = '<div class="jobs-loading" role="status"><span class="spinner" aria-hidden="true"></span> Loading jobs…</div>';

function renderJobs() {
  renderStatusChips();
  renderSoftenNote();
  const list = $('#jobs-list');
  list.setAttribute('aria-busy', String(state.jobsStatus === 'loading' || state.jobsRefreshing));
  if (state.jobsStatus !== 'ready') {
    list.innerHTML = state.jobsStatus === 'loading' ? LOADING_JOBS : '';
    $('#jobs-empty').hidden = true;
    setText($('#jobs-count'), state.jobsStatus === 'loading' ? 'Loading…' : '');
    renderBulkBar();
    drawJobMarkers();
    return;
  }
  const jobs = visibleJobs();
  // Bulk actions only ever apply to jobs on screen: drop selections the filters now hide.
  const shown = new Set(jobs.map((j) => j.id));
  state.selected.forEach((id) => { if (!shown.has(id)) state.selected.delete(id); });
  // Cards are redrawn: keep keyboard focus on the same control of the same card.
  const focusBack = list.contains(document.activeElement) ? focusFinder(document.activeElement) : null;
  list.innerHTML = '';
  $('#jobs-empty').hidden = jobs.length !== 0;
  const hidden = state.jobs.length - jobs.length + state.omittedCount;
  setText($('#jobs-count'), `${jobs.length} shown${hidden ? ` · ${hidden} hidden by filters` : ''}${state.jobsRefreshing ? ' · updating…' : ''}`);
  jobs.forEach((job) => list.appendChild(renderJobCard(job)));
  if (focusBack) (focusBack() || $('#tab-jobs')).focus({ preventScroll: true });
  renderBulkBar();
  drawJobMarkers();
}

function renderJobCard(job) {
  const card = document.createElement('article');
  card.dataset.jobId = job.id;
  card.className = `card job-card${job.excluded_reason ? ' is-excluded' : ''}${state.selected.has(job.id) ? ' is-selected' : ''}`;
  card.setAttribute('aria-labelledby', `job-${job.id}-title`);
  const mode = modeOf(job);
  const dist = job.distance_km === null ? '' : `<span><span aria-hidden="true">📍 </span><span class="visually-hidden">Distance: </span>${job.distance_km} km</span>`;
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
  // The main link: the employer's own page when a merged copy is on it (job.link), else the listed ad. The other
  // copies, and then the listed ad itself, are under "Also on".
  const link = mainLink(job);
  const split = (id, source) => `<button class="link split-btn" type="button" data-split="${id}" title="Not the same job: show this ad as its own card again">split<span class="visually-hidden"> the ${esc(sourceLabel(source))} ad from this card</span></button>`;
  const also = [
    ...(link.copy_id ? [`<span class="also-item"><a href="${esc(job.url)}" target="_blank" rel="noopener">${esc(sourceLabel(job.source))}</a></span>`] : []),
    ...(job.also_on || []).filter((c) => c.id !== link.copy_id).map((c) => `<span class="also-item"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}</a>${c.closed ? ' <span class="muted">(closed)</span>' : ''}
    ${split(c.id, c.source)}</span>`),
  ].join('');
  const fitErr = job.fit && job.fit.error ? `<p class="hint err-text">Scoring failed: ${esc(job.fit.error.slice(0, 240))}</p>` : '';
  const titleId = `job-${job.id}-title`;
  const name = `${job.title || 'job'}${job.company ? ` at ${job.company}` : ''}`;
  card.innerHTML = `
    <div class="job-top">
      <input type="checkbox" data-act="select" aria-label="Select ${esc(name)}" ${state.selected.has(job.id) ? 'checked' : ''} />
      <div class="job-title-row">
        <div class="job-company">${esc(job.company || '—')} ${companyIcon(job)}</div>
        <h3 class="job-title" id="${titleId}"><a href="${esc(link.url)}" target="_blank" rel="noopener">${esc(job.title || '—')}</a></h3>
        <div class="job-meta">
          <span>${esc(job.location_text || 'location unknown')}</span>
          <span class="badge ${mode}"><span class="visually-hidden">Work mode: </span>${mode}</span>${employmentBadge(job)}${dist}${flags}
        </div>
        ${ghostWhy(job)}
      </div>
      ${ratingsHtml(job)}
    </div>
    <div class="job-sub">
      ${job.salary_text ? `<span><span aria-hidden="true">💰 </span><span class="visually-hidden">Salary: </span>${esc(job.salary_text)}</span>` : ''}
      <span class="muted">${esc(sourceLabel(link.source))}${job.posted_at ? ` · posted ${esc(fmtDate(job.posted_at).split(',')[0])}` : ''}</span>${
        link.copy_id ? ` ${split(link.copy_id, link.source)}` : ''}
      <span class="applied-slot">${appliedHtml(job)}</span>
    </div>
    ${officeHtml(job) ? `<div class="job-office">${officeHtml(job)}</div>` : ''}
    ${also ? `<div class="also-on" title="The same job advertised on other sites, merged into this card"><span class="muted">Also on:</span> ${also}</div>` : ''}
    ${excl}${fitErr}
    <div class="job-actions">
      <select data-act="status" aria-label="Application status of ${esc(name)}">
        ${STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}
      </select>
      <button class="btn btn-ghost btn-sm" type="button" data-act="rescore" aria-describedby="${titleId}">${job.fit && job.fit.status === 'ok' ? 'Rescore' : 'Score'}</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="desc" aria-describedby="${titleId}">Description</button>
      ${isSeekJob(job) && job.detail_status === 'summary' ? `<button class="btn btn-sm" type="button" data-act="upload-ad" aria-describedby="${titleId}" title="Replace the listing summary with the full ad from a page you save on SEEK">Upload full ad</button>` : ''}
      <button class="btn btn-ghost btn-sm" type="button" data-act="hide" aria-describedby="${titleId}">${job.hidden ? 'Unhide' : 'Hide'}</button>
      ${showsInclude(job) ? includeToggle(job, titleId) : ''}
      <button class="btn btn-ghost btn-sm" type="button" data-act="letter" aria-describedby="${titleId}">${job.has_cover_letter ? 'Cover letter<span aria-hidden="true"> ✓</span><span class="visually-hidden"> (drafted)</span>' : 'Draft cover letter'}</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="chat" aria-describedby="${titleId}" title="Ask questions about this job in a chat on the right">Chat</button>
      <a class="btn btn-ghost btn-sm" data-act="open" href="./job.html?id=${job.id}&ws=${state.ws}" target="_blank" rel="noopener" aria-describedby="${titleId}" title="Full page in a new tab: the whole ad, the office with commute links, and company details">Open${NEW_TAB}</a>
    </div>
    <details class="reqs" data-section="fit"><summary data-act="fit">Fit: requirements <span aria-hidden="true">→</span><span class="visually-hidden">and</span> evidence</summary><div class="fit-body"></div></details>
    ${gapsSection(job)}`;

  card.querySelector('input[type="checkbox"]').addEventListener('change', (e) => {
    if (e.target.checked) state.selected.add(job.id); else state.selected.delete(job.id);
    card.classList.toggle('is-selected', e.target.checked);
    renderBulkBar();
  });
  const sel = card.querySelector('select');
  sel.addEventListener('change', () => updateJobStatus(job, sel.value, sel));
  card.querySelector('[data-act="rescore"]').addEventListener('click', (e) => rescoreJob(job, e.currentTarget));
  card.querySelector('[data-act="desc"]').addEventListener('click', () => showDescription(job));
  card.querySelector('[data-act="letter"]').addEventListener('click', () => showCoverLetter(job));
  card.querySelector('[data-act="chat"]').addEventListener('click', () => openChat([job.id]));
  card.querySelectorAll('[data-split]').forEach((b) => b.addEventListener('click', () => splitCopy(Number(b.dataset.split))));
  card.querySelector('[data-act="hide"]').addEventListener('click', () => setHidden([job.id], !job.hidden));
  card.querySelector('[data-act="upload-ad"]')?.addEventListener('click', () => showUploadAd(job));
  card.querySelector('[data-act="company"]')?.addEventListener('click', () => showCompany(job));
  card.querySelector('[data-act="filter-warnings"]')?.addEventListener('click', () => showFilterWarnings(job));
  card.querySelector('[data-act="include"]')?.addEventListener('change', (e) => setIncluded(job, e.currentTarget));
  card.querySelector('details[data-section="fit"]').addEventListener('toggle', (e) => {
    if (e.target.open) e.target.querySelector('.fit-body').innerHTML = fitHtml(job);
  });
  card.querySelector('details[data-section="gaps"]')?.addEventListener('toggle', (e) => {
    if (e.target.open) e.target.querySelector('.gaps-body').innerHTML = gapsHtml(job);
  });
  return card;
}

// A card has "Always include" where the filters and the person may disagree: a job the filters leave out (or only the
// office-days what-if shows), one the person included, and one whose box was set here (so it can be ticked again).
const showsInclude = (job) => !!(job.excluded_reason || job.softened_reason || job.included_by_user || state.includeTouched.has(job.id));

async function setIncluded(job, box) {
  const back = focusFinder(box);
  box.disabled = true;
  try {
    const updated = await api(`/api/jobs/${job.id}/included${slackQuery()}`, { method: 'PATCH', body: JSON.stringify({ included: box.checked }) });
    state.includeTouched.add(job.id);
    Object.assign(job, updated);
    state.details.set(job.id, updated);
    renderJobs();
    (back() || $('#tab-jobs')).focus({ preventScroll: true });
    toast(includedWords(job, !state.filter.showExcluded), 'ok', 6000);
  } catch (e) {
    box.checked = !box.checked;
    toast(`Could not change "Always include": ${e.message}`, 'err');
  } finally {
    box.disabled = false;
  }
}

// The ⚠️ of a job the person included: the rules of their filters it doesn't meet. The link to the Search setup tab
// closes the dialog on its way there.
function showFilterWarnings(job) {
  openHtmlModal(filterWarningsTitle(job), filterWarningsHtml(job));
  $('#modal-html a[href="#search"]').addEventListener('click', closeModal);
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
  const slot = selectEl.closest('.job-card')?.querySelector('.applied-slot');
  try {
    const res = await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: next }) });
    job.applied_at = res.applied_at ?? job.applied_at ?? null;
    if (slot) slot.innerHTML = appliedHtml(job); // the card isn't redrawn (focus stays on the select)
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
      <p>${job.source === 'seek' ? 'SEEK only lets the app read the short listing summary.' : 'JobHuntPA has only a short summary of this job.'} To score this job on the full ad:</p>
      <ol>
        <li><a href="${esc(job.url)}" target="_blank" rel="noopener">Open the ad on SEEK${NEW_TAB}</a> and wait for it to load.</li>
        <li>Save the page: press <kbd>${mac ? 'Cmd' : 'Ctrl'}</kbd>+<kbd>S</kbd> (any "Webpage" format works).</li>
        <li><label for="ad-file">Choose the saved file here:</label></li>
      </ol>
      <input id="ad-file" type="file" accept=".html,.htm,.mhtml,.mht,text/html" />
      <p id="ad-status" class="muted" role="status"></p>
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
      // An ad you upload stays in your list; the ⚠️ under its score says which filters it doesn't meet.
      const warn = (updated.filter_warnings || []).length ? ` ${filterWarnWords(updated)} (see the ⚠️ on its card), but it stays in your list.` : '';
      status.textContent = `Updated with the full ad.${warn} Rescoring…`;
      renderJobs();
      try {
        Object.assign(job, await api(`/api/score/${job.id}${slackQuery()}`, { method: 'POST' }));
        status.textContent = `Updated and rescored: ${job.fit?.score ?? '?'}.`;
      } catch (err) {
        status.textContent = `Updated with the full ad, but rescoring failed (${err.message}). Use Rescore on the card later.`;
      }
      renderJobs();
    } catch (err) {
      status.textContent = '';
      setFieldError(e.target, err.message);
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
    const res = await api('/api/jobs/status', { method: 'PATCH', body: JSON.stringify({ ids, status }) });
    state.jobs.forEach((j) => {
      if (!state.selected.has(j.id)) return;
      j.status = status;
      if (res.applied_at && String(j.id) in res.applied_at) j.applied_at = res.applied_at[String(j.id)];
    });
    state.selected.clear();
    renderJobs();
    toast(`Updated ${ids.length} job(s)`, 'ok');
  } catch (e) { toast(`Bulk update failed: ${e.message}`, 'err'); }
}

async function rescoreJob(job, btn) {
  setBusy(btn, true);
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
  // Excluded and hidden jobs are fetched on demand (see loadJobs).
  $('#filter-show-excluded').addEventListener('change', (e) => {
    state.filter.showExcluded = e.target.checked;
    if (e.target.checked && state.loadedOmit.includes('excluded')) loadJobs(); else renderJobs();
  });
  $('#filter-show-closed').addEventListener('change', (e) => { state.filter.showClosed = e.target.checked; renderJobs(); });
  $('#filter-show-hidden').addEventListener('change', (e) => {
    state.filter.showHidden = e.target.checked;
    if (e.target.checked && state.loadedOmit.includes('hidden')) loadJobs(); else renderJobs();
  });
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
  $('#add-job-open').addEventListener('click', showAddJob);
}

/* ================= BACKGROUND RUNS ================= */
const RUN_LABEL = { search: 'Search', score: 'Scoring', research: 'Company research' };
const runLabel = (run, fallback) => (run.scheduled ? 'Scheduled search' : RUN_LABEL[run.kind] || fallback);
async function startRun(url, body) {
  try {
    const run = await api(url, { method: 'POST', body: JSON.stringify(body) });
    if (run.already_running) toast('A run is already in progress; showing its progress.');
    watchRun(run.id);
  } catch (e) { toast(e.message, 'err', 9000); }
}

function watchRun(id) {
  clearTimeout(state.pollTimer);
  const token = ++state.watchToken;
  state.watching = true;
  ['#search-run', '#score-pending', '#fill-info'].forEach((s) => setBusy($(s), true));
  $('#run-progress').hidden = false;
  const tick = async () => {
    let run;
    try { run = await api(`/api/runs/${id}`); } catch (e) { if (token === state.watchToken) state.pollTimer = setTimeout(tick, 3000); return; }
    if (token !== state.watchToken) return; // watching another run now, or another workspace
    state.lastRun = { ws: state.ws, id: run.id };
    const p = run.progress || {};
    const pct = p.total ? Math.round((100 * (p.done || 0)) / p.total) : null;
    const bar = $('#run-progress');
    bar.querySelector('.progress-bar').style.width = pct === null ? '100%' : `${pct}%`;
    bar.classList.toggle('indeterminate', pct === null);
    if (pct === null) { bar.removeAttribute('aria-valuenow'); bar.removeAttribute('aria-valuetext'); }
    else { bar.setAttribute('aria-valuenow', String(pct)); bar.setAttribute('aria-valuetext', `${p.done || 0} of ${p.total} done`); }
    setText($('#run-status'), `${runLabel(run, 'Working')}: ${run.stage || run.state}…`); // said only when the stage changes
    if (run.state === 'queued' || run.state === 'running') { state.pollTimer = setTimeout(tick, 1200); return; }
    finishRun(run);
  };
  tick();
}

function finishRun(run) {
  state.watching = false;
  ['#search-run', '#score-pending', '#fill-info'].forEach((s) => setBusy($(s), false));
  $('#run-progress').hidden = true;
  if (run.state === 'failed') {
    const what = run.scheduled ? 'Scheduled search' : 'Run';
    $('#run-status').textContent = `${what} failed: ${run.error}`;
    toast(`${what} failed: ${run.error}`, 'err', 10000);
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
  loadSchedule({ controls: false });
}

function runSummaryText(run) {
  return `${runLabel(run, 'Run')} finished ${fmtDate(run.finished_at)}: ${runSummaryBits(run)}`;
}

function runSummaryBits(run) {
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
    else {
      bits.push(`researched ${rs.researched}/${rs.requested} companies${rs.errors ? `, ${rs.errors} failed` : ''}`);
      const what = researchRefreshText(rs);
      if (what) bits.push(what);
    }
  }
  const of = run.summary?.offices;
  if (of && !of.aborted) {
    const found = of.from_ads + of.from_companies;
    bits.push(`offices found for ${found} job${found === 1 ? '' : 's'}${of.unknown ? `, ${of.unknown} still unknown${of.recruiters ? ` (${of.recruiters} recruiter ads: your call)` : ''}` : ''}`);
  }
  return bits.join(' · ');
}

// On narrow screens each source becomes a block of "Listed: 12" lines (.stack-table in styles.css), so the
// table never needs sideways scrolling; the explicit roles keep it a table for screen readers either way.
const REPORT_COLS = ['Source', 'Listed', 'New', 'Descriptions fetched', 'Kept', 'Excluded', 'Closed', 'Status'];
function renderRunReport(run) {
  const per = run.summary?.search?.per_source;
  if (!per) return;
  const td = (i, html) => `<td role="cell" data-label="${REPORT_COLS[i]}">${html}</td>`;
  const rows = per.map((s) => `
    <tr role="row">
      <th role="rowheader" scope="row"><strong>${esc(s.label)}</strong><div class="muted">${esc(s.method || '')}</div></th>
      ${td(1, s.listed)}${td(2, s.new)}${td(3, `${s.details_fetched}${s.details_deferred ? ` (+${s.details_deferred} next run)` : ''}`)}
      ${td(4, s.kept)}${td(5, s.excluded)}${td(6, s.closed)}
      ${td(7, `${s.error ? `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(s.error)}</span>` : '<span aria-hidden="true">✓</span><span class="visually-hidden">OK</span>'}${(s.notes || []).length ? `<div class="muted">${(s.notes || []).map(esc).join('<br>')}</div>` : ''}`)}
    </tr>`).join('');
  $('#run-report-body').innerHTML = `<table class="report stack-table" role="table"><caption class="visually-hidden">Results of the last run per source</caption>
    <thead role="rowgroup"><tr role="row">${REPORT_COLS.map((c) => `<th role="columnheader" scope="col">${c}</th>`).join('')}</tr></thead><tbody role="rowgroup">${rows}</tbody></table>`;
  $('#run-report').hidden = false;
}

async function loadLatestRun() {
  const ws = state.ws;
  try {
    const run = await api('/api/runs/latest');
    if (ws !== state.ws) return; // switched workspace meanwhile
    state.lastRun = { ws, id: run?.id ?? null };
    if (!run) return;
    if (run.state === 'queued' || run.state === 'running') { watchRun(run.id); return; }
    $('#run-status').textContent = run.state === 'failed' ? `Last ${run.scheduled ? 'scheduled search' : 'run'} failed: ${run.error}` : runSummaryText(run);
    renderRunReport(run);
  } catch { /* first launch */ }
}

/* A scheduled search starts without this page, so look for one now and then (only while nothing is
   being watched and the tab is in view). A run that started and finished unseen refreshes the page too. */
const IDLE_POLL_MS = 60000;
async function pollForScheduledRun() {
  if (state.watching || document.visibilityState !== 'visible') return;
  if (state.schedule?.next_run && new Date(state.schedule.next_run) < Date.now()) loadSchedule({ controls: false });
  const ws = state.ws;
  let run;
  try { run = await api('/api/runs/latest'); } catch { return; } // busy or restarting: next minute
  if (ws !== state.ws || state.watching || !run) return;
  const seen = state.lastRun;
  state.lastRun = { ws, id: run.id };
  if (run.state === 'queued' || run.state === 'running') watchRun(run.id);
  else if (seen?.ws === ws && seen.id !== run.id) finishRun(run);
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
    title = n ? `${n} News/Controversies item(s) found: click for details` : 'Company profile';
  } else if (p?.status === 'error') { cls = 'err'; title = 'Research failed: click for details'; }
  else if (p?.status === 'skipped') { cls = 'none'; title = 'Not a researchable company'; }
  const n = p?.status === 'done' ? p.controversies.length : 0;
  const count = n ? `<sup>${n}</sup>` : '';
  // The name is hidden text beside the symbol, not an aria-label: the symbol and count ("ⓘ1") would not be part of an
  // aria-label (WCAG 2.5.3 Label in Name). The title says what's known, and is read as the description.
  const label = `Company info: ${job.company}${n ? `, ${n} News/Controversies ${n === 1 ? 'item' : 'items'}` : ''}`;
  return `<button type="button" class="info-btn ${cls}" data-act="company" title="${esc(title)}"><span aria-hidden="true">ⓘ${count}</span><span class="visually-hidden">${esc(label)}</span></button>`;
}

function showCompany(job) {
  const p = state.companies[job.company_key];
  openHtmlModal(p?.official_name || job.company, companyProfileHtml(p, job.company));
  // The profile's button researches all of it, a section's only that section: either at any time.
  $$('#modal-html [data-research-part]').forEach((b) => b.addEventListener('click', () => {
    closeModal();
    startRun('/api/companies/research', { name: job.company, part: b.dataset.researchPart });
  }));
}

function currentWorkspace() { return state.ws; }

/* ================= COVER LETTERS ================= */
// Two ways to a letter: the AI drafts one (POST …/cover-letter), or it is put together word for word from the
// user's blocks (Build from my blocks): the AI suggests which blocks and in what order, and which blocks are worth
// writing for this job; the user ticks, orders, writes, then Assemble joins them (POST …/blocks/assemble).
async function showCoverLetter(job) {
  let letter = null;
  try { letter = await api(`/api/jobs/${job.id}/cover-letter`); } catch { letter = null; }
  openHtmlModal(`Cover letter: ${job.title} at ${job.company}`, `
    <div class="letter-editor">
      <label class="field"><span class="field-label">Extra instructions for the draft (optional)</span>
        <textarea id="cl-instructions" rows="2" placeholder="e.g. Mention I can start in two weeks. Keep it under 300 words.">${esc(letter?.instructions || '')}</textarea></label>
      <div class="toolbar-row">
        <button id="cl-draft" class="btn btn-primary btn-sm" type="button">${letter ? 'Redraft' : 'Draft cover letter'}</button>
        <button id="cl-build" class="btn btn-sm" type="button">Build from my blocks</button>
        <span id="cl-status" class="muted" role="status"></span>
      </div>
      <section id="cl-blocks" class="cl-blocks" aria-labelledby="cl-blocks-title" hidden></section>
      <div class="field"><label class="field-label" for="cl-text">Cover letter</label>
        <textarea id="cl-text" rows="16" placeholder="Your draft appears here. You can edit it before saving.">${esc(letter?.text || '')}</textarea></div>
      <div class="toolbar-row">
        <button id="cl-save" class="btn btn-sm" type="button">Save edits</button>
        <button id="cl-copy" class="btn btn-ghost btn-sm" type="button">Copy<span class="visually-hidden"> the letter</span></button>
        <a id="cl-docx" class="btn btn-ghost btn-sm" href="/api/jobs/${job.id}/cover-letter.docx?ws=${state.ws}" ${letter ? '' : 'hidden'}>Download .docx</a>
        <a id="cl-odt" class="btn btn-ghost btn-sm" href="/api/jobs/${job.id}/cover-letter.odt?ws=${state.ws}" ${letter ? '' : 'hidden'}>Download .odt</a>
        <a id="cl-pdf" class="btn btn-ghost btn-sm" href="/api/jobs/${job.id}/cover-letter.pdf?ws=${state.ws}" ${letter ? '' : 'hidden'}>Download .pdf</a>
        <span id="cl-template-note" class="muted"></span>
        <span id="cl-meta" class="muted"></span>
      </div>
      <p class="hint">Two ways to a letter: <strong>Draft cover letter</strong> has the AI write one from your documents, reusing
        your cover letter blocks where they fit; <strong>Build from my blocks</strong> puts together the paragraphs you wrote
        on the Documents tab, word for word, with the AI only choosing and ordering them. Drafts use only facts from your
        documents, but always check them before sending. Upload earlier cover letters as "Cover letter" documents and
        drafts will follow your style.</p>
    </div>`);
  const meta = (l) => {
    if (!l) return '';
    if (l.source === 'blocks') return `${l.edited ? 'Edited' : 'Put together'} ${fmtDate(l.updated_at)} · Built from your blocks`;
    const effort = l.reasoning_effort ? `, reasoning ${l.reasoning_effort}` : '';
    return `${l.edited ? 'Edited' : 'Drafted'} ${fmtDate(l.updated_at)} · ${l.model || ''}${effort}`;
  };
  const showLetter = (l) => {
    letter = l;
    $('#cl-text').value = l.text;
    $('#cl-meta').textContent = meta(l);
    for (const x of ['docx', 'odt', 'pdf']) $(`#cl-${x}`).hidden = false;
    $('#cl-draft').textContent = 'Redraft';
    job.has_cover_letter = true;
    renderJobs();
  };
  const templateNote = () => {
    const t = state.letterTemplate;
    $('#cl-template-note').textContent = t?.template
      ? `Downloads use your template${t.pdf === 'plain' ? ' (not the PDF: install LibreOffice)' : ''}` : '';
  };
  templateNote();
  if (!state.letterTemplate) loadLetterTemplate().then(templateNote);
  $('#cl-meta').textContent = meta(letter);
  $('#cl-draft').addEventListener('click', async () => {
    const btn = $('#cl-draft');
    if ($('#cl-text').value.trim() && !confirm('Replace the current letter with a new draft?')) return;
    setBusy(btn, true);
    $('#cl-status').textContent = 'Drafting… this can take a minute.';
    try {
      showLetter(await api(`/api/jobs/${job.id}/cover-letter`, { method: 'POST', body: JSON.stringify({ instructions: $('#cl-instructions').value }) }));
      $('#cl-status').textContent = '';
    } catch (e) { $('#cl-status').textContent = `Failed: ${e.message}`; }
    setBusy(btn, false);
  });
  initLetterBlocks(job, showLetter);
  $('#cl-save').addEventListener('click', async () => {
    try {
      letter = await api(`/api/jobs/${job.id}/cover-letter`, { method: 'PUT', body: JSON.stringify({ text: $('#cl-text').value }) });
      $('#cl-meta').textContent = meta(letter);
      for (const x of ['docx', 'odt', 'pdf']) $(`#cl-${x}`).hidden = false;
      job.has_cover_letter = true;
      toast('Cover letter saved', 'ok', 2000);
    } catch (e) { toast(`Save failed: ${e.message}`, 'err'); }
  });
  $('#cl-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#cl-text').value); toast('Copied', 'ok', 1500); }
    catch { $('#cl-text').select(); toast('Press Ctrl+C to copy', '', 2500); }
  });
}

// A tag from a requirement the AI named: tags are split at commas and hold up to BLOCK_TAG_MAX characters.
function tagFromRequirement(req) {
  const t = String(req || '').replace(/,/g, ' ').replace(/\s+/g, ' ').trim();
  if (t.length <= BLOCK_TAG_MAX) return t;
  const cut = t.slice(0, BLOCK_TAG_MAX);
  return (cut.lastIndexOf(' ') > 20 ? cut.slice(0, cut.lastIndexOf(' ')) : cut).trim();
}

// The "Build from my blocks" panel (#cl-blocks) of one job's cover letter window. `rows` is the checklist in display
// order ({ block, ticked, why }); `missing` the blocks worth writing; `writing` the one whose form is open.
function initLetterBlocks(job, showLetter) {
  const panel = $('#cl-blocks');
  const status = $('#cl-status');
  let rows = [];
  let missing = [];
  let writing = null;
  let note = ''; // a message at the top of the panel (HTML, built from escaped text)

  const render = () => {
    const saved = rememberFocus(panel);
    const checklist = rows.length ? `
      <ul class="cl-block-list">${rows.map((r, i) => {
        const name = blockName(r.block);
        return `<li class="cl-block-row${r.ticked ? ' is-ticked' : ''}">
          <div class="cl-block-pick">
            <label class="check"><input type="checkbox" data-pick="${i}" data-focus="pick-${r.block.id}"${r.ticked ? ' checked' : ''} />
              <span><span class="cl-block-name">${esc(name)}</span> <span class="muted">(${esc(BLOCK_KIND_LABEL[r.block.kind] || r.block.kind)})</span></span></label>
            ${r.why ? `<p class="muted cl-block-why">${esc(r.why)}</p>` : ''}
          </div>
          <div class="cl-block-move">
            <button class="btn btn-ghost btn-sm" type="button" data-move="${i}" data-dir="-1" data-focus="up-${r.block.id}" aria-label="Move ${esc(name)} up"${i === 0 ? ' disabled' : ''}><span aria-hidden="true">↑</span></button>
            <button class="btn btn-ghost btn-sm" type="button" data-move="${i}" data-dir="1" data-focus="down-${r.block.id}" aria-label="Move ${esc(name)} down"${i === rows.length - 1 ? ' disabled' : ''}><span aria-hidden="true">↓</span></button>
          </div>
        </li>`;
      }).join('')}</ul>` : '';
    const suggestions = missing.length ? `
      <section class="cl-missing" aria-labelledby="cl-missing-title">
        <h3 id="cl-missing-title" class="cl-blocks-heading">Blocks worth writing</h3>
        <p class="hint">This job asks for these, and none of your blocks covers them yet.</p>
        <ul class="cl-missing-list">${missing.map((m, i) => `<li class="cl-missing-item">
          <p class="cl-missing-name"><strong>${esc(m.title)}</strong></p>
          <p class="muted cl-missing-req">${esc(m.requirement)}</p>
          ${writing === i ? `<form id="cl-newblock" class="cl-newblock" aria-label="Write the block ${esc(m.title)}" novalidate>
            ${blockFormHtml('cl-nb', { title: m.title, kind: 'body', tags: tagFromRequirement(m.requirement), bullets: m.bullets || [] })}
            <p class="hint">Write it in your own words; the bullets are facts from your documents, not text to copy.</p>
            <div class="toolbar-row">
              <button id="cl-nb-save" class="btn btn-primary btn-sm" type="submit">Save block</button>
              <button id="cl-nb-skip" class="btn btn-sm" type="button" data-focus="skip">Skip</button>
              <span id="cl-nb-status" class="muted" role="status"></span>
            </div>
          </form>` : `<button class="btn btn-sm" type="button" data-write="${i}" data-focus="write-${i}">Write this block</button>`}
        </li>`).join('')}</ul>
      </section>` : '';
    panel.innerHTML = `
      <h3 id="cl-blocks-title" class="cl-blocks-heading" tabindex="-1">Your blocks for this letter</h3>
      ${note ? `<p class="cl-blocks-note">${note}</p>` : ''}
      ${rows.length ? '<p class="hint">Tick the blocks to use and put them in order: they are joined word for word, with {company}, {role}, {location} and {date} filled in.</p>' : ''}
      ${checklist}
      <div class="toolbar-row">
        ${rows.length ? '<button id="cl-assemble" class="btn btn-primary btn-sm" type="button">Assemble letter</button>' : ''}
        <button id="cl-blocks-cancel" class="btn btn-sm" type="button">Cancel</button>
      </div>
      ${suggestions}`;
    panel.hidden = false;
    restoreFocus(panel, saved);
  };

  const open = ({ list, picked = [], unpicked = [], gaps = [], message = '' }) => {
    const byId = new Map(list.map((b) => [b.id, b]));
    const used = new Set();
    const take = (id, ticked, why = '') => {
      const block = byId.get(id);
      if (!block || used.has(id)) return null;
      used.add(id);
      return { block, ticked, why };
    };
    rows = [...picked.map((p) => take(p.id, true, p.why || '')), ...unpicked.map((id) => take(id, false))].filter(Boolean);
    rows.push(...list.filter((b) => !used.has(b.id)).map((b) => ({ block: b, ticked: false, why: '' }))); // any the answer left out
    missing = gaps;
    writing = null;
    note = message || (rows.length ? '' : 'You have no blocks yet. Write the suggested ones below, or on the Documents tab.');
    render();
    $('#cl-blocks-title').focus();
  };

  $('#cl-build').addEventListener('click', async () => {
    const btn = $('#cl-build');
    if ($('#cl-text').value.trim() && !confirm('Replace the current letter?')) return;
    setBusy(btn, true);
    status.textContent = 'Choosing blocks…';
    try {
      const res = await api(`/api/jobs/${job.id}/cover-letter/blocks/suggest`, { method: 'POST' });
      state.blocks = sortBlocks(res.blocks || []);
      renderBlocks();
      status.textContent = '';
      open({ list: res.blocks || [], picked: res.picked || [], unpicked: res.unpicked || [], gaps: res.missing || [] });
    } catch (e) {
      if (e.status === 400 || e.status === 502) {
        try { state.blocks = await api('/api/letter-blocks'); renderBlocks(); } catch { /* keep what was loaded */ }
        const detail = e.message.replace(/[.\s]+$/, '');
        if (state.blocks.length) {
          status.textContent = `${detail}: tick the blocks yourself.`;
          open({ list: state.blocks });
        } else {
          status.textContent = '';
          open({ list: [], message: `${esc(detail)}. Write your blocks on the <a href="#docs" data-act="to-docs">Documents tab</a> first.` });
        }
      } else status.textContent = `Failed: ${e.message}`;
    }
    setBusy(btn, false);
  });

  panel.addEventListener('change', (e) => {
    const i = e.target.dataset?.pick;
    if (i === undefined || !rows[i]) return;
    rows[i].ticked = e.target.checked;
    e.target.closest('.cl-block-row').classList.toggle('is-ticked', e.target.checked);
  });

  panel.addEventListener('click', async (e) => {
    const el = e.target.closest('button, a');
    if (!el || !panel.contains(el)) return;
    if (el.dataset.act === 'to-docs') { closeModal(); return; } // the address (#docs) then switches the tab
    if (el.dataset.move !== undefined) {
      const i = Number(el.dataset.move);
      const j = i + Number(el.dataset.dir);
      if (j < 0 || j >= rows.length) return;
      [rows[i], rows[j]] = [rows[j], rows[i]];
      const id = rows[j].block.id;
      render();
      if (!panel.contains(document.activeElement)) (panel.querySelector(`[data-focus="${j === 0 ? 'down' : 'up'}-${id}"]`))?.focus();
      announce(`Moved "${blockName(rows[j].block)}" to number ${j + 1} of ${rows.length}.`);
    } else if (el.dataset.write !== undefined) {
      writing = Number(el.dataset.write);
      render();
      $('#cl-nb-text').focus();
    } else if (el.id === 'cl-nb-skip') {
      const i = writing;
      writing = null;
      render();
      panel.querySelector(`[data-focus="write-${i}"]`)?.focus();
    } else if (el.id === 'cl-blocks-cancel') {
      panel.hidden = true;
      panel.innerHTML = '';
      status.textContent = '';
      $('#cl-build').focus();
    } else if (el.id === 'cl-assemble') {
      const ids = rows.filter((r) => r.ticked).map((r) => r.block.id);
      if (!ids.length) { status.textContent = 'Tick at least one block.'; return; }
      setBusy(el, true);
      status.textContent = 'Putting the letter together…';
      try {
        const l = await api(`/api/jobs/${job.id}/cover-letter/blocks/assemble`, { method: 'POST', body: JSON.stringify({ block_ids: ids }) });
        showLetter(l);
        panel.hidden = true;
        panel.innerHTML = '';
        const unfilled = l.unfilled || [];
        status.textContent = unfilled.length
          ? `Left as written: ${unfilled.map((n) => `{${n}}`).join(', ')} — fill ${unfilled.length === 1 ? 'it' : 'them'} in before sending.`
          : `Put together from ${ids.length} block${ids.length === 1 ? '' : 's'}. Check it, then save any edits.`;
        $('#cl-text').focus();
      } catch (err) {
        setBusy(el, false);
        status.textContent = `Failed: ${err.message}`;
      }
    }
  });

  panel.addEventListener('submit', async (e) => {
    if (e.target.id !== 'cl-newblock') return;
    e.preventDefault();
    const form = e.target;
    const payload = readBlockForm(form);
    if (!payload) return;
    const btn = $('#cl-nb-save');
    setBusy(btn, true);
    $('#cl-nb-status').textContent = 'Saving…';
    try {
      const block = await saveBlock(payload);
      const last = (pred) => rows.reduce((at, r, i) => (r.ticked && pred(r.block.kind) ? i : at), -1);
      let at;
      if (block.kind === 'opening') at = last((k) => k === 'opening') + 1;
      else if (block.kind === 'closing') at = last(() => true) + 1;
      else {
        const body = last((k) => k === 'body');
        const closing = rows.findIndex((r) => r.ticked && r.block.kind === 'closing');
        at = body >= 0 ? body + 1 : closing >= 0 ? closing : last((k) => k === 'opening') + 1;
      }
      rows.splice(at, 0, { block, ticked: true, why: '' });
      missing.splice(writing, 1);
      writing = null;
      const warn = unknownPlaceholdersText(block.unknown_placeholders);
      note = warn ? `<span class="warn-text"><span aria-hidden="true">⚠ </span>${esc(warn)}</span>` : '';
      render();
      panel.querySelector(`[data-focus="pick-${block.id}"]`)?.focus();
      toast('Block saved', 'ok', 2500);
      if (warn) announce(warn);
    } catch (err) {
      setBusy(btn, false);
      $('#cl-nb-status').textContent = `Not saved: ${err.message}`;
      announce(`Not saved: ${err.message}`, true);
    }
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
    <a href="${esc(j.url)}" target="_blank" rel="noopener"><strong>${esc(j.title || '—')}</strong><span class="visually-hidden"> (${esc(sourceLabel(j.source))}, opens in a new tab)</span></a>
    <div>${esc(j.company || '—')}</div>
    <div class="muted">${esc(j.location_text || 'location not stated')}${j.salary_text ? ` · ${esc(j.salary_text)}` : ''}</div>
    <div class="muted">${esc(STATUS_LABEL[j.status] || j.status)}${j.score !== null && j.score !== undefined ? ` · score ${j.score}` : ''} · first seen ${esc(fmtDate(j.first_seen).split(',')[0])}</div>
    <details><summary>Ad text<span class="visually-hidden"> (${esc(sourceLabel(j.source))})</span></summary><div class="dupe-desc" tabindex="0" role="region" aria-label="Ad text, ${esc(sourceLabel(j.source))}">${esc(j.description || '(no description stored)')}</div></details>
  </div>`;
}

function dupePairHtml(p, kind) {
  const buttons = kind === 'suggested'
    ? `<button class="btn btn-primary btn-sm" type="button" data-d="merge">Same job: merge</button>
       <button class="btn btn-sm" type="button" data-d="distinct">Different jobs</button>`
    : `<button class="btn btn-sm" type="button" data-d="confirm">Correct</button>
       <button class="btn btn-sm" type="button" data-d="distinct">Not the same job: split</button>`;
  return `<div class="dupe-pair" data-a="${p.a.id}" data-b="${p.b.id}" role="group" aria-label="${esc(p.a.title || 'Job')}: ${esc(sourceLabel(p.a.source))} and ${esc(sourceLabel(p.b.source))}">
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
    pair.querySelectorAll('button').forEach((b) => setBusy(b, true));
    const section = pair.closest('#dupes-auto') ? '#dupes-auto-heading' : '#dupes-suggested-heading';
    try {
      await decideDuplicate(Number(pair.dataset.a), Number(pair.dataset.b), btn.dataset.d);
      await loadDuplicates();
      renderDupeLists();
      $(section)?.focus(); // the pair with the button is gone
      announce({ merge: 'Merged into one card.', distinct: 'Kept as separate jobs.', confirm: 'Marked as correct.' }[btn.dataset.d] || 'Saved.');
      loadJobs();
    } catch (e) {
      toast(`Could not save that: ${e.message}`, 'err');
      pair.querySelectorAll('button').forEach((b) => setBusy(b, false));
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
        <span id="dupes-status" class="muted" role="status"></span>
      </div>
      <h3 id="dupes-suggested-heading" tabindex="-1">Possible duplicates to check (<span id="dupes-n-suggested">0</span>)</h3>
      <div id="dupes-suggested"></div>
      <h3 id="dupes-auto-heading" tabindex="-1">Merged automatically (<span id="dupes-n-auto">0</span>)</h3>
      <p class="hint">Only merges of jobs in your list are shown. Split any that aren't the same job.</p>
      <button id="dupes-confirm-all" class="btn btn-ghost btn-sm" type="button" hidden>All of these are correct</button>
      <div id="dupes-auto"></div>
    </div>`);
  renderDupeLists();
  $('#dupes-scan').addEventListener('click', async () => {
    const btn = $('#dupes-scan');
    setBusy(btn, true);
    $('#dupes-status').textContent = 'Comparing ads…';
    try {
      const r = await api('/api/duplicates/scan', { method: 'POST' });
      $('#dupes-status').textContent = `Merged ${r.merged} ad${r.merged === 1 ? '' : 's'}; ${r.suggested} possible duplicate${r.suggested === 1 ? '' : 's'} to check.`;
      await loadDuplicates();
      renderDupeLists();
      loadJobs();
    } catch (e) { $('#dupes-status').textContent = `Failed: ${e.message}`; }
    setBusy(btn, false);
  });
  $('#dupes-confirm-all').addEventListener('click', async () => {
    try {
      for (const p of state.dupes.auto_merged) await decideDuplicate(p.a.id, p.b.id, 'confirm');
    } catch (e) { toast(`Could not save that: ${e.message}`, 'err'); }
    await loadDuplicates();
    renderDupeLists();
    $('#dupes-auto-heading').focus(); // the button is hidden once nothing is left to confirm
    announce('Marked the automatic merges as correct.');
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
let chatReturn = null; // finds the control that opened the chat, for focus when it closes

/* Beside the cards on wide screens the chat is a complementary region; on narrower screens it covers the
   page, so there it is a modal dialog: the page behind is inert and Escape closes it (WCAG 2.4.3, 2.4.11). */
const chatCovers = window.matchMedia('(max-width: 1099px)');
function syncChatMode() {
  const panel = $('#chat-panel');
  const modal = !panel.hidden && chatCovers.matches;
  panel.setAttribute('role', modal ? 'dialog' : 'complementary');
  if (modal) panel.setAttribute('aria-modal', 'true'); else panel.removeAttribute('aria-modal');
  ['.skip-link', '.app-header', '#main'].forEach((sel) => { const el = $(sel); if (el) el.inert = modal; });
}

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
  if ($('#chat-panel').hidden) chatReturn = focusFinder(document.activeElement);
  $('#chat-panel').hidden = false;
  document.body.classList.add('chat-open');
  syncChatMode();
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

function closeChat({ restoreFocus = true } = {}) {
  const wasOpen = !$('#chat-panel').hidden;
  const hadFocus = $('#chat-panel').contains(document.activeElement);
  chatToken++;
  state.chat = null;
  $('#chat-panel').hidden = true;
  document.body.classList.remove('chat-open');
  syncChatMode();
  if (wasOpen && restoreFocus && hadFocus) (chatReturn?.() || $('#main')).focus();
  chatReturn = null;
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
  $('#chat-input').placeholder = `Ask anything about ${n === 1 ? 'this job' : 'these jobs'}…`;
  $('#chat-jobs').innerHTML = c.jobs.map((j, i) => `<li><button class="chat-job" type="button" data-id="${j.id}" title="Show this job's card">
    ${n > 1 ? `<strong>${i + 1}</strong> ` : ''}${esc(j.title || '—')} · ${esc(j.company || '—')}<span class="visually-hidden">: show its card</span></button></li>`).join('');
  $$('#chat-jobs .chat-job').forEach((b) => b.addEventListener('click', () => showJobCard(Number(b.dataset.id))));
  const sel = $('#chat-threads');
  const opts = c.threads.map((t) => `<option value="${t.id}" ${t.id === c.threadId ? 'selected' : ''}>${esc(t.title || 'Chat')} · ${esc(fmtDate(t.updated_at).split(',')[0])}</option>`);
  if (!c.threadId) opts.unshift('<option value="" selected>New chat</option>');
  sel.innerHTML = opts.join('');
  sel.disabled = c.busy || c.threads.length === 0;
  // A button that is about to be hidden or disabled hands keyboard focus to the question box.
  const strand = [$('#chat-delete'), $('#chat-new'), $('#chat-send'), sel].includes(document.activeElement)
    || $('#chat-messages').contains(document.activeElement);
  $('#chat-delete').hidden = !c.threadId;
  $('#chat-new').disabled = c.busy || !c.threadId;
  $('#chat-send').disabled = c.busy;
  renderChatMessages();
  if (strand && (!document.activeElement || document.activeElement === document.body || document.activeElement.disabled
      || document.activeElement.closest('[hidden]'))) $('#chat-input').focus();
}

function renderChatMessages() {
  const c = state.chat;
  const box = $('#chat-messages');
  if (!c.messages.length && !c.busy) {
    const starters = CHAT_STARTERS[c.jobIds.length === 1 ? 'one' : 'many'];
    box.innerHTML = `<p class="muted" id="chat-starters-intro">The model sees ${c.jobIds.length === 1 ? 'the ad' : 'each ad'}, your documents, the fit assessment,
      the company info and any cover-letter draft. Try:</p>
      <ul class="chat-starters" aria-labelledby="chat-starters-intro">${starters.map((q) => `<li><button class="btn btn-ghost btn-sm chat-starter" type="button">${esc(q)}</button></li>`).join('')}</ul>`;
    box.querySelectorAll('.chat-starter').forEach((b) => b.addEventListener('click', () => sendChat(b.textContent)));
    return;
  }
  box.innerHTML = c.messages.map((m) => {
    if (m.role === 'user') {
      return `<div class="chat-msg user"><span class="visually-hidden">You asked: </span>${esc(m.content)}${
        m.web_search ? '<div class="chat-meta"><span aria-hidden="true">🌐 </span>with web search</div>' : ''}</div>`;
    }
    const links = m.sources || [];
    const sources = links.length
      ? `<div class="chat-sources"><span class="muted">${links.some((x) => x.cited) ? 'Sources:' : 'Pages the web search found:'}</span>
          ${links.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title || x.url)}</a>`).join(' · ')}</div>` : '';
    return `<div class="chat-msg assistant"><span class="visually-hidden">Answer: </span>${mdToHtml(m.content)}${sources}
      <div class="chat-meta">${esc(m.model || '')}${m.created_at ? ` · ${esc(fmtDate(m.created_at))}` : ''}
        <button class="link chat-copy" type="button" data-id="${m.id}">Copy<span class="visually-hidden"> this answer</span></button></div></div>`;
  }).join('') + (c.busy ? '<div class="chat-msg assistant chat-thinking" aria-hidden="true"><span></span><span></span><span></span></div>' : '');
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
    announce('Answer received: it is the last message in the conversation.');
  } catch (e) {
    if (token !== chatToken) return;
    c.messages.pop();
    if (!$('#chat-input').value) $('#chat-input').value = text; // nothing typed is lost (WCAG 3.3.7)
    setChatStatus(`Failed: ${e.message}`, true);
  }
  c.busy = false;
  renderChat();
}

function showJobCard(id) {
  const card = document.querySelector(`.job-card[data-job-id="${id}"]`);
  if (!card || card.closest('[hidden]')) { toast('That job is hidden by the current filters, or the Jobs tab is closed.', '', 4000); return; }
  if (chatCovers.matches) closeChat({ restoreFocus: false }); // the chat covers the cards on this screen
  card.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
  card.querySelector('.job-title a')?.focus({ preventScroll: true });
  card.classList.remove('flash');
  void card.offsetWidth;
  card.classList.add('flash');
}

function initChat() {
  $('#chat-close').addEventListener('click', () => closeChat());
  $('#chat-panel').addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); closeChat(); }
  });
  chatCovers.addEventListener('change', syncChatMode);
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
  $('#titles-list').innerHTML = titles.length ? `<ul class="titles-ul" aria-label="Suggested job titles">${titles.map((t, i) => `
    <li class="title-row">
      <input type="checkbox" id="title-sugg-${i}" data-i="${i}" aria-describedby="title-sugg-${i}-fit title-sugg-${i}-why" ${t.in_search ? 'disabled' : (t.fit !== 'stretch' ? 'checked' : '')} />
      <label class="title-name" for="title-sugg-${i}">${esc(t.title)}</label>
      <span class="title-badges"><span class="badge fit-${t.fit}" id="title-sugg-${i}-fit">${FIT_LABEL[t.fit] || t.fit}</span>
      ${t.in_search ? '<span class="badge flag">already in this search</span>' : ''}</span>
      <span class="muted title-reason" id="title-sugg-${i}-why">${esc(t.reason)}</span>
    </li>`).join('')}</ul>` : '';
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
    setBusy(btn, true);
    $('#titles-status').textContent = 'Reading your documents… (about 20 seconds)';
    try {
      const r = await api('/api/documents/suggest-titles', { method: 'POST' });
      state.titleSuggestions = r.titles;
      renderTitleSuggestions(r.titles);
      $('#titles-status').textContent = `${r.titles.length} suggestions. Titles are also used as SEEK search terms.`;
    } catch (e) { $('#titles-status').textContent = `Failed: ${e.message}`; }
    setBusy(btn, false);
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

/* ================= STARTING POINTS (Search setup > Commute) ================= */
/* Where the Google Maps links on each job's page start: any number, each a pin or a typed place, for driving, public
   transport or both. Every home pin counts for both without being added (a starting point made from it replaces it).
   They are saved with the rest of the search setup. */
const START_USES = { transit: 'Public transport', driving: 'Driving', both: 'Driving and public transport' };

function startPin(sp) { return sp.pin_id ? state.setupPins.find((p) => p.id === sp.pin_id) : null; }
function startName(sp) {
  const pin = startPin(sp);
  return sp.label || (sp.pin_id ? pin?.label || 'Deleted pin' : sp.place);
}
function startPlace(sp) {
  if (!sp.pin_id) return sp.place;
  const pin = startPin(sp);
  return pin ? `${pin.kind} pin ${pin.label}` : 'a pin that has been deleted';
}

function renderStarts() {
  const list = $('#starts-list');
  if (!list) return;
  const chosen = new Set(state.startingPoints.map((sp) => sp.pin_id).filter(Boolean));
  const homes = state.setupPins.filter((p) => p.kind === 'home' && !chosen.has(p.id));
  $('#starts-home').textContent = homes.length
    ? `Also starting points, for driving and public transport, without being added: your home pin${homes.length > 1 ? 's' : ''} ${homes.map((p) => p.label).join(', ')}.`
    : (state.setupPins.some((p) => p.kind === 'home') ? '' : 'A home pin (Map tab) is a starting point for driving and public transport without being added here.');
  // Redrawn (a pin changed): keep keyboard focus on the same row's control.
  const active = list.contains(document.activeElement) ? document.activeElement : null;
  const back = active ? (active.dataset.startUse !== undefined ? `[data-start-use="${active.dataset.startUse}"]` : `[data-start-remove="${active.dataset.startRemove}"]`) : null;
  list.innerHTML = state.startingPoints.length ? '' : '<li class="muted">No starting points of your own yet.</li>';
  state.startingPoints.forEach((sp, i) => {
    const li = document.createElement('li');
    li.className = 'start-row';
    const name = startName(sp);
    const missing = sp.pin_id && !startPin(sp);
    li.innerHTML = `<span class="start-name"><strong>${esc(name)}</strong>
        <span class="muted">${name === startPlace(sp) ? '' : `from ${esc(startPlace(sp))}`}</span>
        ${missing ? '<span class="err-text">This pin was deleted: remove this starting point or add the place again.</span>' : ''}</span>
      <label class="start-use"><span class="visually-hidden">Used for, ${esc(name)}</span>
        <select data-start-use="${i}">${Object.entries(START_USES).map(([v, t]) => `<option value="${v}"${sp.use === v ? ' selected' : ''}>${t}</option>`).join('')}</select></label>
      <button class="btn btn-ghost btn-sm" type="button" data-start-remove="${i}">Remove<span class="visually-hidden"> ${esc(name)}</span></button>`;
    list.appendChild(li);
  });
  list.querySelectorAll('[data-start-use]').forEach((sel) => sel.addEventListener('change', () => {
    const sp = state.startingPoints[Number(sel.dataset.startUse)];
    sp.use = sel.value;
    announce(`${startName(sp)}: ${START_USES[sp.use]}.`);
    saveSetup();
  }));
  list.querySelectorAll('[data-start-remove]').forEach((b) => b.addEventListener('click', () => {
    const [sp] = state.startingPoints.splice(Number(b.dataset.startRemove), 1);
    renderStarts();
    $('#start-from').focus(); // the row with the button is gone
    announce(`Removed the starting point ${startName(sp)}.`);
    saveSetup();
  }));
  if (back) (list.querySelector(back) || $('#start-from')).focus();
  const from = $('#start-from');
  const picked = from.value;
  from.innerHTML = '<option value="">A place I type</option>'
    + state.setupPins.map((p) => `<option value="${p.id}">Pin: ${esc(p.label)} (${esc(p.kind)})</option>`).join('');
  from.value = state.setupPins.some((p) => String(p.id) === picked) ? picked : '';
  showStartPlace();
}

function showStartPlace() {
  const typed = !$('#start-from').value;
  $('#start-place-group').hidden = !typed;
  if (!typed) setFieldError($('#start-place'), '');
}

function initStarts() {
  $('#start-from').addEventListener('change', () => { setFieldError($('#start-from'), ''); showStartPlace(); });
  $('#start-place').addEventListener('input', () => setFieldError($('#start-place'), ''));
  $('#starts-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const pinId = Number($('#start-from').value) || null;
    const place = $('#start-place').value.trim();
    if (!state.profile) { // saveSetup leaves starting points out until the search setup has loaded
      setFieldError(pinId ? $('#start-from') : $('#start-place'), 'The search setup has not loaded yet. Wait a moment, or reload the page, then add it again.');
      return;
    }
    if (!pinId && !place) {
      setFieldError($('#start-place'), 'Type an address, a station or a stop, or choose a pin under From.');
      return;
    }
    const same = state.startingPoints.find((s) => (pinId ? s.pin_id === pinId : !s.pin_id && s.place.toLowerCase() === place.toLowerCase()));
    if (same) {
      setFieldError(pinId ? $('#start-from') : $('#start-place'), `That is already the starting point ${startName(same)}: change what it is used for in its row.`);
      return;
    }
    setFieldError($('#start-from'), '');
    const sp = { label: $('#start-label').value.trim(), pin_id: pinId, place: pinId ? null : place, use: $('#start-use').value };
    state.startingPoints.push(sp);
    $('#start-place').value = '';
    $('#start-label').value = '';
    $('#start-from').value = '';
    renderStarts();
    announce(`Added the starting point ${startName(sp)}, for ${START_USES[sp.use].toLowerCase()}.`);
    saveSetup();
  });
}

/* ================= SEARCH SETUP ================= */
function tagWrap(field) { return document.querySelector(`.tag-input[data-field="${field}"] .tags`); }
function getTags(field) { return Array.from(tagWrap(field).querySelectorAll('.tag')).map((t) => t.dataset.value); }
function setTags(field, values) { tagWrap(field).innerHTML = ''; (values || []).forEach((v) => addTag(field, v, false)); }

// Tags are list items with a remove button. Adding or removing one is announced, and removing one with its
// button puts focus back in the field (the button is gone).
function addTag(field, value, save = true) {
  value = String(value || '').trim();
  if (!value) return;
  if (getTags(field).some((v) => v.toLowerCase() === value.toLowerCase())) {
    if (save) announce(`${value} is already in the list.`);
    return;
  }
  const el = document.createElement('li');
  el.className = 'tag';
  el.dataset.value = value;
  el.innerHTML = `<span>${esc(value)}</span><button type="button" aria-label="Remove ${esc(value)}"><span aria-hidden="true">×</span></button>`;
  el.querySelector('button').addEventListener('click', () => {
    el.remove();
    document.querySelector(`.tag-input[data-field="${field}"] input`).focus();
    announce(`Removed ${value}.`);
    saveSetup();
  });
  tagWrap(field).appendChild(el);
  if (save) { announce(`Added ${value}.`); saveSetup(); }
}

async function loadSetup() {
  try {
    const [profile, dealbreakers] = await Promise.all([api('/api/profile'), api('/api/dealbreakers')]);
    state.profile = profile;
    ['titles', 'keywords_include', 'keywords_exclude', 'seek_locations'].forEach((f) => setTags(f, profile[f]));
    state.startingPoints = profile.starting_points || [];
    try { state.setupPins = await api('/api/pins'); } catch { state.setupPins = state.pins; }
    renderStarts();
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
    showError($('#search-error'), `Could not load search setup: ${e.message}. Reload the page to try again.`);
  }
}

const saveSetup = debounce(async () => {
  const status = $('#search-save-status');
  const floorInput = $('#salary-floor');
  if (!floorInput.validity.valid) { // e.g. a negative number or letters: say so and don't save it
    setFieldError(floorInput, 'Enter the salary as a whole number of dollars a year, 0 or more (for example 140000), or leave it empty.', { focus: false });
    return;
  }
  setFieldError(floorInput, '');
  status.textContent = 'Saving…';
  const floor = floorInput.value;
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
    // (left out until the search setup has loaded: the saved ones are then kept as they are)
    ...(state.profile ? { starting_points: state.startingPoints } : {}),
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
    loadSchedule({ controls: false }); // titles or SEEK may have changed whether there is anything to search
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
        const last = wrap.querySelector('.tags .tag:last-child');
        if (!last) return;
        last.remove();
        announce(`Removed ${last.dataset.value}.`);
        saveSetup();
      }
    });
    input.addEventListener('blur', () => { if (input.value.trim()) { addTag(field, input.value); input.value = ''; } });
  });
  ['#salary-floor'].forEach((s) => $(s).addEventListener('input', saveSetup));
  ['#remote-aus', '#remote-global', '#allow-hybrid', '#allow-onsite', '#seek-enabled', '#max-office-days'].forEach((s) => $(s).addEventListener('change', saveSetup));
  initStarts();
  $('#search-form').addEventListener('submit', (e) => e.preventDefault());
  $('#import-form').addEventListener('submit', importPages);
  $('#import-input').addEventListener('change', () => setFieldError($('#import-input'), ''));
}

async function importPages(e) {
  e.preventDefault();
  const files = Array.from($('#import-input').files);
  if (!files.length) { setFieldError($('#import-input'), 'Choose one or more pages you saved from SEEK (.html) first.'); return; }
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
  status.textContent = `Imported ${total.imported} job(s): ${total.new} new, ${total.kept} in your list`;
  $('#import-input').value = '';
  loadJobs();
}

/* ---------- "Add a job": pages the person saved in their browser ----------
   Each file goes to POST /api/import/page on its own. A page of one job is the person's own pick, so it is listed
   whatever their filters say (its ⚠️ says what it doesn't meet); a page of search results is filtered like a search.
   Each file gets a line saying what came of it, and the status line counts them. */
function showAddJob() {
  const mac = /Mac/i.test(navigator.platform);
  openHtmlModal('Add a job from its saved page', `
    <form id="add-job-form" class="add-job" novalidate>
      <p>A job you add yourself is always in your list, even when it doesn't meet your search filters. A
        <span aria-hidden="true">⚠️</span><span class="visually-hidden">warning sign</span> under its score then says which ones.</p>
      <ol>
        <li>Open the job ad in your browser and wait until it has fully loaded.</li>
        <li>Save the page: press <kbd>${mac ? 'Cmd' : 'Ctrl'}</kbd>+<kbd>S</kbd> (any "Webpage" format works).</li>
        <li><label for="add-job-file">Choose the saved page here (or several):</label></li>
      </ol>
      <input id="add-job-file" type="file" accept=".html,.htm,.mhtml,.mht,text/html" multiple />
      <div class="form-group add-job-url">
        <label for="add-job-url">Address of the ad <span class="muted">(only if JobHuntPA asks for it)</span></label>
        <input id="add-job-url" type="url" inputmode="url" autocomplete="off" placeholder="https://" aria-describedby="add-job-url-hint" />
        <small id="add-job-url-hint" class="hint">Saved pages usually say where they came from. For one that doesn't, copy the ad's address from your browser's address bar, choose that page on its own and add it again.</small>
      </div>
      <button class="btn btn-primary" type="submit">Add</button>
      <p id="add-job-status" class="muted" role="status"></p>
      <ul id="add-job-results" class="add-job-results"></ul>
      <p class="hint">The browser extension (Settings) adds the job in the tab you have open in one click, without saving the page.</p>
    </form>`);
  $('#add-job-file').addEventListener('change', () => setFieldError($('#add-job-file'), ''));
  $('#add-job-url').addEventListener('input', () => setFieldError($('#add-job-url'), ''));
  $('#add-job-form').addEventListener('submit', addJobPages);
}

function addJobResult(name, r) {
  if (r.jobs.length !== 1) {
    return `<span aria-hidden="true">✓ </span>${esc(name)}: ${r.imported} jobs read, ${r.new} new, ${r.kept} in your list. A page of search results is filtered like a search.`;
  }
  const j = r.jobs[0];
  const what = `${esc(j.title || 'Job')}${j.company ? ` at ${esc(j.company)}` : ''}`;
  const warn = (j.filter_warnings || []).length
    ? ` <span class="warn-text"><span aria-hidden="true">⚠️ </span>${esc(filterWarnWords(j))}: ${esc(j.filter_warnings.join('; '))}.</span>` : '';
  return `<span aria-hidden="true">✓ </span>${j.created ? 'Added' : 'Already in your list, brought up to date'}: <a href="${esc(j.link)}" target="_blank" rel="noopener">${what}${NEW_TAB}</a>.${warn}`;
}

async function addJobPages(e) {
  e.preventDefault();
  const input = $('#add-job-file');
  const files = Array.from(input.files);
  if (!files.length) { setFieldError(input, 'Choose the page you saved (.html or .mhtml) first.'); return; }
  const typed = $('#add-job-url').value.trim();
  if (typed && files.length > 1) { setFieldError($('#add-job-url'), 'An address belongs to one page: choose only that page, then add it.'); return; }
  const status = $('#add-job-status');
  const results = $('#add-job-results');
  const button = $('#add-job-form button[type="submit"]');
  results.innerHTML = '';
  button.disabled = true;
  let added = 0, updated = 0, failed = 0;
  for (const f of files) {
    setText(status, `Adding ${f.name}…`);
    const fd = new FormData();
    fd.append('file', f, f.name);
    if (typed) fd.append('url', typed);
    const li = document.createElement('li');
    try {
      const r = await api('/api/import/page', { method: 'POST', body: fd });
      li.innerHTML = addJobResult(f.name, r);
      added += r.new;
      updated += r.imported - r.new;
    } catch (err) {
      failed += 1;
      li.className = 'err-text';
      li.innerHTML = `<span aria-hidden="true">✕ </span>${esc(f.name)}: ${esc(err.message)}`;
    }
    if (!results.isConnected) break; // the dialog was closed: the other pages aren't sent
    results.appendChild(li);
  }
  if (results.isConnected) {
    button.disabled = false;
    setText(status, `Done: ${added} added, ${updated} brought up to date${failed ? `, ${failed} not added (see below)` : ''}.`);
    input.value = '';
  }
  if (added || updated) loadJobs();
}

/* ---------- scheduled searches ---------- */
// The schedule has its own endpoint (and needs no refilter), so it is saved apart from saveSetup().
const SCHEDULE_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

function scheduleTimes() { return $$('#sched-times .tag').map((t) => t.dataset.value); }

function renderScheduleTimes(times) {
  const sorted = [...new Set(times)].sort(); // "HH:MM" sorts as time
  $('#sched-times').innerHTML = sorted.length
    ? sorted.map((t) => `<li class="tag" data-value="${esc(t)}"><span>${esc(t)}</span><button type="button" aria-label="Remove ${esc(t)}"><span aria-hidden="true">×</span></button></li>`).join('')
    : '<li class="muted">No times yet</li>';
}

function renderScheduleControls(sch) {
  $('#sched-enabled').checked = sch.enabled;
  renderScheduleTimes(sch.times);
  $$('.sched-day').forEach((cb) => { cb.checked = sch.days.includes(Number(cb.value)); });
  $('#sched-holidays').checked = sch.skip_holidays;
  $('#sched-state').innerHTML = sch.states.map((st) => `<option value="${esc(st.code)}">${esc(st.name)} (${esc(st.code)})</option>`).join('');
  $('#sched-state').value = sch.state;
}

function dateParts(opts, date) {
  const parts = new Intl.DateTimeFormat('en-AU', opts).formatToParts(date);
  return Object.fromEntries(parts.map((x) => [x.type, x.value]));
}

// "Tue 6 Oct, 10:00" in the schedule's own time zone (the state's), whatever zone this browser is in.
function fmtScheduleTime(iso, timeZone) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const opts = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
  let p;
  try { p = dateParts({ ...opts, timeZone }, d); } catch { p = dateParts(opts, d); } // unknown zone name
  return `${p.weekday} ${p.day} ${p.month}, ${p.hour}:${p.minute}`;
}

// A public holiday's date (no time or zone) as "Mon 5 Oct".
function fmtHolidayDate(ymd) {
  const d = new Date(`${ymd}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return String(ymd);
  const p = dateParts({ weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' }, d);
  return `${p.weekday} ${p.day} ${p.month}`;
}

const zoneLabel = (tz) => `${String(tz || '').split('/').pop().replace(/_/g, ' ')} time`;

function scheduledRunLine(run) {
  if (!run) return '<p class="muted">Last scheduled search: none yet.</p>';
  if (run.state === 'queued' || run.state === 'running') return '<p class="muted">Last scheduled search: running now…</p>';
  const when = fmtDate(run.finished_at || run.started_at);
  if (run.state === 'failed') return `<p class="muted">Last scheduled search: ${esc(when)} · <span class="err-text">failed: ${esc(run.error || 'unknown error')}</span></p>`;
  return `<p class="muted">Last scheduled search: ${esc([when, runSummaryBits(run)].filter(Boolean).join(' · '))}</p>`;
}

// The status lines under the schedule settings, and the "next search" note on the Jobs tab.
function renderScheduleStatus(sch) {
  const lines = [];
  if (!sch.enabled) lines.push('<p class="muted">Automatic searches are off.</p>');
  else if (sch.background === false || sch.skipped) { // no usable key: every slot is passed over (backend/schedule.py), so say so (Phase G)
    const when = sch.skipped?.slot ? ` (${fmtScheduleTime(sch.skipped.slot, sch.timezone)})` : '';
    const again = sch.skipped?.reason === 'key_unusable'; // the server holds a key it can't open: off and on fixes it
    lines.push(`<p class="sched-warn">${esc(sch.skipped ? `${sch.skipped.text}${when}.` : 'Scheduled searches are skipped while background access is off.')} <button class="link" type="button" data-focus-background>${again ? 'Turn it off and on again' : 'Turn on background access'}</button></p>`);
  }
  else if (!sch.times.length || !sch.days.length) lines.push('<p class="muted">Add at least one time and one day: nothing is scheduled yet.</p>');
  else if (sch.next_run) lines.push(`<p>Next search: <strong>${esc(fmtScheduleTime(sch.next_run, sch.timezone))}</strong> <span class="muted">(${esc(zoneLabel(sch.timezone))})</span></p>`);
  else lines.push('<p class="muted">No upcoming search found.</p>');
  if (sch.enabled && sch.skipped_holidays?.length) {
    lines.push(`<p class="muted">Skipping: ${sch.skipped_holidays.map((h) => `${esc(h.name)} (${esc(fmtHolidayDate(h.date))})`).join(', ')}</p>`);
  }
  lines.push(scheduledRunLine(sch.last_run));
  if (sch.nothing_to_search) lines.push('<p class="sched-warn">Nothing to search yet, so scheduled searches are skipped. Turn on SEEK and add target job titles, or add a company source, in Search setup.</p>');
  $('#sched-status').innerHTML = lines.join('');

  const note = $('#next-scheduled');
  const show = sch.enabled && sch.next_run && !sch.nothing_to_search && sch.background !== false && !sch.skipped;
  note.hidden = !show;
  if (show) {
    const away = Intl.DateTimeFormat().resolvedOptions().timeZone !== sch.timezone; // name the zone only when it isn't this browser's
    note.textContent = `Next scheduled search: ${fmtScheduleTime(sch.next_run, sch.timezone)}${away ? ` (${zoneLabel(sch.timezone)})` : ''}`;
  }
}

// controls: also fill in the settings (when loading); false refreshes only the status, so edits in progress stay.
async function loadSchedule({ controls = true } = {}) {
  const ws = state.ws;
  const first = !state.schedule;
  try {
    const sch = await api('/api/schedule');
    if (ws !== state.ws) return; // switched workspace meanwhile
    state.schedule = sch;
    if (controls || first) renderScheduleControls(sch);
    renderScheduleStatus(sch);
  } catch (e) {
    if (!controls) return; // a failed status refresh keeps the last status
    $('#sched-status').innerHTML = `<p class="err-text">Could not load the schedule: ${esc(e.message)}</p>`;
    announce(`Could not load the schedule: ${e.message}`, true);
    $('#next-scheduled').hidden = true;
  }
}

function readSchedule() {
  return {
    enabled: $('#sched-enabled').checked,
    times: scheduleTimes(),
    days: $$('.sched-day:checked').map((cb) => Number(cb.value)),
    skip_holidays: $('#sched-holidays').checked,
    state: $('#sched-state').value,
  };
}

// The settings are captured when edited, so a quick workspace switch can't send them to the wrong workspace.
let schedulePending = null;
const flushSchedule = debounce(async () => {
  const pending = schedulePending;
  schedulePending = null;
  if (!pending) return;
  const status = $('#sched-save-status');
  status.textContent = 'Saving…';
  try {
    const sch = await api('/api/schedule', { method: 'PUT', headers: { 'X-Workspace': String(pending.ws) }, body: JSON.stringify(pending.body) });
    status.textContent = `Saved ${new Date().toLocaleTimeString()}`;
    if (pending.ws === state.ws) { state.schedule = sch; renderScheduleStatus(sch); }
  } catch (e) {
    status.textContent = 'Save failed';
    toast(`Saving the schedule failed: ${e.message}`, 'err');
  }
}, 500);

function scheduleChanged() {
  if (!state.schedule) { toast("The schedule hasn't loaded, so it can't be saved.", 'err'); return; }
  schedulePending = { ws: state.ws, body: readSchedule() };
  flushSchedule();
}

function addScheduleTime() {
  const input = $('#sched-time-input');
  const time = input.value;
  if (!SCHEDULE_TIME_RE.test(time)) { setFieldError(input, 'Pick a time first: hours and minutes, for example 09:00.'); return; }
  setFieldError(input, '');
  const times = scheduleTimes();
  if (times.includes(time)) { announce(`${time} is already in the list.`); return; }
  renderScheduleTimes([...times, time]);
  announce(`Added ${time}.`);
  scheduleChanged();
}

function initSchedule() {
  ['#sched-enabled', '#sched-holidays', '#sched-state', '#sched-days'].forEach((s) => $(s).addEventListener('change', scheduleChanged));
  $('#sched-time-add').addEventListener('click', addScheduleTime);
  $('#sched-time-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addScheduleTime(); } });
  $('#sched-times').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const tag = btn.closest('.tag');
    tag.remove();
    renderScheduleTimes(scheduleTimes());
    $('#sched-time-input').focus(); // the button is gone
    announce(`Removed ${tag.dataset.value}.`);
    scheduleChanged();
  });
}

/* ---------- sources ---------- */
async function loadSources() {
  try {
    const sources = await api('/api/sources');
    renderSources(sources);
    $('#sources-error').hidden = true;
    if (typeof refreshBrowserOffer === 'function') refreshBrowserOffer(); // (browser.js)
  } catch (e) {
    showError($('#sources-error'), `Could not load sources: ${e.message}`);
  }
}

function renderSources(sources) {
  const box = $('#sources-list');
  const focusBack = box.contains(document.activeElement) ? focusFinder(document.activeElement) : null;
  box.innerHTML = sources.length ? '' : '<div class="empty">No company sources yet. Add a careers page above.</div>';
  const list = document.createElement('ul');
  list.className = 'sources-ul';
  list.setAttribute('aria-label', 'Company sources');
  if (sources.length) box.appendChild(list);
  sources.forEach((src) => {
    const row = document.createElement('li');
    row.className = 'doc-row source-row';
    const last = src.last_run
      ? `${src.last_error ? `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(src.last_error)}</span>` : `<span aria-hidden="true">✓ </span>${src.last_count} listed`} · ${esc(fmtDate(src.last_run))}${src.last_method ? `<br>${esc(src.last_method)}` : ''}`
      : 'not run yet';
    const name = esc(src.label || src.url);
    row.innerHTML = `
      <input type="checkbox" id="src-${src.id}-on" data-act="enabled" aria-label="Search ${name}" ${src.enabled ? 'checked' : ''} />
      <div class="source-main">
        <input class="source-label" id="src-${src.id}-label" data-act="label" type="text" value="${esc(src.label)}" aria-label="Company name for ${esc(src.url)}" autocomplete="off" />
        <a class="doc-meta" href="${esc(src.url)}" target="_blank" rel="noopener">${esc(src.url)}</a>
        <div class="doc-meta">${last}</div>
        ${src.mapping ? '<div class="doc-meta">Read with a page layout the AI worked out (from the browser extension)</div>' : ''}
        <div class="doc-meta test-result" role="status"></div>
      </div>
      <button class="btn btn-ghost btn-sm" id="src-${src.id}-test" data-act="test" type="button">Test<span class="visually-hidden"> ${name}</span></button>
      ${src.mapping ? `<button class="btn btn-ghost btn-sm" id="src-${src.id}-forget" data-act="forget" type="button">Forget layout<span class="visually-hidden"> of ${name}</span></button>` : ''}
      <button class="btn btn-danger btn-sm" id="src-${src.id}-delete" data-act="delete" type="button">Delete<span class="visually-hidden"> ${name}</span></button>`;
    row.querySelector('input[type="checkbox"]').addEventListener('change', (e) => patchSource(src.id, { enabled: e.target.checked }));
    row.querySelector('.source-label').addEventListener('change', (e) => patchSource(src.id, { label: e.target.value }));
    row.querySelector('[data-act="test"]').addEventListener('click', (e) => testSource(src, e.currentTarget, row.querySelector('.test-result')));
    row.querySelector('[data-act="delete"]').addEventListener('click', () => deleteSource(src));
    row.querySelector('[data-act="forget"]')?.addEventListener('click', () => forgetLayout(src));
    list.appendChild(row);
  });
  if (focusBack) (focusBack() || $('#source-url')).focus();
}

async function addSource(e) {
  e?.preventDefault();
  const input = $('#source-url');
  const url = input.value.trim();
  if (!url) { setFieldError(input, 'Paste the address of the careers page, for example https://company.com/careers.'); return; }
  setFieldError(input, '');
  try {
    await api('/api/sources', { method: 'POST', body: JSON.stringify({ url, label: $('#source-label').value.trim() || null }) });
    input.value = '';
    $('#source-label').value = '';
    announce('Source added: it is at the end of the list below.');
    loadSources();
    loadSchedule({ controls: false });
  } catch (err) { setFieldError(input, `Not added: ${err.message}`); }
}

async function patchSource(id, body) {
  try { await api(`/api/sources/${id}`, { method: 'PATCH', body: JSON.stringify(body) }); toast('Source updated', 'ok', 2000); loadSchedule({ controls: false }); }
  catch (e) { toast(`Update failed: ${e.message}`, 'err'); loadSources(); }
}

async function forgetLayout(src) {
  try {
    await api(`/api/sources/${src.id}`, { method: 'PATCH', body: JSON.stringify({ mapping: null }) });
    announce(`${src.label || src.url} is read the usual ways again.`);
    await loadSources();
    // The Forget layout button is gone with the layout: focus stays on the same source's row, at its Test button.
    document.getElementById(`src-${src.id}-test`)?.focus();
  } catch (e) { toast(`Couldn't forget the layout: ${e.message}`, 'err'); }
}

async function deleteSource(src) {
  if (!confirm(`Delete source "${src.label}"? Its jobs stay in the list.`)) return;
  try {
    await api(`/api/sources/${src.id}`, { method: 'DELETE' });
    announce(`Deleted ${src.label || src.url}.`);
    await loadSources();
    loadSchedule({ controls: false });
  } catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
}

async function testSource(src, btn, out) {
  setBusy(btn, true);
  out.textContent = 'Testing (this can take a minute: crawl delays apply)…';
  try {
    const r = await api(`/api/sources/${src.id}/test`, { method: 'POST' });
    out.innerHTML = r.ok
      ? `<span aria-hidden="true">✓ </span>${r.count} postings via ${esc(r.method)}<br>${r.sample.map((s) => `• ${esc(s.title)}${s.location ? ` (${esc(s.location)})` : ''}`).join('<br>')}`
      : `<span class="err-text"><span aria-hidden="true">✕ </span>${esc(r.error)}</span>`;
    if ((r.notes || []).length) out.innerHTML += `<br><span class="muted">${r.notes.map(esc).join('<br>')}</span>`;
  } catch (e) { out.innerHTML = `<span class="err-text"><span aria-hidden="true">✕ </span>Test failed: ${esc(e.message)}</span>`; }
  setBusy(btn, false);
  if (typeof refreshBrowserOffer === 'function') refreshBrowserOffer(); // a JavaScript-only page asks for the browser
}

/* ---------- scorer ---------- */
async function loadScorerStatus() {
  const badge = $('#scorer-status');
  try {
    const s = await api('/api/score/status');
    const failing = s.configured && s.scored === 0 && s.errors > 0;
    badge.className = `api-status ${!s.configured || failing ? 'is-fail' : s.scored ? 'is-ok' : 'is-unknown'}`;
    badge.innerHTML = `<span aria-hidden="true">● </span>${esc(!s.configured ? 'AI not set up (Settings)'
      : failing ? 'scorer: failing (Settings, Test connection)'
        : `${s.model}${s.scored ? '' : ' (not verified yet)'}`)}`;
    badge.title = `${s.scored} scored, ${s.errors} failed`;
    $('#scorer-info').innerHTML = `
      ${s.problem ? `<span class="err-text">${esc(s.problem)}</span><br>` : ''}
      Model <strong>${esc(s.model || '(not set)')}</strong> at ${esc(s.base_url || '(no address set)')}${s.reasoning_effort ? ` · reasoning effort ${esc(s.reasoning_effort)}` : ''}<br>
      ${s.scored} scored · ${s.pending} waiting to be scored · ${s.stale} stale (documents changed) · ${s.errors} failed ·
      profile ${s.profile_chars.toLocaleString()} characters`;
  } catch (e) {
    badge.className = 'api-status is-fail';
    badge.innerHTML = '<span aria-hidden="true">● </span>backend unreachable';
  }
}

/* ================= DOCUMENTS ================= */
// Uploaded files, notes written here and summaries the AI wrote. Each row opens into a panel to read and edit
// the text and its concise version. Unsaved edits live in state.docs.drafts, so a row can be redrawn (after a
// save, a tick, a new concise version) without losing what's typed in another, and focus returns to the
// control that had it (data-focus). Status lines are kept in state too and announced through #docs-live.
const DOC_KINDS = [['resume', 'Resume / CV'], ['cover_letter', 'Cover letter'], ['preferences', 'Preferences'], ['other', 'Other/supporting']];
// Ticked documents coming to more than this (the text the AI gets: each one's sent_len, which is
// Document.text_for_llm) are a lot to send with every request: a warning, never a limit.
const DOCS_WARN_CHARS = 50000;
const DOC_SOURCE = { file: 'File', note: 'Note', summary: 'Summary' };
const KEEP_BOTH_KEY = 'jobhunt.summaryKeepBoth'; // summaries the user chose to keep ticked with their originals

const docById = (id) => state.docs.list.find((d) => d.id === id);
const docKindOptions = (selected) => DOC_KINDS.map(([v, l]) => `<option value="${v}"${v === selected ? ' selected' : ''}>${esc(l)}</option>`).join('');
const charCount = (n) => `${Number(n || 0).toLocaleString()} characters`;
const docDraft = (id, field, saved) => (state.docs.drafts.has(`${id}:${field}`) ? state.docs.drafts.get(`${id}:${field}`) : saved);
// A document's metadata, without the texts some answers carry along.
const docMeta = ({ text: _t, concise_text: _c, sources: _s, ...meta }) => meta;

function replaceDoc(doc) {
  const meta = docMeta(doc);
  state.docs.list = state.docs.list.some((d) => d.id === meta.id)
    ? state.docs.list.map((d) => (d.id === meta.id ? meta : d)) : [meta, ...state.docs.list];
}

function announceDocs(text) {
  const live = $('#docs-live');
  live.textContent = '';
  setTimeout(() => { live.textContent = text; }, 60); // cleared first, so the same message is announced again
}

function setDocStatus(id, area, text, isError = false) {
  state.docs.status.set(`${id}:${area}`, { text, isError });
  const el = document.getElementById(`doc-${id}-${area}-status`);
  if (el) { el.textContent = text; el.className = isError ? 'err-text' : 'muted'; }
  announceDocs(text);
}

function docStatusHtml(id, area) {
  const s = state.docs.status.get(`${id}:${area}`);
  return `<span id="doc-${id}-${area}-status" class="${s?.isError ? 'err-text' : 'muted'}">${esc(s?.text || '')}</span>`;
}

function rememberFocus(root) {
  const el = document.activeElement;
  if (!el || !root.contains(el) || !el.dataset.focus) return null;
  const typed = el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && el.type === 'text');
  return { key: el.dataset.focus, start: typed ? el.selectionStart : null, end: typed ? el.selectionEnd : null, scroll: el.scrollTop };
}

function restoreFocus(root, saved) {
  if (!saved) return;
  const el = Array.from(root.querySelectorAll('[data-focus]')).find((x) => x.dataset.focus === saved.key);
  if (!el) return;
  el.focus({ preventScroll: true });
  if (saved.start !== null && el.setSelectionRange) el.setSelectionRange(saved.start, saved.end);
  el.scrollTop = saved.scroll;
}

async function loadDocs() {
  if (state.docs.ws !== state.ws) { // another workspace: forget what was open, typed or said
    Object.assign(state.docs, { ws: state.ws, list: [], open: new Set(), text: new Map(), drafts: new Map(), status: new Map(), busy: new Set() });
  }
  const ws = state.ws;
  try {
    const list = await api('/api/documents');
    if (ws !== state.ws) return;
    state.docs.list = list;
    renderDocs();
    $('#docs-error').hidden = true;
  } catch (e) {
    showError($('#docs-error'), `Could not load documents: ${e.message}`);
  }
}

function renderDocs() {
  const list = $('#docs-list');
  const focus = rememberFocus(list);
  list.replaceChildren(...state.docs.list.map(docRow));
  $('#docs-empty').hidden = state.docs.list.length !== 0;
  restoreFocus(list, focus);
  renderSummarySuggestion();
  renderDocsSize();
}

/* The warning above Create summary when the ticked documents come to more than DOCS_WARN_CHARS characters, counting
   the version the AI gets. Said once by screen readers when it appears on the open tab (not on every redraw). */
function renderDocsSize() {
  const box = $('#docs-size-warning');
  const total = state.docs.list.filter((d) => d.use_for_scoring).reduce((n, d) => n + Number(d.sent_len || 0), 0);
  const over = total > DOCS_WARN_CHARS;
  if (over) {
    const text = `Your ticked documents come to ${total.toLocaleString()} characters (the versions the AI gets). That is a lot to send with every request to the AI. Create summary merges them into one shorter document.`;
    if (box.dataset.text !== text) {
      box.dataset.text = text;
      box.innerHTML = `<p><strong>Long documents:</strong> ${esc(text)}</p>
        <button id="docs-size-go" class="btn btn-sm" type="button">Go to Create summary</button>`;
      box.querySelector('#docs-size-go').addEventListener('click', () => {
        const btn = $('#summary-make');
        btn.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
        btn.focus({ preventScroll: true });
      });
    }
  }
  if (over && box.hidden && !$('#tab-docs').hidden) announce(`Long documents: your ticked documents come to ${total.toLocaleString()} characters. Create summary can merge them into one shorter document.`);
  box.hidden = !over;
}

function redrawDoc(id) {
  const old = document.getElementById(`doc-${id}`);
  const doc = docById(id);
  if (!old || !doc) { renderDocs(); return; }
  const focus = rememberFocus(old);
  const row = docRow(doc);
  old.replaceWith(row);
  restoreFocus(row, focus);
  renderSummarySuggestion();
  renderDocsSize();
}

function docRow(doc) {
  const id = doc.id;
  const open = state.docs.open.has(id);
  const meta = [DOC_SOURCE[doc.source] || 'File', charCount(doc.text_len)];
  if (doc.use_concise) meta.push(`the AI gets its concise version (${charCount(doc.concise_len)})`);
  else if (doc.concise_len) meta.push('has a concise version (not used)');
  meta.push(fmtDate(doc.updated_at || doc.created_at));
  const sources = (doc.summary_of || []).map(docById).filter(Boolean);
  const madeFrom = doc.source === 'summary' && (doc.summary_of || []).length
    ? `<p class="doc-meta">Made from ${sources.length ? sources.map((d) => esc(d.filename)).join(', ') : ''}${
      sources.length < doc.summary_of.length ? `${sources.length ? ' and ' : ''}${doc.summary_of.length - sources.length} deleted document(s)` : ''}</p>`
    : '';
  const row = document.createElement('article');
  row.className = 'doc-item';
  row.id = `doc-${id}`;
  row.setAttribute('aria-labelledby', `doc-${id}-name`);
  row.innerHTML = `
    <div class="doc-head">
      <div class="doc-title">
        <h4 class="doc-name" id="doc-${id}-name">${esc(doc.filename)}</h4>
        <p class="doc-meta">${esc(meta.join(' · '))}</p>
        ${madeFrom}
      </div>
      <div class="doc-controls">
        <label class="visually-hidden" for="doc-${id}-kind">Type of ${esc(doc.filename)}</label>
        <select id="doc-${id}-kind" data-act="kind" data-focus="${id}-kind">${docKindOptions(doc.kind)}</select>
        <label class="check"><input type="checkbox" data-act="use" data-focus="${id}-use" aria-describedby="doc-${id}-name"${doc.use_for_scoring ? ' checked' : ''} /> use for scoring</label>
        <button class="btn btn-sm" type="button" data-act="toggle" data-focus="${id}-toggle" aria-expanded="${open}" aria-controls="doc-${id}-panel">${open ? 'Close' : 'Open'}<span class="visually-hidden"> ${esc(doc.filename)}</span></button>
        <button class="btn btn-danger btn-sm" type="button" data-act="delete" data-focus="${id}-delete">Delete<span class="visually-hidden"> ${esc(doc.filename)}</span></button>
      </div>
    </div>
    <div class="doc-panel" id="doc-${id}-panel"${open ? '' : ' hidden'}>${open ? docPanelHtml(doc) : ''}</div>`;
  row.addEventListener('input', (e) => {
    const field = e.target.dataset.draft;
    if (field) state.docs.drafts.set(`${id}:${field}`, e.target.value);
  });
  row.addEventListener('change', (e) => {
    const act = e.target.dataset.act;
    if (act === 'kind') patchDoc(id, { kind: e.target.value });
    else if (act === 'use') patchDoc(id, { use_for_scoring: e.target.checked });
    else if (act === 'version') setDocVersion(id, e.target.value === 'concise');
  });
  row.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn || btn.getAttribute('aria-disabled') === 'true') return;
    const actions = { toggle: toggleDoc, delete: deleteDoc, save: saveDoc, 'concise-make': makeConcise, 'concise-save': saveConcise };
    actions[btn.dataset.act]?.(id);
  });
  return row;
}

function docPanelHtml(doc) {
  const id = doc.id;
  const t = state.docs.text.get(id);
  if (!t) return '<p class="muted">Loading the text…</p>';
  const busy = state.docs.busy.has(`${id}:concise-make`);
  const textLabel = doc.editable ? 'Text' : 'Text taken from the file (read only; to change what the AI reads, edit a concise version below)';
  const conciseBy = doc.concise_model ? `<p class="hint">Concise version written by ${esc(doc.concise_model)}, ${esc(fmtDate(doc.concise_at))}.</p>` : '';
  return `
    <div class="doc-edit">
      <div class="field"><label class="field-label" for="doc-${id}-title">${doc.source === 'file' ? 'Name' : 'Title'}</label>
        <input id="doc-${id}-title" type="text" maxlength="200" autocomplete="off" data-draft="title" data-focus="${id}-title" value="${esc(docDraft(id, 'title', doc.filename))}" /></div>
      <div class="field"><label class="field-label" for="doc-${id}-text">${esc(textLabel)}</label>
        <textarea id="doc-${id}-text" class="doc-textarea" rows="12" data-focus="${id}-text"${doc.editable ? ' data-draft="text"' : ' readonly'}>${esc(doc.editable ? docDraft(id, 'text', t.text) : t.text)}</textarea></div>
      <div class="toolbar-row doc-actions">
        <button class="btn btn-primary btn-sm" type="button" data-act="save" data-focus="${id}-save">${doc.editable ? 'Save' : 'Save name'}</button>
        ${docStatusHtml(id, 'main')}
      </div>
    </div>
    <section class="doc-concise" aria-labelledby="doc-${id}-concise-heading">
      <h5 id="doc-${id}-concise-heading">Concise version</h5>
      <p class="hint">A shorter rewrite by the AI that keeps every employer, date, number and qualification, so every request is smaller.
        Check it against the original (the scorer quotes it as evidence) and edit it freely.</p>
      <fieldset class="version-pick">
        <legend>The AI gets</legend>
        <label class="check"><input type="radio" name="doc-${id}-version" value="original" data-act="version" data-focus="${id}-v-original"${doc.use_concise ? '' : ' checked'} /> the original (${charCount(doc.text_len)})</label>
        <label class="check"><input type="radio" name="doc-${id}-version" value="concise" data-act="version" data-focus="${id}-v-concise"${doc.use_concise ? ' checked' : ''}${doc.concise_len ? '' : ' disabled'} /> the concise version (${doc.concise_len ? charCount(doc.concise_len) : 'none yet'})</label>
      </fieldset>
      <div class="field"><label class="field-label" for="doc-${id}-concise">Concise text</label>
        <textarea id="doc-${id}-concise" class="doc-textarea" rows="10" data-draft="concise" data-focus="${id}-concise" placeholder="None yet: Create concise version writes one, or type your own.">${esc(docDraft(id, 'concise', t.concise_text))}</textarea></div>
      <div class="toolbar-row doc-actions">
        <button class="btn btn-sm" type="button" data-act="concise-make" data-focus="${id}-concise-make"${busy ? ' aria-disabled="true"' : ''}>${busy ? 'Writing…' : t.concise_text ? 'Write it again' : 'Create concise version'}</button>
        <button class="btn btn-sm" type="button" data-act="concise-save" data-focus="${id}-concise-save">Save concise version</button>
        ${docStatusHtml(id, 'concise')}
      </div>
      ${conciseBy}
    </section>`;
}

const docsChanged = () => { loadScorerStatus(); loadJobs(); }; // what the AI gets changed: stale scores, profile size

async function patchDoc(id, body) {
  try {
    replaceDoc(await api(`/api/documents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }));
    redrawDoc(id);
    toast('Saved. Existing scores are now marked stale.', 'ok', 3000);
    docsChanged();
  } catch (e) { toast(`Update failed: ${e.message}`, 'err'); loadDocs(); }
}

async function toggleDoc(id) {
  if (state.docs.open.has(id)) { state.docs.open.delete(id); redrawDoc(id); return; }
  state.docs.open.add(id);
  redrawDoc(id);
  if (state.docs.text.has(id)) return;
  try {
    const d = await api(`/api/documents/${id}/text`);
    state.docs.text.set(id, { text: d.text || '', concise_text: d.concise_text || '' });
    replaceDoc(d);
  } catch (e) {
    state.docs.open.delete(id);
    toast(`Could not open it: ${e.message}`, 'err');
  }
  redrawDoc(id);
}

async function deleteDoc(id) {
  const doc = docById(id);
  if (!doc || !confirm(`Delete "${doc.filename}"?${doc.source === 'file' ? '' : ' Its text is stored only here.'}`)) return;
  try {
    await api(`/api/documents/${id}`, { method: 'DELETE' });
    state.docs.list = state.docs.list.filter((d) => d.id !== id);
    state.docs.open.delete(id);
    state.docs.text.delete(id);
    renderDocs();
    $('#docs-list-heading').focus(); // the row that had focus is gone
    announceDocs(`Deleted ${doc.filename}.`);
    if (doc.use_for_scoring) docsChanged(); else loadScorerStatus();
  } catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
}

async function saveDoc(id) {
  const doc = docById(id);
  const t = state.docs.text.get(id);
  const title = docDraft(id, 'title', doc.filename).trim();
  if (!title) { setDocStatus(id, 'main', 'Give it a title first.', true); return; }
  const body = { title };
  if (doc.editable) {
    body.text = docDraft(id, 'text', t.text);
    if (!body.text.trim()) { setDocStatus(id, 'main', 'The text is empty. To remove the document, delete it instead.', true); return; }
  }
  try {
    replaceDoc(await api(`/api/documents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }));
    state.docs.drafts.delete(`${id}:title`);
    state.docs.drafts.delete(`${id}:text`);
    if (doc.editable) t.text = body.text.trim();
    setDocStatus(id, 'main', `Saved.${doc.use_for_scoring ? ' Existing scores are now marked stale.' : ''}`);
    redrawDoc(id);
    if (doc.use_for_scoring) docsChanged();
  } catch (e) { setDocStatus(id, 'main', `Not saved: ${e.message}`, true); }
}

async function saveConcise(id) {
  const t = state.docs.text.get(id);
  const text = docDraft(id, 'concise', t.concise_text);
  try {
    const doc = await api(`/api/documents/${id}`, { method: 'PATCH', body: JSON.stringify({ concise_text: text }) });
    replaceDoc(doc);
    state.docs.drafts.delete(`${id}:concise`);
    t.concise_text = text.trim();
    setDocStatus(id, 'concise', !t.concise_text ? 'Concise version removed: the AI gets the original.'
      : doc.use_concise ? 'Saved. The AI gets this version; existing scores are now marked stale.'
        : 'Saved. Choose "the concise version" above to send it instead of the original.');
    redrawDoc(id);
    if (doc.use_for_scoring) docsChanged();
  } catch (e) { setDocStatus(id, 'concise', `Not saved: ${e.message}`, true); }
}

async function setDocVersion(id, concise) {
  const t = state.docs.text.get(id);
  const body = { use_concise: concise };
  const draft = docDraft(id, 'concise', t.concise_text);
  if (concise && draft !== t.concise_text) body.concise_text = draft; // send what's on screen, not the older copy
  try {
    const doc = await api(`/api/documents/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
    replaceDoc(doc);
    if ('concise_text' in body) { state.docs.drafts.delete(`${id}:concise`); t.concise_text = draft.trim(); }
    setDocStatus(id, 'concise', concise ? 'The AI now gets the concise version.' : 'The AI now gets the original.');
    redrawDoc(id);
    if (doc.use_for_scoring) docsChanged();
  } catch (e) {
    setDocStatus(id, 'concise', `Not changed: ${e.message}`, true);
    redrawDoc(id);
  }
}

async function makeConcise(id) {
  const t = state.docs.text.get(id);
  const doc = docById(id);
  if (docDraft(id, 'concise', t.concise_text).trim()
      && !confirm('Write a new concise version from the original? It replaces the current one, including your changes to it.')) return;
  state.docs.busy.add(`${id}:concise-make`);
  setDocStatus(id, 'concise', 'Writing the concise version… this can take a minute or two.');
  redrawDoc(id);
  try {
    const d = await api(`/api/documents/${id}/concise`, { method: 'POST' });
    state.docs.text.set(id, { text: d.text || '', concise_text: d.concise_text || '' });
    state.docs.drafts.delete(`${id}:concise`);
    replaceDoc(d);
    setDocStatus(id, 'concise', 'Done: the AI now gets the concise version. Read it against the original, then edit it, or switch back to the original if anything is missing.');
    if (doc.use_for_scoring) docsChanged();
  } catch (e) {
    setDocStatus(id, 'concise', `Couldn't write it: ${e.message}`, true);
  }
  state.docs.busy.delete(`${id}:concise-make`);
  redrawDoc(id);
}

function keptBoth() {
  try { return JSON.parse(localStorage.getItem(KEEP_BOTH_KEY) || '[]'); } catch { return []; }
}

// A ticked summary whose ticked originals send the same content twice: suggest unticking them (never done silently).
function renderSummarySuggestion() {
  const box = $('#summary-suggest');
  const kept = keptBoth();
  const items = state.docs.list
    .filter((s) => s.source === 'summary' && s.use_for_scoring && !kept.includes(s.id))
    .map((s) => ({ s, ticked: (s.summary_of || []).map(docById).filter((d) => d && d.use_for_scoring) }))
    .filter((x) => x.ticked.length);
  const focus = rememberFocus(box);
  box.innerHTML = items.map(({ s, ticked }) => {
    const n = ticked.length;
    return `<div class="callout">
      <p><strong>${esc(s.filename)}</strong> is ticked, and so ${n === 1 ? 'is 1 document' : `are ${n} documents`} it was made from
        (${ticked.map((d) => esc(d.filename)).join(', ')}): the AI gets that content twice.
        Once you've read the summary through, untick ${n === 1 ? 'it' : 'them'}.</p>
      <div class="toolbar-row">
        <button class="btn btn-primary btn-sm" type="button" data-act="untick" data-id="${s.id}" data-focus="sum-${s.id}-untick">Untick the ${n === 1 ? 'original' : `${n} originals`}</button>
        <button class="btn btn-sm" type="button" data-act="keep" data-id="${s.id}" data-focus="sum-${s.id}-keep">Keep both ticked</button>
      </div>
    </div>`;
  }).join('');
  box.hidden = !items.length;
  restoreFocus(box, focus);
}

async function onSummarySuggestion(e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const summary = docById(Number(btn.dataset.id));
  if (!summary) return;
  if (btn.dataset.act === 'keep') {
    try { localStorage.setItem(KEEP_BOTH_KEY, JSON.stringify([...keptBoth(), summary.id])); } catch { /* shown again next time */ }
    renderSummarySuggestion();
    $('#docs-list-heading').focus();
    announceDocs(`Kept ${summary.filename} and its originals ticked.`);
    return;
  }
  const ticked = (summary.summary_of || []).map(docById).filter((d) => d && d.use_for_scoring);
  try {
    for (const d of ticked) {
      replaceDoc(await api(`/api/documents/${d.id}`, { method: 'PATCH', body: JSON.stringify({ use_for_scoring: false }) }));
    }
    renderDocs();
    $('#docs-list-heading').focus(); // the suggestion with the button is gone
    announceDocs(`Unticked ${ticked.length} document(s): the AI now gets ${summary.filename} instead.`);
    docsChanged();
  } catch (err) { toast(`Untick failed: ${err.message}`, 'err'); loadDocs(); }
}

async function makeSummary() {
  const btn = $('#summary-make');
  const status = $('#summary-status');
  if (btn.getAttribute('aria-disabled') === 'true') return;
  const ticked = state.docs.list.filter((d) => d.use_for_scoring && d.kind !== 'preferences');
  if (!ticked.length) { status.textContent = 'Tick "use for scoring" on the documents to merge first (preferences stay separate).'; return; }
  btn.setAttribute('aria-disabled', 'true');
  status.textContent = `Merging ${ticked.length} document(s) into one summary… this can take a few minutes.`;
  try {
    const d = await api('/api/documents/summary', { method: 'POST' });
    state.docs.text.set(d.id, { text: d.text || '', concise_text: '' });
    state.docs.open.add(d.id);
    replaceDoc(d);
    renderDocs();
    status.textContent = `Created "${d.filename}" and ticked it; it's open below. The ${d.sources.length} document(s) it was made from are still ticked: read the summary through, then untick them.`;
    docsChanged();
  } catch (e) { status.textContent = `Couldn't create the summary: ${e.message}`; }
  btn.removeAttribute('aria-disabled');
}

async function saveNote(e) {
  e.preventDefault();
  const status = $('#note-status');
  const title = $('#note-title').value.trim();
  const text = $('#note-text').value.trim();
  setFieldError($('#note-title'), '');
  setFieldError($('#note-text'), '');
  if (!text) setFieldError($('#note-text'), 'Write the note here: a note needs some text.', { focus: !!title });
  if (!title) setFieldError($('#note-title'), 'Give the note a title, for example "Project results".');
  if (!title || !text) { status.textContent = ''; return; }
  const body = { title, text, kind: $('#note-kind').value, use_for_scoring: $('#note-use').checked };
  try {
    const doc = await api('/api/documents/notes', { method: 'POST', body: JSON.stringify(body) });
    replaceDoc(doc);
    renderDocs();
    $('#note-title').value = '';
    $('#note-text').value = '';
    status.textContent = `Saved "${doc.filename}": it's first in the list below.`;
    if (doc.use_for_scoring) docsChanged(); else loadScorerStatus();
  } catch (err) { status.textContent = `Not saved: ${err.message}`; announce(`Not saved: ${err.message}`, true); }
}

/* ---------- cover letter blocks (backend/letter_blocks.py) ----------
   Paragraphs the user wrote, put together into a letter word for word. The list is grouped by kind (openings first,
   closings last) and ordered by a workspace-wide position, so a move sends the full new order of every id. The
   form's fields come from blockFormHtml(), which the cover letter window's "Write this block" form uses too. */
const BLOCK_KINDS = [['opening', 'Opening'], ['body', 'Body'], ['closing', 'Closing']];
const BLOCK_KIND_LABEL = Object.fromEntries(BLOCK_KINDS);
const BLOCK_PLACEHOLDERS = '{company}, {role}, {location} and {date}';
const BLOCK_TAG_MAX = 60;
const BLOCK_TAGS_MAX = 30;
const blockKindIndex = (kind) => Math.max(0, BLOCK_KINDS.findIndex(([k]) => k === kind));
const sortBlocks = (list) => list.slice().sort((a, b) => blockKindIndex(a.kind) - blockKindIndex(b.kind) || a.position - b.position || a.id - b.id);
const blockPreview = (text, n = 120) => { const t = String(text || '').replace(/\s+/g, ' ').trim(); return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t; };
const blockName = (b) => b.title || blockPreview(b.text, 60) || 'Untitled block';
const unknownPlaceholdersText = (names) => (names?.length
  ? `Unknown placeholder${names.length === 1 ? '' : 's'}: ${names.map((n) => `{${n}}`).join(', ')} — only ${BLOCK_PLACEHOLDERS} are filled in.` : '');

// The fields of a block's form: title, kind, tags and text. `p` prefixes the ids (one form on the Documents tab, one
// in the cover letter window). With `bullets`, the evidence the AI found sits beside the text field (.block-write).
function blockFormHtml(p, { title = '', kind = 'body', tags = '', text = '', bullets = null } = {}) {
  const tagText = Array.isArray(tags) ? tags.join(', ') : tags;
  const textField = `<div class="field block-text-field"><label class="field-label" for="${p}-text">Text</label>
      <textarea id="${p}-text" class="doc-textarea" rows="${bullets ? 9 : 6}" maxlength="8000" data-block="text">${esc(text)}</textarea></div>`;
  return `
    <div class="note-grid">
      <div class="field"><label class="field-label" for="${p}-title">Title</label>
        <input id="${p}-title" type="text" maxlength="200" autocomplete="off" data-block="title" value="${esc(title)}" /></div>
      <div class="field"><label class="field-label" for="${p}-kind">Part of the letter</label>
        <select id="${p}-kind" data-block="kind">${BLOCK_KINDS.map(([v, l]) => `<option value="${v}"${v === kind ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></div>
      <div class="field"><label class="field-label" for="${p}-tags">Tags (comma-separated)</label>
        <input id="${p}-tags" type="text" autocomplete="off" data-block="tags" value="${esc(tagText)}" /></div>
    </div>
    ${bullets ? `<div class="block-write">
      <div class="block-bullets"><h4 class="block-bullets-title">From your documents</h4>
        ${bullets.length ? `<ul>${bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : '<p class="muted">Nothing found in your documents for this one.</p>'}</div>
      ${textField}
    </div>` : textField}`;
}

// The block in a form made by blockFormHtml(), or null (with the fields' errors shown) when it can't be saved.
function readBlockForm(root) {
  const field = (name) => root.querySelector(`[data-block="${name}"]`);
  const text = field('text').value;
  const tags = field('tags').value.split(',').map((t) => t.trim()).filter(Boolean);
  setFieldError(field('text'), '');
  setFieldError(field('tags'), '');
  let ok = true;
  const longTag = tags.find((t) => t.length > BLOCK_TAG_MAX);
  if (longTag || tags.length > BLOCK_TAGS_MAX) {
    setFieldError(field('tags'), longTag ? `A tag can be up to ${BLOCK_TAG_MAX} characters: shorten "${blockPreview(longTag, 30)}".`
      : `Up to ${BLOCK_TAGS_MAX} tags a block.`, { focus: false });
    ok = false;
  }
  if (!text.trim()) {
    setFieldError(field('text'), 'Write the paragraph here: a block needs some text.', { focus: false });
    ok = false;
  }
  if (!ok) { root.querySelector('[aria-invalid="true"]')?.focus(); return null; }
  return { title: field('title').value.trim(), kind: field('kind').value, tags, text };
}

// Creates (no id) or changes a block, and keeps state.blocks and the Documents tab's list up to date.
async function saveBlock(payload, id = null) {
  const block = await api(id ? `/api/letter-blocks/${id}` : '/api/letter-blocks',
    { method: id ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
  state.blocks = sortBlocks([...state.blocks.filter((b) => b.id !== block.id), block]);
  renderBlocks();
  return block;
}

async function loadBlocks() {
  if (state.blocksWs !== state.ws) { state.blocksWs = state.ws; state.blocks = []; resetBlockForm(); renderBlocks(); }
  const ws = state.ws;
  try {
    const list = await api('/api/letter-blocks');
    if (ws !== state.ws) return;
    state.blocks = list;
    renderBlocks();
  } catch (e) {
    if (ws === state.ws) $('#block-list').innerHTML = `<p class="err-text">Could not load your blocks: ${esc(e.message)}</p>`;
  }
}

function blockRowHtml(b, first, last) {
  const name = blockName(b);
  const nameId = `block-${b.id}-name`;
  const warn = unknownPlaceholdersText(b.unknown_placeholders);
  return `<li class="block-row" data-block-id="${b.id}">
    <div class="block-main">
      <p id="${nameId}" class="block-title">${esc(name)}</p>
      ${b.tags.length ? `<ul class="block-tags" aria-label="Tags">${b.tags.map((t) => `<li class="block-tag">${esc(t)}</li>`).join('')}</ul>` : ''}
      <p class="block-preview muted">${esc(blockPreview(b.text))}</p>
      ${warn ? `<p class="warn-text block-row-warn"><span aria-hidden="true">⚠ </span>${esc(warn)}</p>` : ''}
    </div>
    <div class="block-actions">
      <button class="btn btn-ghost btn-sm" type="button" data-act="edit" data-focus="edit-${b.id}" aria-describedby="${nameId}">Edit</button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="up" data-focus="up-${b.id}" aria-label="Move ${esc(name)} up"${first ? ' disabled' : ''}><span aria-hidden="true">↑</span></button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="down" data-focus="down-${b.id}" aria-label="Move ${esc(name)} down"${last ? ' disabled' : ''}><span aria-hidden="true">↓</span></button>
      <button class="btn btn-ghost btn-sm" type="button" data-act="delete" data-focus="delete-${b.id}" aria-describedby="${nameId}">Delete</button>
    </div>
  </li>`;
}

function renderBlocks() {
  const root = $('#block-list');
  if (!root) return;
  const saved = rememberFocus(root);
  if (!state.blocks.length) {
    root.innerHTML = '<p class="empty block-empty">No blocks yet. Write your first one above: an opening, a paragraph about a project or a result, a closing.</p>';
    return;
  }
  root.innerHTML = BLOCK_KINDS.map(([kind, label]) => {
    const group = state.blocks.filter((b) => b.kind === kind);
    if (!group.length) return '';
    return `<section class="block-group" aria-labelledby="block-group-${kind}">
      <h4 id="block-group-${kind}" class="block-group-title">${esc(label)}</h4>
      <ul class="block-ul">${group.map((b, i) => blockRowHtml(b, i === 0, i === group.length - 1)).join('')}</ul>
    </section>`;
  }).join('');
  restoreFocus(root, saved);
  // A move to the top or bottom disables the button that had focus: the other one of the pair takes it.
  if (saved && !root.contains(document.activeElement)) {
    const id = saved.key.split('-').pop();
    (root.querySelector(`[data-focus="up-${id}"]:not(:disabled)`) || root.querySelector(`[data-focus="down-${id}"]:not(:disabled)`)
      || root.querySelector(`[data-focus="edit-${id}"]`))?.focus();
  }
}

function resetBlockForm() {
  state.blockEdit = null;
  const fields = $('#block-form-fields');
  if (!fields) return;
  fields.innerHTML = blockFormHtml('block');
  $('#block-save').textContent = 'Save block';
  $('#block-cancel').hidden = true;
}

function editBlock(b) {
  state.blockEdit = b.id;
  $('#block-form-fields').innerHTML = blockFormHtml('block', b);
  $('#block-save').textContent = 'Save changes';
  $('#block-cancel').hidden = false;
  $('#block-form-warn').hidden = true;
  $('#block-status').textContent = `Editing "${blockName(b)}".`;
  $('#block-form').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
  $('#block-title').focus({ preventScroll: true });
}

async function submitBlockForm(e) {
  e.preventDefault();
  const status = $('#block-status');
  const warn = $('#block-form-warn');
  const payload = readBlockForm($('#block-form'));
  if (!payload) { status.textContent = ''; return; }
  const btn = $('#block-save');
  setBusy(btn, true);
  try {
    const editing = state.blockEdit;
    const block = await saveBlock(payload, editing);
    resetBlockForm();
    status.textContent = editing ? `Saved your changes to "${blockName(block)}".` : `Saved "${blockName(block)}": it's in the list below.`;
    warn.textContent = unknownPlaceholdersText(block.unknown_placeholders);
    warn.hidden = !warn.textContent;
    if (!warn.hidden) announce(warn.textContent);
  } catch (err) { status.textContent = `Not saved: ${err.message}`; announce(`Not saved: ${err.message}`, true); }
  setBusy(btn, false);
}

async function moveBlock(b, dir) {
  const ids = state.blocks.map((x) => x.id);
  const i = ids.indexOf(b.id);
  const j = i + dir;
  if (j < 0 || j >= ids.length || state.blocks[j].kind !== b.kind) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  try {
    state.blocks = await api('/api/letter-blocks/order', { method: 'PUT', body: JSON.stringify({ ids }) });
    renderBlocks();
    announce(`Moved "${blockName(b)}" ${dir < 0 ? 'up' : 'down'}.`);
  } catch (err) { toast(`Not moved: ${err.message}`, 'err'); }
}

async function deleteBlock(b) {
  if (!confirm(`Delete the block "${blockName(b)}"? This can't be undone.`)) return;
  try {
    await api(`/api/letter-blocks/${b.id}`, { method: 'DELETE' });
    state.blocks = state.blocks.filter((x) => x.id !== b.id);
    if (state.blockEdit === b.id) resetBlockForm();
    renderBlocks();
    $('#block-status').textContent = `Deleted "${blockName(b)}".`;
    if (!document.activeElement || document.activeElement === document.body) $('#blocks-card-title').focus();
  } catch (err) { toast(`Not deleted: ${err.message}`, 'err'); }
}

function initBlocks() {
  resetBlockForm();
  renderBlocks();
  $('#block-form').addEventListener('submit', submitBlockForm);
  $('#block-cancel').addEventListener('click', () => {
    resetBlockForm();
    $('#block-status').textContent = '';
    $('#block-title').focus();
  });
  $('#block-list').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    const b = btn && state.blocks.find((x) => x.id === Number(btn.closest('[data-block-id]').dataset.blockId));
    if (!b) return;
    if (btn.dataset.act === 'edit') editBlock(b);
    else if (btn.dataset.act === 'up') moveBlock(b, -1);
    else if (btn.dataset.act === 'down') moveBlock(b, 1);
    else if (btn.dataset.act === 'delete') deleteBlock(b);
  });
}

/* The cover letter template: one .docx/.odt file the downloads are poured into. */
async function loadLetterTemplate() {
  try { state.letterTemplate = await api('/api/letter-template'); }
  catch { state.letterTemplate = state.letterTemplate || null; }
  renderLetterTemplate();
  return state.letterTemplate;
}

function renderLetterTemplate() {
  const box = $('#template-current');
  if (!box) return;
  const t = state.letterTemplate;
  if (t?.template) {
    box.innerHTML = `Using <strong>${esc(t.template.filename)}</strong> (uploaded ${esc(fmtDate(t.template.created_at))}) <button id="template-remove" class="btn btn-ghost btn-sm" type="button">Remove<span class="visually-hidden"> the template</span></button>`;
    $('#template-remove').addEventListener('click', async () => {
      if (!confirm('Remove the cover letter template? Downloads will be plain documents again.')) return;
      try { await api('/api/letter-template', { method: 'DELETE' }); toast('Template removed', 'ok', 2000); }
      catch (e) { $('#template-status').textContent = `Failed: ${e.message}`; return; }
      $('#template-status').textContent = '';
      loadLetterTemplate();
    });
  } else {
    box.textContent = 'No template: downloads are plain documents.';
  }
  $('#template-pdf').textContent = !t ? '' : t.pdf === 'libreoffice'
    ? 'PDFs are made with LibreOffice, so they use your template.'
    : 'PDFs are plain text. Install LibreOffice to get PDFs with your template.';
}

function initLetterTemplate() {
  $('#template-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = $('#template-input').files[0];
    if (!f) { setFieldError($('#template-input'), 'Choose a Word (.docx) or OpenDocument (.odt) file first.'); return; }
    const btn = $('#template-upload');
    setBusy(btn, true);
    $('#template-status').textContent = `Uploading ${f.name}…`;
    try {
      const fd = new FormData();
      fd.append('file', f, f.name);
      state.letterTemplate = await api('/api/letter-template', { method: 'POST', body: fd });
      $('#template-input').value = '';
      $('#template-status').textContent = 'Template saved.';
      renderLetterTemplate();
    } catch (err) {
      $('#template-status').textContent = err.status === 413 ? 'That file is too large (20 MB at most).' : `Failed: ${err.message}`;
    }
    setBusy(btn, false);
  });
  $('#template-input').addEventListener('change', () => { setFieldError($('#template-input'), ''); $('#template-status').textContent = ''; });
}

function initDocs() {
  initBlocks();
  initLetterTemplate();
  $('#note-form').addEventListener('submit', saveNote);
  $('#summary-make').addEventListener('click', makeSummary);
  $('#summary-suggest').addEventListener('click', onSummarySuggestion);
  $('#upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const files = Array.from($('#upload-input').files);
    if (!files.length) { setFieldError($('#upload-input'), 'Choose a file first: a PDF, Word (.docx), OpenDocument or text file.'); return; }
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
  $('#upload-input').addEventListener('change', () => setFieldError($('#upload-input'), ''));
}

/* ================= MAP ================= */
// Two modes: 'jobs' (read-only: jobs + pins with radii, jobs in view listed below)
// and 'pins' (click or tap to drop pins, drag to move, pins listed below).
// Nothing on the map needs a mouse or a drag (WCAG 2.1.1, 2.5.7): the list under the map has every job in
// view (Show opens its popup, with focus in it), and in pin mode "Drop a pin at the cross" and "Move to the
// cross" use the map's centre, which the arrow keys move. Colour is never the only sign (1.4.1): excluded jobs
// are hollow dashed rings, and the pin kinds have solid, dashed and dotted radius rings. Outlines use --map-ink,
// one dark ink for every theme, because the OpenStreetMap tiles are light whatever the theme.
const PIN_HUE = { home: 'special', hybrid: 'info', onsite: 'warn' }; // theme slots
const PIN_DASH = { home: null, hybrid: '10 7', onsite: '2 7' }; // the radius ring's pattern, as in the legend
const PIN_KINDS = ['home', 'hybrid', 'onsite'];
const pinColour = (kind) => themeColor(PIN_HUE[kind] || 'accent');
const MAP_LIST_MAX = 200;
const mapAnimate = () => !reducedMotion();

function storedMapMode() {
  try { return localStorage.getItem('jobhunt.mapMode') === 'pins' ? 'pins' : 'jobs'; } catch { return 'jobs'; }
}

function initMap() {
  if (state.map) return;
  if (typeof L === 'undefined') {
    showError($('#pins-error'), "The map didn't load (are you offline?). Reload the page to try again.");
    return;
  }
  const animate = mapAnimate();
  state.map = L.map('map', { zoomAnimation: animate, fadeAnimation: animate, markerZoomAnimation: animate }).setView([-33.87, 151.21], 9);
  // The tiles send this site's address (never the page's): OpenStreetMap's servers refuse a request without it,
  // and the app's Referrer-Policy (same-origin) tells other sites nothing.
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, referrerPolicy: 'origin', attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);
  state.pinLayer = L.layerGroup().addTo(state.map);
  state.jobLayer = L.layerGroup().addTo(state.map);
  state.map.on('click', (e) => { if (state.mapMode === 'pins') addPinAt(e.latlng); });
  state.map.on('moveend', () => { if (state.mapMode === 'jobs') renderMapJobs(); else if ($('#pins-in-view').checked) renderPinsList(); });
  $('#pin-radius').addEventListener('input', (e) => { $('#pin-radius-output').textContent = `${e.target.value} km`; });
  $('#pins-in-view').addEventListener('change', renderPinsList);
  $('#pin-add-centre').addEventListener('click', () => addPinAt(state.map.getCenter()));
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
  $('#map').setAttribute('aria-describedby', edit ? 'map-hint-pins' : 'map-hint-jobs');
  $('#map').setAttribute('aria-label', edit ? 'Map for placing pins' : 'Map of jobs and pins');
  if (!redraw) return;
  drawPins();
  if (edit) renderPinsList(); else renderMapJobs();
}

/* Jobs on the map: the ones the Jobs tab's filters show, grouped by place
   (many ads that only name a city share one point). */
function mapJobs() { return visibleJobs().filter((j) => j.lat !== null && j.lng !== null); }

function jobLinkHtml(j) {
  return `<a href="./job.html?id=${j.id}&ws=${state.ws}" target="_blank" rel="noopener" title="Full page in a new tab">Open<span class="visually-hidden"> ${esc(j.title)}</span>${NEW_TAB}</a>`;
}

/* A popup opened from a button outside the map: focus moves into it, Escape closes it, and focus goes back
   to the button when it closes. */
function focusPopup(layer, returnTo) {
  const el = layer.getPopup()?.getElement();
  if (!el) return;
  const findReturn = focusFinder(returnTo); // the list under the map may be redrawn while the popup is open
  const content = el.querySelector('.leaflet-popup-content');
  content.tabIndex = -1;
  content.focus({ preventScroll: true });
  const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); layer.closePopup(); } };
  el.addEventListener('keydown', onKey);
  layer.once('popupclose', () => {
    el.removeEventListener('keydown', onKey);
    setTimeout(() => {
      const now = document.activeElement;
      if (!now || now === document.body || el.contains(now)) findReturn()?.focus({ preventScroll: true });
    }, 0);
  });
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
  const ink = themeColor('map-ink');
  groups.forEach((jobs) => {
    const kept = jobs.some((j) => !j.excluded_reason);
    const n = jobs.length;
    const radius = Math.min(16, 6 + 2.5 * Math.log2(n));
    const marker = L.circleMarker([jobs[0].lat, jobs[0].lng], kept
      ? { radius, weight: 2, color: ink, fillColor: themeColor('ok'), fillOpacity: 0.9 }
      : { radius, weight: 2, color: ink, dashArray: '3 3', fillColor: themeColor('quiet'), fillOpacity: 0.3 });
    const rows = jobs.slice(0, 40).map((j) => `<li><b>${esc(j.title)}</b> · ${esc(j.company || '')}
      ${scoreOf(j) !== null ? ` · <span class="visually-hidden">fit score </span>${scoreOf(j)}` : ''}${j.excluded_reason ? ' <i>(excluded)</i>' : ''}<br>${jobLinkHtml(j)}
      · <a href="${esc(mainLink(j).url)}" target="_blank" rel="noopener">ad<span class="visually-hidden"> for ${esc(j.title)}</span>${NEW_TAB}</a></li>`).join('');
    const place = esc(jobs[0].location_text || 'This place');
    marker.bindPopup(`<div class="map-popup" role="region" aria-label="Jobs at ${place}"><p class="muted map-popup-head">${place}${n > 1 ? ` · ${n} jobs` : ''}</p>
      <ul>${rows}</ul>${n > 40 ? `<p class="muted">…and ${n - 40} more: see the list under the map.</p>` : ''}</div>`, { maxWidth: 320, maxHeight: 300 });
    if (n > 1) marker.bindTooltip(String(n), { permanent: true, direction: 'right', offset: [radius - 4, 0], className: 'map-count' });
    marker.addTo(state.jobLayer);
    jobs.forEach((j) => state.jobMarkers.set(j.id, marker));
  });
  renderMapLegend();
  renderMapJobs();
}

function renderMapLegend() {
  const all = visibleJobs().length;
  const onMap = mapJobs().length;
  $('#map-legend').innerHTML = `<span class="visually-hidden">Key: </span>
    <span class="dot" aria-hidden="true"></span> job you'd consider (filled)
    <span class="dot hollow" aria-hidden="true"></span> excluded (hollow, dashed)
    ${PIN_KINDS.map((k) => `<span class="ring ring-${k} hue-${PIN_HUE[k]}" aria-hidden="true"></span> ${k} pin (${{ home: 'solid', hybrid: 'dashed', onsite: 'dotted' }[k]} ring)`).join(' ')}
    · ${onMap} of ${all} filtered job${all === 1 ? '' : 's'} on the map${all > onMap ? ` (${all - onMap} remote or without a location)` : ''}`;
}

function renderMapJobs() {
  const box = $('#map-jobs');
  if (!state.map || state.mapMode !== 'jobs') return;
  const bounds = state.map.getBounds();
  if (state.jobsStatus === 'loading') { box.innerHTML = LOADING_JOBS; return; }
  const inView = mapJobs().filter((j) => bounds.contains([j.lat, j.lng]));
  const focusBack = box.contains(document.activeElement) ? focusFinder(document.activeElement) : null;
  if (!inView.length) {
    box.innerHTML = '<div class="empty">No jobs in this part of the map. Zoom out, or change the Jobs tab filters.</div>';
    return;
  }
  const shown = inView.slice(0, MAP_LIST_MAX);
  box.innerHTML = `<div class="map-jobs-head"><h3 class="map-jobs-title" id="map-jobs-title">${inView.length} job${inView.length === 1 ? '' : 's'} in view</h3>
      <span class="muted">sorted like the Jobs tab${inView.length > MAP_LIST_MAX ? `; first ${MAP_LIST_MAX} shown, zoom in for the rest` : ''}</span></div>
    <ul class="map-jobs-ul" aria-labelledby="map-jobs-title">${shown.map((j) => `<li class="map-job-row" data-id="${j.id}">
      ${ratingsHtml(j)}
      <div class="map-job-main">
        <div><a class="map-job-title" id="mapjob-${j.id}-title" href="./job.html?id=${j.id}&ws=${state.ws}" target="_blank" rel="noopener" title="Open the full page in a new tab">${esc(j.title || '—')}<span class="visually-hidden"> (opens in a new tab)</span></a>
          <span class="muted">· ${esc(j.company || '—')}</span></div>
        <div class="muted">${esc(j.location_text || '')}${j.office_unknown ? ' · office unknown' : ''}${j.distance_km !== null ? ` · <span aria-hidden="true">📍 </span><span class="visually-hidden">distance </span>${j.distance_km} km` : ''}
          · <span class="badge ${modeOf(j)}">${modeOf(j)}</span>${j.ghost?.level ? ` · possible ghost job (${GHOST_STRENGTH[j.ghost.level]})` : ''} · ${esc(STATUS_LABEL[j.status] || j.status)}${j.excluded_reason ? ` · excluded: ${esc(j.excluded_reason)}` : ''}</div>
      </div>
      <button class="btn btn-ghost btn-sm" type="button" id="mapjob-${j.id}-show" data-act="show" title="Show it on the map">Show<span class="visually-hidden"> ${esc(j.title || 'job')} on the map</span></button>
      <a class="btn btn-ghost btn-sm" id="mapjob-${j.id}-open" href="./job.html?id=${j.id}&ws=${state.ws}" target="_blank" rel="noopener">Open<span class="visually-hidden"> ${esc(j.title || 'job')}</span>${NEW_TAB}</a>
    </li>`).join('')}</ul>`;
  box.querySelectorAll('[data-act="filter-warnings"]').forEach((b) => b.addEventListener('click', () => {
    const job = state.jobs.find((j) => j.id === Number(b.closest('.map-job-row').dataset.id));
    if (job) showFilterWarnings(job);
  }));
  box.querySelectorAll('[data-act="show"]').forEach((b) => b.addEventListener('click', () => {
    const marker = state.jobMarkers.get(Number(b.closest('.map-job-row').dataset.id));
    if (!marker) return;
    // The list sits below the map: bring the map back on screen first.
    $('#map').scrollIntoView({ block: 'center', behavior: scrollBehavior() });
    state.map.panTo(marker.getLatLng(), { animate: mapAnimate() });
    marker.openPopup();
    focusPopup(marker, b);
  }));
  if (focusBack) focusBack()?.focus({ preventScroll: true });
}

/* Pins: draggable markers in edit mode; small dots with their radius otherwise. */
function drawPin(p) {
  const edit = state.mapMode === 'pins';
  const colour = pinColour(p.kind);
  const circle = L.circle([p.lat, p.lng], {
    radius: p.radius_km * 1000, weight: 2, color: colour, dashArray: PIN_DASH[p.kind], fillOpacity: edit ? 0.12 : 0.06, interactive: false,
  });
  const name = `${p.kind} pin: ${p.label}`;
  const marker = edit
    ? L.marker([p.lat, p.lng], { draggable: true, keyboard: true, alt: name, title: `${name} (drag to move)` })
    : L.circleMarker([p.lat, p.lng], { radius: 5, weight: 2, color: themeColor('map-ink'), fillColor: colour, fillOpacity: 1 });
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
    state.setupPins = state.pins;
    renderStarts();
    drawPins();
    const sane = state.pins.filter((p) => p.radius_km < 1000);
    if (sane.length) state.map.fitBounds(L.latLngBounds(sane.map((p) => [p.lat, p.lng])).pad(0.3), { animate: false });
    $('#pins-error').hidden = true;
  } catch (e) {
    showError($('#pins-error'), `Could not load pins: ${e.message}`);
  }
  renderPinsList();
  drawJobMarkers();
}

async function addPinAt(latlng) {
  const kind = $('#pin-kind').value;
  const label = $('#pin-label').value.trim() || kind;
  const body = { lat: latlng.lat, lng: latlng.lng, radius_km: Number($('#pin-radius').value), kind, label };
  try {
    await api('/api/pins', { method: 'POST', body: JSON.stringify(body) });
    await loadPins();
    announce(`Dropped a ${kind} pin, ${label}, with a ${body.radius_km} km radius.`);
    pinsChanged();
  } catch (err) { toast(`Pin save failed: ${err.message}`, 'err'); }
}

async function savePin(p, patch) {
  try {
    Object.assign(p, await api(`/api/pins/${p.id}`, { method: 'PUT', body: JSON.stringify(patch) }));
    const old = state.pinLayers.get(p.id);
    if (old) { old.marker.remove(); old.circle.remove(); }
    drawPin(p);
    renderPinsList();
    pinsChanged();
    return true;
  } catch (e) { toast(`Pin update failed: ${e.message}`, 'err'); return false; }
}

async function deletePin(p) {
  try {
    await api(`/api/pins/${p.id}`, { method: 'DELETE' });
    // The server dropped the starting points made from this pin along with it.
    state.startingPoints = state.startingPoints.filter((sp) => sp.pin_id !== p.id);
    await loadPins();
    $('#pin-add-centre').focus(); // the row with the button is gone
    announce(`Deleted the ${p.kind} pin ${p.label}.`);
    pinsChanged();
  } catch (e) { toast(`Pin delete failed: ${e.message}`, 'err'); }
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
  const focusBack = box.contains(document.activeElement) ? focusFinder(document.activeElement) : null;
  box.innerHTML = '';
  if (!state.pins.length) {
    box.innerHTML = '<div class="empty">No pins yet. Click or tap the map to drop one, or use Drop a pin at the cross.</div>';
    return;
  }
  if (inViewOnly) {
    box.insertAdjacentHTML('beforeend', `<p class="muted">${pins.length} of ${state.pins.length} pins in view</p>`);
    if (!pins.length) return;
  }
  const list = document.createElement('ul');
  list.className = 'pins-ul';
  list.setAttribute('aria-label', 'Your pins');
  box.appendChild(list);
  pins.forEach((p) => {
    const row = document.createElement('li');
    row.className = 'pin-row';
    const id = `pin-${p.id}`;
    const name = esc(p.label);
    const huge = p.radius_km >= 1000 ? '<span class="err-text" title="Ignored by the location filter: a radius this large covers everywhere. Remote roles are handled by the Remote toggles in Search setup."><span aria-hidden="true">⚠ </span>ignored by location filter (radius ≥ 1000 km)</span>' : '';
    row.innerHTML = `
      <span class="pin-kind pin-kind-${esc(p.kind)} hue-${PIN_HUE[p.kind] || 'border'}">${esc(p.kind)}</span>
      <strong id="${id}-name">${name}</strong>
      <span class="muted"><span class="visually-hidden">at </span>${p.lat.toFixed(4)}, ${p.lng.toFixed(4)}</span>
      <span class="pin-radius"><label for="${id}-radius">Radius</label> <input id="${id}-radius" type="number" min="0.1" step="0.5" inputmode="decimal" value="${p.radius_km}" aria-describedby="${id}-name" /> <span aria-hidden="true">km</span><span class="visually-hidden">kilometres</span></span>
      <select id="${id}-kind" aria-label="Kind of pin ${name}">${PIN_KINDS.map((k) => `<option ${p.kind === k ? 'selected' : ''}>${k}</option>`).join('')}</select>
      ${huge}
      <span class="pin-actions">
        <button class="btn btn-ghost btn-sm" id="${id}-locate" data-act="locate" type="button">Locate<span class="visually-hidden"> ${name}</span></button>
        <button class="btn btn-ghost btn-sm" id="${id}-move" data-act="move" type="button" title="Move this pin to the cross in the middle of the map">Move to the cross<span class="visually-hidden">: ${name}</span></button>
        <button class="btn btn-danger btn-sm" id="${id}-delete" data-act="del" type="button">Delete<span class="visually-hidden"> ${name}</span></button>
      </span>`;
    row.querySelector('input').addEventListener('change', (e) => {
      const km = Number(e.target.value);
      if (!e.target.validity.valid || !(km > 0)) {
        setFieldError(e.target, 'Enter the radius in kilometres, more than 0 (for example 10).');
        return;
      }
      setFieldError(e.target, '');
      savePin(p, { radius_km: km });
    });
    row.querySelector('select').addEventListener('change', (e) => savePin(p, { kind: e.target.value }));
    row.querySelector('[data-act="locate"]').addEventListener('click', (e) => {
      state.map.setView([p.lat, p.lng], 14, { animate: mapAnimate() });
      const marker = state.pinLayers.get(p.id)?.marker;
      if (marker) { marker.openPopup(); focusPopup(marker, e.currentTarget); }
    });
    row.querySelector('[data-act="move"]').addEventListener('click', async () => {
      const c = state.map.getCenter();
      if (await savePin(p, { lat: c.lat, lng: c.lng })) announce(`Moved ${p.label} to the middle of the map.`);
    });
    row.querySelector('[data-act="del"]').addEventListener('click', () => deletePin(p));
    list.appendChild(row);
  });
  if (focusBack) focusBack()?.focus({ preventScroll: true });
}

/* ---------- boot ---------- */
/* ================= WORKSPACES ================= */
function storedWorkspace() {
  try { return Number(localStorage.getItem('jobhunt.ws')) || 0; } catch { return 0; } // 0: the account's first
}

async function loadWorkspaces() {
  state.workspaces = await api('/api/workspaces');
  if (!state.workspaces.some((w) => w.id === state.ws)) state.ws = state.workspaces[0]?.id ?? 0;
  $('#ws-select').innerHTML = state.workspaces
    .map((w) => `<option value="${w.id}" ${w.id === state.ws ? 'selected' : ''}>${esc(w.name)}</option>`).join('');
  $('#ws-delete').disabled = state.workspaces.length <= 1;
  const current = state.workspaces.find((w) => w.id === state.ws);
  $('#sched-scope').textContent = current ? `This workspace: ${current.name}` : 'This workspace';
  updateTitle();
}

async function switchWorkspace(id) {
  state.ws = Number(id);
  state.officeSlack = 0;
  try { localStorage.setItem('jobhunt.ws', String(state.ws)); } catch { /* private mode */ }
  clearTimeout(state.pollTimer);
  state.watchToken++; // stops the old workspace's progress polling
  state.watching = false;
  state.lastRun = null;
  state.schedule = null;
  state.selected.clear();
  state.details.clear();
  state.includeTouched.clear();
  state.jobs = []; // the old workspace's jobs must not show under the new one's name
  state.jobsStatus = 'loading';
  state.jobsRefreshing = false;
  state.loadedOmit = 'excluded,hidden';
  state.omittedCount = 0;
  state.titleSuggestions = [];
  $('#titles-list').innerHTML = '';
  $('#titles-actions').hidden = true;
  $('#titles-status').textContent = '';
  $('#run-status').textContent = '';
  $('#run-report').hidden = true;
  ['#search-run', '#score-pending', '#fill-info'].forEach((s) => setBusy($(s), false));
  $('#run-progress').hidden = true;
  closeChat();
  state.dupes = { suggested: [], auto_merged: [] };
  await loadWorkspaces();
  loadAll();
}

/* The signed-in account's header and reminders. There are no roles: the Settings tab's cards for the whole of this
   JobHuntPA (frontend/server-settings.js) are everyone's. */
function applyAccount(me) {
  initAccountMenu(me);
  remindRecoveryCodes(me);
  remindLeftovers();
  showSignInNotice();
}

function loadAll() {
  loadCompanies();
  loadJobs();
  loadSetup();
  loadSchedule();
  loadSources();
  loadDocs();
  loadBlocks();
  loadLetterTemplate();
  loadScorerStatus();
  loadLatestRun();
  if (state.map) loadPins();
}

function workspaceForm(title, { name = '', create = false } = {}) {
  const current = state.workspaces.find((w) => w.id === state.ws);
  openHtmlModal(title, `
    <form id="ws-form" class="ws-form" novalidate>
      <label class="field"><span class="field-label">Name</span>
        <input id="ws-name" type="text" maxlength="80" value="${esc(name)}" autocomplete="off" required /></label>
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
      if (!out.name) { setFieldError($('#ws-name'), 'Give the workspace a name, for example "Product roles".'); return; }
      if (create && $('#ws-copy').checked) { out.copy_from = state.ws; out.copy_documents = $('#ws-copy-docs').checked; }
      closeModal();
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
      $('#ws-select').focus(); // Delete is disabled when one workspace is left
    } catch (e) { toast(`Delete failed: ${e.message}`, 'err'); }
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  let me;
  try { me = await loadSession(); } // signed out: goes to the sign-in page and never returns
  catch (e) { toast(e.message, 'err', 15000); return; }
  applyAccount(me);
  state.ws = storedWorkspace();
  try { await loadWorkspaces(); } catch (e) { toast(`Could not load workspaces: ${e.message}`, 'err'); }
  initWorkspaces();
  initTitleSuggestions();
  initTabs();
  initJobs();
  initSetup();
  initSchedule();
  initDocs();
  initChat();
  initModal();
  initTheme();
  $('#theme-picker-slot').replaceWith(createThemePicker({ legend: 'Theme', onChange: saveAccountTheme }));
  // Map markers are drawn in script, so they take the new theme's colours here.
  document.addEventListener('themechange', () => { if (state.map) { drawPins(); drawJobMarkers(); } });
  $('#source-form').addEventListener('submit', addSource);
  $('#score-stale').addEventListener('click', () => startRun('/api/score/run', { mode: 'stale' }));
  $('#score-all').addEventListener('click', () => { if (confirm('Rescore every job in this workspace? This makes one API call per job.')) startRun('/api/score/run', { mode: 'all' }); });
  loadAll();
  setInterval(pollForScheduledRun, IDLE_POLL_MS);
  document.addEventListener('visibilitychange', pollForScheduledRun);
});

/* The tasks tray (tasks-tray.js) names jobs, documents and workspaces from what this page has loaded. */
taskNames.job = (ws, id) => {
  if (ws !== state.ws) return null;
  const job = state.details.get(id) || state.jobs.find((j) => j.id === id);
  return job ? { title: job.title, company: job.company } : null;
};
taskNames.document = (ws, id) => (ws === state.docs.ws ? state.docs.list.find((d) => d.id === id)?.filename || null : null);
taskNames.workspace = (ws) => state.workspaces.find((w) => w.id === ws)?.name || null;
