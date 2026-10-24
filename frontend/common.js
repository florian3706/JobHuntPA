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
const ERROR_ICON = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v6M12 16.5v.5"></path></svg>';
// The message under a field, as Vanilla's validation message: an icon, and "Error:" for screen readers.
function fieldErrorEl(input, id, msg) {
  const el = document.createElement('p');
  el.id = id;
  el.className = 'p-form-validation__message field-error';
  el.innerHTML = `${ERROR_ICON}<span><span class="u-sr">Error: </span>${esc(msg)}</span>`;
  input.closest('.p-field')?.classList.add('is-error');
  return el;
}

function setFieldError(input, msg, { focus = true } = {}) {
  const id = `${input.id}-error`;
  let el = document.getElementById(id);
  const described = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter((x) => x && x !== id);
  if (!msg) {
    el?.remove();
    input.closest('.p-field')?.classList.remove('is-error');
    input.removeAttribute('aria-invalid');
    if (described.length) input.setAttribute('aria-describedby', described.join(' ')); else input.removeAttribute('aria-describedby');
    return;
  }
  if (!el) {
    el = fieldErrorEl(input, id, msg);
    (input.closest('.key-row, .tag-input, .sched-add, .pw-row') || input).insertAdjacentElement('afterend', el);
  } else {
    el.innerHTML = `${ERROR_ICON}<span><span class="u-sr">Error: </span>${esc(msg)}</span>`;
  }
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

/* ---------- focus across a redraw ----------
   A list drawn again from scratch (innerHTML) loses the focused control: each control that can have it carries a
   data-focus key, and the one with the same key gets the focus back (with the caret, in a text field). */
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

/* ---------- the signed-in account (accounts design, sections 3 and 6) ----------
   The session cookie is HttpOnly, so scripts never see it. /api/auth/me says who is signed in and gives the
   page's CSRF token, which every POST, PUT, PATCH and DELETE sends back as X-CSRF-Token. A 401 sends the
   browser to the sign-in page (or to the first-run page before the first account exists), and back here after. */
const SESSION = { me: null, loading: null };
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
let leavingPage = false;

/* Goes to the sign-in page. The promise never settles: the page is going away, so callers stop quietly instead
   of showing "signed out" errors on their way out. */
function signedOut(code) {
  if (!leavingPage) {
    leavingPage = true;
    const here = `${location.pathname}${location.search}${location.hash}`;
    location.replace(code === 'setup_required' ? 'first-run.html' : `login.html?next=${encodeURIComponent(here)}`);
  }
  return new Promise(() => {});
}

/* Where a signed-in person with an unfinished sign-up walkthrough (docs/design/accounts.md 6.1, Phase K) goes
   instead of the page they asked for: their recovery codes first if they haven't saved them (first-run.html shows
   them, then comes back here), then welcome.html, which resumes where they left off. null when the page may load. */
function walkthroughPage(me) {
  if (!me.onboarding_step) return null;
  // first-run.html is where the codes are shown: sending it there again would reload it for ever (its password
  // dialog calls loadSession() first)
  if (['welcome', 'first-run'].includes(document.body?.dataset.page)) return null;
  return me.recovery_codes_pending ? 'first-run.html' : 'welcome.html';
}

/* Like signedOut(): leaves for another page and never settles, so callers stop quietly. */
function leaveFor(page) {
  leavingPage = true;
  location.replace(page);
  return new Promise(() => {});
}

/* The theme saved for the account (sealed on the server) wins when this browser has none of its own or a
   different one: a new device or browser picks up the choice. localStorage stays the fast path for first paint. */
function adoptAccountTheme(me) {
  const theme = me.prefs?.theme;
  if (theme && THEMES.some((t) => t.id === theme)) {
    let stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch { /* private mode */ }
    if (stored !== theme) setTheme(theme);
  }
}

/* Saves the chosen theme to the account (best effort: the browser already has it). */
function saveAccountTheme(theme) {
  return api('/api/account/prefs', { method: 'PUT', body: JSON.stringify({ theme }) }).catch(() => {});
}

/* Who is signed in: { user: {id, name}, csrf, recovery_codes_pending, workspaces, features, onboarding_step,
   prefs: {theme} }. There are no roles: whoever is signed in may change every setting. */
function loadSession(force = false) {
  if (!SESSION.loading || force) {
    SESSION.loading = (async () => {
      const res = await fetch('/api/auth/me', { headers: { Accept: 'application/json' } });
      let data = null;
      try { data = await res.json(); } catch { /* not JSON */ }
      if (res.status === 401) return signedOut(data?.code);
      if (!res.ok) throw new Error(data?.detail || `Could not check who is signed in (error ${res.status})`);
      SESSION.me = data;
      adoptAccountTheme(data);
      const away = walkthroughPage(data);
      if (away) return leaveFor(away);
      return data;
    })();
  }
  return SESSION.loading;
}

async function api(url, opts = {}) {
  return (await apiResponse(url, opts)).data;
}

// Like api(), plus the response headers ({ data, headers }). With `blob: true` the data is the body's bytes (a Blob:
// a download), read only when the answer is OK; a refusal is handled as for any other call.
async function apiResponse(url, opts = {}, retried = false) {
  const { blob: asBlob = false, ...fetchOpts } = opts;
  const isForm = opts.body instanceof FormData;
  const method = (opts.method || 'GET').toUpperCase();
  const ws = currentWorkspace(); // 0 before the page knows one: the server then uses the account's first
  const headers = { ...(ws ? { 'X-Workspace': String(ws) } : {}), ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...(opts.headers || {}) };
  if (UNSAFE_METHODS.has(method)) headers['X-CSRF-Token'] = (SESSION.me || await loadSession())?.csrf || '';
  // A change may start AI work: the tasks tray (tasks-tray.js) looks again soon.
  if (UNSAFE_METHODS.has(method)) document.dispatchEvent(new Event('jobhunt:api-write'));
  const res = await fetch(url, { ...fetchOpts, headers });
  if (asBlob && res.ok) return { data: await res.blob(), headers: res.headers };
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const code = data && typeof data === 'object' ? data.code : null;
    if (res.status === 401) return signedOut(code);
    // The token is from an older session (signed in again in another tab): fetch the new one and try once more.
    if (res.status === 403 && code === 'csrf' && !retried) {
      await loadSession(true);
      return apiResponse(url, opts, true);
    }
    // The data is being encrypted: the notice shows the progress and reloads the page when it's done, so callers
    // stop quietly (no "could not load" errors on the way), as for a 401.
    if (res.status === 423) { showAccessNotice('encrypting', data); return new Promise(() => {}); }
    const detail = data && typeof data === 'object' && data.detail ? data.detail : text;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.code = code; // e.g. 'reauth_required' (withRecentAuth)
    err.field = data && typeof data === 'object' ? data.field : undefined; // the form field a refusal is about
    throw err;
  }
  return { data, headers: res.headers };
}

