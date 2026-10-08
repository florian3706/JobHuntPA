/* JobHuntPA sign-in pages: login.html, first-run.html, recover.html and signup.html (accounts design, sections 5 and 6).

   There is no workspace and often no session yet, so these pages call /api/auth themselves rather than through
   common.js's api(). WCAG 2.2 notes:
   - 3.3.8 accessible authentication: no puzzle or memory test; paste always works; the fields carry the
     autocomplete values password managers look for (username, current-password, new-password, name).
   - 3.3.1 / 3.3.3: a problem is listed in an error summary at the top, which takes focus and links to each field,
     and is repeated under the field it belongs to (aria-describedby).
   - 3.3.7: nothing is asked twice: no "confirm password" field; "Show password" lets people check what they typed.
   - 4.1.3: progress ("Signing in…") is said in a status region.
   - The code step (two-step sign-in) and recover.html follow the same pattern; the code field is
     autocomplete="one-time-code" and takes spaces and hyphens. */
'use strict';

const PAGE = document.body.dataset.page;

/* ---------- talking to the server ---------- */
async function authCall(path, { method = 'GET', body = null, csrf = null } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  if (csrf) headers['X-CSRF-Token'] = csrf;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === null ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  } catch {
    return { status: 0, data: { detail: "JobHuntPA isn't answering. Check that it's running, then try again." } };
  }
  let data = {};
  try { data = await res.json(); } catch { /* an empty or non-JSON answer */ }
  return { status: res.status, data: data || {}, headers: res.headers };
}

/* Where to go after signing in: the page that sent people here (?next=), if it is a path on this site. The value is
   resolved the way the browser will (which drops tabs and newlines, so "/<tab>/evil.example" means "//evil.example")
   and must stay on this origin. A path can stay on the origin and still start with two slashes once the dots are
   resolved ("/.//evil.example" has the path "//evil.example"), which the browser reads as another site. */
function nextPage() {
  const next = new URLSearchParams(location.search).get('next') || '';
  if (!next.startsWith('/')) return './';
  let url;
  try { url = new URL(next, location.origin); } catch { return './'; }
  if (url.origin !== location.origin || /^\/(login|first-run|recover|signup)\.html/.test(url.pathname)) return './';
  if (/^\/[\/\\]/.test(url.pathname)) return './'; // "//host" or "/\host" is a link to another site
  return url.pathname + url.search + url.hash;
}

/* ---------- errors: the summary at the top and the message under each field ---------- */
function clearErrors() {
  $('#error-summary').hidden = true;
  $('#error-summary-list').innerHTML = '';
  $$('.auth-form .field-error, .codes-step .field-error').forEach((el) => el.remove());
  $$('[aria-invalid="true"]').forEach((input) => {
    input.removeAttribute('aria-invalid');
    const ids = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter((x) => x && x !== `${input.id}-error`);
    if (ids.length) input.setAttribute('aria-describedby', ids.join(' ')); else input.removeAttribute('aria-describedby');
  });
}

/* errors: [{ field: input id or null, message }]. Shows them and moves focus to the summary. */
function showErrors(errors) {
  clearErrors();
  const list = $('#error-summary-list');
  for (const { field, message } of errors) {
    const input = field && document.getElementById(field);
    const li = document.createElement('li');
    if (input && !input.closest('[hidden]')) {
      li.innerHTML = `<a href="#${esc(field)}">${esc(message)}</a>`;
      li.querySelector('a').addEventListener('click', (e) => { e.preventDefault(); input.focus(); });
      const note = document.createElement('p');
      note.id = `${input.id}-error`;
      note.className = 'field-error';
      note.innerHTML = `<span class="visually-hidden">Error: </span>${esc(message)}`;
      (input.closest('.pw-row') || input.closest('label.check') || input).insertAdjacentElement('afterend', note);
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', [note.id, input.getAttribute('aria-describedby')].filter(Boolean).join(' '));
    } else {
      li.textContent = message;
    }
    list.appendChild(li);
  }
  const summary = $('#error-summary');
  summary.hidden = false;
  summary.focus();
}

