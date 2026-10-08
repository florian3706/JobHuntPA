/* JobHuntPA reset page (reset-password.html, accounts design §6.5): the link in a password-reset email opens it as
   reset-password.html#token=…. The token is in the #fragment, so it never reaches a server log or a Referer header;
   it is read once and taken out of the address bar.

   - With background access the data is kept. Without it the data is locked with the old password: the page says so,
     points to the recovery-code page, and asks for "I understand my saved data will be deleted" before it sends
     anything. A wipe ends with the new recovery codes, in the same step as first run (auth.js showCodes).
   - The link is checked first (reset/info; this doesn't use it up), so the page shows only what applies.
   - It builds on auth.js (loaded first): authCall, showErrors, the error summary and the password toggle.
     WCAG notes are in auth.js. */
'use strict';

let resetToken = '';
let mfaRequired = false;

function readResetToken() {
  const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (!m) return;
  resetToken = m[1];
  history.replaceState(null, '', location.pathname + location.search); // out of the address bar and history
}

function showInvalid(text) {
  clearErrors();
  status('');
  $('#setup-step').hidden = true;
  $('#invalid-text').textContent = text;
  $('#invalid-step').hidden = false;
  document.title = "This link doesn't work · JobHuntPA";
  $('#page-title').textContent = "This link doesn't work";
  $('#page-title').focus();
}

async function initReset() {
  readResetToken();
  if (!resetToken) { showInvalid("This link isn't complete. Open the link in your email again, or ask for a new one."); return; }
  const info = await authCall('/api/auth/reset/info', { method: 'POST', body: { token: resetToken } });
  if (info.status === 200) {
    showResetForm(info.data);
  } else if (info.status === 410 || info.data.code === 'link_invalid') {
    showInvalid('This link has expired or was already used. Ask for a new one.');
  } else {
    status('');
    showErrors(answerProblem(info, `This link couldn't be checked (error ${info.status}). Reload the page to try again.`));
    return;
  }
}

function showResetForm({ keeps_data: keeps, mfa_required: mfa }) {
  mfaRequired = Boolean(mfa);
  status('');
  $('#keeps-note').hidden = !keeps;
  $('#wipe-warning').hidden = keeps;
  $('#wipe-field').hidden = keeps;
  $('#code-field').hidden = !mfaRequired;
  $('#setup-step').hidden = false;
  $('#reset-form').addEventListener('submit', submitReset);
  (keeps ? $('#password') : $('#accept-wipe')).focus();
}

async function submitReset(e) {
  e.preventDefault();
  const password = $('#password').value;
  const code = $('#code').value.trim();
  const wipe = !$('#wipe-field').hidden;
  const problems = [];
  if (wipe && !$('#accept-wipe').checked) {
    problems.push({ field: 'accept-wipe', message: 'Tick "I understand my saved data will be deleted" to go on, or use a recovery code instead.' });
  }
  if (!password) problems.push({ field: 'password', message: 'Choose a new password.' });
  else if (password.length < 10) problems.push({ field: 'password', message: 'Use at least 10 characters. A few random words work well.' });
  if (mfaRequired && !code) problems.push({ field: 'code', message: "Enter the code from your authenticator app, or one of your recovery codes." });
  if (problems.length) { showErrors(problems); return; }
  const body = { token: resetToken, new_password: password, remember: $('#remember').checked };
  if (wipe) body.accept_wipe = true;
  if (mfaRequired) body[/^\d{6}$/.test(code.replace(/[\s-]/g, '')) ? 'code' : 'recovery_code'] = code;
  const btn = $('#reset-submit');
  setBusy(btn, true);
  status('Setting your new password…');
  hidePasswords();
  const res = await authCall('/api/auth/reset', { method: 'POST', body });
  setBusy(btn, false);
  status('');
  if (res.status === 200) { showResetDone(res.data); return; }
  const d = res.data;
  if (d.code === 'link_invalid') { showInvalid('This link has expired or was already used. Ask for a new one.'); return; }
  if (d.code === 'disabled') { showInvalid(typeof d.detail === 'string' ? d.detail : 'This account is turned off. Ask the person who runs this JobHuntPA.'); return; }
  const errors = answerProblem(res, `That didn't work (error ${res.status}). Try again.`);
  if (d.code === 'second_factor_required') {
    $('#code-field').hidden = false;
    mfaRequired = true;
    errors[0].field = 'code';
  }
  if (d.field === 'accept_wipe') errors[0].field = 'accept-wipe';
  if (d.field === 'recovery_code' || d.field === 'code') errors[0].field = 'code';
  showErrors(errors);
}

function showResetDone(data) {
  clearErrors();
  $('#setup-step').hidden = true;
  announce('Password changed. You are signed in.');
  if (Array.isArray(data.recovery_codes) && data.recovery_codes.length) {
    $('#admin-key-lost').hidden = !data.admin_key_lost;
    showCodes(data.recovery_codes, data.csrf); // the same step as first run: Copy, Download, Print, "I've saved them"
    return;
  }
  document.title = 'Password changed · JobHuntPA';
  $('#page-title').textContent = 'Password changed';
  $('#done-text').textContent = "You're signed in, your data is kept and other browsers are signed out.";
  $('#done-step').hidden = false;
  $('#page-title').focus();
}

document.addEventListener('DOMContentLoaded', () => {
  if (PAGE === 'reset-password') initReset();
});
