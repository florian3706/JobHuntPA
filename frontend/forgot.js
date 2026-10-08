/* "Email me a link" on login.html and recover.html (accounts design §6.5): the forgot-password path for people with
   no recovery code. It shows only when the server has email set up (/api/auth/state: email_enabled). The address is the
   one already typed above (WCAG 3.3.7: nothing is asked twice), and the answer is the same whether or not an account
   has it. Builds on auth.js (loaded first). */
'use strict';

async function initForgot() {
  const item = document.getElementById('forgot-item');
  if (!item) return;
  const state = await authCall('/api/auth/state');
  if (state.status !== 200 || !state.data.email_enabled) return;
  item.hidden = false;
  const btn = $('#forgot-btn');
  const out = $('#forgot-status');
  btn.addEventListener('click', async () => {
    const email = ($('#email').value || '').trim() || (typeof loginEmail === 'string' ? loginEmail : '');
    if (!email) {
      showErrors([{ field: 'email', message: 'Enter your email address above, then choose "Email me a link".' }]);
      return;
    }
    clearErrors();
    setBusy(btn, true);
    setText(out, 'Sending…');
    const res = await authCall('/api/auth/forgot', { method: 'POST', body: { email } });
    setBusy(btn, false);
    setText(out, '');
    if (res.status === 202) {
      const message = typeof res.data.message === 'string' ? res.data.message : "If an account has that address, we've sent it a link.";
      setText(out, message);
      announce(message);
      return;
    }
    if (res.data.code === 'email_not_set_up') item.hidden = true;
    showErrors(answerProblem(res, `That didn't work (error ${res.status}). Try again.`).map((x) => (res.data.field === 'email' ? { ...x, field: 'email' } : { ...x, field: null })));
  });
}

document.addEventListener('DOMContentLoaded', initForgot);
