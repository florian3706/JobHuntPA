/* JobHuntPA job page: one job in full, with its office, commute and company. */
'use strict';

const params = new URLSearchParams(location.search);
const JOB_ID = Number(params.get('id'));
const WS = Number(params.get('ws')) || (() => { try { return Number(localStorage.getItem('jobhunt.ws')) || 0; } catch { return 0; } })();
// Without one in the link or this browser: the account's first workspace.
function currentWorkspace() { return WS || SESSION.me?.workspaces?.[0] || 0; }

let job = null;

function showPageError(msg) {
  $('#job-head').innerHTML = '<h1 id="job-title" class="job-page-title">Job not available</h1><p><a href="./#jobs">Back to the dashboard</a></p>';
  $('#job-cols').hidden = true;
  document.title = 'Job not available · JobHuntPA';
  showError($('#job-error'), msg);
}

/* ---------- header ---------- */
function renderHead() {
  const mode = modeOf(job);
  // The main link goes to the employer's own page when a merged copy is on it; the listed ad is then "also on".
  const link = mainLink(job);
  const also = [
    ...(link.copy_id ? [{ url: job.url, source: job.source }] : []),
    ...(job.also_on || []).filter((c) => c.id !== link.copy_id),
  ].map((c) => `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}${NEW_TAB}</a>`).join(' · ');
  const facts = [
    `<span>${esc(job.location_text || 'location unknown')}</span>`,
    `<span class="badge ${mode}"><span class="visually-hidden">Work mode: </span>${mode}</span>`,
    employmentBadge(job),
    job.office_days ? `<span class="badge flag">${job.office_days} day${job.office_days > 1 ? 's' : ''} in office</span>` : '',
    job.distance_km !== null ? `<span><span aria-hidden="true">📍 </span>${job.distance_km} km<span class="muted"> straight line to your nearest pin</span></span>` : '',
    job.salary_text ? `<span><span aria-hidden="true">💰 </span><span class="visually-hidden">Salary: </span>${esc(job.salary_text)}</span>` : '',
    job.posted_at ? `<span class="muted">posted ${esc(fmtDate(job.posted_at).split(',')[0])}</span>` : '',
    `<span class="applied-slot">${appliedHtml(job)}</span>`,
    job.closed ? '<span class="badge flag">closed</span>' : '',
    job.recruiter ? '<span class="badge flag">recruitment agency</span>' : '',
  ].join('');
  $('#job-head').innerHTML = `
    <div class="job-top">
      <div class="job-title-row">
        <div class="job-company">${esc(job.company || '—')}</div>
        <h1 id="job-title" class="job-page-title"><a href="${esc(link.url)}" target="_blank" rel="noopener">${esc(job.title || '—')}<span class="visually-hidden">: the ad on ${esc(sourceLabel(link.source))}</span>${NEW_TAB}</a></h1>
        <div class="job-meta">${facts}</div>
        <div class="job-sub"><span class="muted">${esc(sourceLabel(link.source))}${also ? ` · also on ${also}` : ''}</span></div>
        ${job.excluded_reason ? `<p class="hint">Excluded: ${esc(job.excluded_reason)}</p>` : ''}
      </div>
      ${ratingsHtml(job, '#ghost-title')}
    </div>
    <div class="job-actions">
      <select id="job-status" aria-label="Application status">
        ${STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}
      </select>
      <a class="btn btn-ghost btn-sm" href="./#jobs">Back to the dashboard</a>
    </div>`;
  document.title = `${job.title} · ${job.company} · JobHuntPA`;
  // The circle links to the ghost section; its reasons are collapsed, so following the link opens them.
  $('#job-head a.ghost-dot')?.addEventListener('click', () => { const why = $('#ghost-why'); if (why) why.open = true; });
  $('#job-status').addEventListener('change', async (e) => {
    try {
      const res = await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: e.target.value }) });
      job.status = e.target.value;
      job.applied_at = res.applied_at ?? job.applied_at ?? null;
      $('#job-head .applied-slot').innerHTML = appliedHtml(job); // (the header isn't redrawn: focus stays)
      toast(`Status saved: ${STATUS_LABEL[job.status]}`, 'ok', 2500);
    } catch (err) { toast(`Status update failed: ${err.message}`, 'err'); e.target.value = job.status; }
  });
}