function status(text) { setText($('#auth-status'), text); }

/* ---------- "Show password" ---------- */
function initPasswordToggles() {
  $$('.pw-toggle').forEach((btn) => {
    const input = document.getElementById(btn.getAttribute('aria-controls'));
    btn.addEventListener('click', () => {
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', String(show));
    });
  });
}

/* Before sending a form again, the password goes back to dots, so a password manager sees a password field. */
function hidePasswords() {
  $$('.pw-toggle').forEach((btn) => {
    document.getElementById(btn.getAttribute('aria-controls')).type = 'password';
    btn.setAttribute('aria-pressed', 'false');
  });
}

/* The server names fields like the API (new_password); the pages' inputs have their own ids. */
const FIELD_IDS = { new_password: 'password', recovery_code: 'recovery-code', second_recovery_code: 'code' };

function answerProblem(res, fallback) {
  const { data } = res;
  const message = typeof data.detail === 'string' ? data.detail : fallback;
  return [{ field: FIELD_IDS[data.field] || data.field || null, message }];
}

/* ================= login.html ================= */
let loginEmail = '';
let usingRecovery = false;
const EMAIL_KEY = 'jobhunt.login-email'; // this tab only: lets the code step after a reload still name the account

function rememberEmail(email) {
  loginEmail = email;
  try { if (email) sessionStorage.setItem(EMAIL_KEY, email); else sessionStorage.removeItem(EMAIL_KEY); } catch { /* private mode */ }
}

function showPasswordStep() {
  clearErrors();
  $('#mfa-form').hidden = true;
  $('#login-form').hidden = false;
  $('#page-title').textContent = 'Sign in';
  document.title = 'Sign in · JobHuntPA';
  $('#password').value = '';
}

/* The password was right and the account has two-step sign-in: ask for a code. The session is only half open until
   the code is accepted (10 minutes); a reload comes back here, because /api/auth/state says mfa_pending. */
function showCodeStep(recovery = false) {
  clearErrors();
  status('');
  $('#login-form').hidden = true;
  $('#mfa-form').hidden = false;
  $('#page-title').textContent = 'Enter your code';
  document.title = 'Enter your code · JobHuntPA';
  $('#mfa-username').value = loginEmail;
  setCodeMode(recovery);
}

function setCodeMode(recovery) {
  usingRecovery = recovery;
  clearErrors();
  $('#code-field').hidden = recovery;
  $('#recovery-field').hidden = !recovery;
  $('#mfa-switch').textContent = recovery ? 'Use my authenticator app instead' : 'Use a recovery code instead';
  $('#mfa-intro').textContent = recovery
    ? 'Enter one of your recovery codes. It works once.'
    : 'Your account uses two-step sign-in. Open your authenticator app and enter the 6-digit code.';
  (recovery ? $('#recovery-code') : $('#code')).focus();
}

