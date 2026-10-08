/* JobHuntPA: the "Job digest emails" card on the Settings tab (accounts design §17, #account-email-slot).
   One switch and "every n days" (1 to 7), saved at once through PUT /api/account/digest. A digest can be switched on
   even while it can't be sent: the card then lists why, in the server's words, with a link to the card that fixes it.
   WCAG 2.2: native checkbox (role=switch), labelled select, results in one status region (not also announce(): a second live region reads each message twice). */
(function () {
  'use strict';

  const slot = () => document.getElementById('account-email-slot');
  let info = null;
  let saving = false;

  const say = (text) => setText(document.getElementById('digest-status'), text);
  const every = (n) => (n === 1 ? 'every day' : `every ${n} days`);

  // Where each problem is fixed: a card on this page, or another page. "Ask the admin" has no link.
  const FIXES = {
    email_unconfirmed: { card: 'acct-addr-heading', label: 'Go to Your email address' },
    background_off: { card: 'background-title', label: 'Go to Background access' },
    membership_required: { href: 'billing.html', label: 'Open Membership' },
  };

  function fix(code) {
    const f = FIXES[code];
    if (!f) return '';
    return f.href ? ` <a href="${f.href}">${esc(f.label)}</a>`
      : ` <a href="#${f.card}" data-digest-go="${f.card}">${esc(f.label)}</a>`;
  }

  function render() {
    const days = [1, 2, 3, 4, 5, 6, 7].map((n) =>
      `<option value="${n}"${n === info.every_days ? ' selected' : ''}>${n === 1 ? 'Every day' : `Every ${n} days`}</option>`).join('');
    const problems = info.problems.length
      ? `<div id="digest-problems" class="hint"><p>${info.enabled ? 'No digest can be sent yet:' : 'Before a digest can be sent:'}</p>
          <ul>${info.problems.map((p) => `<li>${esc(p.text)}${fix(p.code)}</li>`).join('')}</ul></div>` : '';
    const when = `<p id="digest-when" class="hint">A digest goes out ${info.enabled ? every(info.every_days) : 'on the days you choose'}, around ${esc(info.send_time)} (${esc(info.time_zone)}),
      and only when there are new jobs. Every email has an unsubscribe link.</p>`;
    slot().innerHTML = `<div id="digest-card" class="card form-card account-card">
      <div class="card-head"><h3 id="digest-heading" tabindex="-1">Job digest emails <span class="scope-tag">This account</span></h3></div>
      <p id="digest-explain">One email with the new jobs your scheduled searches found, best fit first.</p>
      <label class="check"><input type="checkbox" id="digest-switch" role="switch" aria-describedby="digest-when"${info.enabled ? ' checked' : ''} /> Email me a digest of new jobs</label>
      <div class="auth-field">
        <label for="digest-days">How often</label>
        <select id="digest-days">${days}</select>
      </div>
      ${when}${problems}
      <p id="digest-status" class="account-status" role="status"></p>
    </div>`;
    document.getElementById('digest-switch').addEventListener('change', (ev) => save({ enabled: ev.target.checked }, ev.target));
    document.getElementById('digest-days').addEventListener('change', (ev) => save({ every_days: Number(ev.target.value) }, ev.target));
    slot().querySelectorAll('[data-digest-go]').forEach((a) => a.addEventListener('click', (ev) => {
      const target = document.getElementById(a.dataset.digestGo);
      if (!target) return;  // the card isn't on this page: follow the plain link
      ev.preventDefault();
      target.scrollIntoView({ block: 'center' });
      target.focus({ preventScroll: true });
    }));
  }

  async function save(change, control) {
    if (saving) return;
    saving = true;
    const id = control.id;
    say('Saving…');
    try {
      info = await api('/api/account/digest', { method: 'PUT', body: JSON.stringify(change) });
      render();
      document.getElementById(id).focus();
      say(change.every_days ? `Saved. Digests go out ${every(info.every_days)}.`
        : info.enabled ? 'Saved. Job digest emails are on.' : 'Saved. Job digest emails are off.');
    } catch (e) {
      render();  // puts the control back as the server has it
      document.getElementById(id).focus();
      say(`That didn't save: ${e.message}`);
    }
    saving = false;
  }

  async function load() {
    try { info = await api('/api/account/digest'); } catch { return false; }
    return true;
  }

  async function init() {
    if (!slot()) return;
    try { await loadSession(); } catch { return; }
    if (await load()) render();
    // Fixing a problem (confirming, background access) happens on this page: look again when the person comes back.
    document.addEventListener('visibilitychange', async () => {
      if (document.visibilityState === 'visible' && !saving && await load()) {
        const a = document.activeElement;
        if (!slot().contains(a)) render();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
