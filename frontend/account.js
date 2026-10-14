/* JobHuntPA: the account cards on the Settings tab (accounts design, sections 3 and 6): change password, recovery
   codes, sign out everywhere else, and delete account. They apply to the signed-in account, in
   every workspace.

   - Everything that needs a recent sign-in goes through withRecentAuth() (common.js), which asks for the password in
     the "Confirm your password" dialog. Where a form already has the password in it, that is used first, so nobody
     types it twice (WCAG 3.3.7).
   - WCAG 2.2: every field has a label; a problem is written under its field (aria-describedby), the field is marked
     invalid and gets focus, and the message is announced; results go to a status region; one column from 320 px.
   - The new recovery codes are shown by first-run.html, the same Copy / Download / Print / "I've saved them" step as
     the first set, so that code isn't copied here. */
(function () {
  'use strict';

  const STATE = { sec: null };
  const NO_RECENT = 'Your password was not confirmed, so nothing was changed.';

  const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) });
  const email = () => SESSION.me?.user?.email || '';
  /* The result is also announced through the page's permanent live region: the card's own status region is hidden while
     empty, and text that arrives together with its display is often not read. */
  const say = (id, text) => { setText(document.getElementById(id), text); if (text) announce(text); };

  /* ---------- the skeleton, drawn once; each card's body is redrawn from the server's state ---------- */
  function card(id, title, hint) {
    return `<div id="${id}-card" class="card form-card account-card">
      <div class="card-head"><h3 id="${id}-heading" tabindex="-1">${title} <span class="scope-tag">This account</span></h3></div>
      ${hint ? `<p class="hint">${hint}</p>` : ''}
      <div id="${id}-body" class="account-body"></div>
      <p id="${id}-status" class="account-status" role="status"></p>
    </div>`;
  }

  function pwField(id, label, autocomplete, hintId, hint) {
    return `<div class="auth-field">
      <label for="${id}">${label}</label>
      ${hint ? `<p id="${hintId}" class="hint">${hint}</p>` : ''}
      <div class="pw-row">
        <input id="${id}" type="password" autocomplete="${autocomplete}" spellcheck="false"${hint ? ` aria-describedby="${hintId}"` : ''} required />
        <button class="btn pw-toggle" type="button" aria-controls="${id}" aria-pressed="false">Show password</button>
      </div>
    </div>`;
  }

  /* A hidden username field, so a password manager files the password under this account. */
  const usernameField = () => `<input class="visually-hidden" type="text" autocomplete="username" tabindex="-1" readonly
    aria-hidden="true" aria-label="Email address" value="${esc(email())}" />`;

  const errorBox = '<div class="error acct-error" hidden></div>';

  function mount() {
    $('#account-security-slot').innerHTML =
      card('acct-pw', 'Password', 'Changing it keeps all your data. It is never sent anywhere, and JobHuntPA can\'t show it to you.')
      + card('acct-rc', 'Recovery codes', 'If you forget your password, a recovery code sets a new one and keeps your data. It is the only way back in: keep them safe. Each code works once.')
      + card('acct-ses', 'Signed-in devices');
    $('#account-delete-slot').innerHTML =
      `<div id="acct-del-card" class="card form-card account-card account-danger">
        <div class="card-head"><h3 id="acct-del-heading" tabindex="-1">Delete account <span class="scope-tag">This account</span></h3></div>
        <p>This deletes your account and everything in it: your workspaces, jobs, documents and the files you uploaded, chats,
          cover letters, search settings, keys and sign-ins. <strong>It can't be undone.</strong> Job postings and company
          research that everyone shares stay, because they hold nothing of yours.</p>
        <form id="acct-del-form" class="account-form" novalidate>
          ${usernameField()}
          ${pwField('acct-del-password', 'Your password', 'current-password')}
          ${errorBox}
          <button id="acct-del-submit" class="btn btn-danger" type="submit">Delete my account</button>
        </form>
        <p id="acct-del-status" class="account-status" role="status"></p>
      </div>`;
    $('#acct-del-form').addEventListener('submit', deleteAccount);
    for (const slot of ['#account-security-slot', '#account-delete-slot']) {
      $(slot).addEventListener('click', (e) => {
        const btn = e.target.closest('.pw-toggle');
        if (!btn) return;
        const input = document.getElementById(btn.getAttribute('aria-controls'));
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        btn.setAttribute('aria-pressed', String(show));
      });
    }
  }

  const hidePasswords = (form) => form.querySelectorAll('.pw-toggle').forEach((btn) => {
    document.getElementById(btn.getAttribute('aria-controls')).type = 'password';
    btn.setAttribute('aria-pressed', 'false');
  });

  /* ---------- errors and busy buttons shared by the forms ---------- */
  function clearForm(form) {
    form.querySelectorAll('[aria-invalid="true"]').forEach((input) => setFieldError(input, '', { focus: false }));
    const box = form.querySelector('.acct-error');
    if (box) box.hidden = true;
  }

  /* fields: { api field name: input }. A problem about a named field goes under it; any other under the form. */
  function formError(form, e, fields) {
    const input = fields[e.field];
    if (input) setFieldError(input, e.message);
    else showError(form.querySelector('.acct-error'), e.code === 'reauth_required' ? NO_RECENT : e.message);
  }

  async function guarded(form, btn, fields, work) {
    clearForm(form);
    setBusy(btn, true);
    hidePasswords(form);
    try {
      await work();
    } catch (e) {
      formError(form, e, fields);
    } finally {
      setBusy(btn, false);
    }
  }

  /* The password is in the form already: use it for the recent sign-in, so the dialog isn't needed as well. */
  async function recentWith(password, fn) {
    if (!STATE.sec?.recent_auth) {
      await post('/api/auth/reauth', { password });
      if (STATE.sec) STATE.sec.recent_auth = true;
    }
    return withRecentAuth(fn);
  }

  async function loadSecurity() {
    try {
      STATE.sec = await api('/api/auth/security');
    } catch (e) {
      STATE.sec = null;
      for (const id of ['acct-pw', 'acct-rc', 'acct-ses']) {
        $(`#${id}-body`).innerHTML = '<p class="muted">Couldn\'t load these settings. Reload the page to try again.</p>';
      }
      return false;
    }
    return true;
  }

  async function refresh(focusId) {
    if (await loadSecurity()) renderAll();
    if (focusId) document.getElementById(focusId)?.focus();
  }

  function renderAll() {
    renderPassword();
    renderCodes();
    renderSessions();
  }

  /* ---------- password ---------- */
  function renderPassword() {
    $('#acct-pw-body').innerHTML = `<form id="acct-pw-form" class="account-form" novalidate>
      ${usernameField()}
      ${pwField('acct-pw-current', 'Current password', 'current-password')}
      ${pwField('acct-pw-new', 'New password', 'new-password', 'acct-pw-new-hint', 'At least 10 characters. A few random words work well; a password manager can make and remember one for you.')}
      <label class="check"><input id="acct-pw-others" type="checkbox" checked /> Sign out other devices</label>
      ${errorBox}
      <button id="acct-pw-submit" class="btn btn-primary" type="submit">Change password</button>
    </form>`;
    $('#acct-pw-form').addEventListener('submit', changePassword);
  }

  async function changePassword(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const current = $('#acct-pw-current');
    const next = $('#acct-pw-new');
    clearForm(form);
    if (!current.value) { setFieldError(current, 'Enter your current password.'); return; }
    if (!next.value) { setFieldError(next, 'Choose a new password.'); return; }
    if (next.value.length < 10) { setFieldError(next, 'Use at least 10 characters. A few random words work well.'); return; }
    say('acct-pw-status', '');
    await guarded(form, $('#acct-pw-submit'), { current_password: current, new_password: next }, async () => {
      const others = $('#acct-pw-others').checked;
      const res = await withRecentAuth(() => post('/api/auth/password', {
        current_password: current.value, new_password: next.value, sign_out_others: others,
      }));
      if (STATE.sec) STATE.sec.recent_auth = true;
      const n = res.signed_out_others || 0;
      form.reset();
      renderPassword();
      document.getElementById('acct-pw-heading')?.focus(); // the button that had focus was replaced
      say('acct-pw-status', `Password changed. Your data is untouched.${others ? ` ${n === 0 ? 'No other devices were signed in.' : `${n} other ${n === 1 ? 'device was' : 'devices were'} signed out.`}` : ''}`);
      loadSecurity().then((ok) => ok && renderSessions());
    });
  }

  /* ---------- recovery codes ---------- */
  function renderCodes() {
    const sec = STATE.sec;
    if (!sec) return;
    const left = sec.recovery_codes_left;
    const pending = sec.recovery_codes_pending;
    const old = sec.recovery_codes_old
      ? `<p class="callout" role="note"><strong>Make new codes.</strong> Your codes were made before the admin key got its own
        protection. Resetting your password with one of them keeps your data, but you would then need another admin to share
        the admin key with you again.</p>` : '';
    $('#acct-rc-body').innerHTML = `<p>${pending
      ? '<strong>You haven\'t saved your new codes yet.</strong> <a href="first-run.html">Show my recovery codes</a>'
      : `You have <strong>${left} of 8</strong> recovery codes left.`}</p>${old}
      <p class="hint">Making new codes replaces all eight at once, including any you haven't used. You then see them once, to copy, download or print. Because a code can set a new password, you enter your password.</p>
      <form id="acct-rc-form" class="account-form" novalidate>
        ${usernameField()}
        ${pwField('acct-rc-password', 'Your password', 'current-password')}
        ${errorBox}
        <div><button id="acct-rc-new" class="btn" type="submit">Make new recovery codes</button></div>
      </form>`;
    $('#acct-rc-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const password = $('#acct-rc-password');
      clearForm(form);
      if (!password.value) { setFieldError(password, 'Enter your password.'); return; }
      await guarded(form, $('#acct-rc-new'), { password }, async () => {
        const made = await recentWith(password.value, () => post('/api/auth/recovery-codes', { password: password.value }));
        stashRecoveryCodes(made.recovery_codes); // (first-run.html shows them; this tab has them without asking again)
        location.assign('first-run.html'); // shows the new codes once and asks to confirm they're saved
      });
    });
  }

  /* ---------- sign out everywhere else ---------- */
  function renderSessions() {
    const sec = STATE.sec;
    if (!sec) return;
    const n = sec.other_sessions;
    $('#acct-ses-body').innerHTML = `<p>${n === 0
      ? 'You\'re signed in on this device only.'
      : `You're signed in on this device and <strong>${n} other ${n === 1 ? 'device' : 'devices'}</strong>.`}</p>
      <p class="hint">Use this if you signed in on a shared or lost device. This device stays signed in; connected browser extensions are disconnected too.</p>
      ${errorBox}
      <div><button id="acct-ses-end" class="btn" type="button">Sign out everywhere else</button></div>`;
    $('#acct-ses-end').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const box = $('#acct-ses-body .acct-error');
      box.hidden = true;
      setBusy(btn, true);
      try {
        const res = await withRecentAuth(() => post('/api/auth/sessions/end-others'));
        const ended = res.ended || 0;
        await refresh('acct-ses-end');
        say('acct-ses-status', ended ? `Signed out ${ended} other ${ended === 1 ? 'device' : 'devices'}.` : 'No other devices were signed in.');
      } catch (err) {
        showError(box, err.code === 'reauth_required' ? NO_RECENT : err.message);
        setBusy(btn, false);
      }
    });
  }

  /* ---------- delete account ---------- */
  async function deleteAccount(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const password = $('#acct-del-password');
    clearForm(form);
    if (!password.value) { setFieldError(password, 'Enter your password to delete your account.'); return; }
    say('acct-del-status', '');
    await guarded(form, $('#acct-del-submit'), { password }, async () => {
      await recentWith(password.value, () => post('/api/account/delete', { password: password.value }));
      try { localStorage.removeItem('jobhunt.ws'); } catch { /* private mode */ }
      say('acct-del-status', 'Your account was deleted. Taking you to the sign-in page…');
      leavingPage = true;
      location.replace('login.html');
    });
  }

  /* ---------- start ---------- */
  async function init() {
    if (!$('#account-security-slot')) return;
    try { await loadSession(); } catch { return; }
    mount();
    await refresh();
    // The count of other devices and the codes left can change elsewhere: redraw those cards when the tab opens (not
    // the password form, which may be half filled in).
    const panel = $('#tab-settings');
    if (panel && 'MutationObserver' in window) {
      new MutationObserver(async () => {
        if (panel.hidden) return;
        if (!(await loadSecurity())) return;
        renderCodes();
        renderSessions();
      }).observe(panel, { attributes: true, attributeFilter: ['hidden'] });
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
