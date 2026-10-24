/* JobHuntPA job page: one job in full. A sticky summary (who, what, the fit score, the actions) over six in-page
   tabs: Overview, Fit, Company, Commute, The ad and Chats. Wherever requirements are listed, the gaps come before
   the matches. The URL hash remembers the tab (#fit, #company, …), so a link can open one directly; an id inside a
   tab (#ghost-title, the ad's marks) opens its tab too. Needs common.js, tasks-tray.js and chat.js. */
'use strict';

const params = new URLSearchParams(location.search);
const JOB_ID = Number(params.get('id'));
const WS = Number(params.get('ws')) || (() => { try { return Number(localStorage.getItem('jobhunt.ws')) || 0; } catch { return 0; } })();
// Without one in the link or this browser: the account's first workspace.
function currentWorkspace() { return WS || SESSION.me?.workspaces?.[0] || 0; }

let job = null;
let profile = null; // the employer's company profile, once loaded
let commute = null; // GET /api/jobs/{id}/commute, once loaded
let chatThreads = null; // the chats about this job (for the counts), once loaded
let otherJobs = null; // the workspace's other jobs (GET /api/jobs), loaded when a tab needs them
let docs = null; // the workspace's documents (how many are ticked for scoring), loaded with the Fit tab

const TABS = ['overview', 'fit', 'company', 'commute', 'ad', 'chats'];
const phone = window.matchMedia('(max-width: 719px)'); // the compact header and the bottom action bar
const narrow = window.matchMedia('(max-width: 899px)'); // the rails fold above the content

function showPageError(msg) {
  $('#job-title').textContent = 'Job not available';
  $('#job-where').textContent = '';
  $('#job-sub').innerHTML = '<a href="./#jobs">Back to the dashboard</a>';
  $$('.jp-summary__labels, .jp-summary__actions, .jp-tabs, .jp-panel').forEach((el) => { el.hidden = true; });
  document.title = 'Job not available · JobHuntPA';
  showError($('#job-error'), msg);
}

/* ---------- small helpers ---------- */
const MODE_WORDS = { remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site', unknown: 'Work mode unknown' };
const dayWords = (n) => `${n} day${n === 1 ? '' : 's'}`;
const dateOnly = (iso) => (iso ? fmtDate(iso).split(',')[0] : '');
const mustFirst = (a, b) => (b.must_have !== false) - (a.must_have !== false);
const byId = (id) => document.getElementById(id);
const scoreClass = (score) => `p-status-label--${fitTone(score)}`; // (the same steps as common.js fitLabel)
const label = (text, kind = '', extra = '') => `<span class="p-status-label${kind ? ` p-status-label--${kind}` : ''}${extra ? ` ${extra}` : ''}">${text}</span>`;
const svgTick = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12.5l4.5 4.5L19 7"></path></svg>';
const svgCross = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 6l12 12M18 6L6 18"></path></svg>';

/* The mode in words with the office days: "Hybrid, 2 days", "Remote", "On-site". */
function modeWords(j) {
  const mode = modeOf(j);
  return mode === 'hybrid' && j.office_days ? `Hybrid, ${dayWords(j.office_days)}` : MODE_WORDS[mode];
}

/* The requirements of a scored job, the gaps first (must-haves before nice-to-haves in each group). A gap the scorer
   named in its own words (fit.gaps) that is not one of the unmatched requirements counts as a gap too (`note`). Each
   keeps an id `i`: the anchors on the ad (#jp-ad-r{i}) and in its rail (#jp-rail-r{i}) use it. */
function requirementGroups(fit) {
  if (fit?.status !== 'ok') return { reqs: [], gaps: [], matched: [] };
  const reqs = (fit.requirements || []).map((r, i) => ({ ...r, i }));
  const unmatched = reqs.filter((r) => !r.matched);
  const notes = (fit.gaps || []).filter((g) => !unmatched.some((r) => r.point.toLowerCase() === g.toLowerCase()))
    .map((g, k) => ({ point: g, must_have: true, matched: false, evidence: [], note: true, i: `g${k}` }));
  return { reqs: [...reqs, ...notes], gaps: [...unmatched.sort(mustFirst), ...notes], matched: reqs.filter((r) => r.matched).sort(mustFirst) };
}

/* The ghost-job rating as a label that links to the explanation in the Overview. */
function ghostLabelHtml(j) {
  if (!j.ghost) return '';
  const level = j.ghost.level;
  const kind = level === 'likely' ? 'negative' : level === 'possible' ? 'caution' : 'positive';
  const words = level ? ghostWords(j) : 'Real vacancy: likely';
  return `<a class="p-status-label p-status-label--${kind} jp-ghost-link" href="#ghost-title" title="${esc(ghostHover(j))}">${esc(words)}<span class="visually-hidden">: why this rating</span></a>`;
}

/* The line under the fit summary: who scored it, when, and whether the documents have changed since. */
function scoredLine(j) {
  const fit = j.fit;
  if (!fit) return 'Not scored yet.';
  if (fit.status === 'error') return `Scoring failed: ${esc(fit.error || 'unknown error')}.`;
  return `Scored by ${esc(fit.model || 'the model')} on ${esc(fmtDate(fit.scored_at))}. ${fit.stale ? 'Your documents have changed since: the score is against the older ones.' : 'Your documents have not changed since.'}`;
}
const rescoreButton = (j, cls = 'p-button p-button--base is-dense') => `<button class="${cls}" type="button" data-act="rescore">${j.fit && j.fit.status === 'ok' ? 'Rescore' : 'Score this job'}</button>`;

/* ---------- the summary (sticky on wide screens) ---------- */
function renderHead() {
  const mode = modeOf(job);
  // The main link goes to the employer's own page when a merged copy is on it; the listed ad is then "also on".
  const link = mainLink(job);
  const copies = [
    ...(link.copy_id ? [{ url: job.url, source: job.source }] : []),
    ...(job.also_on || []).filter((c) => c.id !== link.copy_id),
  ];
  const also = copies.map((c) => `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}${NEW_TAB}</a>`).join(' · ');
  $('#job-where').textContent = [job.company || 'Employer not named', job.location_text || 'location unknown'].join(', ');
  $('#job-title').innerHTML = `<a href="${esc(link.url)}" target="_blank" rel="noopener">${esc(job.title || '—')}<span class="visually-hidden">: the ad on ${esc(sourceLabel(link.source))}</span>${NEW_TAB}</a>`;
  $('#job-sub').innerHTML = `<span class="jp-sub__phone">${esc(job.company || '—')}, ${esc((job.location_text || 'location unknown').split(';')[0])}. ${esc(modeWords(job))}</span>
    <span class="jp-sub__wide">${esc(sourceLabel(link.source))}${also ? ` · also on ${also}` : ''}${job.posted_at ? ` · posted ${esc(dateOnly(job.posted_at))}` : ''}</span>
    <span class="applied-slot">${appliedHtml(job)}</span>
    ${job.excluded_reason ? `<span class="jp-sub__excluded">Excluded: ${esc(job.excluded_reason)}</span>` : ''}`;
  $('#job-labels').innerHTML = [
    fitLabel(job, { large: true }),
    filterWarnButton(job),
    job.fit?.stale ? label('Older documents', 'caution', 'jp-phone-hide') : '',
    label(`<span class="visually-hidden">Work mode: </span>${esc(modeWords(job))}`, MODE_TONE[mode], 'jp-phone-hide'),
    ghostLabelHtml(job) ? `<span class="jp-phone-hide">${ghostLabelHtml(job)}</span>` : '',
    job.closed ? label('Closed', 'negative', 'jp-phone-hide') : '',
    job.recruiter ? label('Recruitment agency', '', 'jp-phone-hide') : '',
    job.hidden ? label('Hidden', '', 'jp-phone-hide') : '',
  ].join('');
  // "Always include": ticked, the filters never leave this job out (a job the person added is ticked already).
  $('#job-include').innerHTML = `${includeToggle(job)}`;
  document.title = `${job.title} · ${job.company} · JobHuntPA`;
  $('#job-head [data-act="filter-warnings"]')?.addEventListener('click', showFilterWarnings);
  $('#job-head [data-act="include"]').addEventListener('change', setIncluded);
  // The rating's label links to the Overview's section; its reasons are collapsed, so following the link opens them.
  $('#job-head a.jp-ghost-link')?.addEventListener('click', () => { const why = $('#ghost-why'); if (why) why.open = true; });
  const sel = $('#job-status');
  sel.innerHTML = STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('');
  $('#job-letter').innerHTML = job.has_cover_letter
    ? '<span class="jp-long">Cover letter<span aria-hidden="true"> ✓</span></span><span class="jp-short" aria-hidden="true">Letter ✓</span><span class="visually-hidden"> (drafted)</span>'
    : '<span class="jp-long">Cover letter</span><span class="jp-short" aria-hidden="true">Letter</span>';
  const opens = [{ url: link.url, source: link.source }, ...copies];
  $('#job-more-list').innerHTML = [
    `<li><button class="p-button p-button--base is-dense jp-more__item" type="button" data-act="rescore">${job.fit && job.fit.status === 'ok' ? 'Rescore' : 'Score this job'}</button></li>`,
    `<li><button class="p-button p-button--base is-dense jp-more__item" type="button" data-act="hide">${job.hidden ? 'Unhide' : 'Hide'}<span class="visually-hidden"> this job on the dashboard</span></button></li>`,
    '<li><button class="p-button p-button--base is-dense jp-more__item" type="button" data-act="tab:ad">Description<span class="visually-hidden">: open The ad</span></button></li>',
    ...opens.map((o) => `<li><a class="p-button p-button--base is-dense jp-more__item" href="${esc(o.url)}" target="_blank" rel="noopener">Open on ${esc(sourceLabel(o.source))}${NEW_TAB}</a></li>`),
    '<li><a class="p-button p-button--base is-dense jp-more__item" href="./#jobs">Back to the dashboard</a></li>',
  ].join('');
}

/* "Always include" (common.js includeToggle): the summary is redrawn for the job's new excluded reason and ⚠️, with
   focus back on the box. */
async function setIncluded(e) {
  const box = e.currentTarget;
  box.disabled = true;
  try {
    const updated = await api(`/api/jobs/${job.id}/included`, { method: 'PATCH', body: JSON.stringify({ included: box.checked }) });
    for (const k of ['included_by_user', 'excluded_reason', 'softened_reason', 'filter_warnings']) job[k] = updated[k];
    renderHead();
    $('#job-head [data-act="include"]').focus();
    toast(includedWords(job), 'ok', 6000);
  } catch (err) {
    box.checked = !box.checked;
    box.disabled = false;
    toast(`Could not change "Always include": ${err.message}`, 'err');
  }
}

/* ---------- the filters a job the person included doesn't meet (its ⚠️) ----------
   In the page's #modal: focus goes back to the ⚠️ when it closes (or to its redrawn copy, the summary having been
   drawn again meanwhile). */
function showFilterWarnings(e) {
  const back = e.currentTarget;
  openModal(filterWarningsTitle(job), filterWarningsHtml(job, './#search'),
    () => (back.isConnected ? back : $('#job-head [data-act="filter-warnings"]')));
}

function initStatus() {
  $('#job-status').addEventListener('change', async (e) => {
    try {
      const res = await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: e.target.value }) });
      job.status = e.target.value;
      job.applied_at = res.applied_at ?? job.applied_at ?? null;
      $('#job-head .applied-slot').innerHTML = appliedHtml(job); // (the header isn't redrawn: focus stays)
      renderFacts();
      toast(`Status saved: ${STATUS_LABEL[job.status]}`, 'ok', 2500);
    } catch (err) { toast(`Status update failed: ${err.message}`, 'err'); e.target.value = job.status; }
  });
}