/* ---------- is this a real vacancy? ----------
   Every job has this section: the rating (the same circle as on its card) in words and what it rests on (the
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
    <p class="ghost-big"><span class="ghost-big-dot" aria-hidden="true">👻 ${ghostDot(job)}</span> <span>${verdict}${
      unknown ? ` ${unknown === 1 ? 'One check' : `${unknown} checks`} could not be made, so the rating rests on less.` : ''}</span></p>
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

function renderFit() {
  const fit = job.fit;
  $('#job-fit').innerHTML = fitHtml(job) + (fit && fit.status === 'ok' && ((fit.gaps || []).length || missingRequirements(fit).length)
    ? `<h3 class="gaps-title">Gaps</h3>${gapsHtml(job)}` : '');
}

function renderAd() {
  const note = job.detail_status === 'summary' ? '<p class="notice">Only the job board\'s listing summary is stored. Open the ad for the full text.</p>' : '';
  $('#job-ad').innerHTML = note + (job.description ? `<div class="ad-text">${esc(job.description)}</div>` : '<p class="muted">No description stored.</p>');
}

/* ---------- office ---------- */
const sameAddress = (a, b) => (a || '').replace(/\s+/g, ' ').trim().toLowerCase() === (b || '').replace(/\s+/g, ' ').trim().toLowerCase();

// Every office company research knows for the employer (spec §1.9): the one picked is stored as the job's office,
// and the commute links go to it.
function officeListHtml(offices) {
  if (!offices.length) return '';
  const ordered = offices.some((o) => o.near === true);
  const items = offices.map((o, i) => {
    const picked = !!job.office_text && sameAddress(o.address, job.office_text);
    return `<li><button class="btn btn-sm${picked ? ' btn-primary' : ''}" type="button" data-cand="${i}" aria-pressed="${picked}" aria-describedby="office-cands-intro"><span>${picked ? '<span aria-hidden="true">✓ </span>' : ''}${o.name ? `<strong>${esc(o.name)}</strong>: ` : ''}${esc(o.address)}${o.near === false ? ' <span class="muted">(not near the ad\'s place)</span>' : ''}</span></button></li>`;
  }).join('');
  return `<div class="office-cands"><p class="muted" id="office-cands-intro">The employer's offices that company research found${ordered ? ' (those near the ad\'s place first, closest to your pins first)' : ''}. Pick the one this job is at: the commute links go there.</p>
    <ul class="office-cands-ul">${items}</ul></div>`;
}

