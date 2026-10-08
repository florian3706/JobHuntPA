/* JobHuntPA job page: one job in full, with its office, commute and company. */
'use strict';

const params = new URLSearchParams(location.search);
const JOB_ID = Number(params.get('id'));
const WS = Number(params.get('ws')) || (() => { try { return Number(localStorage.getItem('jobhunt.ws')) || 0; } catch { return 0; } })();
// Without one in the link or this browser: the account's first workspace.
function currentWorkspace() { return WS || SESSION.me?.workspaces?.[0] || 0; }

let job = null;
const view = { map: null, layer: null };
// Theme slot per mode of transport (the label is always shown too).
const MODE_HUE = { Train: 'warn', Metro: 'info', 'Light rail': 'special', Bus: 'accent', Coach: 'accent', 'School bus': 'accent',
  Ferry: 'ok', Cycle: 'ok', Walk: 'quiet' };
const modeHue = (mode) => MODE_HUE[mode] || 'quiet';
let lastCommute = null; // redrawn in the new colours when the theme changes

function minutes(n) {
  if (n === null || n === undefined) return '';
  return n >= 60 ? `${Math.floor(n / 60)} h ${n % 60} min` : `${n} min`;
}

function showPageError(msg) {
  $('#job-head').innerHTML = '<h1 id="job-title" class="job-page-title">Job not available</h1><p><a href="./#jobs">Back to the dashboard</a></p>';
  $('#job-cols').hidden = true;
  document.title = 'Job not available · JobHuntPA';
  showError($('#job-error'), msg);
}

