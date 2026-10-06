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

/* ---------- announcements (WCAG 4.1.3) ----------
   Two visually hidden live regions, made at start-up so they exist before anything is said in them:
   announce() for results and progress (polite), announce(msg, true) for errors (assertive). */
function liveRegion(id, role) {
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('div');
    el.id = id;
    el.className = 'visually-hidden';
    el.setAttribute('role', role);
    if (role === 'status') el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  return el;
}

function announce(msg, urgent = false) {
  const el = liveRegion(urgent ? 'sr-alert' : 'sr-status', urgent ? 'alert' : 'status');
  el.textContent = '';
  setTimeout(() => { el.textContent = msg; }, 60); // cleared first, so the same message is announced again
}

/* Writes text only when it changed, so a live region isn't re-announced on every redraw. */
function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

/* Shows an error box (.error) and says it. */
function showError(el, msg) {
  el.textContent = msg;
  el.hidden = false;
  announce(msg, true);
}

/* Toasts: a mark and the message (so colour isn't the only sign of an error), a Dismiss button, and a
   timer that waits while the pointer or keyboard focus is on the toast. Screen readers get the message
   through announce(). */
const TOAST_MARK = { ok: '✓', err: '⚠' };
function toast(msg, kind = '', ms = 5000) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`.trim();
  el.innerHTML = `<span class="toast-mark" aria-hidden="true">${TOAST_MARK[kind] || 'ℹ'}</span>
    <span class="toast-msg">${kind === 'err' ? '<span class="visually-hidden">Error: </span>' : ''}${esc(msg)}</span>
    <button class="toast-close" type="button" aria-label="Dismiss message"><span aria-hidden="true">×</span></button>`;
  let timer = null;
  let held = false;
  const remove = () => {
    if (el.contains(document.activeElement)) document.activeElement.blur();
    el.remove();
  };
  const start = () => { clearTimeout(timer); timer = setTimeout(() => { if (!held) remove(); }, ms); };
  const hold = (on) => { held = on; if (!on) start(); };
  el.addEventListener('mouseenter', () => hold(true));
  el.addEventListener('mouseleave', () => hold(el.contains(document.activeElement)));
  el.addEventListener('focusin', () => hold(true));
  el.addEventListener('focusout', (e) => { if (!el.contains(e.relatedTarget)) hold(el.matches(':hover')); });
  el.querySelector('.toast-close').addEventListener('click', remove);
  $('#toasts').appendChild(el);
  announce(kind === 'err' ? `Error: ${msg}` : msg, kind === 'err');
  start();
}

/* ---------- form errors (WCAG 3.3.1, 3.3.3) ----------
   Marks a field invalid and shows the message under it, tied to the field with aria-describedby, so it
   is read with the field. setFieldError(input, '') clears it; typing in the field clears it too. */
function setFieldError(input, msg, { focus = true } = {}) {
  const id = `${input.id}-error`;
  let el = document.getElementById(id);
  const described = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter((x) => x && x !== id);
  if (!msg) {
    el?.remove();
    input.removeAttribute('aria-invalid');
    if (described.length) input.setAttribute('aria-describedby', described.join(' ')); else input.removeAttribute('aria-describedby');
    return;
  }
  if (!el) {
    el = document.createElement('p');
    el.id = id;
    el.className = 'field-error';
    (input.closest('.key-row, .tag-input, .sched-add') || input).insertAdjacentElement('afterend', el);
  }
  el.innerHTML = `<span aria-hidden="true">⚠ </span>${esc(msg)}`;
  input.setAttribute('aria-invalid', 'true');
  input.setAttribute('aria-describedby', [id, ...described].join(' '));
  input.addEventListener('input', () => setFieldError(input, ''), { once: true });
  if (focus) input.focus();
  else announce(msg, true);
}

/* ---------- busy buttons ----------
   A button that is working says so with aria-disabled instead of disabled: a disabled button drops keyboard
   focus to the page, so someone using the keyboard would lose their place each time they press one.
   Clicks on it are ignored here, before any handler sees them. */
function setBusy(btn, busy) {
  if (!btn) return;
  if (busy) btn.setAttribute('aria-disabled', 'true'); else btn.removeAttribute('aria-disabled');
}
document.addEventListener('click', (e) => {
  const btn = e.target.closest?.('button[aria-disabled="true"], a[aria-disabled="true"]');
  if (btn) { e.preventDefault(); e.stopImmediatePropagation(); }
}, true);

/* ---------- motion ---------- */
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const scrollBehavior = () => (reducedMotion() ? 'auto' : 'smooth');

/* ---------- the sticky header ----------
   Its height goes into --header-h, which styles.css uses as scroll padding, so a control that gets keyboard
   focus is never scrolled in under the header (WCAG 2.4.11). On small screens the header isn't sticky. */
function trackHeaderHeight() {
  const header = document.querySelector('.app-header');
  if (!header) return;
  const set = () => document.documentElement.style.setProperty('--header-h', `${Math.ceil(header.getBoundingClientRect().height)}px`);
  set();
  if ('ResizeObserver' in window) new ResizeObserver(set).observe(header);
  else window.addEventListener('resize', set);
}

/* ---------- tooltips for keyboard users (WCAG 1.4.13) ----------
   A mouse shows an element's title as the browser's own tooltip. A control focused with the keyboard
   shows the same text in a small box under it: it stays while the control has focus, Escape hides it,
   and the pointer can move onto it. Screen readers already read the title as the description, so the
   box is hidden from them. */
function initFocusTips() {
  const tip = document.createElement('div');
  tip.className = 'focus-tip';
  tip.setAttribute('aria-hidden', 'true');
  tip.hidden = true;
  document.body.appendChild(tip);
  let owner = null;
  const hide = () => { tip.hidden = true; owner = null; };
  const place = () => {
    if (!owner) return;
    const r = owner.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    tip.style.maxWidth = `${Math.min(320, vw - 16)}px`;
    const left = Math.max(8, Math.min(r.left, vw - tip.offsetWidth - 8));
    const below = r.bottom + 6 + tip.offsetHeight < window.innerHeight;
    tip.style.left = `${left}px`;
    tip.style.top = `${below ? r.bottom + 6 : Math.max(8, r.top - tip.offsetHeight - 6)}px`;
  };
  document.addEventListener('focusin', (e) => {
    const el = e.target;
    const text = (el.getAttribute?.('title') || el.closest?.('label[title]')?.getAttribute('title') || '').trim();
    if (!text || !el.matches(':focus-visible')) { hide(); return; }
    owner = el;
    tip.textContent = text;
    tip.hidden = false;
    place();
  });
  document.addEventListener('focusout', (e) => { if (e.target === owner) hide(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && owner && !tip.hidden) { e.preventDefault(); e.stopPropagation(); hide(); }
  }, true);
  window.addEventListener('scroll', place, { passive: true, capture: true });
  window.addEventListener('resize', place);
}

document.addEventListener('DOMContentLoaded', () => {
  liveRegion('sr-status', 'status');
  liveRegion('sr-alert', 'alert');
  trackHeaderHeight();
  initFocusTips();
});

async function api(url, opts = {}) {
  return (await apiResponse(url, opts)).data;
}

// Like api(), plus the response headers ({ data, headers }).
async function apiResponse(url, opts = {}) {
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
  return { data, headers: res.headers };
}

const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(/T\d\d:\d\d/.test(iso) && !/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? `${iso}Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function scoreOf(job) { return job.fit && job.fit.status === 'ok' ? job.fit.score : null; }
function modeOf(job) { return ['remote', 'hybrid', 'onsite'].includes(job.work_mode) ? job.work_mode : 'unknown'; }