async function initLogin() {
  try { loginEmail = sessionStorage.getItem(EMAIL_KEY) || ''; } catch { /* private mode */ }
  const state = await authCall('/api/auth/state');
  if (state.status === 200 && state.data.setup_required) { location.replace('first-run.html'); return; }
  if (state.status === 200 && state.data.signed_in) { location.replace(nextPage()); return; }
  const form = $('#login-form');
  if (state.status === 200 && state.data.mfa_pending) showCodeStep(); else $('#email').focus();
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = $('#email').value.trim();
    const password = $('#password').value;
    const missing = [];
    if (!email) missing.push({ field: 'email', message: 'Enter your email address.' });
    if (!password) missing.push({ field: 'password', message: 'Enter your password.' });
    if (missing.length) { showErrors(missing); return; }
    const btn = $('#login-submit');
    setBusy(btn, true);
    status('Signing in…');
    hidePasswords();
    const res = await authCall('/api/auth/login', { method: 'POST', body: { email, password, remember: $('#remember').checked } });
    setBusy(btn, false);
    if (res.status === 200 && res.data.mfa_required) { rememberEmail(email); showCodeStep(); return; }
    if (res.status === 200) {
      rememberEmail('');
      status('Signed in. Opening JobHuntPA…');
      location.replace(nextPage());
      return;
    }
    status('');
    if (res.data.code === 'setup_required') { location.replace('first-run.html'); return; }
    const errors = answerProblem(res, `Signing in didn't work (error ${res.status}). Try again.`);
    if (res.data.code === 'wrong_password') errors[0].field = 'password';
    showErrors(errors);
  });

  $('#mfa-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const field = usingRecovery ? 'recovery-code' : 'code';
    const value = $(`#${field}`).value.trim();
    if (!value) {
      showErrors([{ field, message: usingRecovery ? 'Enter a recovery code.' : 'Enter the 6-digit code from your app.' }]);
      return;
    }
    const btn = $('#mfa-submit');
    setBusy(btn, true);
    status('Checking your code…');
    const res = await authCall('/api/auth/login/mfa', { method: 'POST', body: usingRecovery ? { recovery_code: value } : { code: value } });
    setBusy(btn, false);
    if (res.status === 200) {
      rememberEmail('');
      // A recovery code is used up: tell the person how many are left, on the page that opens next (common.js).
      const left = res.data.recovery_codes_left;
      if (usingRecovery && typeof left === 'number') {
        try { sessionStorage.setItem(SIGN_IN_NOTICE_KEY, recoveryLeftText(left)); } catch { /* private mode */ }
      }
      status('Signed in. Opening JobHuntPA…');
      location.replace(nextPage());
      return;
    }
    status('');
    if (res.data.code === 'mfa_expired') {
      rememberEmail('');
      // The half-open session ended (10 minutes, or too many wrong codes): start again from the password.
      showPasswordStep();
      showErrors(answerProblem(res, 'That sign-in timed out. Enter your password again.').map((x) => ({ ...x, field: null })));
      return;
    }
    const errors = answerProblem(res, `That didn't work (error ${res.status}). Try again.`);
    if (['wrong_code', 'wrong_recovery_code', 'invalid'].includes(res.data.code)) errors[0].field = field;
    showErrors(errors);
  });
  $('#mfa-switch').addEventListener('click', () => setCodeMode(!usingRecovery));
  $('#mfa-restart').addEventListener('click', async () => {
    await authCall('/api/auth/logout', { method: 'POST', body: {} }); // ends the half-open session; no CSRF before sign-in
    rememberEmail('');
    showPasswordStep();
    $('#password').focus();
  });
}

/* ================= recover.html ================= */
/* Forgot the password, but have a recovery code: set a new one and keep the data. With two-step sign-in on, the
   server asks for a second proof (an app code or a second recovery code); the page can't know beforehand, which
   keeps it from telling anyone whether an account has two-step sign-in. */
function initRecover() {
  $('#email').focus();
  $('#recover-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = $('#email').value.trim();
    const recovery = $('#recovery-code').value.trim();
    const password = $('#password').value;
    const second = $('#code').value.trim();
    const problems = [];
    if (!email) problems.push({ field: 'email', message: 'Enter your email address.' });
    if (!recovery) problems.push({ field: 'recovery-code', message: 'Enter one of your recovery codes.' });
    if (!password) problems.push({ field: 'password', message: 'Choose a new password.' });
    else if (password.length < 10) problems.push({ field: 'password', message: 'Use at least 10 characters. A few random words work well.' });
    if (!$('#code-field').hidden && !second) problems.push({ field: 'code', message: 'Enter the code from your app, or a second recovery code.' });
    if (problems.length) { showErrors(problems); return; }
    const body = { email, recovery_code: recovery, new_password: password, remember: $('#remember').checked };
    if (second) body[/^\d{6}$/.test(second.replace(/[\s-]/g, '')) ? 'code' : 'second_recovery_code'] = second;
    const btn = $('#recover-submit');
    setBusy(btn, true);
    status('Setting your new password…');
    hidePasswords();
    const res = await authCall('/api/auth/recover', { method: 'POST', body });
    setBusy(btn, false);
    status('');
    if (res.status === 200) { showRecovered(res.data.recovery_codes_left); return; }
    if (res.data.code === 'second_factor_required') {
      $('#code-field').hidden = false;
      showErrors([{ field: 'code', message: 'Your account uses two-step sign-in. Enter the code from your app, or a second recovery code.' }]);
      return;
    }
    showErrors(answerProblem(res, `That didn't work (error ${res.status}). Try again.`));
  });
}

