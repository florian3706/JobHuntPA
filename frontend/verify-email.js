/* JobHuntPA email confirmation (verify-email.html): the link in the confirmation email opens it as
   verify-email.html#token=…. It works signed in or out, and in any browser: the token is all it needs.
   The token is read from the #fragment and taken out of the address bar at once. Builds on auth.js (loaded first). */
'use strict';

async function initVerify() {
  const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  history.replaceState(null, '', location.pathname + location.search);
  if (!m) {
    status('');
    showErrors([{ field: null, message: "This link isn't complete. Open the link in your email again." }]);
    return;
  }
  const res = await authCall('/api/auth/verify-email', { method: 'POST', body: { token: m[1] } });
  status('');
  if (res.status === 200) {
    const changed = Boolean(res.data.changed);
    document.title = (changed ? 'Email address changed' : 'Email address confirmed') + ' · JobHuntPA';
    $('#page-title').textContent = changed ? 'Email address changed' : 'Email address confirmed';
    $('#verify-text').textContent = changed
      ? 'Your email address is now the new one. Use it to sign in from now on.'
      : 'Thank you. Your email address is confirmed.';
    $('#verify-result').hidden = false;
    $('#page-title').focus();
    announce($('#page-title').textContent);
    return;
  }
  let message = typeof res.data.detail === 'string' ? res.data.detail : `That didn't work (error ${res.status}). Try again.`;
  if (res.data.code === 'link_invalid' || res.status === 410) message = 'This link has expired or was already used. Sign in and open Settings to get a new one.';
  if (res.data.code === 'email_taken') message = 'Another account uses that address now, so it can\'t be changed to it. Sign in and choose another address in Settings.';
  showErrors([{ field: null, message }]);
}

document.addEventListener('DOMContentLoaded', () => {
  if (PAGE === 'verify-email') initVerify();
});