/* The More menu: a disclosure of the less frequent actions. Escape closes it and returns focus to the button. */
function initMore() {
  const btn = $('#job-more'), menu = $('#job-more-menu');
  const open = (yes) => { menu.hidden = !yes; btn.setAttribute('aria-expanded', String(yes)); };
  btn.addEventListener('click', () => { open(menu.hidden); if (!menu.hidden) menu.querySelector('button, a')?.focus(); });
  document.addEventListener('click', (e) => { if (!menu.hidden && !e.target.closest('.jp-more')) open(false); });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); open(false); btn.focus(); } });
  menu.addEventListener('focusout', (e) => { if (!menu.contains(e.relatedTarget) && e.relatedTarget !== btn) open(false); });
}
function closeMore() { $('#job-more-menu').hidden = true; $('#job-more').setAttribute('aria-expanded', 'false'); }

/* On phones the status, Letter and Chat controls sit in a bar at the bottom of the screen; on wide screens in the
   summary. One set of controls, moved between the two. */
function placeControls() {
  const controls = $('#job-controls'), bar = $('#job-bar');
  if (phone.matches) { bar.appendChild(controls); bar.hidden = false; } else { $('.jp-summary__actions').prepend(controls); bar.hidden = true; }
}

/* The rails fold on narrow screens (details above the content, most of them closed); on wide screens they are
   open cards. Each fold's own `open` in the page says whether it starts open on a phone. */
function setFolds() {
  $$('.jp-fold').forEach((d) => {
    if (!('phoneOpen' in d.dataset)) d.dataset.phoneOpen = String(d.open);
    d.open = narrow.matches ? d.dataset.phoneOpen === 'true' : true;
  });
}

/* ---------- tabs ---------- */
function currentTab() { return TABS.find((t) => byId(`tab-${t}`).getAttribute('aria-selected') === 'true') || 'overview'; }

function selectTab(name, { focus = null, hash = true } = {}) {
  if (!TABS.includes(name)) name = 'overview';
  TABS.forEach((t) => {
    const tab = byId(`tab-${t}`), panel = byId(`panel-${t}`);
    const on = t === name;
    tab.setAttribute('aria-selected', String(on));
    tab.tabIndex = on ? 0 : -1;
    panel.hidden = !on;
  });
  byId(`tab-${name}`).scrollIntoView({ inline: 'nearest', block: 'nearest' });
  if (hash && location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'chats') ensureChat();
  if (name === 'fit' || name === 'company') ensureOtherJobs();
  if (name === 'fit') ensureDocs();
  if (focus === 'tab') byId(`tab-${name}`).focus();
  else if (focus === 'panel') byId(`panel-${name}`).focus({ preventScroll: true });
}

/* Which tab an id in the page belongs to (#ghost-title is the Overview's, the ad's marks are The ad's). */
function tabOf(el) { return el?.closest('.jp-panel')?.id.replace('panel-', '') || null; }

/* The hash: a tab name, or an id inside a tab (then that tab opens and the element is scrolled to and focused). */
function applyHash({ focus = 'panel' } = {}) {
  const id = decodeURIComponent(location.hash.slice(1));
  if (!id) { selectTab('overview', { hash: false }); return; }
  if (TABS.includes(id)) { selectTab(id, { hash: false, focus }); return; }
  const el = byId(id);
  const tab = tabOf(el);
  if (!tab) return;
  selectTab(tab, { hash: false });
  if (id === 'ghost-title') { const why = $('#ghost-why'); if (why) why.open = true; }
  el.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
  if (!el.hasAttribute('tabindex') && !['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY'].includes(el.tagName)) el.tabIndex = -1;
  el.focus({ preventScroll: true });
}

function initTabs() {
  const list = $('#job-tabs');
  list.addEventListener('click', (e) => {
    const tab = e.target.closest('[role="tab"]');
    if (tab) selectTab(tab.id.replace('tab-', ''), { focus: 'tab' });
  });
  list.addEventListener('keydown', (e) => {
    const keys = { ArrowRight: 1, ArrowLeft: -1, Home: 0, End: 0 };
    if (!(e.key in keys)) return;
    e.preventDefault();
    const focused = document.activeElement?.closest?.('[role="tab"]');
    const i = Math.max(0, TABS.indexOf(focused ? focused.id.replace('tab-', '') : currentTab()));
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : (i + keys[e.key] + TABS.length) % TABS.length;
    selectTab(TABS[next], { focus: 'tab' });
  });
  window.addEventListener('hashchange', () => applyHash());
}

/* ---------- Overview ---------- */
function renderVerdict() {
  const fit = job.fit;
  const box = $('#job-verdict-body');
  if (!fit) {
    box.innerHTML = `<p>Not scored yet. The scorer reads the ad against your ticked documents and says how well you fit, what to lean on and what to prepare for.</p><div class="u-row">${rescoreButton(job, 'p-button p-button--positive is-dense')}</div>`;
    return;
  }
  if (fit.status === 'error') {
    box.innerHTML = `<p class="err-text">Scoring failed: ${esc(fit.error || 'unknown error')}</p><div class="u-row">${rescoreButton(job, 'p-button is-dense')}</div>`;
    return;
  }
  const { reqs, gaps, matched } = requirementGroups(fit);
  const gapItems = gaps.map((r) => r.point);
  const item = (cls, svg, text) => `<li class="${cls}">${svg}<span>${esc(text)}</span></li>`;
  box.innerHTML = `
    <p class="jp-lead">${esc(fit.summary || 'The scorer gave no summary.')}</p>
    <div class="jp-two">
      <div class="u-stack jp-two__col">
        <h3 class="jp-h3 jp-h3--gap">Gaps: what to prepare for</h3>
        ${gapItems.length ? `<ul class="p-list jp-list">${gapItems.slice(0, 4).map((t) => item('p-list__item--crossed', svgCross, t)).join('')}</ul>` : '<p class="u-small u-muted">No gaps: every requirement has evidence in your documents.</p>'}
        <a class="p-button p-button--base is-dense jp-self-start" href="#fit">Open the Fit tab: all ${reqs.length} requirement${reqs.length === 1 ? '' : 's'}</a>
      </div>
      <div class="u-stack jp-two__col">
        <h3 class="jp-h3 jp-h3--match">Matched: what to lean on</h3>
        ${matched.length ? `<ul class="p-list jp-list">${matched.slice(0, 4).map((r) => item('p-list__item--ticked', svgTick, r.point)).join('')}</ul>` : '<p class="u-small u-muted">Nothing matched yet.</p>'}
      </div>
    </div>`;
}

/* ---------- is this a real vacancy? ----------
   Every job has this section: the rating (the same as in the summary) in words and what it rests on (the
   disclaimer), always shown; then, collapsed until asked for (#ghost-why), each kind of sign the app looks for and
   whether it was found, clear or could not be checked, what the three colours mean, and, for a flagged job, what to
   ask the employer. */