function renderOffice() {
  const known = officeHtml(job);
  let status;
  if (known) status = `<p>${known}</p>`;
  else if (job.office_unknown) {
    status = `<p class="notice">The ad only says <strong>${esc(job.location_text)}</strong>, so the office could be anywhere in it.
      ${job.recruiter ? "It's a recruiter's ad, so the client's office usually isn't named: set it if you know it, or make the call yourself." : 'Pick or set it if you know it (Fill missing info on the dashboard looks it up), or make the call yourself.'}
      Until then the commute links go to the city.</p>`;
  } else status = `<p class="muted">Office: ${esc(job.location_text || 'not stated')} (from the ad's location).</p>`;
  const offices = job.office_candidates || [];
  $('#job-office').innerHTML = `
    ${status}
    ${job.office_source === 'company' && offices.filter((o) => o.near).length > 1
    ? '<p class="muted">The employer has several offices near the ad\'s place; this is the closest to your pins. Pick another if you know which one it is.</p>' : ''}
    ${officeListHtml(offices)}
    <form id="office-form" class="toolbar-row office-form">
      <label class="field search-field"><span class="field-label">Office address or suburb</span>
        <input id="office-input" type="text" autocomplete="off" placeholder="e.g. 1 Denison St, North Sydney" value="${esc(job.office_source === 'user' ? job.office_text || '' : '')}" /></label>
      <button class="btn btn-primary btn-sm" type="submit">Set office</button>
      ${job.office_text ? '<button id="office-clear" class="btn btn-ghost btn-sm" type="button">Clear office</button>' : ''}
    </form>
    <div class="toolbar-row verdict-row" role="group" aria-labelledby="verdict-label">
      <span class="muted" id="verdict-label">Your call on the location:</span>
      <button class="btn btn-sm ${job.location_verdict === 'ok' ? 'btn-primary' : ''}" type="button" data-verdict="ok" aria-pressed="${job.location_verdict === 'ok'}" title="Keep this job whatever the location filter says">${job.location_verdict === 'ok' ? '<span aria-hidden="true">✓ </span>' : ''}Location OK</button>
      <button class="btn btn-sm ${job.location_verdict === 'too_far' ? 'btn-primary' : ''}" type="button" data-verdict="too_far" aria-pressed="${job.location_verdict === 'too_far'}" title="Exclude this job: the office is too far">${job.location_verdict === 'too_far' ? '<span aria-hidden="true">✓ </span>' : ''}Too far</button>
      ${job.location_verdict ? '<button class="btn btn-ghost btn-sm" type="button" data-verdict="" title="Let the location filter decide again">Undo<span class="visually-hidden"> your call on the location</span></button>' : ''}
    </div>`;
  $('#office-form').addEventListener('submit', (e) => { e.preventDefault(); saveOffice({ office: $('#office-input').value.trim() || null }); });
  $('#office-clear')?.addEventListener('click', () => saveOffice({ office: null }));
  $$('[data-cand]').forEach((b) => b.addEventListener('click', () => saveOffice({ office: job.office_candidates[Number(b.dataset.cand)].address })));
  $$('[data-verdict]').forEach((b) => b.addEventListener('click', () => saveOffice({ verdict: b.dataset.verdict || null })));
}

async function saveOffice(body) {
  // The office section is redrawn: keep keyboard focus on the same control.
  const active = document.activeElement;
  const back = active?.dataset?.verdict !== undefined ? `[data-verdict="${active.dataset.verdict}"]`
    : active?.id ? `#${active.id}` : active?.closest?.('#office-form') ? '#office-input' : null;
  try {
    job = await api(`/api/jobs/${job.id}/office`, { method: 'PUT', body: JSON.stringify(body) });
    renderHead();
    renderOffice();
    if (back) ($(back) || $('#office-input')).focus();
    toast(job.excluded_reason ? `Saved. The job is now excluded: ${job.excluded_reason}` : 'Saved', job.excluded_reason ? '' : 'ok', 4000);
    loadCommute();
  } catch (e) { toast(e.message, 'err', 8000); }
}

/* ---------- commute links ---------- */
// Google Maps directions from each starting point (spec §1.8). The server only says which places; the links are built
// here, and nothing is sent anywhere until the person opens one (in a new tab, with no referrer).
const TRAVEL_TEXT = { driving: 'Drive', transit: 'Public transport' };

function mapsDirectionsUrl(origin, destination, mode) {
  return `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}&travelmode=${mode === 'transit' ? 'transit' : 'driving'}`;
}

function commuteLink(start, mode, destination) {
  const li = document.createElement('li');
  const a = document.createElement('a');
  a.href = mapsDirectionsUrl(start.origin, destination, mode);
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = `${TRAVEL_TEXT[mode] || 'Directions'} from ${start.label}`;
  a.insertAdjacentHTML('beforeend', NEW_TAB);
  li.appendChild(a);
  return li;
}