function showRecovered(left) {
  clearErrors();
  $('#recover-form').hidden = true;
  document.title = 'Password changed · JobHuntPA';
  $('#page-title').textContent = 'Password changed';
  const note = typeof left === 'number' ? ` ${recoveryLeftText(left)}` : '';
  const done = $('#recover-done');
  done.querySelector('p').textContent = `You're signed in, your data is kept and other browsers are signed out.${note}`;
  done.hidden = false;
  $('#recover-continue').href = nextPage();
  $('#page-title').focus();
  announce('Password changed. You are signed in.');
}

/* ================= first-run.html ================= */
let setupToken = '';

function readToken() {
  const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (m) {
    setupToken = m[1];
    // Out of the address bar, so it isn't bookmarked or shared; the page keeps it until the account exists.
    history.replaceState(null, '', location.pathname + location.search);
  }
}

async function initFirstRun() {
  readToken();
  const state = await authCall('/api/auth/state');
  if (state.status !== 200) { status(''); showErrors(answerProblem(state, "JobHuntPA isn't answering. Check that it's running, then reload this page.")); return; }
  if (!state.data.setup_required) {
    if (!state.data.signed_in) { location.replace('login.html'); return; }
    // Only the codes of an account that hasn't confirmed saving them are left to show.
    const me = await authCall('/api/auth/me');
    if (me.status !== 200 || !me.data.recovery_codes_pending) { forgetRecoveryCodes(); location.replace('./'); return; }
    const kept = stashedRecoveryCodes(); // (made or shown in this tab)
    if (kept) { showCodes(kept, me.data.csrf); return; }
    await askForCodes(me.data.csrf);
    return;
  }
  status('');
  $('#setup-step').hidden = false;
  $('#token-field').hidden = Boolean(setupToken);
  (setupToken ? $('#email') : $('#token')).focus();
  $('#setup-form').addEventListener('submit', submitSetup);
  // The setup link opened in a tab that already shows this page changes only the #fragment: no reload.
  window.addEventListener('hashchange', () => {
    readToken();
    if (setupToken) $('#token-field').hidden = true;
  });
}

/* Each recovery code opens the data key and can set a new password, so a session alone doesn't get to read them: the
   password (and, with two-step sign-in on, a code) is asked for again. */
async function askForCodes(csrf) {
  const security = await authCall('/api/auth/security');
  const mfa = security.status === 200 && Boolean(security.data.mfa_enabled);
  status('');
  document.title = 'Your recovery codes · JobHuntPA';
  $('#page-title').textContent = 'Your recovery codes';
  $('#setup-step').hidden = true;
  $('#show-code-field').hidden = !mfa;
  $('#show-step').hidden = false;
  $('#show-password').focus();
  $('#show-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = $('#show-password').value;
    const code = $('#show-code').value.trim();
    const problems = [];
    if (!password) problems.push({ field: 'show-password', message: 'Enter your password.' });
    if (mfa && !code) problems.push({ field: 'show-code', message: 'Enter a code from your authenticator app, or a recovery code.' });
    if (problems.length) { showErrors(problems); return; }
    const btn = $('#show-submit');
    setBusy(btn, true);
    hidePasswords();
    const body = { password };
    if (mfa) body[/^\d{6}$/.test(code.replace(/[\s-]/g, '')) ? 'code' : 'recovery_code'] = code;
    const res = await authCall('/api/auth/recovery-codes/show', { method: 'POST', body, csrf });
    setBusy(btn, false);
    if (res.status === 200) {
      $('#show-password').value = '';
      $('#show-code').value = '';
      $('#show-step').hidden = true;
      stashRecoveryCodes(res.data.codes);
      showCodes(res.data.codes, csrf);
      return;
    }
    if (res.status === 404 || res.status === 401) { location.replace('./'); return; } // saved already, or signed out
    const errors = answerProblem(res, `That didn't work (error ${res.status}). Try again.`);
    errors[0].field = errors[0].field === 'password' ? 'show-password' : errors[0].field ? 'show-code' : null;
    showErrors(errors);
  });
}