const GHOST_DISCLAIMER = 'This is worked out from your own searches only. It indicates whether an ad may be a ghost job or a real position; it proves neither.';
const GHOST_VERDICT = {
  likely: '<strong>Several signs</strong> suggest this ad may not be a current vacancy (a "ghost job").',
  possible: '<strong>Some signs</strong> suggest this ad may not be a current vacancy (a "ghost job").',
};
const GHOST_STRENGTH_LABEL = { strong: 'strong sign', moderate: 'moderate sign', weak: 'weak sign' };
const GHOST_STATE_RANK = { found: 0, clear: 1, unknown: 2 };
const GHOST_NOTE_CLASS = { likely: 'p-notification--negative', possible: 'p-notification--caution' };

function ghostCheckHtml(check, sign) {
  if (check.state === 'found' && sign) {
    return `<li class="ghost-check found ${sign.strength === 'strong' ? 'strong' : ''}">
      <span class="ghost-state"><span aria-hidden="true">⚠ </span>Found${GHOST_STRENGTH_LABEL[sign.strength] ? `: ${GHOST_STRENGTH_LABEL[sign.strength]}` : ''}</span>
      <strong>${esc(sign.text)}</strong>${sign.detail ? `<br><span class="muted">${esc(sign.detail)}</span>` : ''}</li>`;
  }
  const unknown = check.state === 'unknown';
  return `<li class="ghost-check ${unknown ? 'unknown' : 'clear'}">
    <span class="ghost-state">${unknown ? '<span aria-hidden="true">? </span>Could not be checked' : '<span aria-hidden="true">✓ </span>Not found'}</span>
    ${esc(check.label)}${check.note ? `<br><span class="muted">${esc(check.note)}</span>` : ''}</li>`;
}

function renderGhost() {
  const g = job.ghost;
  const box = $('#job-ghost-section');
  box.hidden = !g;
  if (!g) return;
  const signs = g.signals || [];
  const byCode = Object.fromEntries(signs.map((s) => [s.code, s]));
  const order = (c) => (c.state === 'found' ? signs.findIndex((s) => s.code === c.code) : 0); // found: strongest first
  // (a server from before the checks lists only what it found)
  const checks = (g.checks?.length ? g.checks : signs.map((s) => ({ code: s.code, state: 'found' })))
    .slice().sort((a, b) => GHOST_STATE_RANK[a.state] - GHOST_STATE_RANK[b.state] || order(a) - order(b));
  const unknown = checks.filter((c) => c.state === 'unknown').length;
  const verdict = GHOST_VERDICT[g.level]
    || (signs.length ? `<strong>No strong signs</strong> of a ghost job. ${signs.length === 1 ? 'One weak sign was' : 'A few weak signs were'} found: too little to flag this job.`
      : '<strong>No signs</strong> of a ghost job: none of the signs the app looks for were found.');
  $('#job-ghost').innerHTML = `
    <div class="p-notification ${GHOST_NOTE_CLASS[g.level] || 'p-notification--positive'}" role="note"><div class="p-notification__body">
    <p class="ghost-big"><span class="ghost-big-dot" aria-hidden="true">👻 ${ghostDot(job)}</span> <span>${verdict}${
      unknown ? ` ${unknown === 1 ? 'One check' : `${unknown} checks`} could not be made, so the rating rests on less.` : ''}</span></p>
    </div></div>
    <p class="hint ghost-disclaimer">${GHOST_DISCLAIMER}</p>
    <details id="ghost-why" class="ghost-details"><summary>${g.level ? 'Why this rating, and what to do' : 'Why this rating'}</summary>
    <h3 class="ghost-sub" id="ghost-why-title">Why this rating</h3>
    <ul class="ghost-checks" aria-labelledby="ghost-why-title">${checks.map((c) => ghostCheckHtml(c, byCode[c.code])).join('')}</ul>
    <h3 class="ghost-sub" id="ghost-key-title">What the colours mean</h3>
    <ul class="ghost-key" aria-labelledby="ghost-key-title">
      <li><span aria-hidden="true">🟢 </span><strong>Green:</strong> no signs, or only one or two weak ones.</li>
      <li><span aria-hidden="true">🟡 </span><strong>Yellow:</strong> some signs. The job is flagged as a possible ghost job.</li>
      <li><span aria-hidden="true">🔴 </span><strong>Red:</strong> one strong sign, or several moderate ones.</li>
    </ul>
    <p class="muted">A rating from the signs above, not proof: the app can't know whether a job is real.</p>
    ${g.level ? `<h3 class="ghost-sub">What to do</h3>
    <p>These are signs, not proof: this may well be a real job. Employers sometimes keep an ad up while they interview, or post it again to reach more people.</p>
    <p>You could ask the recruiter or employer:</p>
    <ul class="ghost-ask">
      <li>Is the role open now, and has anyone been hired for it?</li>
      <li>When do they plan to interview, and when do they need someone to start?</li>
      <li>${signs.some((s) => s.code === 'reposted' || s.code === 'date_refreshed') ? 'Why was the ad posted again?'
        : signs.some((s) => s.code === 'evergreen_wording' || s.code === 'ad_signs') ? 'Is this for a specific team, or are they building a list of candidates?'
        : 'Has the closing date or the number of openings changed?'}</li>
    </ul>` : ''}
    </details>`;
}

function renderNext() {
  const n = chatThreads?.length ?? 0;
  const rows = [
    [job.has_cover_letter ? 'A cover letter is drafted for this job' : 'No cover letter yet',
      `<button class="p-button is-dense" type="button" data-act="letter">${job.has_cover_letter ? 'Open the letter' : 'Draft a cover letter'}</button>`],
    [chatThreads === null ? 'Chats about this job' : n ? `${n} chat${n === 1 ? '' : 's'} about this job` : 'No chats about this job yet',
      `<button class="p-button is-dense" type="button" data-act="tab:chats">${n ? 'Continue the chat' : 'Start a chat'}</button>`],
    [scoredLine(job), rescoreButton(job)],
  ];
  $('#job-next-list').innerHTML = rows.map(([text, action]) => `<li class="u-row jp-next__row"><span>${text}</span>${action}</li>`).join('');
}

function renderFacts() {
  const link = mainLink(job);
  const copies = [...(link.copy_id ? [{ url: job.url, source: job.source }] : []), ...(job.also_on || []).filter((c) => c.id !== link.copy_id)];
  const by = { ad: 'from the ad', company: 'from company research', user: 'set by you' };
  const office = job.office_text
    ? `${esc(job.office_text)} <span class="u-small u-muted">(${by[job.office_source] || ''}${job.office_placed ? '' : ', not found on the map'})</span>`
    : job.office_unknown ? `Unknown: the ad only says ${esc(job.location_text || 'nothing')}` : esc(job.location_text || 'Not stated');
  const facts = [
    ['Salary', job.salary_text ? esc(job.salary_text) : '<span class="u-muted">Not stated</span>'],
    ['Work mode', esc(modeWords(job)) + (modeOf(job) === 'hybrid' && !job.office_days ? ' <span class="u-small u-muted">(days not stated)</span>' : '')],
    ['Office', office],
    ['Nearest pin', job.distance_km !== null && job.distance_km !== undefined ? `${job.distance_km} km straight line` : '<span class="u-muted">Not placed on the map</span>'],
    ['Posted', `${job.posted_at ? esc(dateOnly(job.posted_at)) : '<span class="u-muted">date unknown</span>'} on ${esc(sourceLabel(link.source))}${
      copies.length ? `; also on ${copies.map((c) => `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}${NEW_TAB}</a>`).join(', ')}` : ''}`],
    ['Employment', EMPLOYMENT_LABEL[job.employment_type] || '<span class="u-muted">Not stated</span>'],
    ['Status', `${esc(STATUS_LABEL[job.status] || job.status)}${job.status === 'applied' && job.applied_at ? ` (applied ${esc(dateOnly(job.applied_at))})` : ''}`],
  ];
  if (job.closed) facts.push(['Listing', 'Closed: the source no longer lists it']);
  if (job.recruiter) facts.push(['Advertiser', 'A recruitment agency; the employer is usually not named']);
  if (job.excluded_reason) facts.push(['Filters', `Excluded: ${esc(job.excluded_reason)}`]);
  if (job.softened_reason) facts.push(['Filters', `Let through by the office-days what-if: ${esc(job.softened_reason)}`]);
  $('#job-facts').innerHTML = facts.map(([k, v]) => `<div class="jp-fact"><dt class="u-small u-muted">${k}</dt><dd>${v}</dd></div>`).join('');
}