/* A one-off message from a sign-in page (auth.js) for the page that opens next, such as how many recovery codes are
   left after signing in with one. It is kept for this tab only and shown once. */
const SIGN_IN_NOTICE_KEY = 'jobhunt.signin-notice';

function recoveryLeftText(left) {
  if (left <= 0) return 'You have no recovery codes left. Make new ones in Settings > Recovery codes.';
  return `You have ${left} recovery code${left === 1 ? '' : 's'} left. Make new ones in Settings > Recovery codes.`;
}

function showSignInNotice() {
  let msg = '';
  try { msg = sessionStorage.getItem(SIGN_IN_NOTICE_KEY) || ''; sessionStorage.removeItem(SIGN_IN_NOTICE_KEY); } catch { /* private mode */ }
  if (msg) toast(msg, '', 15000);
}

/* What the Background access card (background.js) and the walkthrough's automatic-searches step (welcome.js) say.
   Say only what exists: the server key runs scheduled searches. */
const BACKGROUND_ON_COPY = "JobHuntPA keeps a key on the server so it can run scheduled searches while you're away. The server's operator could technically read your data.";
const BACKGROUND_OFF_COPY = "Only you can unlock your data. JobHuntPA can't run searches while you're away.";

/* Sensitive actions (accounts design §3) answer 403 {code: 'reauth_required'} unless the password was entered in
   the last 15 minutes. withRecentAuth(fn) runs fn (which calls api()); on that refusal it asks for the password with
   confirmPassword() and, once confirmed, runs fn again. Everything that needs recent sign-in goes through it. */
async function withRecentAuth(fn) {
  try {
    return await fn();
  } catch (e) {
    if (e?.code !== 'reauth_required' || !(await confirmPassword())) throw e;
    return fn();
  }
}

/* Asks for the password again (POST /api/auth/reauth) in a "Confirm your password" dialog and resolves true once it
   is confirmed, false if the person cancels (Cancel or Escape). A native <dialog>: focus moves into it and goes back
   to the control that opened it. A wrong password is said under the field and announced; the dialog stays open.
   Two calls at once share one dialog. A hidden username field holds the account's sign-in name, so password managers
   fill in the right password. */
let confirmDialogOpen = null;
function confirmPassword() {
  if (!confirmDialogOpen) confirmDialogOpen = openConfirmDialog().finally(() => { confirmDialogOpen = null; });
  return confirmDialogOpen;
}

async function openConfirmDialog() {
  const opener = document.activeElement;
  const name = (SESSION.me || await loadSession())?.user?.name || '';
  const dlg = document.createElement('dialog');
  dlg.className = 'confirm-dialog';
  dlg.setAttribute('aria-labelledby', 'confirm-pw-title');
  dlg.setAttribute('aria-describedby', 'confirm-pw-intro');
  dlg.innerHTML = `<form class="confirm-box" novalidate>
    <h2 id="confirm-pw-title">Confirm your password</h2>
    <p id="confirm-pw-intro">To keep your account safe, enter your password to go on. You won't be asked again for 15 minutes.</p>
    <div id="confirm-pw-status" class="visually-hidden" role="status"></div>
    <input class="visually-hidden" type="text" name="username" autocomplete="username" tabindex="-1" readonly
           aria-hidden="true" aria-label="Name" value="${esc(name)}" />
    <div class="auth-field">
      <label for="confirm-password">Password</label>
      <div class="pw-row">
        <input id="confirm-password" name="password" type="password" autocomplete="current-password" spellcheck="false" required />
        <button id="confirm-toggle" class="btn pw-toggle" type="button" aria-controls="confirm-password" aria-pressed="false">Show password</button>
      </div>
    </div>
    <div class="confirm-actions">
      <button id="confirm-pw-cancel" class="btn" type="button">Cancel</button>
      <button id="confirm-submit" class="btn btn-primary" type="submit">Confirm</button>
    </div>
  </form>`;
  document.body.appendChild(dlg);
  const form = dlg.querySelector('form');
  const input = dlg.querySelector('#confirm-password');
  const toggle = dlg.querySelector('#confirm-toggle');
  const submit = dlg.querySelector('#confirm-submit');
  const status = dlg.querySelector('#confirm-pw-status');
  let confirmed = false;
  let busy = false;
  const problem = (msg) => {
    setFieldError(input, '');
    if (!msg) return;
    const note = document.createElement('p');
    note.id = 'confirm-pw-error';
    note.className = 'field-error';
    note.setAttribute('role', 'alert'); // read out inside the dialog, where the page's own live regions are inert
    note.innerHTML = `<span class="visually-hidden">Error: </span>${esc(msg)}`;
    input.closest('.pw-row').insertAdjacentElement('afterend', note);
    input.setAttribute('aria-invalid', 'true');
    input.setAttribute('aria-describedby', note.id);
    input.addEventListener('input', () => problem(''), { once: true });
    input.focus(); // the error is read from here (aria-describedby)
  };
  toggle.addEventListener('click', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(show));
  });
  dlg.querySelector('#confirm-pw-cancel').addEventListener('click', () => dlg.close());
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (busy) return;
    const old = document.getElementById('confirm-pw-error');
    if (old) { old.remove(); input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby'); }
    if (!input.value) { problem('Enter your password.'); return; }
    busy = true;
    setBusy(submit, true);
    setText(status, 'Checking your password…'); // inside the dialog: the rest of the page is inert while it is open
    input.type = 'password';
    toggle.setAttribute('aria-pressed', 'false');
    try {
      await api('/api/auth/reauth', { method: 'POST', body: JSON.stringify({ password: input.value }) });
      confirmed = true;
      dlg.close();
    } catch (err) {
      setText(status, '');
      problem(err.message || "That didn't work. Try again.");
    } finally {
      busy = false;
      setBusy(submit, false);
    }
  });
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => {
      dlg.remove();
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
      resolve(confirmed);
    });
    dlg.showModal(); // Escape fires 'cancel', which closes the dialog: resolved as false
    input.focus();
  });
}