/* ---------- header ---------- */
function renderHead() {
  const mode = modeOf(job);
  const also = (job.also_on || []).map((c) => `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}${NEW_TAB}</a>`).join(' · ');
  const facts = [
    `<span>${esc(job.location_text || 'location unknown')}</span>`,
    `<span class="badge ${mode}"><span class="visually-hidden">Work mode: </span>${mode}</span>`,
    employmentBadge(job),
    job.office_days ? `<span class="badge flag">${job.office_days} day${job.office_days > 1 ? 's' : ''} in office</span>` : '',
    job.distance_km !== null ? `<span><span aria-hidden="true">📍 </span>${job.distance_km} km<span class="muted"> straight line to your nearest pin</span></span>` : '',
    job.salary_text ? `<span><span aria-hidden="true">💰 </span><span class="visually-hidden">Salary: </span>${esc(job.salary_text)}</span>` : '',
    job.posted_at ? `<span class="muted">posted ${esc(fmtDate(job.posted_at).split(',')[0])}</span>` : '',
    job.closed ? '<span class="badge flag">closed</span>' : '',
    job.recruiter ? '<span class="badge flag">recruitment agency</span>' : '',
  ].join('');
  $('#job-head').innerHTML = `
    <div class="job-top">
      <div class="job-title-row">
        <div class="job-company">${esc(job.company || '—')}</div>
        <h1 id="job-title" class="job-page-title"><a href="${esc(job.url)}" target="_blank" rel="noopener">${esc(job.title || '—')}<span class="visually-hidden">: the ad on ${esc(sourceLabel(job.source))}</span>${NEW_TAB}</a></h1>
        <div class="job-meta">${facts}</div>
        <div class="job-sub"><span class="muted">${esc(sourceLabel(job.source))}${also ? ` · also on ${also}` : ''}</span></div>
        ${job.excluded_reason ? `<p class="hint">Excluded: ${esc(job.excluded_reason)}</p>` : ''}
      </div>
      ${scoreBadge(job)}
    </div>
    <div class="job-actions">
      <select id="job-status" aria-label="Application status">
        ${STATUSES.map((s) => `<option value="${s}" ${job.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}
      </select>
      <a class="btn btn-ghost btn-sm" href="./#jobs">Back to the dashboard</a>
    </div>`;
  document.title = `${job.title} · ${job.company} · JobHuntPA`;
  $('#job-status').addEventListener('change', async (e) => {
    try {
      await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: e.target.value }) });
      job.status = e.target.value;
      toast(`Status saved: ${STATUS_LABEL[job.status]}`, 'ok', 2500);
    } catch (err) { toast(`Status update failed: ${err.message}`, 'err'); e.target.value = job.status; }
  });
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
function renderOffice() {
  const known = officeHtml(job);
  let status;
  if (known) status = `<p>${known}</p>`;
  else if (job.office_unknown) {
    status = `<p class="notice">The ad only says <strong>${esc(job.location_text)}</strong>, so the office could be anywhere in it.
      ${job.recruiter ? "It's a recruiter's ad, so the client's office usually isn't named: set it if you know it, or make the call yourself." : 'Set it if you know it (Fill missing info on the dashboard looks it up), or make the call yourself.'}
      Until then the commute is to the city centre.</p>`;
  } else status = `<p class="muted">Office: ${esc(job.location_text || 'not stated')} (from the ad's location).</p>`;
  const cands = (job.office_candidates || []).map((o, i) => `<li><button class="btn btn-sm" type="button" data-cand="${i}" title="${esc(o.address)}" aria-describedby="office-cands-intro">${esc(o.name || o.address)}${o.name ? `<span class="visually-hidden">, ${esc(o.address)}</span>` : ''}</button></li>`).join('');
  $('#job-office').innerHTML = `
    ${status}
    ${job.office_source === 'company' && (job.office_candidates || []).length > 1
    ? `<p class="muted">The employer has ${job.office_candidates.length} offices here; this is the closest to your commute. Pick another if you know which one it is.</p>` : ''}
    ${cands ? `<div class="office-cands"><span class="muted" id="office-cands-intro">The employer's offices in this city (closest to your commute first):</span> <ul class="office-cands-ul">${cands}</ul></div>` : ''}
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

/* ---------- commute ---------- */
function journeyHtml(j, label) {
  if (!j) return `<div class="trip"><strong>${label}</strong> <span class="muted">no journey found</span></div>`;
  const to = '<span aria-hidden="true"> → </span><span class="visually-hidden"> to </span>';
  const legs = j.legs.map((l) => `<li><span class="leg-mode" style="--c:var(--${modeHue(l.mode)})">${esc(l.mode)}${l.line ? ` ${esc(l.line)}` : ''}</span>
    ${esc(l.depart)} ${esc(l.from)}${to}${esc(l.arrive)} ${esc(l.to)} <span class="muted">(${minutes(l.minutes)}${l.towards && l.mode !== 'Walk' ? `, towards ${esc(l.towards)}` : ''})</span></li>`).join('');
  return `<details class="trip"><summary><strong>${label}</strong> ${esc(j.depart)}${to}${esc(j.arrive)} · <strong>${minutes(j.minutes)}</strong>
      · ${esc(j.summary)}${j.changes ? ` · ${j.changes} change${j.changes > 1 ? 's' : ''}` : ''}</summary><ol class="legs" aria-label="Steps">${legs}</ol></details>`;
}

function carHtml(c, label) {
  if (!c) return '';
  const when = c.depart && c.arrive ? `${esc(c.depart)}<span aria-hidden="true"> → </span><span class="visually-hidden"> to </span>${esc(c.arrive)} · ` : '';
  const traffic = c.traffic ? (c.traffic_minutes ? ` (incl. ${minutes(c.traffic_minutes)} of traffic)` : ' (with traffic)') : ' <span class="muted">(no traffic)</span>';
  return `<div class="trip"><strong>${label}</strong> ${when}<strong>${minutes(c.minutes)}</strong>${traffic} · ${c.km} km</div>`;
}

// A commute note may point to the admin panel (API keys): make that a link.
const noteHtml = (n) => esc(n).replace('the admin panel', isAdmin() ? `<a href="admin.html#server" target="_blank" rel="noopener">the admin panel${NEW_TAB}</a>` : 'the admin panel');

async function loadCommute() {
  const box = $('#job-commute');
  box.innerHTML = '<p class="muted">Working out the commute…</p>';
  let c;
  try { c = await api(`/api/jobs/${job.id}/commute`); } catch (e) {
    box.innerHTML = `<p class="err-text"><span aria-hidden="true">⚠ </span>Couldn't work out the commute: ${esc(e.message)}</p>`;
    drawMap(null);
    return;
  }
  const best = c.transit.filter((t) => t.there).sort((a, b) => a.there.minutes - b.there.minutes)[0];
  const transit = c.transit.map((t) => `
    <div class="commute-from"><h3>Public transport from ${esc(t.from)}${t === best && c.transit.length > 1 ? ' <span class="badge fit-strong">quickest</span>' : ''}</h3>
      ${t.error ? `<p class="err-text">${esc(t.error)}</p>` : journeyHtml(t.there, `There (arrive by ${esc(c.arrive_by)}):`) + journeyHtml(t.back, `Back (leave ${esc(c.leave_at)}):`)}</div>`).join('');
  const car = c.car.error ? `<p class="err-text">${esc(c.car.error)}</p>`
    : carHtml(c.car.there, `There (arrive by ${esc(c.arrive_by)}):`) + carHtml(c.car.back, `Back (leave ${esc(c.leave_at)}):`);
  box.innerHTML = `
    <p class="muted">${esc(c.day)}: arriving by ${esc(c.arrive_by)}, leaving at ${esc(c.leave_at)}.
      ${c.from_stations ? "Public transport times start at the station (getting there isn't included)."
        : c.stations.length ? '' : 'Public transport starts at your home pin; set stations under Search setup > Commute.'}</p>
    ${c.notes.map((n) => `<p class="notice">${noteHtml(n)}</p>`).join('')}
    ${transit}
    <div class="commute-from"><h3>By car from ${esc(c.home.name)}</h3>${car}</div>
    <p class="commute-links"><a href="${esc(c.links.transit)}" target="_blank" rel="noopener">Public transport in Google Maps${NEW_TAB}</a> ·
      <a href="${esc(c.links.car)}" target="_blank" rel="noopener">Driving in Google Maps${NEW_TAB}</a></p>`;
  lastCommute = [c, best];
  drawMap(c, best);
}

function drawMap(c, best) {
  if (typeof L === 'undefined') return;
  if (!view.map) {
    const animate = !reducedMotion();
    view.map = L.map('commute-map', { scrollWheelZoom: false, zoomAnimation: animate, fadeAnimation: animate, markerZoomAnimation: animate });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(view.map);
    view.layer = L.layerGroup().addTo(view.map);
  }
  view.layer.clearLayers();
  const points = [];
  const office = c ? c.office : (job.lat !== null ? { lat: job.lat, lng: job.lng, name: job.location_text, precise: false } : null);
  if (!office) { view.map.setView([-33.87, 151.21], 9); return; }
  if (office.precise) {
    L.marker([office.lat, office.lng], { alt: `Office: ${office.name}`, title: `Office: ${office.name}` }).addTo(view.layer).bindPopup(`<b>Office</b><br>${esc(office.name)}`);
  } else {
    L.circle([office.lat, office.lng], { radius: 4000, color: themeColor('warn'), weight: 1, fillOpacity: 0.12 })
      .addTo(view.layer).bindPopup(`<b>Office somewhere in ${esc(office.name)}</b><br>The ad names only the city.`);
  }
  points.push([office.lat, office.lng]);
  if (c) {
    L.circleMarker([c.home.lat, c.home.lng], { radius: 7, color: themeColor('ok'), fillOpacity: 0.9 }).addTo(view.layer).bindPopup(`<b>${esc(c.home.name)}</b>`);
    points.push([c.home.lat, c.home.lng]);
    c.transit.forEach((t) => {
      L.circleMarker([t.start.lat, t.start.lng], { radius: 5, color: themeColor('warn'), fillOpacity: 0.9 }).addTo(view.layer).bindPopup(esc(t.from));
      points.push([t.start.lat, t.start.lng]);
    });
    (best?.there?.legs || []).forEach((l) => {
      if (l.coords.length < 2) return;
      L.polyline(l.coords, { color: themeColor(modeHue(l.mode)), weight: l.mode === 'Walk' ? 3 : 5, dashArray: l.mode === 'Walk' ? '4 6' : null })
        .addTo(view.layer).bindPopup(`${esc(l.mode)} ${esc(l.line)}: ${esc(l.from)} → ${esc(l.to)}`);
      points.push(...l.coords);
    });
    if (c.car?.there?.coords?.length) {
      L.polyline(c.car.there.coords, { color: themeColor('special'), weight: 3, opacity: 0.8, dashArray: '8 6' }).addTo(view.layer).bindPopup('By car');
      points.push(...c.car.there.coords);
    }
  }
  view.map.fitBounds(L.latLngBounds(points).pad(0.08), { animate: false });
  setTimeout(() => view.map.invalidateSize(), 50);
}

/* ---------- company ---------- */
async function loadCompany() {
  let p = null;
  try { p = (await api('/api/companies'))[job.company_key] || null; } catch { p = null; }
  $('#company-title').textContent = p?.official_name || job.company || 'Company';
  $('#job-company').innerHTML = job.company ? companyProfileHtml(p, job.company) : '<p class="muted">No employer named.</p>';
  $('#company-research')?.addEventListener('click', researchCompany);
}

async function researchCompany() {
  const status = $('#company-status');
  try {
    let run = await api('/api/companies/research', { method: 'POST', body: JSON.stringify({ name: job.company }) });
    const hadFocus = document.activeElement === $('#company-research');
    setBusy($('#company-research'), true);
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
  document.addEventListener('themechange', () => { if (view.map) drawMap(...(lastCommute || [null])); });
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
  renderFit();
  renderAd();
  loadCompany();
  loadCommute();
});