async function loadCommute() {
  const box = $('#job-commute');
  box.innerHTML = '<p class="muted">Listing the commute links…</p>';
  let c;
  try { c = await api(`/api/jobs/${job.id}/commute`); } catch (e) {
    box.innerHTML = `<p class="err-text"><span aria-hidden="true">⚠ </span>No commute links: ${esc(e.message)}</p>`;
    return;
  }
  box.innerHTML = c.notes.map((n) => `<p class="notice">${esc(n)}</p>`).join('');
  if (!c.starts.length) {
    box.insertAdjacentHTML('beforeend', `<p class="muted">No starting points yet: drop a home pin on the <a href="./#map" target="_blank" rel="noopener">Map tab${NEW_TAB}</a>,
      or add starting points under <a href="./#search" target="_blank" rel="noopener">Search setup &gt; Commute${NEW_TAB}</a>.</p>`);
    return;
  }
  box.insertAdjacentHTML('beforeend', `<h3 class="commute-title" id="commute-links-title">Directions in Google Maps to ${esc(c.destination)}</h3>`);
  const list = document.createElement('ul');
  list.className = 'commute-links';
  list.setAttribute('aria-labelledby', 'commute-links-title');
  c.starts.forEach((start) => start.modes.forEach((mode) => list.appendChild(commuteLink(start, mode, c.destination))));
  box.appendChild(list);
  box.insertAdjacentHTML('beforeend', '<p class="hint">Opening a link shows Google both places: the starting point and the office. JobHuntPA itself sends them nowhere. Times and routes are chosen in Google Maps; your starting points are under Search setup &gt; Commute.</p>');
}

/* ---------- company ---------- */
async function loadCompany() {
  let p = null;
  try { p = (await api('/api/companies'))[job.company_key] || null; } catch { p = null; }
  $('#company-title').textContent = p?.official_name || job.company || 'Company';
  $('#job-company').innerHTML = job.company ? companyProfileHtml(p, job.company) : '<p class="muted">No employer named.</p>';
  // The profile's button researches all of it, a section's own button only that section: either at any time.
  $$('#job-company [data-research-part]').forEach((b) => b.addEventListener('click', () => researchCompany(b)));
}

async function researchCompany(button) {
  const status = $('#company-status');
  try {
    let run = await api('/api/companies/research', { method: 'POST', body: JSON.stringify({ name: job.company, part: button.dataset.researchPart }) });
    const hadFocus = document.activeElement === button;
    $$('#job-company [data-research-part]').forEach((b) => setBusy(b, true));
    while (run.state === 'queued' || run.state === 'running') {
      status.textContent = `Researching… ${run.stage || ''}`;
      await new Promise((r) => setTimeout(r, 2000));
      run = await api(`/api/runs/${run.id}`);
    }
    status.textContent = run.state === 'failed' ? `Research failed: ${run.error}` : `Research done: the profile above is updated.${researchRefreshText(run.summary?.research) ? ` ${researchRefreshText(run.summary.research)}.` : ''}`;
    await loadCompany();
    if (hadFocus) $('#company-title').focus(); // the button was redrawn with the profile
    return;
  } catch (e) { status.textContent = `Research failed: ${e.message}`; }
  loadCompany();
}

/* ---------- boot ---------- */
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  if (!JOB_ID) { showPageError('No job given. Open a job from the dashboard (Open ↗ on a job card).'); return; }
  try {
    const me = await loadSession(); // signed out: goes to the sign-in page, then back here
    initAccountMenu(me);
    remindRecoveryCodes(me);
    showSignInNotice();
    const workspaces = await api('/api/workspaces');
    const ws = workspaces.find((w) => w.id === currentWorkspace());
    if (ws) $('#ws-name').textContent = ws.name;
    job = await api(`/api/jobs/${JOB_ID}`);
  } catch (e) { showPageError(`Could not load this job: ${e.message}`); return; }
  renderHead();
  renderOffice();
  renderGhost();
  renderFit();
  renderAd();
  loadCompany();
  loadCommute();
});