/* A notice at the top of the page that stays until the situation changes. One per kind; showing it again
   replaces its text. role="status" says it once without taking focus. */
function pageNotice(kind, html) {
  const main = document.getElementById('main');
  if (!main) return null;
  let el = document.getElementById(`notice-${kind}`);
  if (!el) {
    el = document.createElement('div');
    el.id = `notice-${kind}`;
    el.className = 'page-notice';
    el.setAttribute('role', 'status');
    main.prepend(el);
  }
  el.innerHTML = html;
  return el;
}

/* 423: the data is being encrypted (Phase D). */
function showAccessNotice(kind, data) {
  if (kind === 'encrypting') showEncrypting(data);
}

/* The one-time encryption of the data from before accounts (backend/migrate_encryption.py). The notice says what
   is happening once (role="status"); the progress bar follows the run, polled every 2 seconds, without being
   announced each time (it isn't a live region: its value is there when someone moves to it). The page reloads
   when the run has finished. */
function showEncrypting(data) {
  const failed = data?.state === 'failed';
  const message = failed
    ? (data?.error ? `Encrypting your data stopped (${data.error}). JobHuntPA tries again in a minute.` : data?.detail)
    : (data?.detail || 'JobHuntPA is encrypting your data. This page reloads by itself when it has finished.');
  let el = document.getElementById('notice-encrypting');
  if (!el || el.dataset.message !== message) {
    el = pageNotice('encrypting', `<p>${esc(message)}</p>
      <div class="progress" role="progressbar" aria-label="Encrypting your data" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="progress-bar"></div></div>
      <p class="muted encrypt-count" aria-hidden="true"></p>`);
    if (!el) return;
    el.dataset.message = message;
  }
  const done = Number(data?.done) || 0;
  const total = Number(data?.total) || 0;
  const pct = total ? Math.min(100, Math.floor((100 * done) / total)) : 0;
  const bar = el.querySelector('.progress');
  const count = total ? `${done.toLocaleString()} of ${total.toLocaleString()} rows and files done` : 'Starting…';
  bar.setAttribute('aria-valuenow', String(pct));
  bar.setAttribute('aria-valuetext', count);
  bar.querySelector('.progress-bar').style.width = `${pct}%`;
  setText(el.querySelector('.encrypt-count'), count);
  if (!showEncrypting.polling) {
    showEncrypting.polling = true;
    const poll = async () => {
      let status = null;
      try {
        const res = await fetch('/api/encryption/status', { headers: { Accept: 'application/json' } });
        if (res.ok) status = await res.json();
      } catch { /* the server may be restarting: try again */ }
      if (status?.state === 'done') { location.reload(); return; }
      if (status) showEncrypting({ ...status, detail: data?.detail });
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  }
}

// A file the page made or fetched, saved through a blob: address (the page's policy allows it).
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function fmtBytes(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} bytes`;
}

/* After the encryption: the copies of the data that still hold it unencrypted (the backup it made first, older
   backups, an old page cache) are reported in the Settings tab (accounts design §5.5), where they can be deleted.
   This notice only points there, until they are gone. */
async function remindLeftovers() {
  let status;
  try { status = await api('/api/encryption/status'); } catch { return; }
  const files = status?.leftovers || [];
  if (status?.state !== 'done' || !files.length) return;
  pageNotice('leftovers', `<p><strong>Your data is encrypted now.</strong> ${files.length === 1 ? 'A file' : `${files.length} files`} from before still
      ${files.length === 1 ? 'holds' : 'hold'} it unencrypted, with old keys. <a href="./#settings">See them in Settings</a> (Unencrypted copies of data) and delete the <code>.bak-*</code> files you don't need.</p>`);
}

/* ---------- the account menu in the header ----------
   A disclosure button: it opens a small panel with who is signed in, a link to Settings, and Sign out. Escape or a
   click elsewhere closes it. */
function initAccountMenu(me) {
  const btn = document.getElementById('account-btn');
  const panel = document.getElementById('account-panel');
  if (!btn || !panel) return;
  const user = me.user || {};
  setText(document.getElementById('account-name'), user.name || '');
  const open = (yes) => {
    btn.setAttribute('aria-expanded', String(yes));
    panel.hidden = !yes;
  };
  btn.addEventListener('click', () => open(panel.hidden));
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); open(false); btn.focus(); } });
  btn.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !panel.hidden) { e.preventDefault(); open(false); } });
  document.addEventListener('click', (e) => { if (!panel.hidden && !e.target.closest('#account-menu')) open(false); });
  document.getElementById('account-menu').addEventListener('focusout', (e) => {
    if (!panel.hidden && e.relatedTarget && !e.currentTarget.contains(e.relatedTarget)) open(false);
  });
  document.getElementById('account-signout')?.addEventListener('click', signOut);
}

async function signOut() {
  const btn = document.getElementById('account-signout');
  setBusy(btn, true);
  try {
    await fetch('/api/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': SESSION.me?.csrf || '' } });
  } catch { /* signed out on this page either way */ }
  try { localStorage.removeItem('jobhunt.ws'); } catch { /* private mode */ }
  leavingPage = true;
  location.replace('login.html');
}

/* Recovery codes not yet confirmed as saved, kept for this tab only (sessionStorage) so a reload of the page that shows
   them, or the "Show my recovery codes" link right after they were made, doesn't need the password again. The server
   gives them back only for the password (POST /api/auth/recovery-codes/show), because a stolen cookie must not read
   them. Gone when the tab closes, or when "I've saved them" is pressed. */
const CODES_KEY = 'jobhunt.codes';
function stashRecoveryCodes(codes) {
  try { sessionStorage.setItem(CODES_KEY, JSON.stringify(codes)); } catch { /* private mode: asks for the password instead */ }
}
function stashedRecoveryCodes() {
  try {
    const codes = JSON.parse(sessionStorage.getItem(CODES_KEY));
    return Array.isArray(codes) && codes.length && codes.every((c) => typeof c === 'string') ? codes : null;
  } catch { return null; }
}
function forgetRecoveryCodes() {
  try { sessionStorage.removeItem(CODES_KEY); } catch { /* nothing kept */ }
}

