/* JobHuntPA: the "Your email address" card on the Settings tab (accounts design §6.9 and §9, #account-address-slot).
   It shows the address, whether it is confirmed, and offers "Send the confirmation again" and "Change email address".
   Both need email to be set up on the server (an admin does that); while it isn't, the card says so and offers nothing.

   - Changing the address needs a recent sign-in, so it goes through withRecentAuth() (common.js). The link goes to the
     NEW address and nothing changes until it is opened, in any browser.
   - WCAG 2.2: labelled field, the problem written under it (setFieldError), results said in a status region and
     through announce(); one column from 320 px. */
(function () {
  'use strict';

  const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) });
  const slot = () => document.getElementById('account-address-slot');
  let info = null;
  let changing = false;

  const say = (text) => { setText(document.getElementById('acct-addr-status'), text); if (text) announce(text); };

  function render() {
    const card = slot();
    const addr = info.email
      ? `<p>Your address is <strong>${esc(info.email)}</strong>. ${info.verified ? '<span class="ok-text">Confirmed</span>' : '<span class="muted">Not confirmed yet</span>'}</p>`
      : '<p class="muted">No email address is saved for this account.</p>';
    const off = info.enabled ? '' : '<p class="hint">Email isn\'t set up on this JobHuntPA yet, so it can\'t send you links. An admin sets it up in the admin panel.</p>';
    const pending = info.pending_change
      ? '<p class="hint">A link was sent to your new address. Your address changes when you open it.</p>' : '';
    const buttons = info.enabled
      ? `<div class="toolbar-row">
          ${info.email && !info.verified ? '<button id="acct-addr-resend" class="btn" type="button">Send the confirmation again</button>' : ''}
          <button id="acct-addr-change" class="btn" type="button" aria-expanded="${changing}" aria-controls="acct-addr-form">Change email address</button>
        </div>
        <form id="acct-addr-form" class="account-form" novalidate${changing ? '' : ' hidden'}>
          <div class="auth-field">
            <label for="acct-addr-new">New email address</label>
            <p id="acct-addr-new-hint" class="hint">We send a link to the new address. Nothing changes until you open it. You may be asked for your password.</p>
            <input id="acct-addr-new" type="email" autocomplete="email" inputmode="email" autocapitalize="none" spellcheck="false" aria-describedby="acct-addr-new-hint" required />
          </div>
          <div class="acct-error error" role="alert" hidden></div>
          <div class="toolbar-row">
            <button id="acct-addr-send" class="btn btn-primary" type="submit">Send a link to the new address</button>
            <button id="acct-addr-cancel" class="btn btn-ghost" type="button">Cancel</button>
          </div>
        </form>` : '';
    card.innerHTML = `<div id="acct-addr-card" class="card form-card account-card">
      <div class="card-head"><h3 id="acct-addr-heading" tabindex="-1">Your email address <span class="scope-tag">This account</span></h3></div>
      ${addr}${off}${pending}${buttons}
      <p id="acct-addr-status" class="account-status" role="status"></p>
    </div>`;
    if (!info.enabled) return;
    document.getElementById('acct-addr-change').addEventListener('click', () => toggleChange(!changing));
    document.getElementById('acct-addr-cancel').addEventListener('click', () => toggleChange(false, true));
    document.getElementById('acct-addr-form').addEventListener('submit', changeAddress);
    const resend = document.getElementById('acct-addr-resend');
    if (resend) resend.addEventListener('click', resendLink);
  }

  function toggleChange(open, back = false) {
    changing = open;
    document.getElementById('acct-addr-form').hidden = !open;
    document.getElementById('acct-addr-change').setAttribute('aria-expanded', String(open));
    if (open) document.getElementById('acct-addr-new').focus();
    else if (back) document.getElementById('acct-addr-change').focus();
  }

  async function load() {
    try { info = await api('/api/auth/email'); } catch (e) { return false; }
    return true;
  }

  async function resendLink(ev) {
    const btn = ev.currentTarget;
    setBusy(btn, true);
    say('Sending…');
    try {
      const r = await post('/api/auth/verify-email/resend');
      if (r.already_verified) {  // confirmed elsewhere: the card is rebuilt without this button, so say it after that
        await load();
        render();
        say('Your address is already confirmed.');
        document.getElementById('acct-addr-heading').focus();
        return;
      }
      say(r.sent ? 'Sent. Open the link in the email to confirm your address.' : 'The link could not be sent just now. Try again in a few minutes.');
    } catch (e) {
      say(e.message);
    }
    setBusy(btn, false);
  }

  async function changeAddress(ev) {
    ev.preventDefault();
    const input = document.getElementById('acct-addr-new');
    const box = ev.currentTarget.querySelector('.acct-error');
    const btn = document.getElementById('acct-addr-send');
    box.hidden = true;
    setFieldError(input, '');
    const value = input.value.trim();
    if (!value) { setFieldError(input, 'Enter the new email address.'); return; }
    setBusy(btn, true);
    say('Sending…');
    try {
      await withRecentAuth(() => post('/api/auth/email/change', { new_email: value }));
      await load();  // (a failed reload keeps the last card, which render() rebuilds without the busy button)
      changing = false;
      render();
      say('Sent. Open the link we emailed to the new address to finish. Your address stays the same until you do.');
      document.getElementById('acct-addr-heading').focus();
      return;
    } catch (e) {
      say('');
      if (e.field === 'new_email') setFieldError(input, e.message);
      else showError(box, e.code === 'reauth_required' ? 'Your password was not confirmed, so nothing was changed.' : e.message);
    }
    setBusy(btn, false);
  }

  async function init() {
    if (!slot()) return;
    try { await loadSession(); } catch { return; }
    if (await load()) render();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