function renderCompanyBrief() {
  const box = $('#job-company-brief');
  const name = profile?.official_name || job.company;
  $('#company-brief-title').textContent = name || 'Company';
  if (!job.company) { box.innerHTML = '<p class="u-small u-muted">The ad names no employer.</p>'; return; }
  if (!profile || !RESEARCHED.includes(profile.status)) {
    box.innerHTML = '<p class="u-small u-muted">Not researched yet.</p><a class="p-button p-button--base is-dense jp-self-start" href="#company">Company tab</a>';
    return;
  }
  if (profile.status !== 'done') {
    box.innerHTML = `<p class="u-small">${profile.status === 'error' ? `Research failed: ${esc(profile.error || '')}` : 'Not an identifiable organisation (for example an anonymous advertiser).'}</p><a class="p-button p-button--base is-dense jp-self-start" href="#company">Company tab</a>`;
    return;
  }
  const bits = [profile.ownership, profile.employee_count ? `${profile.employee_count} people` : '', profile.headquarters].filter(Boolean).map((b) => b.trim().replace(/\.$/, ''));
  const s = profile.sentiment;
  box.innerHTML = `
    ${profile.business_model ? `<p class="u-small">${esc(profile.business_model)}</p>` : ''}
    ${bits.length ? `<p class="u-small">${esc(bits.join('. '))}.</p>` : ''}
    <p class="u-small">${s?.rating ? `${esc(s.rating)}. ` : ''}${profile.controversies?.length ? `${profile.controversies.length} News/Controversies item${profile.controversies.length === 1 ? '' : 's'}. ` : ''}<a href="#company">Company tab</a></p>`;
}