/* Recovery codes not yet confirmed as saved: a reminder on every page until they are. */
function remindRecoveryCodes(me) {
  if (!me.recovery_codes_pending) return;
  pageNotice('codes', `<p><strong>Save your recovery codes.</strong> If you forget your password, they are how you get back
    in and keep your data. <a href="first-run.html">Show my recovery codes</a></p>`);
}

const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(/T\d\d:\d\d/.test(iso) && !/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? `${iso}Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function scoreOf(job) { return job.fit && job.fit.status === 'ok' ? job.fit.score : null; }
/* "Applied <date>" while a job is in Applied and the date it last moved there is known (job.applied_at); nothing
   otherwise, also for a job that was in Applied before the date was kept. */
function appliedHtml(job) {
  return job.status === 'applied' && job.applied_at
    ? `<span class="applied-on"><span aria-hidden="true">📨 </span>Applied ${esc(fmtDate(job.applied_at).split(',')[0])}</span>` : '';
}
const EMPLOYMENT_LABEL = { full_time: 'Full-time', part_time: 'Part-time', contract: 'Contract', casual: 'Casual', temporary: 'Temporary', internship: 'Internship' };
/* The employment type as a neutral status label (a fact, not a warning); nothing when unknown. */
function employmentBadge(job) {
  const label = EMPLOYMENT_LABEL[job.employment_type];
  return label ? `<span class="p-status-label"><span class="visually-hidden">Employment type: </span>${label}</span>` : '';
}
/* Ghost jobs (backend/ghost.py): signs that the ad may not be a current vacancy. Every job shows 👻 and a coloured
   circle under its fit score: 🟢 no strong signs, 🟡 some signs, 🔴 several signs. Hovering over it says which signs
   were found. The circles differ only in colour, so the words are always there too: hidden beside the circle for
   screen readers, in the hover text, and in view for a flagged job (the card's "why?" line, the map list, the job
   page's section). Never an accusation: these are signs, and the job page says how they were weighed. */
const GHOST_STRENGTH = { likely: 'several signs', possible: 'some signs' };
const GHOST_DOT = { likely: '🔴', possible: '🟡' };
const ghostDot = (job) => GHOST_DOT[job.ghost?.level] || '🟢';
const ghostWords = (job) => (job.ghost?.level ? `Possible ghost job: ${GHOST_STRENGTH[job.ghost.level]}` : 'No strong signs of a ghost job');
/* The hover text: the rating in words, then each sign found, one a line. */
function ghostHover(job) {
  const signs = (job.ghost?.signals || []).map((s) => `• ${s.text}`);
  if (!job.ghost?.level && !signs.length) return 'No signs of a ghost job were found. Open the job to see what was checked.';
  const head = job.ghost?.level ? `${ghostWords(job)}.` : `No strong signs of a ghost job. ${signs.length === 1 ? 'One weak sign' : 'Weak signs'}:`;
  return [head, ...signs, 'Open the job to see why.'].join('\n');
}
/* 👻 and the circle. `href` makes it a link to the explanation (the job page's own section). */
function ghostMeter(job, href = '') {
  if (!job.ghost) return '';
  const body = `<span aria-hidden="true">👻 ${ghostDot(job)}</span><span class="visually-hidden">${ghostWords(job)}</span>`;
  if (href) return `<a class="ghost-dot" href="${esc(href)}" title="${esc(ghostHover(job))}">${body}<span class="visually-hidden">: why this rating</span></a>`;
  return `<span class="ghost-dot" title="${esc(ghostHover(job))}">${body}</span>`;
}
/* A job the person included themselves (added through the browser extension or a saved page of the one job, or its
   "Always include" ticked: includeToggle) is listed whatever their filters say; job.filter_warnings are the rules it
   doesn't meet. ⚠️ under its score says so, and opens a dialog that lists them (filterWarningsHtml; each page wires
   data-act="filter-warnings" to its own dialog). */
function filterWarnWords(job) {
  const n = (job.filter_warnings || []).length;
  return `Doesn't meet ${n === 1 ? 'one of your filters' : `${n} of your filters`}`;
}
function filterWarnButton(job) {
  if (!(job.filter_warnings || []).length) return '';
  return `<button class="filter-warn" type="button" data-act="filter-warnings" aria-haspopup="dialog" title="${esc(`${filterWarnWords(job)}. Select to see which.`)}"><span aria-hidden="true">⚠️</span><span class="visually-hidden">${filterWarnWords(job)}: show which</span></button>`;
}
const filterWarningsTitle = (job) => `${job.title || 'This job'}: filters it doesn't meet`;
function filterWarningsHtml(job, searchHref = '#search') {
  const list = job.filter_warnings || [];
  const upper = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  return `<p>You included this job yourself (you added it, or ticked <em>Always include</em>), so it is in your list even though it doesn't meet ${list.length === 1 ? 'this condition' : 'these conditions'} of your search filters:</p>
    <ul class="filter-warnings">${list.map((w) => `<li><span aria-hidden="true">⚠️ </span>${esc(upper(w))}</li>`).join('')}</ul>
    <p class="hint">Jobs your searches find are left out of the list when they don't meet your filters. Untick <em>Always include</em> to let the filters decide on this one too, or change the filters on the <a href="${esc(searchHref)}">Search setup</a> tab.</p>`;
}
/* "Always include": ticked, the job stays in the list whatever the search filters say (job.included_by_user, also set
   for a job the person added); unticked, the filters decide again. PATCH /api/jobs/{id}/included; each page wires
   data-act="include". `describedBy`: the id of the job's title, for a control on a card. */