// The number is the score; its colour only repeats it. Screen readers get "Fit score 82 of 100".
function scoreBadge(job) {
  const fit = job.fit;
  if (!fit) return '<span class="score none" title="Not scored yet"><span aria-hidden="true">–</span><span class="visually-hidden">Not scored yet</span></span>';
  if (fit.status === 'error') {
    return `<span class="score err" title="${esc(fit.error || 'error')}"><span aria-hidden="true">!</span><span class="visually-hidden">Scoring failed</span></span>`;
  }
  const cls = fit.score >= 75 ? 'high' : fit.score >= 45 ? 'mid' : 'low';
  const stale = fit.stale ? ' stale' : '';
  const title = fit.stale ? 'Scored against older documents' : `Scored ${fmtDate(fit.scored_at)}`;
  return `<span class="score ${cls}${stale}" title="${esc(title)}"><span class="visually-hidden">Fit score </span>${fit.score}${
    fit.stale ? '<span aria-hidden="true">*</span><span class="visually-hidden"> of 100, scored against older documents</span>' : '<span class="visually-hidden"> of 100</span>'}</span>`;
}

function fitHtml(job) {
  const fit = job.fit;
  if (!fit) return '<p class="muted">Not scored yet.</p>';
  if (fit.status === 'error') return `<p class="hint err-text">${esc(fit.error || 'Scoring failed')}</p>`;
  const reqs = (fit.requirements || []).map((r) => {
    const ev = (r.evidence || []).map((e) => {
      const subs = (e.sub_bullets || []).map((s) => `<li>${esc(s)}</li>`).join('');
      return `<li class="ev"><span aria-hidden="true">✓ </span>${esc(e.bullet)}${subs ? `<ul>${subs}</ul>` : ''}</li>`;
    }).join('');
    const tag = r.matched ? '<span class="ok-text"><span aria-hidden="true">✓ </span>matched</span>' : '<span class="err-text"><span aria-hidden="true">✕ </span>missing</span>';
    const must = r.must_have === false ? ' <span class="muted">(nice to have)</span>' : '';
    return `<li><div>${esc(r.point)} · ${tag}${must}</div>${ev ? `<ul aria-label="Evidence">${ev}</ul>` : ''}</li>`;
  }).join('') || '<li class="muted">No requirement breakdown.</li>';
  return `${fit.summary ? `<p class="summary">${esc(fit.summary)}</p>` : ''}
    <ul aria-label="Requirements">${reqs}</ul>
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
  return `<details class="reqs gaps" data-section="gaps"><summary data-act="gaps">Gaps (${n})</summary><div class="gaps-body"></div></details>`;
}

function gapsHtml(job) {
  const fit = job.fit;
  const gaps = (fit.gaps || []).map((g) => `<li class="gap"><span aria-hidden="true">✕ </span>${esc(g)}</li>`).join('');
  const missing = missingRequirements(fit).map((r) =>
    `<li class="gap">${esc(r.point)}${r.must_have === false ? ' <span class="muted">(nice to have)</span>' : ' <span class="muted">(must have)</span>'}</li>`).join('');
  return `${gaps ? `<ul aria-label="Gaps">${gaps}</ul>` : ''}
    ${missing ? `<p class="muted gaps-sub" id="gaps-sub-${job.id}">Requirements your documents don't evidence:</p><ul aria-labelledby="gaps-sub-${job.id}">${missing}</ul>` : ''}`;
}

function linkList(sources) {
  return (sources || []).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)}</a>`).join(' · ');
}

