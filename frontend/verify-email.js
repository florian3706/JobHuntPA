/* JobHuntPA email confirmation (verify-email.html): the link in the confirmation email opens it as
   verify-email.html#token=…. It works signed in or out, and in any browser: the token is all it needs.
   The token is read from the #fragment and taken out of the address bar at once. Builds on auth.js (loaded first).

   Opening the link only asks what it is for (verify-email/info) and shows the address; the confirming is sent when the
   person presses the button. A page that confirmed on load would let a mail scanner that runs scripts, or one stray
   click, put one account on another person's address (a change of address names the address in the email, but anyone
   who can ask for it can aim it at any mailbox). */
'use strict';

let verifyToken = '';

function showVerifyProblem(res) {
  let message = typeof res.data.detail === 'string' ? res.data.detail : `That didn't work (error ${res.status}). Try again.`;
  if (res.data.code === 'link_invalid' || res.status === 410) message = 'This link has expired or was already used. Sign in and open Settings to get a new one.';
  if (res.data.code === 'email_taken') message = 'Another account uses that address now, so it can\'t be changed to it. Sign in and choose another address in Settings.';
  showErrors([{ field: null, message }]);
}

function askToConfirm({ kind, email }) {
  const change = kind === 'change';
  document.title = (change ? 'Use this email address?' : 'Confirm your email address') + ' · JobHuntPA';
  $('#page-title').textContent = change ? 'Use this email address?' : 'Confirm your email address';
  $('#verify-question').textContent = change
    ? 'Use this address for a JobHuntPA account, instead of the address that account has now?'
    : 'Is this your email address for JobHuntPA?';
  $('#verify-address').innerHTML = `<strong>${esc(email)}</strong>`;
  $('#verify-note').textContent = change
    ? "Someone signed in to a JobHuntPA account asked to use this address. Press the button only if that was you. If it wasn't, close this page: nothing changes."
    : 'Press the button to confirm it.';
  $('#verify-confirm').textContent = change ? 'Yes, use this address' : 'Yes, confirm this address';
  $('#verify-ask').hidden = false;
  $('#verify-confirm').addEventListener('click', confirmAddress);
  $('#page-title').focus();
  announce($('#page-title').textContent);
}

async function confirmAddress() {
  const btn = $('#verify-confirm');
  clearErrors();
  setBusy(btn, true);
  status('Confirming your address…');
  const res = await authCall('/api/auth/verify-email', { method: 'POST', body: { token: verifyToken } });
  status('');
  if (res.status !== 200) { setBusy(btn, false); $('#verify-ask').hidden = true; showVerifyProblem(res); return; }
  const changed = Boolean(res.data.changed);
  $('#verify-ask').hidden = true;
  document.title = (changed ? 'Email address changed' : 'Email address confirmed') + ' · JobHuntPA';
  $('#page-title').textContent = changed ? 'Email address changed' : 'Email address confirmed';
  $('#verify-text').textContent = changed
    ? 'Your email address is now the new one. Use it to sign in from now on.'
    : 'Thank you. Your email address is confirmed.';
  $('#verify-result').hidden = false;
  $('#page-title').focus();
  announce($('#page-title').textContent);
}

async function initVerify() {
  const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  history.replaceState(null, '', location.pathname + location.search);
  if (!m) {
    status('');
    showErrors([{ field: null, message: "This link isn't complete. Open the link in your email again." }]);
    return;
  }
  verifyToken = m[1];
  const res = await authCall('/api/auth/verify-email/info', { method: 'POST', body: { token: verifyToken } });
  status('');
  if (res.status === 200) { askToConfirm(res.data); return; }
  showVerifyProblem(res);
}

document.addEventListener('DOMContentLoaded', () => {
  if (PAGE === 'verify-email') initVerify();
});