function includeToggle(job, describedBy = '') {
  return `<label class="check-field include-toggle"><input type="checkbox" data-act="include"${describedBy ? ` aria-describedby="${esc(describedBy)}"` : ''}${job.included_by_user ? ' checked' : ''} /> <span>Always include</span></label>`;
}
// What became of a job whose "Always include" was just set (`job`: the server's answer), for a toast.
function includedWords(job, showExcludedHint = false) {
  if (job.included_by_user) {
    const n = (job.filter_warnings || []).length;
    return n ? `Always included: it stays in your list though it doesn't meet ${n === 1 ? 'one of your filters' : `${n} of your filters`} (the ⚠️).`
      : 'Always included: it stays in your list whatever your filters say.';
  }
  if (job.softened_reason) return `Your filters decide on it again. Your saved filters leave it out (${job.softened_reason}), but the extra office days you allowed show it for now.`;
  if (!job.excluded_reason) return 'Your filters decide on it again. It meets them, so it stays in your list.';
  return `Your filters decide on it again and leave it out: ${job.excluded_reason}.${showExcludedHint ? ' Tick "Show excluded" to see it.' : ''}`;
}
// The fit score with the filter warning (a job the person added) and the ghost-job rating under it (job cards, the
// map list, the job page).
function ratingsHtml(job, ghostHref = '') {
  return `<div class="ratings">${scoreBadge(job)}${filterWarnButton(job)}${ghostMeter(job, ghostHref)}</div>`;
}
// On the card of a flagged job: the signs that raised the flag, in words.
function ghostWhy(job) {
  const g = job.ghost;
  if (!g?.level || !(g.signals || []).length) return '';
  return `<details class="ghost-why" data-section="ghost"><summary><span aria-hidden="true">👻 ${ghostDot(job)} </span>Possible ghost job (${GHOST_STRENGTH[g.level]}): why?</summary>
    <ul class="ghost-signs">${g.signals.map((s) => `<li>${esc(s.text)}</li>`).join('')}</ul>
    <p class="hint">This may still be a real job. Ask the employer whether the role is open now. Open the job to see everything that was checked.</p></details>`;
}
function modeOf(job) { return ['remote', 'hybrid', 'onsite'].includes(job.work_mode) ? job.work_mode : 'unknown'; }
// The work mode's status-label tone (Jobs tab, map list, job page). Unknown is the plain label.
const MODE_TONE = { remote: 'positive', hybrid: 'caution', onsite: 'information', unknown: '' };

/* The fit score as a status label (the Jobs tab's table and board, the job page's summary; the cards keep the 🎯 score
   with the ghost-job circle under it: ratingsHtml). The number is the score and its colour only repeats it; screen
   readers get "Fit score 82 out of 100". A score against older documents carries a * (`star: false` leaves it out,
   where an "Older documents" label stands beside it). `large`: the job page's size. */
const fitTone = (score) => (score >= 75 ? 'positive' : score >= 45 ? 'caution' : 'negative');
function fitLabel(job, { large = false, star = !large } = {}) {
  const fit = job.fit;
  const cls = `p-status-label${large ? ' is-large' : ''}`;
  if (!fit) return `<span class="${cls}" title="Not scored yet">Not scored yet</span>`;
  if (fit.status === 'error') return `<span class="${cls} p-status-label--negative" title="${esc(fit.error || 'Scoring failed')}">Scoring failed</span>`;
  const title = fit.stale ? 'Scored against older documents' : `Scored ${fmtDate(fit.scored_at)}`;
  return `<span class="${cls} p-status-label--${fitTone(fit.score)}" title="${esc(title)}"><span class="visually-hidden">Fit score </span>${fit.score}<span class="visually-hidden"> out of 100${
    fit.stale ? ', scored against older documents' : ''}</span>${fit.stale && star ? '<span aria-hidden="true">*</span>' : ''}</span>`;
}

