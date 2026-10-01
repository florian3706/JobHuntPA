/* JobHuntPA job page: one job in full, with its office, commute and company. */
'use strict';

const params = new URLSearchParams(location.search);
const JOB_ID = Number(params.get('id'));
const WS = Number(params.get('ws')) || (() => { try { return Number(localStorage.getItem('jobhunt.ws')) || 1; } catch { return 1; } })();
function currentWorkspace() { return WS; }

let job = null;
const view = { map: null, layer: null };
const MODE_COLOUR = { Train: '#f5a524', Metro: '#16a3a3', 'Light rail': '#e0457b', Bus: '#4f9cf9', Coach: '#4f9cf9', Ferry: '#22c07a', Walk: '#9aa6b8', 'School bus': '#4f9cf9' };

function minutes(n) {
  if (n === null || n === undefined) return '';
  return n >= 60 ? `${Math.floor(n / 60)} h ${n % 60} min` : `${n} min`;
}

function showError(msg) {
  $('#job-error').textContent = msg;
  $('#job-error').hidden = false;
}

/* ---------- header ---------- */
function renderHead() {
  const mode = modeOf(job);
  const also = (job.also_on || []).map((c) => `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(sourceLabel(c.source))}</a>`).join(' · ');
  const facts = [
    `<span>${esc(job.location_text || 'location unknown')}</span>`,
    `<span class="badge ${mode}">${mode}</span>`,
    job.office_days ? `<span class="badge flag">${job.office_days} day${job.office_days > 1 ? 's' : ''} in office</span>` : '',
    job.distance_km !== null ? `<span title="Straight-line distance to your nearest home pin (or nearest pin)">📍 ${job.distance_km} km</span>` : '',
    job.salary_text ? `<span>💰 ${esc(job.salary_text)}</span>` : '',
    job.posted_at ? `<span class="muted">posted ${esc(fmtDate(job.posted_at).split(',')[0])}</span>` : '',
    job.closed ? '<span class="badge flag">closed</span>' : '',
    job.recruiter ? '<span class="badge flag">recruitment agency</span>' : '',
  ].join('');
  $('#job-head').innerHTML = `
    <div class="job-top">
      <div class="job-title-row">
        <div class="job-company">${esc(job.company || '—')}</div>
        <h2 class="job-page-title"><a href="${esc(job.url)}" target="_blank" rel="noopener">${esc(job.title || '—')} ↗</a></h2>
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
      <a class="btn btn-ghost btn-sm" href="./#jobs">Dashboard</a>
    </div>`;
  document.title = `${job.title} · ${job.company} · JobHuntPA`;
  $('#job-status').addEventListener('change', async (e) => {
    try {
      await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: e.target.value }) });
      job.status = e.target.value;
      toast('Status saved', 'ok', 1500);
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
  const cands = (job.office_candidates || []).map((o, i) => `<button class="btn btn-sm" type="button" data-cand="${i}" title="${esc(o.address)}">${esc(o.name || o.address)}</button>`).join('');
  $('#job-office').innerHTML = `
    ${status}
    ${job.office_source === 'company' && (job.office_candidates || []).length > 1
    ? `<p class="muted">The employer has ${job.office_candidates.length} offices here; this is the closest to your commute. Pick another if you know which one it is.</p>` : ''}
    ${cands ? `<div class="office-cands"><span class="muted">The employer's offices in this city (closest to your commute first):</span> ${cands}</div>` : ''}
    <form id="office-form" class="toolbar-row office-form">
      <label class="field search-field"><span class="field-label">Office address or suburb</span>
        <input id="office-input" type="text" placeholder="e.g. 1 Denison St, North Sydney" value="${esc(job.office_source === 'user' ? job.office_text || '' : '')}" /></label>
      <button class="btn btn-primary btn-sm" type="submit">Set office</button>
      ${job.office_text ? '<button id="office-clear" class="btn btn-ghost btn-sm" type="button">Clear office</button>' : ''}
    </form>
    <div class="toolbar-row verdict-row">
      <span class="muted">Your call on the location:</span>
      <button class="btn btn-sm ${job.location_verdict === 'ok' ? 'btn-primary' : ''}" type="button" data-verdict="ok" title="Keep this job whatever the location filter says">Location OK</button>
      <button class="btn btn-sm ${job.location_verdict === 'too_far' ? 'btn-primary' : ''}" type="button" data-verdict="too_far" title="Exclude this job: the office is too far">Too far</button>
      ${job.location_verdict ? '<button class="btn btn-ghost btn-sm" type="button" data-verdict="" title="Let the location filter decide again">Undo</button>' : ''}
    </div>`;
  $('#office-form').addEventListener('submit', (e) => { e.preventDefault(); saveOffice({ office: $('#office-input').value.trim() || null }); });
  $('#office-clear')?.addEventListener('click', () => saveOffice({ office: null }));
  $$('[data-cand]').forEach((b) => b.addEventListener('click', () => saveOffice({ office: job.office_candidates[Number(b.dataset.cand)].address })));
  $$('[data-verdict]').forEach((b) => b.addEventListener('click', () => saveOffice({ verdict: b.dataset.verdict || null })));
}

async function saveOffice(body) {
  try {
    job = await api(`/api/jobs/${job.id}/office`, { method: 'PUT', body: JSON.stringify(body) });
    renderHead();
    renderOffice();
    toast(job.excluded_reason ? `Saved. The job is now excluded: ${job.excluded_reason}` : 'Saved', job.excluded_reason ? '' : 'ok', 4000);
    loadCommute();
  } catch (e) { toast(e.message, 'err', 8000); }
}

/* ---------- commute ---------- */
function journeyHtml(j, label) {
  if (!j) return `<div class="trip"><strong>${label}</strong> <span class="muted">no journey found</span></div>`;
  const legs = j.legs.map((l) => `<li><span class="leg-mode" style="--c:${MODE_COLOUR[l.mode] || '#9aa6b8'}">${esc(l.mode)}${l.line ? ` ${esc(l.line)}` : ''}</span>
    ${esc(l.depart)} ${esc(l.from)} → ${esc(l.arrive)} ${esc(l.to)} <span class="muted">(${minutes(l.minutes)}${l.towards && l.mode !== 'Walk' ? `, towards ${esc(l.towards)}` : ''})</span></li>`).join('');
  return `<details class="trip"><summary><strong>${label}</strong> ${esc(j.depart)} → ${esc(j.arrive)} · <strong>${minutes(j.minutes)}</strong>
      · ${esc(j.summary)}${j.changes ? ` · ${j.changes} change${j.changes > 1 ? 's' : ''}` : ''}</summary><ol class="legs">${legs}</ol></details>`;
}

function carHtml(c, label) {
  if (!c) return '';
  const when = c.depart && c.arrive ? `${esc(c.depart)} → ${esc(c.arrive)} · ` : '';
  const traffic = c.traffic ? (c.traffic_minutes ? ` (incl. ${minutes(c.traffic_minutes)} of traffic)` : ' (with traffic)') : ' <span class="muted">(no traffic)</span>';
  return `<div class="trip"><strong>${label}</strong> ${when}<strong>${minutes(c.minutes)}</strong>${traffic} · ${c.km} km</div>`;
}

async function loadCommute() {
  const box = $('#job-commute');
  box.innerHTML = '<p class="muted">Working out the commute…</p>';
  let c;
  try { c = await api(`/api/jobs/${job.id}/commute`); } catch (e) {
    box.innerHTML = `<p class="err-text">${esc(e.message)}</p>`;
    drawMap(null);
    return;
  }
  const best = c.transit.filter((t) => t.there).sort((a, b) => a.there.minutes - b.there.minutes)[0];
  const transit = c.transit.map((t) => `
    <div class="commute-from"><h4>Public transport from ${esc(t.from)}${t === best && c.transit.length > 1 ? ' <span class="badge fit-strong">quickest</span>' : ''}</h4>
      ${t.error ? `<p class="err-text">${esc(t.error)}</p>` : journeyHtml(t.there, `There (arrive by ${esc(c.arrive_by)}):`) + journeyHtml(t.back, `Back (leave ${esc(c.leave_at)}):`)}</div>`).join('');
  const car = c.car.error ? `<p class="err-text">${esc(c.car.error)}</p>`
    : carHtml(c.car.there, `There (arrive by ${esc(c.arrive_by)}):`) + carHtml(c.car.back, `Back (leave ${esc(c.leave_at)}):`);
  box.innerHTML = `
    <p class="muted">${esc(c.day)}: arriving by ${esc(c.arrive_by)}, leaving at ${esc(c.leave_at)}.
      ${c.from_stations ? "Public transport times start at the station (getting there isn't included)."
        : c.stations.length ? '' : 'Public transport starts at your home pin; set stations under Search setup > Commute.'}</p>
    ${c.notes.map((n) => `<p class="notice">${esc(n)}</p>`).join('')}
    ${transit}
    <div class="commute-from"><h4>By car from ${esc(c.home.name)}</h4>${car}</div>
    <p class="commute-links"><a href="${esc(c.links.transit)}" target="_blank" rel="noopener">Public transport in Google Maps ↗</a> ·
      <a href="${esc(c.links.car)}" target="_blank" rel="noopener">Driving in Google Maps ↗</a></p>`;
  drawMap(c, best);
}

function drawMap(c, best) {
  if (typeof L === 'undefined') return;
  if (!view.map) {
    view.map = L.map('commute-map', { scrollWheelZoom: false });
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
    L.marker([office.lat, office.lng]).addTo(view.layer).bindPopup(`<b>Office</b><br>${esc(office.name)}`);
  } else {
    L.circle([office.lat, office.lng], { radius: 4000, color: '#f5a524', weight: 1, fillOpacity: 0.12 })
      .addTo(view.layer).bindPopup(`<b>Office somewhere in ${esc(office.name)}</b><br>The ad names only the city.`);
  }
  points.push([office.lat, office.lng]);
  if (c) {
    L.circleMarker([c.home.lat, c.home.lng], { radius: 7, color: '#22c07a', fillOpacity: 0.9 }).addTo(view.layer).bindPopup(`<b>${esc(c.home.name)}</b>`);
    points.push([c.home.lat, c.home.lng]);
    c.transit.forEach((t) => {
      L.circleMarker([t.start.lat, t.start.lng], { radius: 5, color: '#f5a524', fillOpacity: 0.9 }).addTo(view.layer).bindPopup(esc(t.from));
      points.push([t.start.lat, t.start.lng]);
    });
    (best?.there?.legs || []).forEach((l) => {
      if (l.coords.length < 2) return;
      L.polyline(l.coords, { color: MODE_COLOUR[l.mode] || '#9aa6b8', weight: l.mode === 'Walk' ? 3 : 5, dashArray: l.mode === 'Walk' ? '4 6' : null })
        .addTo(view.layer).bindPopup(`${esc(l.mode)} ${esc(l.line)}: ${esc(l.from)} → ${esc(l.to)}`);
      points.push(...l.coords);
    });
    if (c.car?.there?.coords?.length) {
      L.polyline(c.car.there.coords, { color: '#a78bfa', weight: 3, opacity: 0.8, dashArray: '8 6' }).addTo(view.layer).bindPopup('By car');
      points.push(...c.car.there.coords);
    }
  }
  view.map.fitBounds(L.latLngBounds(points).pad(0.08));
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
    $('#company-research').disabled = true;
    while (run.state === 'queued' || run.state === 'running') {
      status.textContent = `Researching… ${run.stage || ''}`;
      await new Promise((r) => setTimeout(r, 2000));
      run = await api(`/api/runs/${run.id}`);
    }
    status.textContent = run.state === 'failed' ? `Research failed: ${run.error}` : '';
  } catch (e) { status.textContent = `Research failed: ${e.message}`; }
  loadCompany();
}

/* ---------- boot ---------- */
document.addEventListener('DOMContentLoaded', async () => {
  if (!JOB_ID) { showError('No job given. Open a job from the dashboard (Open ↗ on a job card).'); return; }
  try {
    const workspaces = await api('/api/workspaces');
    const ws = workspaces.find((w) => w.id === WS);
    if (ws) $('#ws-name').textContent = ws.name;
    job = await api(`/api/jobs/${JOB_ID}`);
  } catch (e) { showError(`Could not load this job: ${e.message}`); $('#job-head').hidden = true; return; }
  renderHead();
  renderOffice();
  renderFit();
  renderAd();
  loadCompany();
  loadCommute();
});