async function submitSetup(e) {
  e.preventDefault();
  const token = setupToken || $('#token').value.trim();
  const email = $('#email').value.trim();
  const password = $('#password').value;
  const problems = [];
  if (!token) problems.push({ field: 'token', message: 'Paste the setup code from first-run.txt.' });
  if (!email) problems.push({ field: 'email', message: 'Enter your email address.' });
  if (!password) problems.push({ field: 'password', message: 'Choose a password.' });
  else if (password.length < 10) problems.push({ field: 'password', message: 'Use at least 10 characters. A few random words work well.' });
  if (problems.length) { showErrors(problems); return; }
  const btn = $('#setup-submit');
  setBusy(btn, true);
  status('Creating your account…');
  hidePasswords();
  const res = await authCall('/api/auth/first-run', {
    method: 'POST',
    body: { token, email, password, name: $('#name').value.trim(), remember: $('#remember').checked, background: $('#background').checked },
  });
  setBusy(btn, false);
  if (res.status === 201) {
    status('');
    showCodes(res.data.recovery_codes, res.data.csrf);
    return;
  }
  status('');
  if (res.data.code === 'already_set_up') { location.replace('login.html'); return; }
  if (res.data.code === 'bad_token') {
    setupToken = '';
    $('#token-field').hidden = false; // let them paste the code from the file instead
  }
  showErrors(answerProblem(res, `Creating the account didn't work (error ${res.status}). Try again.`));
}

/* ---------- recovery codes, shown once ---------- */
function codesText(codes) {
  return ['JobHuntPA recovery codes', `Saved ${new Date().toLocaleString()}`,
    'Each code works once. Keep them private.', '', ...codes.map((c, i) => `${i + 1}. ${c}`), ''].join('\n');
}

function showCodes(codes, csrf) {
  clearErrors();
  status('');
  stashRecoveryCodes(codes); // for a reload before they are confirmed
  document.title = 'Your recovery codes · JobHuntPA';
  $('#page-title').textContent = 'Your recovery codes';
  $('#setup-step').hidden = true;
  const list = $('#codes-list');
  list.innerHTML = codes.map((c) => `<li><code>${esc(c)}</code></li>`).join('');
  $('#codes-step').hidden = false;
  $('#codes-title').focus();
  const say = (text) => setText($('#codes-status'), text);
  $('#codes-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(codes.join('\n')); say('Copied all 8 codes.'); }
    catch { say("Your browser didn't allow copying. Select the codes and copy them, or download them instead."); }
  });
  $('#codes-download').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([codesText(codes)], { type: 'text/plain' }));
    const a = Object.assign(document.createElement('a'), { href: url, download: 'jobhuntpa-recovery-codes.txt' });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    say('Downloaded jobhuntpa-recovery-codes.txt.');
  });
  $('#codes-print').addEventListener('click', () => window.print());
  $('#codes-continue').addEventListener('click', async () => {
    if (!$('#codes-saved').checked) {
      showErrors([{ field: 'codes-saved', message: 'Save your recovery codes, then tick "I\'ve saved my recovery codes".' }]);
      return;
    }
    const btn = $('#codes-continue');
    setBusy(btn, true);
    const res = await authCall('/api/auth/recovery-codes/ack', { method: 'POST', body: {}, csrf });
    setBusy(btn, false);
    if (res.status === 200 || res.status === 401) { forgetRecoveryCodes(); location.replace('./'); return; }
    showErrors(answerProblem(res, `That didn't work (error ${res.status}). Try again.`));
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initPasswordToggles();
  if (PAGE === 'login') initLogin();
  else if (PAGE === 'first-run') initFirstRun();
  else if (PAGE === 'recover') initRecover();
});