// 🎯 and the fit score. The number is the score; its colour only repeats it. Screen readers get "Fit score 82 of 100".
const SCORE_ICON = '<span aria-hidden="true">🎯 </span>';
function scoreBadge(job) {
  const fit = job.fit;
  if (!fit) return `<span class="score none" title="Not scored yet">${SCORE_ICON}<span aria-hidden="true">–</span><span class="visually-hidden">Not scored yet</span></span>`;
  if (fit.status === 'error') {
    return `<span class="score err" title="${esc(fit.error || 'error')}">${SCORE_ICON}<span aria-hidden="true">!</span><span class="visually-hidden">Scoring failed</span></span>`;
  }
  const cls = fit.score >= 75 ? 'high' : fit.score >= 45 ? 'mid' : 'low';
  const stale = fit.stale ? ' stale' : '';
  const title = fit.stale ? 'Scored against older documents' : `Scored ${fmtDate(fit.scored_at)}`;
  return `<span class="score ${cls}${stale}" title="${esc(title)}">${SCORE_ICON}<span class="visually-hidden">Fit score </span>${fit.score}${
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

/* A removable chip, as in the tag fields and the scheduled times: a list item (.tag keeps its value for the code that
   reads the fields back) with a real button to remove it. The button's name says what it removes. */
const CHIP_X = '<svg aria-hidden="true" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4l8 8M12 4l-8 8"></path></svg>';
const chipHtml = (value) => `<li class="tag p-chip p-chip--dismiss" data-value="${esc(value)}"><span>${esc(value)}</span><button class="p-chip__x" type="button" aria-label="Remove ${esc(value)}">${CHIP_X}</button></li>`;

function glassdoorLink(p, name) {
  if (p?.glassdoor_url) {
    return `<a class="glassdoor" href="${esc(p.glassdoor_url)}" target="_blank" rel="noopener noreferrer">Glassdoor page${NEW_TAB}</a>`;
  }
  // No verified page: a Glassdoor search for the name (built here, not by the model).
  const q = encodeURIComponent(p?.official_name || name);
  return `<a class="glassdoor" href="https://www.glassdoor.com.au/Search/results.htm?keyword=${q}" target="_blank" rel="noopener noreferrer">Search Glassdoor${NEW_TAB}</a>`;
}

/* "Researched <date>" note inside a profile section (read in context by screen readers). */
function sectionDateNote(iso) { return iso ? `<div class="muted section-date">Researched ${esc(fmtDate(iso))}</div>` : ''; }

/* A profile section's own date and its "Research again" button, which researches only that section, at any time
   (POST /api/companies/research with part). The section's name is hidden text, so the three buttons of a profile
   have different names. Pages wire every [data-research-part] button the same way. */
const RESEARCH_SECTION_NAME = { sentiment: 'Employee sentiment', news: 'News/Controversies' };
function sectionResearch(part, iso) {
  return `<div class="section-research">${sectionDateNote(iso)}<button id="company-research-${part}" class="btn btn-sm" type="button" data-research-part="${part}">Research again<span class="visually-hidden">: ${RESEARCH_SECTION_NAME[part]} only</span></button></div>`;
}

/* Plain-language result of a research run, from its summary.research ({refreshed, up_to_date}). */
const RESEARCH_PART_LABEL = { base: 'company profile', sentiment: 'employee sentiment', news: 'news/controversies' };
function researchRefreshText(rs) {
  const joinAnd = (a) => (a.length > 1 ? `${a.slice(0, -1).join(', ')} and ${a[a.length - 1]}` : a[0] || '');
  const parts = Object.entries(rs?.refreshed || {}).map(([name, kinds]) => `${name}: ${joinAnd((kinds || []).map((k) => RESEARCH_PART_LABEL[k] || k))} updated`);
  if (parts.length) return parts.join('; ');
  const n = Array.isArray(rs?.up_to_date) ? rs.up_to_date.length : Number(rs?.up_to_date) || 0;
  return n ? 'Everything was already up to date' : '';
}

function sentimentHtml(p) {
  const s = p.sentiment;
  if (!s) return `<span class="muted">Not researched yet.</span>${sectionResearch('sentiment', null)}`;
  const list = (items, cls, mark, label) => (items.length
    ? `<ul class="themes ${cls}" aria-label="${label}">${items.map((t) => `<li><span aria-hidden="true">${mark} </span>${esc(t)}</li>`).join('')}</ul>` : '');
  return `
    ${s.rating ? `<div><strong>${esc(s.rating)}</strong></div>` : ''}
    <div>${esc(s.summary || 'No employee reviews found.')}</div>
    ${list(s.positives, 'pos', '+', 'Positives')}${list(s.negatives, 'neg', '−', 'Negatives')}
    ${s.sources.length ? `<div class="sources">${linkList(s.sources)}</div>` : ''}
    ${sectionResearch('sentiment', p.sentiment_researched_at)}`;
}

/* Where a job's main link goes (GET /api/jobs: job.link): the employer's own page when a merged copy of the ad is on
   it and the listed ad is on a job board, else the listed ad. A server from before sends none: the listed ad. */
function mainLink(job) {
  return job.link?.url ? job.link : { url: job.url, source: job.source, copy_id: null };
}

/* ================= SOURCES ================= */
/* A job at one of SEEK's own addresses, whatever brought it in (a search, a saved page, the browser extension):
   the full ad can be uploaded from a page saved on SEEK (POST /api/jobs/{id}/import-page, which matches the page
   to the job by SEEK's job number). SEEK lives at au.seek.com now; jobs are stored under www.seek.com.au. */
function isSeekJob(job) {
  return job.source === 'seek' || /^https:\/\/([a-z0-9-]+\.)?seek\.(com|com\.au|co\.nz)\/job\/\d+([/?#]|$)/.test(job.url || '');
}

/* Sites named by more than their address (backend/job_source.py SITE_NAMES). */
const SITE_NAMES = { 'seek.com.au': 'SEEK', 'seek.co.nz': 'SEEK', 'seek.com': 'SEEK', 'linkedin.com': 'LinkedIn', 'indeed.com': 'Indeed' };
function siteName(host) {
  const known = Object.keys(SITE_NAMES).find((k) => host === k || host.endsWith(`.${k}`));
  return known ? SITE_NAMES[known] : host;
}

/* Where a job's ad came from, in words (backend/job_source.py lists the values). */
function sourceLabel(src) {
  const s = String(src || '');
  if (s === 'seek') return 'SEEK';
  if (s === 'import') return 'imported page';
  if (s === 'browser') return 'browser extension';
  const i = s.indexOf(':');
  if (i < 0) return s;
  const kind = s.slice(0, i), rest = s.slice(i + 1);
  if (kind === 'browser') { // "browser:bulk:seek.com.au": the browser extension, how (one job or a page of them), the site
    const j = rest.indexOf(':');
    const how = j < 0 ? rest : rest.slice(0, j), site = j < 0 ? '' : rest.slice(j + 1);
    const words = `browser extension${how === 'bulk' || how === 'single' ? ` (${how})` : ''}`;
    return site ? `${siteName(site)} - ${words}` : words;
  }
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

/* Company profile body (info modal and job page). Has a #company-research button (data-research-part="all": the
   whole profile, whatever its age) and, on a researched profile, one per section (sectionResearch). */
function companyProfileHtml(p, name) {
  if (p && !RESEARCHED.includes(p.status)) p = null; // only offices known so far
  const researchBtn = `<button id="company-research" class="btn btn-sm research-btn" type="button" data-research-part="all">${p ? 'Research the whole profile again' : 'Research this company'}</button>`;
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
      <dt>News/Controversies</dt><dd>${esc(p.controversy_note || (p.controversies.length ? '' : 'None found.'))}${cons}${sectionResearch('news', p.news_researched_at)}</dd>
    </dl>
    ${p.sources.length ? `<p class="sources"><span class="muted">Sources:</span> ${linkList(p.sources)}</p>` : ''}
    <p class="muted">Researched ${esc(fmtDate(p.researched_at))}.
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
// be "system", which follows the OS between Vanilla Light and Vanilla Dark; with
// nothing stored (a new install) the theme is Vanilla Dark. themes.css has one
// block per theme; each page's <head> script applies the theme before first
// paint, and initTheme() keeps it in step afterwards (and across open tabs).
// The Settings tab chooses it with createThemePicker(), which the sign-up wizard
// (docs/design/accounts.md, Phase K) reuses as its first step.
// Each family theme comes twice: '<id>-aa', adjusted to WCAG AA, and the plain id, the
// project's exact palette. aa: true marks the themes that meet AA (the picker's badge);
// tools/theme_contrast.py checks exactly these and the tests check the flag matches.
const DYSLEXIA_FONT = 'Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400';
const THEMES = [
  { id: 'system', label: 'System', family: 'Vanilla', mode: 'auto', note: 'follows this device', aa: true },
  { id: 'vanilla-dark-aa', label: 'Vanilla Dark', family: 'Vanilla', mode: 'dark', aa: true },
  { id: 'vanilla-dark', label: 'Vanilla Dark', family: 'Vanilla', mode: 'dark' },
  { id: 'vanilla-light-aa', label: 'Vanilla Light', family: 'Vanilla', mode: 'light', aa: true },
  { id: 'vanilla-light', label: 'Vanilla Light', family: 'Vanilla', mode: 'light' },
  { id: 'dark', label: 'Dark', family: 'JobHuntPA', mode: 'dark', aa: true },
  { id: 'light', label: 'Light', family: 'JobHuntPA', mode: 'light', aa: true },
  { id: 'catppuccin-latte-aa', label: 'Catppuccin Latte', family: 'Catppuccin', mode: 'light', aa: true },
  { id: 'catppuccin-latte', label: 'Catppuccin Latte', family: 'Catppuccin', mode: 'light' },
  { id: 'catppuccin-frappe-aa', label: 'Catppuccin Frappé', family: 'Catppuccin', mode: 'dark', aa: true },
  { id: 'catppuccin-frappe', label: 'Catppuccin Frappé', family: 'Catppuccin', mode: 'dark' },
  { id: 'catppuccin-macchiato-aa', label: 'Catppuccin Macchiato', family: 'Catppuccin', mode: 'dark', aa: true },
  { id: 'catppuccin-macchiato', label: 'Catppuccin Macchiato', family: 'Catppuccin', mode: 'dark' },
  { id: 'catppuccin-mocha-aa', label: 'Catppuccin Mocha', family: 'Catppuccin', mode: 'dark', aa: true },
  { id: 'catppuccin-mocha', label: 'Catppuccin Mocha', family: 'Catppuccin', mode: 'dark' },
  { id: 'rose-pine-aa', label: 'Rosé Pine', family: 'Rosé Pine', mode: 'dark', aa: true },
  { id: 'rose-pine', label: 'Rosé Pine', family: 'Rosé Pine', mode: 'dark' },
  { id: 'rose-pine-moon-aa', label: 'Rosé Pine Moon', family: 'Rosé Pine', mode: 'dark', aa: true },
  { id: 'rose-pine-moon', label: 'Rosé Pine Moon', family: 'Rosé Pine', mode: 'dark' },
  { id: 'rose-pine-dawn-aa', label: 'Rosé Pine Dawn', family: 'Rosé Pine', mode: 'light', aa: true },
  { id: 'rose-pine-dawn', label: 'Rosé Pine Dawn', family: 'Rosé Pine', mode: 'light' },
  { id: 'nord-aa', label: 'Nord', family: 'Nord', mode: 'dark', aa: true },
  { id: 'nord', label: 'Nord', family: 'Nord', mode: 'dark' },
  { id: 'nord-light-aa', label: 'Nord Light', family: 'Nord', mode: 'light', aa: true },
  { id: 'nord-light', label: 'Nord Light', family: 'Nord', mode: 'light' },
  { id: 'kanagawa-wave-aa', label: 'Kanagawa Wave', family: 'Kanagawa', mode: 'dark', aa: true },
  { id: 'kanagawa-wave', label: 'Kanagawa Wave', family: 'Kanagawa', mode: 'dark' },
  { id: 'kanagawa-dragon-aa', label: 'Kanagawa Dragon', family: 'Kanagawa', mode: 'dark', aa: true },
  { id: 'kanagawa-dragon', label: 'Kanagawa Dragon', family: 'Kanagawa', mode: 'dark' },
  { id: 'kanagawa-lotus-aa', label: 'Kanagawa Lotus', family: 'Kanagawa', mode: 'light', aa: true },
  { id: 'kanagawa-lotus', label: 'Kanagawa Lotus', family: 'Kanagawa', mode: 'light' },
  { id: 'everforest-dark-aa', label: 'Everforest Dark', family: 'Everforest', mode: 'dark', aa: true },
  { id: 'everforest-dark', label: 'Everforest Dark', family: 'Everforest', mode: 'dark' },
  { id: 'everforest-light-aa', label: 'Everforest Light', family: 'Everforest', mode: 'light', aa: true },
  { id: 'everforest-light', label: 'Everforest Light', family: 'Everforest', mode: 'light' },
  { id: 'dyslexia', label: 'Dyslexia', family: 'Built for reading', mode: 'light', font: DYSLEXIA_FONT, note: 'Atkinson Hyperlegible, wider spacing', aa: true },
  { id: 'dyslexia-dark', label: 'Dyslexia (dark)', family: 'Built for reading', mode: 'dark', font: DYSLEXIA_FONT, note: 'Atkinson Hyperlegible, wider spacing', aa: true },
  { id: 'protanomaly', label: 'Protanomaly', family: 'Built for colour vision', mode: 'light', note: 'red-weak', aa: true },
  { id: 'protanomaly-dark', label: 'Protanomaly (dark)', family: 'Built for colour vision', mode: 'dark', note: 'red-weak', aa: true },
  { id: 'deuteranomaly', label: 'Deuteranomaly', family: 'Built for colour vision', mode: 'light', note: 'green-weak', aa: true },
  { id: 'deuteranomaly-dark', label: 'Deuteranomaly (dark)', family: 'Built for colour vision', mode: 'dark', note: 'green-weak', aa: true },
  { id: 'tritanomaly', label: 'Tritanomaly', family: 'Built for colour vision', mode: 'light', note: 'blue-weak', aa: true },
  { id: 'tritanomaly-dark', label: 'Tritanomaly (dark)', family: 'Built for colour vision', mode: 'dark', note: 'blue-weak', aa: true },
  { id: 'dichromacy', label: 'Dichromacy', family: 'Built for colour vision', mode: 'light', note: 'any kind', aa: true },
  { id: 'dichromacy-dark', label: 'Dichromacy (dark)', family: 'Built for colour vision', mode: 'dark', note: 'any kind', aa: true },
  { id: 'monochromacy', label: 'Monochromacy', family: 'Built for colour vision', mode: 'light', note: 'greys only', aa: true },
  { id: 'monochromacy-dark', label: 'Monochromacy (dark)', family: 'Built for colour vision', mode: 'dark', note: 'greys only', aa: true },
];
const THEME_KEY = 'jobhunt.theme';
const DEFAULT_THEME = 'vanilla-dark-aa'; // also in each page's first-paint script
// Before the exact palettes came in, the plain family ids were the AA-adjusted palettes. A browser that
// saved one keeps what it chose: once, the saved id becomes '<id>-aa' (the first-paint script in each
// page does the same, with the same list as a pattern; tests/test_accessibility.py checks they agree).
const THEME_MIGRATED_KEY = 'jobhunt.themeMigrated';
const EXACT_THEMES = THEMES.filter((t) => !t.aa).map((t) => t.id);

function migrateStoredTheme() {
  try {
    if (localStorage.getItem(THEME_MIGRATED_KEY) === 'aa') return;
    const t = localStorage.getItem(THEME_KEY);
    if (EXACT_THEMES.includes(t)) localStorage.setItem(THEME_KEY, `${t}-aa`);
    localStorage.setItem(THEME_MIGRATED_KEY, 'aa');
  } catch { /* private mode: nothing saved to migrate */ }
}
const MODE_LABEL = { light: 'Light', dark: 'Dark', auto: 'Light or dark' };
// The picker's mark of a theme that meets WCAG AA (a status label with the words in it, so screen readers
// hear it), and the line under an exact palette instead.
const AA_BADGE = '<span class="p-status-label p-status-label--positive theme-aa">WCAG 2.0 AA</span>';
const EXACT_NOTE = 'The project\'s exact colours; some text is under 4.5:1';

function storedTheme() {
  migrateStoredTheme();
  try {
    const t = localStorage.getItem(THEME_KEY);
    return THEMES.some((x) => x.id === t) ? t : DEFAULT_THEME;
  } catch { return DEFAULT_THEME; }
}

function resolveTheme(pref) {
  return pref === 'system' ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'vanilla-dark-aa' : 'vanilla-light-aa') : pref;
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
  document.dispatchEvent(new CustomEvent('themechange', { detail: { id, pref } }));
}

/* Saves a theme for this browser and applies it. */
function setTheme(pref) {
  try { localStorage.setItem(THEME_KEY, pref); } catch { /* still applies to this page */ }
  applyTheme(pref);
}

/* A theme colour for things drawn in script (map markers and routes). */
function themeColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim() || '#888888';
}

/* Applies the stored theme now, then follows it: a change in another tab (the dashboard or a job page),
   and the device's light/dark switch while the theme is System. Every page calls this once. */
function initTheme() {
  window.addEventListener('storage', (e) => { if (e.key === THEME_KEY) applyTheme(); });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (storedTheme() === 'system') applyTheme();
  });
  applyTheme();
}

/* A theme's miniature: a header bar with a selected tab, and a job card with a title, a line of muted
   text, a work-mode badge, a score and a primary button, all drawn in that theme's own tokens because
   the preview carries data-theme (themes.css scopes every theme to the attribute). Decorative. */
function themePreviewHtml(id) {
  return `<span class="theme-preview" data-theme="${esc(id)}" aria-hidden="true">
      <span class="tp-bar"><span class="tp-tab"></span><span class="tp-tab tp-off"></span></span>
      <span class="tp-page"><span class="tp-card">
        <span class="tp-title">Product Manager</span>
        <span class="tp-muted">Sydney · hybrid</span>
        <span class="tp-row"><span class="tp-badge">remote</span><span class="tp-score">82</span><span class="tp-btn">Apply</span></span>
      </span></span>
    </span>`;
}

/* The theme picker: one radio group (native radios, so the arrow keys move between themes and Tab enters
   at the chosen one), in a fieldset per family. Each option is a card with a live preview, the name and
   whether it is light or dark; the chosen card shows a filled radio, a thicker border and "Selected", so
   the choice never rests on colour. Choosing applies the theme at once, saves it for this browser and
   announces it. Returns the element; options:
     legend    the group's name (visually hidden; the card it sits in has its own heading)
     name      the radios' name, unique on the page (default "theme")
     onChange  called with the theme id after it is applied and saved (the sign-up wizard saves it to the account) */
function createThemePicker({ legend = 'Theme', name = 'theme', onChange = null } = {}) {
  const root = document.createElement('fieldset');
  root.className = 'theme-picker';
  const families = [...new Set(THEMES.map((t) => t.family))];
  const card = (t) => {
    const id = `${name}-opt-${t.id}`;
    const preview = t.id === 'system'
      ? `<span class="theme-split">${themePreviewHtml('vanilla-light-aa')}${themePreviewHtml('vanilla-dark-aa')}</span>`
      : themePreviewHtml(t.id);
    return `<label class="theme-card" for="${id}">
        ${preview}
        <span class="theme-card-text">
          <input type="radio" name="${esc(name)}" id="${id}" value="${esc(t.id)}" />
          <span class="theme-name">${esc(t.label)}</span>
          <span class="theme-mode">${MODE_LABEL[t.mode]}${t.note ? `, ${esc(t.note)}` : ''}</span>
          ${t.aa ? `<span class="theme-badge">${AA_BADGE}</span>` : `<span class="theme-mode theme-exact">${EXACT_NOTE}</span>`}
          <span class="theme-chosen" aria-hidden="true">✓ Selected</span>
        </span>
      </label>`;
  };
  root.innerHTML = `<legend class="visually-hidden">${esc(legend)}</legend>
    ${families.map((f) => `<fieldset class="theme-family">
        <legend class="theme-family-name">${esc(f)}</legend>
        <div class="theme-grid">${THEMES.filter((t) => t.family === f).map(card).join('')}</div>
      </fieldset>`).join('')}`;
  const sync = () => {
    const current = storedTheme();
    root.querySelectorAll('input[type="radio"]').forEach((r) => { r.checked = r.value === current; });
  };
  root.addEventListener('change', (e) => {
    const radio = e.target.closest('input[type="radio"]');
    if (!radio) return;
    setTheme(radio.value);
    const t = THEMES.find((x) => x.id === radio.value);
    announce(`Theme: ${t.label}${t.aa ? ', WCAG 2.0 AA' : ', exact colours'}${t.id === 'system' ? `, now ${resolveTheme('system') === 'vanilla-dark-aa' ? 'Vanilla Dark' : 'Vanilla Light'}` : ''}.`);
    onChange?.(radio.value);
  });
  document.addEventListener('themechange', sync); // chosen in another tab
  sync();
  return root;
}
