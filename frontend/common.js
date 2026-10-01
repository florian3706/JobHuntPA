/* JobHuntPA: helpers shared by the dashboard (app.js) and the job page (job.js).
   Each page defines currentWorkspace(), the workspace its API calls use. */
'use strict';

const STATUSES = ['to_review', 'shortlisted', 'applied', 'interviewing', 'rejected', 'not_interested'];
const STATUS_LABEL = {
  to_review: 'to review', shortlisted: 'shortlisted', applied: 'applied',
  interviewing: 'interviewing', rejected: 'rejected', not_interested: 'not interested',
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
  const headers = { 'X-Workspace': String(currentWorkspace()), ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...(opts.headers || {}) };
  const res = await fetch(url, { ...opts, headers });
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

function scoreOf(job) { return job.fit && job.fit.status === 'ok' ? job.fit.score : null; }
function modeOf(job) { return ['remote', 'hybrid', 'onsite'].includes(job.work_mode) ? job.work_mode : 'unknown'; }

function scoreBadge(job) {
  const fit = job.fit;
  if (!fit) return `<span class="score none" title="Not scored yet">–</span>`;
  if (fit.status === 'error') return `<span class="score err" title="${esc(fit.error || 'error')}">!</span>`;
  const cls = fit.score >= 75 ? 'high' : fit.score >= 45 ? 'mid' : 'low';
  const stale = fit.stale ? ' stale' : '';
  const title = fit.stale ? 'Scored against older documents' : `Scored ${fmtDate(fit.scored_at)}`;
  return `<span class="score ${cls}${stale}" title="${esc(title)}">${fit.score}${fit.stale ? '*' : ''}</span>`;
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

function linkList(sources) {
  return (sources || []).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>`).join(' · ');
}

function glassdoorLink(p, name) {
  if (p?.glassdoor_url) {
    return `<a class="glassdoor" href="${esc(p.glassdoor_url)}" target="_blank" rel="noopener noreferrer">Glassdoor page ↗</a>`;
  }
  // No verified page: a Glassdoor search for the name (built here, not by the model).
  const q = encodeURIComponent(p?.official_name || name);
  return `<a class="glassdoor" href="https://www.glassdoor.com.au/Search/results.htm?keyword=${q}" target="_blank" rel="noopener noreferrer">Search Glassdoor ↗</a>`;
}

function sentimentHtml(p) {
  const s = p.sentiment;
  if (!s) return '<span class="muted">Not researched yet: click Research again (or Fill missing info) to add it.</span>';
  const list = (items, cls, mark) => (items.length ? `<ul class="themes ${cls}">${items.map((t) => `<li>${mark} ${esc(t)}</li>`).join('')}</ul>` : '');
  return `
    ${s.rating ? `<div><strong>${esc(s.rating)}</strong></div>` : ''}
    <div>${esc(s.summary || 'No employee reviews found.')}</div>
    ${list(s.positives, 'pos', '+')}${list(s.negatives, 'neg', '−')}
    ${s.sources.length ? `<div class="sources">${linkList(s.sources)}</div>` : ''}`;
}

/* ================= SOURCES ================= */
function sourceLabel(src) {
  const s = String(src || '');
  if (s === 'seek') return 'SEEK';
  if (s === 'import') return 'imported page';
  const i = s.indexOf(':');
  if (i < 0) return s;
  const kind = s.slice(0, i), rest = s.slice(i + 1);
  if (kind === 'apsjobs') return 'APSJobs';
  if (kind === 'site' || kind === 'generic') return rest;
  return `${rest.split('/')[0]} (${kind[0].toUpperCase()}${kind.slice(1)})`;
}

/* ================= MARKDOWN (chat answers) ================= */
function mdInline(s) { // s is already HTML-escaped
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}

function mdTable(rows) {
  const cells = (r) => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => mdInline(c.trim()));
  const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
  if (!body.length) return '';
  const [head, ...rest] = body;
  return `<table><thead><tr>${cells(head).map((c) => `<th>${c}</th>`).join('')}</tr></thead>
    <tbody>${rest.map((r) => `<tr>${cells(r).map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

function mdToHtml(text) {
  const lines = esc(text).split('\n');
  let html = '', list = null, para = [], table = [];
  const flushPara = () => { if (para.length) { html += `<p>${mdInline(para.join('<br>'))}</p>`; para = []; } };
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const flushTable = () => { if (table.length) { html += mdTable(table); table = []; } };
  const startList = (kind) => { flushPara(); flushTable(); if (list !== kind) { closeList(); html += `<${kind}>`; list = kind; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); closeList(); table.push(line); continue; }
    flushTable();
    if (!line.trim()) { flushPara(); closeList(); continue; }
    if ((m = line.match(/^\s*#{1,6}\s+(.*)$/))) { flushPara(); closeList(); html += `<h4>${mdInline(m[1])}</h4>`; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) { startList('ul'); html += `<li>${mdInline(m[1])}</li>`; continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { startList('ol'); html += `<li>${mdInline(m[1])}</li>`; continue; }
    closeList();
    para.push(line);
  }
  flushPara(); flushTable(); closeList();
  return html;
}

const RESEARCHED = ['done', 'error', 'skipped'];

/* Company profile body (info modal and job page). Has a #company-research button. */
function companyProfileHtml(p, name) {
  if (p && !RESEARCHED.includes(p.status)) p = null; // only offices known so far
  const researchBtn = `<button id="company-research" class="btn btn-sm" type="button">${p ? 'Research again' : 'Research this company'}</button>`;
  if (!p) {
    return `<p class="modal-links">${glassdoorLink(p, name)}</p>
      <p class="muted">Not researched yet. <strong>Fill missing info</strong> researches every company in your list, or research just this one:</p>${researchBtn}`;
  }
  if (p.status === 'error') return `<p class="modal-links">${glassdoorLink(p, name)}</p><p class="err-text">Research failed: ${esc(p.error)}</p>${researchBtn}`;
  if (p.status === 'skipped') return `<p class="muted">"${esc(p.name)}" isn't an identifiable organisation (for example an anonymous advertiser).</p>${researchBtn}`;
  const cons = p.controversies.length
    ? `<ul class="controversies">${p.controversies.map((c) => `
        <li><strong>${esc(c.title)}</strong>${c.year ? ` <span class="muted">(${esc(c.year)})</span>` : ''}
          <div>${esc(c.summary)}</div><div class="sources">${linkList(c.sources)}</div></li>`).join('')}</ul>`
    : '';
  const offices = (p.offices || []).length
    ? `<dt>Offices</dt><dd><ul class="offices">${p.offices.map((o) => `<li>${esc(o.name ? `${o.name}: ` : '')}${esc(o.address)}${o.url ? ` <a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">source</a>` : ''}</li>`).join('')}</ul></dd>`
    : '';
  return `
    <p class="modal-links">${glassdoorLink(p, name)}</p>
    ${p.is_recruiter ? '<p class="notice">This is a recruitment agency. The employer behind the ad is usually not disclosed; ask the recruiter who the client is.</p>' : ''}
    <dl class="profile">
      <dt>How they make money</dt><dd>${esc(p.business_model || 'Unknown')}</dd>
      <dt>Ownership</dt><dd>${esc(p.ownership || 'Unknown')}</dd>
      <dt>Headquarters</dt><dd>${esc(p.headquarters || 'Unknown')}</dd>
      <dt>Employees</dt><dd>${esc(p.employee_count || (p.research_version >= 2 ? 'Unknown' : 'Not researched yet'))}</dd>
      ${offices}
      <dt>Employee sentiment</dt><dd>${sentimentHtml(p)}</dd>
      <dt>Controversies</dt><dd>${esc(p.controversy_note || (p.controversies.length ? '' : 'None found.'))}${cons}</dd>
    </dl>
    ${p.sources.length ? `<p class="sources"><span class="muted">Sources:</span> ${linkList(p.sources)}</p>` : ''}
    <p class="muted">Researched ${esc(fmtDate(p.researched_at))} by ${esc(p.model || 'the LLM')} with web search.
      Only links the search actually returned are shown${p.unverified_dropped ? `; ${p.unverified_dropped} unverifiable link(s) and their claims were removed` : ''}.
      Automated research can be wrong: check the linked articles.</p>
    ${researchBtn}`;
}

/* Office line for a job: where it is, or that it's unknown. */
function officeHtml(job) {
  const by = { ad: 'from the ad', company: 'from company research', user: 'set by you' };
  if (job.office_text) {
    return `🏢 ${esc(job.office_text)} <span class="muted">(${by[job.office_source] || ''}${job.office_placed ? '' : ', not found on the map'})</span>`;
  }
  if (job.location_verdict === 'ok') return '<span class="ok-text">Location OK (your call)</span>';
  if (job.location_verdict === 'too_far') return '<span class="err-text">Too far (your call)</span>';
  return '';
}

/* ================= THEMES ================= */
// A per-browser display preference, as in ProjectTimeline. The stored value may
// be "system", which follows the OS between Light and Dark. themes.css has one
// block per theme; each page's <head> script applies the theme before first
// paint, and this keeps it in step afterwards (and across open tabs).
const DYSLEXIA_FONT = 'Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400';
const THEMES = [
  { id: 'system', label: 'System (Light or Dark)', group: 'JobHuntPA' },
  { id: 'dark', label: 'Dark', group: 'JobHuntPA' },
  { id: 'light', label: 'Light', group: 'JobHuntPA' },
  { id: 'catppuccin-latte', label: 'Catppuccin Latte', group: 'Catppuccin' },
  { id: 'catppuccin-frappe', label: 'Catppuccin Frappé', group: 'Catppuccin' },
  { id: 'catppuccin-macchiato', label: 'Catppuccin Macchiato', group: 'Catppuccin' },
  { id: 'catppuccin-mocha', label: 'Catppuccin Mocha', group: 'Catppuccin' },
  { id: 'dyslexia', label: 'Dyslexia', group: 'Reading', font: DYSLEXIA_FONT },
  { id: 'dyslexia-dark', label: 'Dyslexia (dark)', group: 'Reading', font: DYSLEXIA_FONT },
  { id: 'protanomaly', label: 'Protanomaly (red-weak)', group: 'Colour vision' },
  { id: 'protanomaly-dark', label: 'Protanomaly (dark)', group: 'Colour vision' },
  { id: 'deuteranomaly', label: 'Deuteranomaly (green-weak)', group: 'Colour vision' },
  { id: 'deuteranomaly-dark', label: 'Deuteranomaly (dark)', group: 'Colour vision' },
  { id: 'tritanomaly', label: 'Tritanomaly (blue-weak)', group: 'Colour vision' },
  { id: 'tritanomaly-dark', label: 'Tritanomaly (dark)', group: 'Colour vision' },
  { id: 'dichromacy', label: 'Dichromacy (any)', group: 'Colour vision' },
  { id: 'dichromacy-dark', label: 'Dichromacy (dark)', group: 'Colour vision' },
  { id: 'monochromacy', label: 'Monochromacy (greys)', group: 'Colour vision' },
  { id: 'monochromacy-dark', label: 'Monochromacy (dark)', group: 'Colour vision' },
];
const THEME_KEY = 'jobhunt.theme';

function storedTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return THEMES.some((x) => x.id === t) ? t : 'dark';
  } catch { return 'dark'; }
}

function resolveTheme(pref) {
  return pref === 'system' ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : pref;
}

function applyTheme(pref = storedTheme()) {
  const id = resolveTheme(pref);
  document.documentElement.setAttribute('data-theme', id);
  // The dyslexia themes' typeface is only downloaded while one of them is on;
  // their font stack falls back to Verdana until it arrives (or if it can't).
  const font = THEMES.find((t) => t.id === id)?.font;
  let link = document.getElementById('theme-font');
  if (!font) link?.remove();
  else {
    if (!link) {
      link = document.createElement('link');
      link.id = 'theme-font';
      link.rel = 'stylesheet';
      document.head.appendChild(link);
    }
    link.href = `https://fonts.googleapis.com/css2?family=${font}&display=swap`;
  }
  document.dispatchEvent(new CustomEvent('themechange', { detail: id }));
}

/* A theme colour for things drawn in script (map markers and routes). */
function themeColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim() || '#888888';
}

function initThemePicker(select) {
  const groups = [...new Set(THEMES.map((t) => t.group))];
  select.innerHTML = groups.map((g) => `<optgroup label="${esc(g)}">${THEMES.filter((t) => t.group === g)
    .map((t) => `<option value="${t.id}">${esc(t.label)}</option>`).join('')}</optgroup>`).join('');
  select.value = storedTheme();
  select.addEventListener('change', () => {
    try { localStorage.setItem(THEME_KEY, select.value); } catch { /* still applies to this page */ }
    applyTheme(select.value);
  });
  // Changed in another tab (dashboard or a job page): follow it.
  window.addEventListener('storage', (e) => {
    if (e.key === THEME_KEY) { select.value = storedTheme(); applyTheme(); }
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (storedTheme() === 'system') applyTheme();
  });
  applyTheme();
}