// For a link that opens in a new tab: the arrow people see, and words for screen readers.
const NEW_TAB = '<span aria-hidden="true"> ↗</span><span class="visually-hidden"> (opens in a new tab)</span>';

function glassdoorLink(p, name) {
  if (p?.glassdoor_url) {
    return `<a class="glassdoor" href="${esc(p.glassdoor_url)}" target="_blank" rel="noopener noreferrer">Glassdoor page${NEW_TAB}</a>`;
  }
  // No verified page: a Glassdoor search for the name (built here, not by the model).
  const q = encodeURIComponent(p?.official_name || name);
  return `<a class="glassdoor" href="https://www.glassdoor.com.au/Search/results.htm?keyword=${q}" target="_blank" rel="noopener noreferrer">Search Glassdoor${NEW_TAB}</a>`;
}

function sentimentHtml(p) {
  const s = p.sentiment;
  if (!s) return '<span class="muted">Not researched yet: click Research again (or Fill missing info) to add it.</span>';
  const list = (items, cls, mark, label) => (items.length
    ? `<ul class="themes ${cls}" aria-label="${label}">${items.map((t) => `<li><span aria-hidden="true">${mark} </span>${esc(t)}</li>`).join('')}</ul>` : '');
  return `
    ${s.rating ? `<div><strong>${esc(s.rating)}</strong></div>` : ''}
    <div>${esc(s.summary || 'No employee reviews found.')}</div>
    ${list(s.positives, 'pos', '+', 'Positives')}${list(s.negatives, 'neg', '−', 'Negatives')}
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

// Narrow screens show each row as a block with the column names beside the values (styles.css,
// .stack-table), so the roles are explicit: CSS that changes a table's display can drop its semantics.
function mdTable(rows) {
  const cells = (r) => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => mdInline(c.trim()));
  const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
  if (!body.length) return '';
  const [head, ...rest] = body;
  const names = cells(head);
  const label = (i) => esc(names[i] ? names[i].replace(/<[^>]+>/g, '') : '');
  return `<table class="stack-table" role="table"><thead role="rowgroup"><tr role="row">${names.map((c) => `<th role="columnheader">${c}</th>`).join('')}</tr></thead>
    <tbody role="rowgroup">${rest.map((r) => `<tr role="row">${cells(r).map((c, i) => `<td role="cell" data-label="${label(i)}">${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
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
    if ((m = line.match(/^\s*#{1,6}\s+(.*)$/))) { flushPara(); closeList(); html += `<h3>${mdInline(m[1])}</h3>`; continue; }
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
    return `<span aria-hidden="true">🏢 </span><span class="visually-hidden">Office: </span>${esc(job.office_text)} <span class="muted">(${by[job.office_source] || ''}${job.office_placed ? '' : ', not found on the map'})</span>`;
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