/* ---------- Fit ---------- */
function reqRow(r, kind) {
  const nice = r.must_have === false ? '<span class="u-small u-muted">nice to have</span>' : '';
  const ev = (r.evidence || []).map((e) => {
    const subs = (e.sub_bullets || []).filter(Boolean);
    return `<li>${esc(e.bullet)}${subs.length ? `<ul class="jp-evidence__subs">${subs.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}</li>`;
  }).join('');
  return `<li class="u-stack jp-req" id="jp-fit-r${r.i}">
    <div class="u-row jp-req__head"><strong>${esc(r.point)}</strong>${kind === 'gap' ? label('Gap', 'negative') : label('Matched', 'positive')}${nice}<a class="u-small" href="#jp-ad-r${r.i}">In the ad</a></div>
    ${kind === 'gap' ? `<p class="u-small u-muted">${r.note ? 'Named by the scorer as a gap, in its own words.' : 'No evidence in your documents.'}</p>` : ev ? `<ul class="jp-evidence u-small" aria-label="Evidence">${ev}</ul>` : '<p class="u-small u-muted">Matched without a quoted line of evidence.</p>'}
  </li>`;
}

function renderFit() {
  const fit = job.fit;
  const box = $('#job-fit');
  const count = $('#tab-fit-count');
  if (!fit || fit.status !== 'ok') {
    count.innerHTML = '';
    box.innerHTML = `<section class="p-card u-stack jp-card" aria-labelledby="fit-title"><h2 id="fit-title" class="jp-h2">How well you fit</h2>
      <p>${fit ? `<span class="err-text">Scoring failed: ${esc(fit.error || 'unknown error')}</span>` : 'Not scored yet.'} The scorer reads the ad against your ticked documents (resumes first, then cover letters and notes as supporting evidence) and lists each requirement with the evidence for it.</p>
      <div class="u-row">${rescoreButton(job, 'p-button p-button--positive is-dense')}</div></section>`;
    renderFitHow();
    return;
  }
  const { gaps, matched } = requirementGroups(fit);
  count.innerHTML = gaps.length ? ` ${label(`${gaps.length} gap${gaps.length === 1 ? '' : 's'}`, 'negative')}` : '';
  box.innerHTML = `
    <section class="p-card u-stack jp-card" aria-labelledby="fit-title">
      <h2 id="fit-title" class="jp-h2">How well you fit, in the scorer's words</h2>
      <p class="jp-lead">${esc(fit.summary || 'The scorer gave no summary.')}</p>
      <p class="u-small u-muted">${scoredLine(job)} ${rescoreButton(job, 'p-button p-button--base is-dense jp-inline-btn')}</p>
    </section>
    <section class="p-card u-stack jp-card" aria-labelledby="gaps-title">
      <h2 id="gaps-title" class="jp-h2 jp-h2--gap">Gaps: ${gaps.length ? `${gaps.length} requirement${gaps.length === 1 ? '' : 's'} without evidence` : 'none'}</h2>
      ${gaps.length ? `<ul class="p-list p-list--divided jp-reqs">${gaps.map((r) => reqRow(r, 'gap')).join('')}</ul>` : '<p>Every requirement the scorer found has evidence in your documents.</p>'}
      ${gaps.length ? '<div class="u-row"><a class="p-button is-dense" href="./#docs">Add a note to your documents</a><button class="p-button p-button--base is-dense" type="button" data-act="ask:How should I address my gaps in an interview?">Ask the chat how to present this</button></div>' : ''}
    </section>
    <section class="p-card u-stack jp-card" aria-labelledby="matched-title">
      <h2 id="matched-title" class="jp-h2 jp-h2--match">Matched: ${matched.length ? `${matched.length} requirement${matched.length === 1 ? '' : 's'} with evidence` : 'none'}</h2>
      ${matched.length ? `<ul class="p-list p-list--divided jp-reqs">${matched.map((r) => reqRow(r, 'match')).join('')}</ul>` : '<p>No requirement has evidence in your documents yet.</p>'}
    </section>`;
  renderFitHow();
}

function renderFitHow() {
  const fit = job.fit;
  const ticked = docs ? docs.filter((d) => d.use_for_scoring).length : null;
  const rows = [];
  if (fit?.status === 'ok') {
    rows.push(['Model', esc(fit.model || 'unknown')], ['Scored', esc(fmtDate(fit.scored_at))]);
    rows.push(['Documents since', fit.stale ? 'Changed: the score is against the older ones' : 'Unchanged']);
  }
  rows.push(['Ad text', job.detail_status === 'summary' ? 'Listing summary only' : 'Full description']);
  if (ticked !== null) rows.push(['Documents ticked now', `${ticked} of ${docs.length}`]);
  $('#job-fit-how').innerHTML = `
    <p class="u-small">The AI reads the ad against your ticked documents: resumes first, then cover letters, then notes as supporting evidence. Preferences are checked for conflicts but never count as experience. The number is its overall judgement, not a count of matches.</p>
    <dl class="jp-facts jp-facts--rows u-small">${rows.map(([k, v]) => `<div class="jp-fact"><dt class="u-muted">${k}</dt><dd>${v}</dd></div>`).join('')}</dl>`;
}

function renderCompare() {
  const card = $('#job-compare-card');
  if (!otherJobs) { card.hidden = true; return; }
  const scored = otherJobs.filter((j) => j.id !== job.id && scoreOf(j) !== null).sort((a, b) => scoreOf(b) - scoreOf(a)).slice(0, 3);
  if (!scored.length) { card.hidden = true; return; }
  const row = (text, score, href) => `<li class="u-row jp-compare__row">${href ? `<a href="${href}">${text}</a>` : `<span>${text}</span>`}${label(`<span class="visually-hidden">Fit score </span>${score}`, scoreClass(score))}</li>`;
  card.hidden = false;
  $('#job-compare').innerHTML = `<ul class="p-list jp-compare u-small">
    ${scoreOf(job) !== null ? row('This job', scoreOf(job)) : '<li class="u-muted">This job: not scored yet</li>'}
    ${scored.map((j) => row(`${esc(j.title || '—')} <span class="u-muted">${esc(j.company || '')}</span>`, scoreOf(j), `./job.html?id=${j.id}&ws=${currentWorkspace()}`)).join('')}
  </ul><p class="u-small u-muted">Your three best-scored other jobs. To compare them in a chat, tick them on the Jobs tab and choose Chat about selected.</p>`;
}

async function ensureOtherJobs() {
  if (otherJobs) return;
  try { otherJobs = await api('/api/jobs'); } catch { otherJobs = []; }
  renderCompare();
  renderOtherJobs();
}

async function ensureDocs() {
  if (docs) return;
  try { docs = await api('/api/documents'); } catch { docs = null; return; }
  renderFitHow();
}

/* ---------- Company ---------- */
async function loadCompany() {
  try { profile = (await api('/api/companies'))[job.company_key] || null; } catch { profile = null; }
  renderCompany();
  renderCompanyBrief();
}

function researchNote(p) {
  // The research button is a real bordered button on its own line, not a link-like word inside the sentence.
  const note = (tone, text, btnText) => `<div class="p-notification p-notification--${tone}" role="note"><div class="p-notification__body u-small"><p>${text}</p><p><button id="company-research" class="p-button is-dense" type="button" data-research-part="all">${btnText}</button></p></div></div>`;
  if (!job.company) return '';
  if (!p) return note('information', 'Not researched yet. <strong>Fill missing info</strong> on the dashboard researches every company in your list, or research just this one.', 'Research this company');
  if (p.status === 'error') return note('negative', `Research failed: ${esc(p.error || '')}`, 'Research this company again');
  if (p.status === 'skipped') return note('information', `"${esc(p.name)}" isn't an identifiable organisation (for example an anonymous advertiser).`, 'Research it anyway');
  return note('information', `Researched ${esc(fmtDate(p.researched_at))} with web search. Only links the search actually returned are shown${p.unverified_dropped ? `; ${p.unverified_dropped} unverifiable link(s) and their claims were removed` : ''}. Automated research can be wrong: check the linked pages.`, 'Research the whole profile again');
}

function hostOf(url) { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; } }

function renderCompany() {
  let p = profile;
  if (p && !RESEARCHED.includes(p.status)) p = null; // only offices known so far
  $('#job-company-note').innerHTML = researchNote(p);
  const main = $('#job-company');
  const sources = $('#job-company-sources');
  if (!job.company) { main.innerHTML = '<p class="u-muted">No employer named.</p>'; sources.innerHTML = ''; }
  else if (!p || p.status !== 'done') {
    main.innerHTML = `<section class="p-card u-stack jp-card" aria-labelledby="co-title"><div class="p-card__head jp-card__head"><h2 id="co-title" class="jp-h2">${esc(job.company)}</h2>${glassdoorLink(p, job.company)}</div>
      <p class="u-small u-muted">${p ? 'No profile to show.' : 'No profile yet: how they make money, ownership, headquarters, offices, employee sentiment and news appear here once researched.'}</p></section>`;
    sources.innerHTML = '<p class="u-small u-muted">None yet.</p>';
  } else {
    const dl = [
      ['Ownership', esc(p.ownership || 'Unknown')],
      ['Headquarters', esc(p.headquarters || 'Unknown')],
      ['Employees', esc(p.employee_count || (p.research_version >= 2 ? 'Unknown' : 'Not researched yet'))],
    ];
    if ((p.offices || []).length) {
      dl.push(['Offices', `<ul class="jp-offices">${p.offices.map((o) => `<li>${o.name ? `<strong>${esc(o.name)}:</strong> ` : ''}${esc(o.address)}${o.url ? ` (<a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">source${NEW_TAB}</a>)` : ''}</li>`).join('')}</ul>`]);
    }
    const s = p.sentiment;
    const themes = (items, cls, svg, title, titleCls) => `<div class="u-stack jp-two__col"><h3 class="jp-h3 ${titleCls}">${title}</h3>${
      (items || []).length ? `<ul class="p-list jp-list">${items.map((t) => `<li class="${cls}">${svg}<span>${esc(t)}</span></li>`).join('')}</ul>` : '<p class="u-small u-muted">None named.</p>'}</div>`;
    const cons = (p.controversies || []).length
      ? `<ul class="p-list p-list--divided jp-news">${p.controversies.map((c) => `<li class="u-stack jp-news__item"><strong>${esc(c.title)}</strong>${c.year ? ` <span class="u-muted">(${esc(c.year)})</span>` : ''}
          <p>${esc(c.summary)}</p>${(c.sources || []).length ? `<p class="u-small jp-sources">${linkList(c.sources)}</p>` : ''}</li>`).join('')}</ul>` : '';
    main.innerHTML = `
      <section class="p-card u-stack jp-card" aria-labelledby="co-title">
        <div class="p-card__head jp-card__head"><h2 id="co-title" class="jp-h2">${esc(p.official_name || job.company)}</h2>${glassdoorLink(p, job.company)}</div>
        ${p.is_recruiter ? '<div class="p-notification p-notification--caution" role="note"><div class="p-notification__body u-small">This is a recruitment agency. The employer behind the ad is usually not disclosed; ask the recruiter who the client is.</div></div>' : ''}
        <h3 class="jp-h3">How they make money</h3>
        <p>${esc(p.business_model || 'Unknown')}</p>
        <dl class="jp-dl">${dl.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
        <p class="u-small u-muted">Researched ${esc(fmtDate(p.researched_at))}.</p>
      </section>
      <section class="p-card u-stack jp-card" aria-labelledby="sentiment-title">
        <h2 id="sentiment-title" class="jp-h2">Employee sentiment${s?.rating ? `: ${esc(s.rating)}` : ''}</h2>
        ${s ? `<p>${esc(s.summary || 'No employee reviews found.')}</p>
          <div class="jp-two">${themes(s.negatives, 'p-list__item--crossed', svgCross, 'Often criticised', 'jp-h3--gap')}${themes(s.positives, 'p-list__item--ticked', svgTick, 'Often praised', 'jp-h3--match')}</div>
          ${(s.sources || []).length ? `<p class="u-small jp-sources">${linkList(s.sources)}</p>` : ''}` : '<p class="u-muted">Not researched yet.</p>'}
        ${sectionResearch('sentiment', p.sentiment_researched_at)}
      </section>
      <section class="p-card u-stack jp-card" aria-labelledby="news-title">
        <h2 id="news-title" class="jp-h2">News/Controversies</h2>
        <p>${esc(p.controversy_note || ((p.controversies || []).length ? '' : 'None found.'))}</p>${cons}
        ${sectionResearch('news', p.news_researched_at)}
      </section>`;
    sources.innerHTML = (p.sources || []).length
      ? `<ul class="p-list jp-source-list u-small">${p.sources.map((x) => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title || x.url)}${NEW_TAB}</a> <span class="u-muted">${esc(hostOf(x.url))}</span></li>`).join('')}</ul>`
      : '<p class="u-small u-muted">The research named no sources.</p>';
  }
  $('#job-company-ask').innerHTML = ['What should I ask about their plans in an interview?', `How stable is ${job.company || 'this employer'} as an employer?`, 'What do employees say about working there?']
    .map((q) => `<li><button class="p-button p-button--base is-dense jp-ask__btn" type="button" data-act="ask:${esc(q)}">${esc(q)}</button></li>`).join('');
  renderOtherJobs();
  // The profile's button researches all of it, a section's own button only that section: either at any time.
  $$('#job-company [data-research-part]').forEach((b) => b.addEventListener('click', () => researchCompany(b)));
  $('#job-company-note [data-research-part]')?.addEventListener('click', (e) => researchCompany(e.currentTarget));
}

function renderOtherJobs() {
  const box = $('#job-company-others');
  $('#other-jobs-title').textContent = `Other jobs at ${profile?.official_name || job.company || 'this company'}`;
  if (!otherJobs) { box.innerHTML = '<p class="u-small u-muted">Loading…</p>'; return; }
  const others = otherJobs.filter((j) => j.id !== job.id && j.company_key === job.company_key);
  box.innerHTML = others.length
    ? `<ul class="p-list jp-others u-small">${others.map((j) => `<li class="u-row jp-compare__row"><a href="./job.html?id=${j.id}&ws=${currentWorkspace()}">${esc(j.title || '—')}</a>${scoreOf(j) !== null ? label(`<span class="visually-hidden">Fit score </span>${scoreOf(j)}`, scoreClass(scoreOf(j))) : ''}</li>`).join('')}</ul>`
    : '<p class="u-small u-muted">None in this workspace.</p>';
}

async function researchCompany(button) {
  const status = $('#company-status');
  try {
    let run = await api('/api/companies/research', { method: 'POST', body: JSON.stringify({ name: job.company, part: button.dataset.researchPart }) });
    const hadFocus = document.activeElement === button;
    $$('#panel-company [data-research-part]').forEach((b) => setBusy(b, true));
    while (run.state === 'queued' || run.state === 'running') {
      status.textContent = `Researching… ${run.stage || ''}`;
      await new Promise((r) => setTimeout(r, 2000));
      run = await api(`/api/runs/${run.id}`);
    }
    status.textContent = run.state === 'failed' ? `Research failed: ${run.error}` : `Research done: the profile above is updated.${researchRefreshText(run.summary?.research) ? ` ${researchRefreshText(run.summary.research)}.` : ''}`;
    await loadCompany();
    if (hadFocus) $('#co-title')?.focus(); // the button was redrawn with the profile
    return;
  } catch (e) { status.textContent = `Research failed: ${e.message}`; }
  loadCompany();
}

/* ---------- Commute: the office ---------- */
const sameAddress = (a, b) => (a || '').replace(/\s+/g, ' ').trim().toLowerCase() === (b || '').replace(/\s+/g, ' ').trim().toLowerCase();

// Every office company research knows for the employer (spec §1.9): the one picked is stored as the job's office,
// and the directions go to it. A radio each, with "Another address" for one typed in.
function officeListHtml(offices) {
  if (!offices.length) return '';
  const ordered = offices.some((o) => o.near === true);
  const pickedIndex = offices.findIndex((o) => !!job.office_text && sameAddress(o.address, job.office_text));
  const items = offices.map((o, i) => `<label class="p-check jp-office__choice"><input class="p-radio" type="radio" name="office-choice" value="${i}" ${pickedIndex === i ? 'checked' : ''} />
      <span>${o.name ? `<strong>${esc(o.name)}:</strong> ` : ''}${esc(o.address)}${o.near === false ? ' <span class="u-muted">(not near the ad\'s place)</span>' : ''}</span></label>`).join('');
  return `<fieldset class="jp-office__list">
    <legend class="p-form-help-text" id="office-cands-intro">The employer's offices that company research found${ordered ? ' (those near the ad\'s place first, closest to your pins first)' : ''}. Pick the one this job is at: the directions go there and the distance filter uses it.</legend>
    ${items}
    <label class="p-check jp-office__choice"><input class="p-radio" type="radio" name="office-choice" value="other" ${pickedIndex < 0 && job.office_source === 'user' ? 'checked' : ''} /><span>Another address</span></label>
  </fieldset>`;
}

function renderOffice() {
  const known = officeHtml(job);
  let status;
  if (known) status = `<p class="jp-office__line">${known}${job.distance_km !== null && job.distance_km !== undefined ? ` <span class="u-muted">${job.distance_km} km straight line to your nearest pin.</span>` : ''}</p>`;
  else if (job.office_unknown) {
    status = `<div class="p-notification p-notification--caution" role="note"><div class="p-notification__body u-small">The ad only says <strong>${esc(job.location_text)}</strong>, so the office could be anywhere in it.
      ${job.recruiter ? "It's a recruiter's ad, so the client's office usually isn't named: set it if you know it, or make the call yourself." : 'Pick or set it if you know it (Fill missing info on the dashboard looks it up), or make the call yourself.'}
      Until then the directions go to the city.</div></div>`;
  } else status = `<p class="u-muted">Office: ${esc(job.location_text || 'not stated')} (from the ad's location).</p>`;
  const offices = job.office_candidates || [];
  $('#job-office').innerHTML = `
    ${status}
    ${job.office_source === 'company' && offices.filter((o) => o.near).length > 1
    ? '<p class="u-small u-muted">The employer has several offices near the ad\'s place; this is the closest to your pins. Pick another if you know which one it is.</p>' : ''}
    ${officeListHtml(offices)}
    <form id="office-form" class="p-field jp-office__form">
      <label class="p-label" for="office-input">Office address or suburb</label>
      <div class="u-row jp-office__row">
        <input id="office-input" class="p-input jp-office__input" type="text" autocomplete="off" placeholder="e.g. 1 Denison St, North Sydney" value="${esc(job.office_source === 'user' ? job.office_text || '' : '')}" />
        <button class="p-button p-button--positive is-dense" type="submit">Set office</button>
        ${job.office_text ? '<button id="office-clear" class="p-button p-button--base is-dense" type="button">Clear office</button>' : ''}
      </div>
    </form>
    <div class="u-row jp-verdict" role="group" aria-labelledby="verdict-label">
      <span class="u-small u-muted" id="verdict-label">Your call on the location:</span>
      <button class="p-button is-dense" type="button" data-verdict="ok" aria-pressed="${job.location_verdict === 'ok'}" title="Keep this job whatever the location filter says">${job.location_verdict === 'ok' ? '<span aria-hidden="true">✓ </span>' : ''}Location OK</button>
      <button class="p-button is-dense" type="button" data-verdict="too_far" aria-pressed="${job.location_verdict === 'too_far'}" title="Exclude this job: the office is too far">${job.location_verdict === 'too_far' ? '<span aria-hidden="true">✓ </span>' : ''}Too far</button>
      ${job.location_verdict ? '<button class="p-button p-button--base is-dense" type="button" data-verdict="" title="Let the location filter decide again">Undo<span class="visually-hidden"> your call on the location</span></button>' : ''}
      <span class="u-small u-muted">Overrides the pin radius for this job only.</span>
    </div>`;
  $('#office-form').addEventListener('submit', (e) => { e.preventDefault(); saveOffice({ office: $('#office-input').value.trim() || null }); });
  $('#office-clear')?.addEventListener('click', () => saveOffice({ office: null }));
  $$('input[name="office-choice"]').forEach((r) => r.addEventListener('change', () => {
    if (r.value === 'other') { $('#office-input').focus(); return; }
    saveOffice({ office: job.office_candidates[Number(r.value)].address });
  }));
  $$('[data-verdict]').forEach((b) => b.addEventListener('click', () => saveOffice({ verdict: b.dataset.verdict || null })));
  renderThere();
}

/* What the ad says about being there, against this person's rules and pins. */
function renderThere() {
  const mode = modeOf(job);
  const place = job.location_text ? ` in ${esc(job.location_text.split(';')[0])}` : '';
  const modeText = mode === 'remote' ? 'The ad says this job is remote.'
    : mode === 'hybrid' ? (job.office_days ? `${dayWords(job.office_days)} a week in the office${place}, the rest from home.` : `Hybrid${place}; the ad does not say how many office days.`)
      : mode === 'onsite' ? `In the office${place} every day.` : 'The ad does not say whether the job is remote, hybrid or in the office.';
  const rule = job.excluded_reason ? `Excluded by your filters: ${esc(job.excluded_reason)}.`
    : job.softened_reason ? `Let through by the office-days what-if on the dashboard: ${esc(job.softened_reason)}.`
      : job.location_verdict === 'ok' ? 'Your call: the location is fine for this job.'
        : job.location_verdict === 'too_far' ? 'Your call: too far.' : 'Passes your filters.';
  const pin = job.distance_km !== null && job.distance_km !== undefined ? `${job.distance_km} km straight line to your nearest pin.`
    : job.office_text && !job.office_placed ? 'The office was not found on the map, so no distance.' : 'No distance: no pins yet, or the place is not on the map. Pins are on the Map tab.';
  $('#job-there').innerHTML = [
    [label(esc(MODE_WORDS[mode]), mode === 'remote' ? 'positive' : mode === 'hybrid' ? 'caution' : mode === 'onsite' ? 'information' : ''), modeText],
    [label('Your rule'), rule],
    [label('Pin'), pin],
  ].map(([l, t]) => `<li class="u-row jp-there__row">${l}<span>${t}</span></li>`).join('');
}

async function saveOffice(body) {
  // The office section is redrawn: keep keyboard focus on the same control.
  const active = document.activeElement;
  const back = active?.dataset?.verdict !== undefined ? `[data-verdict="${active.dataset.verdict}"]`
    : active?.name === 'office-choice' ? `input[name="office-choice"][value="${active.value}"]`
      : active?.id ? `#${active.id}` : active?.closest?.('#office-form') ? '#office-input' : null;
  try {
    job = await api(`/api/jobs/${job.id}/office`, { method: 'PUT', body: JSON.stringify(body) });
    renderHead();
    renderFacts();
    renderOffice();
    if (back) ($(back) || $('#office-input')).focus();
    toast(job.excluded_reason ? `Saved. The job is now excluded: ${job.excluded_reason}` : 'Saved', job.excluded_reason ? '' : 'ok', 4000);
    loadCommute();
  } catch (e) { toast(e.message, 'err', 8000); }
}

/* ---------- Commute: directions ---------- */
// Google Maps directions from each starting point (spec §1.8). The server only says which places; the links are built
// here, and nothing is sent anywhere until the person opens one (in a new tab, with no referrer).
const TRAVEL_TEXT = { driving: 'Drive', transit: 'Public transport' };
const TRAVEL_MODES = ['driving', 'transit'];

function mapsDirectionsUrl(origin, destination, mode) {
  return `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&travelmode=${mode === 'transit' ? 'transit' : 'driving'}`;
}

/* A directions link: "Drive from Home" in a list; in the table the starting point is hidden text (its row says it). */
function commuteLink(start, mode, destination, { inTable = false } = {}) {
  const a = document.createElement('a');
  a.href = mapsDirectionsUrl(start.origin, destination, mode);
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = inTable ? TRAVEL_TEXT[mode] || 'Directions' : `${TRAVEL_TEXT[mode] || 'Directions'} from ${start.label}`;
  if (inTable) a.insertAdjacentHTML('beforeend', `<span class="visually-hidden"> from ${esc(start.label)}, in Google Maps</span>`);
  a.insertAdjacentHTML('beforeend', NEW_TAB);
  return a;
}

/* The short list of links (the Overview's rail and The ad's office card). */
function commuteBriefHtml() {
  if (!commute) return '<p class="u-small u-muted">Listing the commute links…</p>';
  if (commute.error) return `<p class="u-small err-text"><span aria-hidden="true">⚠ </span>No commute links: ${esc(commute.error)}</p>`;
  if (!commute.starts.length) return '<p class="u-small u-muted">No starting points yet: drop a home pin on the Map tab, or add starting points under Search setup &gt; Commute. <a href="#commute">Commute tab</a></p>';
  const list = document.createElement('ul');
  list.className = 'p-list jp-commute-list u-small';
  list.setAttribute('aria-label', `Directions to ${commute.destination}`);
  commute.starts.forEach((start) => start.modes.forEach((mode) => { const li = document.createElement('li'); li.appendChild(commuteLink(start, mode, commute.destination)); list.appendChild(li); }));
  return `${commute.to_office ? '' : '<p class="u-small u-muted">The links go to the city: the office is not set.</p>'}${list.outerHTML}`;
}

function renderCommuteBriefs() {
  $('#job-commute-brief').innerHTML = commuteBriefHtml();
  $('#job-ad-office').innerHTML = `<p class="u-small">${officeHtml(job) || esc(job.location_text || 'No place named')}${job.distance_km !== null && job.distance_km !== undefined ? ` ${job.distance_km} km straight line to your nearest pin.` : ''}</p>${commuteBriefHtml()}`;
}

async function loadCommute() {
  const box = $('#job-commute');
  box.innerHTML = '<p class="u-small u-muted">Listing the commute links…</p>';
  commute = null;
  renderCommuteBriefs();
  let c;
  try { c = await api(`/api/jobs/${job.id}/commute`); } catch (e) {
    commute = { error: e.message, starts: [], notes: [] };
    box.innerHTML = `<p class="err-text"><span aria-hidden="true">⚠ </span>No commute links: ${esc(e.message)}</p>`;
    renderCommuteBriefs();
    return;
  }
  commute = c;
  renderCommuteBriefs();
  box.innerHTML = c.notes.map((n) => `<div class="p-notification p-notification--caution" role="note"><div class="p-notification__body u-small">${esc(n)}</div></div>`).join('');
  if (!c.starts.length) {
    box.insertAdjacentHTML('beforeend', `<p class="u-muted">No starting points yet: drop a home pin on the <a href="./#map" target="_blank" rel="noopener">Map tab${NEW_TAB}</a>,
      or add starting points under <a href="./#search" target="_blank" rel="noopener">Search setup &gt; Commute${NEW_TAB}</a>.</p>`);
    return;
  }
  box.insertAdjacentHTML('beforeend', `<p class="u-small u-muted" id="commute-links-title">To ${esc(c.destination)}. One link for each starting point and way of getting there; times and the route are chosen in Google Maps.</p>`);
  const wrap = document.createElement('div');
  wrap.className = 'p-table-wrap';
  const table = document.createElement('table');
  table.className = 'p-table jp-directions';
  table.setAttribute('aria-labelledby', 'commute-links-title');
  table.innerHTML = `<thead><tr><th scope="col">From</th>${TRAVEL_MODES.map((m) => `<th scope="col">${TRAVEL_TEXT[m]}</th>`).join('')}</tr></thead><tbody></tbody>`;
  const tbody = table.querySelector('tbody');
  c.starts.forEach((start) => {
    const tr = document.createElement('tr');
    const th = document.createElement('th');
    th.scope = 'row';
    th.textContent = start.label;
    tr.appendChild(th);
    TRAVEL_MODES.forEach((mode) => {
      const td = document.createElement('td');
      if (start.modes.includes(mode)) td.appendChild(commuteLink(start, mode, c.destination, { inTable: true }));
      else { td.className = 'u-muted'; td.textContent = 'Not set'; }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  wrap.appendChild(table);
  box.appendChild(wrap);
  box.insertAdjacentHTML('beforeend', `<p class="hint">Opening a link shows Google both places: the starting point and the office. JobHuntPA itself sends them nowhere.</p>
    <a class="p-button p-button--base is-dense jp-self-start" href="./#search">Starting points are under Search setup, Commute</a>`);
}

/* ---------- The ad, annotated ----------
   Each requirement the scorer found is tied to the sentence of the ad it most overlaps with (plain word overlap,
   worked out here in the browser; nothing is sent anywhere). A word label follows that sentence, "Gap" or
   "Matched", and links to the requirement's line in the "Fit at a glance" rail, which links back to the sentence.
   A requirement that matches no sentence well enough gets its label under the heading instead. */
const AD_STOP = new Set(('the a an and or of to in for with on at by from as is are be this that you your our we will have has ' +
  'who what their they them its into over under than then also able about across after any all can could should would ' +
  'must may more most very well work working experience years year plus strong good').split(' '));
function adStem(w) { return w.replace(/(ations|ation|tion|ments|ment|ness|ings|ing|ies|ers|er|es|ed|ly|s|e)$/, ''); }
function adTokens(text) {
  return [...new Set((text.toLowerCase().match(/[a-z0-9][a-z0-9+#./-]*/g) || []).map((w) => w.replace(/[./-]+$/, ''))
    .filter((w) => w.length >= 3 && !AD_STOP.has(w)).map(adStem).filter(Boolean))];
}

/* The ad as pieces: sentences (text) and the gaps between them (sep), in order. */
function splitAd(text) {
  const out = [];
  text.split(/(\n+)/).forEach((part) => {
    if (!part) return;
    if (/^\n+$/.test(part)) { out.push({ sep: part }); return; }
    const pieces = part.split(/(?<=[.!?])\s+(?=[A-Z0-9"“(])/);
    pieces.forEach((s, i) => { out.push({ text: s }); if (i < pieces.length - 1) out.push({ sep: ' ' }); });
  });
  return out;
}

/* For each requirement, the index of the piece it is tied to, or -1. */
function placeRequirements(pieces, reqs) {
  const pieceTokens = pieces.map((p) => (p.text ? new Set(adTokens(p.text)) : null));
  return reqs.map((r) => {
    const want = adTokens(r.point);
    if (!want.length) return -1;
    let best = -1, bestScore = 0;
    pieceTokens.forEach((set, i) => {
      if (!set) return;
      const hits = want.filter((w) => set.has(w)).length;
      const score = hits / want.length;
      const enough = (hits >= 3 && score >= 0.4) || (hits >= 2 && score >= 0.5) || (want.length <= 2 && hits >= 1 && want[0].length >= 4);
      if (enough && score > bestScore) { best = i; bestScore = score; }
    });
    return best;
  });
}

function adLabelHtml(r, kind) {
  const words = `${kind === 'gap' ? 'Gap' : 'Matched'}${r.must_have === false ? ', nice to have' : ''}`;
  return `<a class="p-status-label p-status-label--${kind === 'gap' ? 'negative' : 'positive'} jp-ad-label" href="#jp-rail-r${r.i}" title="${esc(r.point)}">${words}<span class="visually-hidden">: ${esc(r.point)}. Go to it in Fit at a glance</span></a>`;
}

function renderAd() {
  const link = mainLink(job);
  const box = $('#job-ad-body');
  const { reqs, gaps, matched } = requirementGroups(job.fit);
  const ordered = [...gaps, ...matched]; // gaps first wherever labels sit side by side
  const kindOf = (r) => (r.matched ? 'match' : 'gap');
  const intro = `<p class="p-form-help-text jp-ad__intro">The ad as the employer wrote it, from ${esc(sourceLabel(link.source))}${job.posted_at ? ` on ${esc(dateOnly(job.posted_at))}` : ''}.${
    reqs.length ? ' Where the fit analysis found a gap or a match, a word label follows the sentence and links to the rail; the rail links back. Labels are words, so they read the same in every theme and to a screen reader.' : ''}</p>`;
  const note = job.detail_status === 'summary' ? '<div class="p-notification p-notification--caution" role="note"><div class="p-notification__body u-small">Only the job board\'s listing summary is stored. Open the ad for the full text.</div></div>' : '';
  let body = '<p class="u-muted">No description stored.</p>';
  let unplaced = ordered;
  if (job.description) {
    const pieces = splitAd(job.description);
    const at = placeRequirements(pieces, ordered);
    const labelsAt = new Map(); // piece index -> requirements (gaps first)
    ordered.forEach((r, k) => { if (at[k] >= 0) labelsAt.set(at[k], [...(labelsAt.get(at[k]) || []), r]); });
    unplaced = ordered.filter((r, k) => at[k] < 0);
    body = `<div class="jp-ad__text">${pieces.map((p, i) => {
      if (p.sep !== undefined) return p.sep;
      const rs = labelsAt.get(i);
      if (!rs) return esc(p.text);
      const kinds = new Set(rs.map(kindOf));
      const cls = kinds.size === 2 ? 'jp-mark--both' : kinds.has('gap') ? 'jp-mark--gap' : 'jp-mark--match';
      return `<mark class="jp-mark ${cls}" id="jp-ad-r${rs[0].i}" tabindex="-1">${esc(p.text)}</mark>${rs.slice(1).map((r) => `<span id="jp-ad-r${r.i}" class="jp-anchor"></span>`).join('')} ${rs.map((r) => adLabelHtml(r, kindOf(r))).join(' ')}`;
    }).join('')}</div>`;
  }
  const unplacedHtml = unplaced.length
    ? `<p class="u-small jp-ad__unplaced"><span class="u-muted">Not tied to one sentence: </span>${unplaced.map((r) => `<span id="jp-ad-r${r.i}" class="jp-anchor" tabindex="-1">${adLabelHtml(r, kindOf(r))}</span>`).join(' ')}</p>` : '';
  const copies = [...(link.copy_id ? [{ url: job.url, source: job.source }] : []), ...(job.also_on || []).filter((c) => c.id !== link.copy_id)];
  const upload = isSeekJob(job) && job.detail_status === 'summary' ? uploadAdHtml() : '';
  const opens = [{ url: link.url, source: link.source }, ...copies].map((o) => `<a class="p-button p-button--base is-dense" href="${esc(o.url)}" target="_blank" rel="noopener">Open on ${esc(sourceLabel(o.source))}${NEW_TAB}</a>`).join('');
  box.innerHTML = `${intro}${unplacedHtml}${note}${body}<div class="u-row jp-ad__actions">${opens}</div>${upload}`;
  initUploadAd();
  renderGlance(gaps, matched);
}

function renderGlance(gaps, matched) {
  const row = (r, kind) => `<li id="jp-rail-r${r.i}" class="u-row jp-glance__row"><a href="#jp-ad-r${r.i}">${esc(r.point)}${r.must_have === false ? ' <span class="u-muted">(nice to have)</span>' : ''}</a>${kind === 'gap' ? label('Gap', 'negative') : label('Matched', 'positive')}</li>`;
  const fit = job.fit;
  $('#glance-title').textContent = fit?.status === 'ok' ? `Fit at a glance: ${gaps.length} gap${gaps.length === 1 ? '' : 's'}, ${matched.length} matched` : 'Fit at a glance';
  if (fit?.status !== 'ok') {
    $('#job-glance').innerHTML = `<p class="u-small u-muted">${scoredLine(job)}</p>${rescoreButton(job, 'p-button is-dense')}`;
    return;
  }
  $('#job-glance').innerHTML = `
    <h3 class="jp-h3 jp-h3--gap">Gaps: what to prepare for</h3>
    ${gaps.length ? `<ul class="p-list jp-glance">${gaps.map((r) => row(r, 'gap')).join('')}</ul>` : '<p class="u-small u-muted">None.</p>'}
    <h3 class="jp-h3 jp-h3--match">Matched: what to lean on</h3>
    ${matched.length ? `<ul class="p-list jp-glance">${matched.map((r) => row(r, 'match')).join('')}</ul>` : '<p class="u-small u-muted">None yet.</p>'}
    <p class="u-small u-muted">${scoredLine(job)} ${rescoreButton(job, 'p-button p-button--base is-dense jp-inline-btn')}</p>`;
}

/* Upload the full ad (a SEEK job with only the listing summary): the page saved in the browser, matched by SEEK's
   job number (POST /api/jobs/{id}/import-page), then rescored. */
function uploadAdHtml() {
  const mac = /Mac/i.test(navigator.platform);
  return `<details class="p-details jp-upload"><summary>Upload the full ad</summary>
    <div class="upload-ad u-stack">
      <p>${job.source === 'seek' ? 'SEEK only lets the app read the short listing summary.' : 'JobHuntPA has only a short summary of this job.'} To score this job on the full ad:</p>
      <ol>
        <li><a href="${esc(job.url)}" target="_blank" rel="noopener">Open the ad on SEEK${NEW_TAB}</a> and wait for it to load.</li>
        <li>Save the page: press <kbd>${mac ? 'Cmd' : 'Ctrl'}</kbd>+<kbd>S</kbd> (any "Webpage" format works).</li>
        <li><label for="ad-file">Choose the saved file here:</label></li>
      </ol>
      <input id="ad-file" type="file" accept=".html,.htm,.mhtml,.mht,text/html" />
      <p id="ad-status" class="muted" role="status"></p>
    </div></details>`;
}

function initUploadAd() {
  $('#ad-file')?.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const status = $('#ad-status');
    status.textContent = 'Reading the ad…';
    const fd = new FormData();
    fd.append('file', file, file.name);
    try {
      job = await api(`/api/jobs/${job.id}/import-page`, { method: 'POST', body: fd });
      renderAll();
      const open = $('#job-ad .jp-upload');
      if (open) open.open = true;
      const said = $('#ad-status');
      if (job.excluded_reason) { said.innerHTML = `Updated. With the full ad this job is now excluded: ${esc(job.excluded_reason)}.`; return; }
      said.textContent = 'Updated with the full ad. Rescoring…';
      try {
        job = await api(`/api/score/${job.id}`, { method: 'POST' });
        renderAll();
        toast(`Updated and rescored: ${job.fit?.score ?? '?'}.`, 'ok');
      } catch (err) { toast(`Updated with the full ad, but rescoring failed (${err.message}). Use Rescore later.`, 'err', 9000); }
    } catch (err) {
      status.textContent = '';
      setFieldError(e.target, err.message);
      e.target.value = '';
    }
  });
}

/* ---------- actions: rescore, hide, the chat ---------- */
async function rescoreJob(btn) {
  const back = btn?.closest('#job-more-menu') ? '#job-more' : btn?.closest('.jp-panel') ? `#${btn.closest('.jp-panel').id}` : null;
  $$('[data-act="rescore"]').forEach((b) => { setBusy(b, true); b.textContent = 'Scoring…'; });
  announce('Scoring this job…');
  try {
    job = await api(`/api/score/${job.id}`, { method: 'POST' });
    renderAll();
    toast(`Scored: ${job.fit?.score ?? '?'} out of 100`, 'ok');
    announce(`Scored: ${job.fit?.score ?? '?'} out of 100.`);
  } catch (e) {
    toast(`Scoring failed: ${e.message}`, 'err', 9000);
    try { job = await api(`/api/jobs/${job.id}`); } catch { /* keep what we have */ }
    renderAll();
  }
  closeMore();
  if (back) $(back)?.focus();
}

async function toggleHidden() {
  const hidden = !job.hidden;
  try {
    await api('/api/jobs/hidden', { method: 'PATCH', body: JSON.stringify({ ids: [job.id], hidden }) });
    job.hidden = hidden;
    renderHead();
    toast(hidden ? 'Hidden. Tick "Show hidden" on the dashboard to see it again.' : 'Unhidden.', 'ok', 3500);
  } catch (e) { toast(`Could not ${hidden ? 'hide' : 'unhide'}: ${e.message}`, 'err'); }
  closeMore();
  $('#job-more').focus();
}

let chatOpened = false;
async function ensureChat() {
  if (chatOpened) return;
  chatOpened = true;
  await openChat([job.id], { jobs: [job], focus: false });
  chatThreads = chatState.chat?.threads || chatThreads;
  renderChatCount();
  renderNext();
}

function renderChatCount() {
  const n = chatState.chat?.threads.length ?? chatThreads?.length ?? 0;
  $('#tab-chats-count').innerHTML = n ? ` ${label(String(n))}<span class="visually-hidden"> chat${n === 1 ? '' : 's'}</span>` : '';
}

async function loadChatCount() {
  try { chatThreads = await api(`/api/chats?job_ids=${job.id}`); } catch { chatThreads = null; }
  renderChatCount();
  renderNext();
}

/* The page's buttons that aren't wired one by one: data-act="rescore" | "hide" | "letter" | "tab:<name>" | "ask:<question>". */
function initActions() {
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || !job) return;
    const act = btn.dataset.act;
    if (act === 'rescore') rescoreJob(btn);
    else if (act === 'hide') toggleHidden();
    else if (act === 'letter') showCoverLetter();
    else if (act.startsWith('tab:')) { closeMore(); selectTab(act.slice(4), { focus: 'panel' }); }
    else if (act.startsWith('ask:')) { selectTab('chats'); ensureChat().then(() => draftChatQuestion(act.slice(4))); }
  });
  $('#job-chat').addEventListener('click', () => { selectTab('chats'); ensureChat().then(() => $('#chat-input').focus()); });
  $('#job-letter').addEventListener('click', () => showCoverLetter());
  document.addEventListener('chat:changed', () => { chatThreads = chatState.chat?.threads || chatThreads; renderChatCount(); renderNext(); });
}

/* ---------- the dialog (#modal, a native <dialog>) ----------
   showModal() moves focus in, keeps it there (the page behind is inert) and closes on Escape; a click on the backdrop
   closes it too. When it closes, its content goes and focus goes back to `back()` (by default the control that
   opened it, while it is still in the page). The cover letter window (letter.js) and the ⚠️ list open in it. */
let modalReturn = null;
function openModal(title, html, back = null) {
  const dlg = $('#modal');
  $('#modal-title').textContent = title;
  $('#modal-html').innerHTML = html;
  $('#modal-html').scrollTop = 0;
  if (dlg.open) return;
  const opener = document.activeElement;
  modalReturn = back || (() => (opener?.isConnected ? opener : null));
  dlg.showModal();
}

function initModal() {
  const dlg = $('#modal');
  $('#modal-close').addEventListener('click', () => dlg.close());
  // A click on the backdrop lands on the <dialog> itself (its box fills it otherwise).
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener('close', () => {
    $('#modal-html').innerHTML = ''; // nothing of the old content stays reachable
    (modalReturn?.() || $('#main')).focus();
    modalReturn = null;
  });
}

/* ---------- the cover letter: letter.js's window, in #modal ---------- */
function showCoverLetter() {
  const dialog = $('#modal');
  return openLetterWindow(job, {
    dialog,
    open: (title, html) => openModal(title, html),
    close: () => dialog.close(),
    docsHref: './#docs',
    onChange: () => { renderHead(); renderNext(); renderFacts(); },
  });
}

/* ---------- boot ---------- */
function renderAll() {
  renderHead();
  renderVerdict();
  renderGhost();
  renderNext();
  renderFacts();
  renderFit();
  renderCompare();
  renderOffice();
  renderAd();
  renderCommuteBriefs();
  renderCompanyBrief();
}

/* The sticky summary's height is kept in --jp-sticky-h, so a focused control or a hash target never scrolls in
   underneath it (WCAG 2.4.11). */
function trackStickyHeight() {
  const el = $('#job-summary');
  const set = () => document.documentElement.style.setProperty('--jp-sticky-h', `${phone.matches ? 0 : Math.ceil(el.getBoundingClientRect().height)}px`);
  set();
  if ('ResizeObserver' in window) new ResizeObserver(set).observe(el); else window.addEventListener('resize', set);
}

document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  placeControls();
  setFolds();
  phone.addEventListener('change', placeControls);
  narrow.addEventListener('change', setFolds);
  trackStickyHeight();
  if (!JOB_ID) { showPageError('No job given. Open a job from the dashboard (Open ↗ on a job card).'); return; }
  try {
    const me = await loadSession(); // signed out: goes to the sign-in page, then back here
    initAccountMenu(me);
    remindRecoveryCodes(me);
    showSignInNotice();
    const workspaces = await api('/api/workspaces');
    taskNames.workspace = (id) => workspaces.find((w) => w.id === id)?.name || null;
    const ws = workspaces.find((w) => w.id === currentWorkspace());
    if (ws) $('#ws-name').textContent = ws.name;
    job = await api(`/api/jobs/${JOB_ID}`);
  } catch (e) { showPageError(`Could not load this job: ${e.message}`); return; }
  initTabs();
  initStatus();
  initMore();
  initActions();
  initModal();
  initChat({ findJob: (id) => (id === job.id ? job : null) });
  renderAll();
  applyHash({ focus: null });
  loadCompany();
  loadCommute();
  loadChatCount();
});

/* The tasks tray (tasks-tray.js) names this page's job. */
taskNames.job = (ws, id) => (job && id === job.id && ws === currentWorkspace() ? { title: job.title, company: job.company } : null);
