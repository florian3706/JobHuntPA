/* JobHuntPA: the "Browser extension" card on the Settings tab (#account-extension-slot; backend/extension.py).

   The Chrome and Firefox extensions add the page in the current tab as a job, or as a company source. To connect one,
   the person names it and gets the server address and an extension key, shown once, to paste into the extension's
   options. Connecting needs the password entered in the last 15 minutes (withRecentAuth); disconnecting doesn't.
   The card lists the connected extensions with Disconnect buttons, and the job sites whose page layout the AI worked
   out for the extension (backend/mappings.py) with Forget buttons.

   WCAG 2.2: the name field has a label and its problem is written under it; results go to a status region and are
   announced; the key and the address are read-only fields with their own labels and Copy buttons (a failed copy
   selects the text instead); each Disconnect button names its extension; one column from 320 px. */
(function () {
  'use strict';

  const CHROME_URL = 'https://github.com/florian3706/JobHuntPA-chrome-extension';
  const FIREFOX_URL = 'https://github.com/florian3706/JobHuntPA-firefox-extension';
  const NO_RECENT = 'Your password was not confirmed, so no extension was connected.';
  const keys = { list: [], loaded: false };
  const sites = { list: [], loaded: false };

  const say = (text) => { setText(document.getElementById('ext-status'), text); if (text) announce(text); };
  const day = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
  };

  function mount(slot) {
    slot.innerHTML = `<div id="ext-card" class="p-card u-stack account-card">
      <div class="p-card__head"><h3 id="ext-heading" tabindex="-1">Browser extension <span class="p-scope-tag">This account</span></h3></div>
      <p>Add the job ad you are looking at, the jobs on a SEEK results page, or a company's careers page, to
        JobHuntPA in one click from Chrome or Firefox. The extension reads the page in your own browser, so ads
        behind a sign-in work too.</p>
      <p>Get the extension for <a href="${CHROME_URL}" target="_blank" rel="noopener">Chrome${NEW_TAB}</a> or
        <a href="${FIREFOX_URL}" target="_blank" rel="noopener">Firefox${NEW_TAB}</a>, then connect it here.</p>
      <div id="ext-list" class="account-body"><p class="muted">Loading…</p></div>
      <h4 id="ext-sites-heading" tabindex="-1">Job sites the AI worked out</h4>
      <p class="hint">When the extension can't find a job on a page, it can ask the AI (through JobHuntPA) which parts of
        the page hold the job, and remember that for the site. Forget one to have it worked out again.</p>
      <div id="ext-sites" class="account-body"><p class="muted">Loading…</p></div>
      <form id="ext-form" class="account-form" novalidate>
        <div class="auth-field">
          <label for="ext-label">Name for this connection</label>
          <p id="ext-label-hint" class="hint">So you can tell your extensions apart, for example "Firefox on my laptop".</p>
          <input id="ext-label" type="text" maxlength="80" autocomplete="off" aria-describedby="ext-label-hint" />
        </div>
        <div class="error ext-error" hidden></div>
        <div><button id="ext-connect" class="p-button p-button--positive" type="submit">Connect a browser extension</button></div>
      </form>
      <div id="ext-result" class="callout ext-result" hidden>
        <h4 id="ext-result-title" tabindex="-1">Paste these into the extension's options</h4>
        <p>Open the extension's options, paste the server address and the key, then pick a workspace.
          The key is shown only now. Keep it secret: it opens your data.</p>
        <label for="ext-server" class="field-label">Server address</label>
        <div class="key-row">
          <input id="ext-server" class="key-input" type="text" readonly spellcheck="false" />
          <button id="ext-copy-server" class="p-button is-dense" type="button">Copy address</button>
        </div>
        <label for="ext-key" class="field-label">Extension key</label>
        <div class="key-row">
          <input id="ext-key" class="key-input" type="text" readonly spellcheck="false" autocomplete="off" />
          <button id="ext-copy-key" class="p-button is-dense" type="button">Copy key</button>
        </div>
        <div><button id="ext-done" class="p-button" type="button">Done</button></div>
      </div>
      <p id="ext-status" class="account-status" role="status"></p>
    </div>`;
    $('#ext-form').addEventListener('submit', connect);
    $('#ext-copy-server').addEventListener('click', () => copy('#ext-server', 'Copied the server address.'));
    $('#ext-copy-key').addEventListener('click', () => copy('#ext-key', 'Copied the key.'));
    $('#ext-done').addEventListener('click', () => {
      $('#ext-key').value = '';
      $('#ext-result').hidden = true;
      say('');
      $('#ext-heading').focus();
    });
    $('#ext-list').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-ext-revoke]');
      if (btn) revoke(btn);
    });
    $('#ext-sites').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-ext-forget]');
      if (btn) forget(btn);
    });
  }

  function renderSites() {
    const box = $('#ext-sites');
    if (!sites.loaded) return;
    if (!sites.list.length) {
      box.innerHTML = '<p>None yet.</p>';
      return;
    }
    box.innerHTML = `<ul class="ext-keys" aria-labelledby="ext-sites-heading">${sites.list.map((m) => `<li class="ext-key">
      <div><strong>${esc(m.site)}</strong>
        <div class="muted">Saved ${esc(day(m.updated_at))}</div></div>
      <button class="p-button is-dense" type="button" data-ext-forget="${esc(m.site)}">Forget<span class="visually-hidden"> ${esc(m.site)}</span></button>
    </li>`).join('')}</ul>`;
  }

  async function loadSites() {
    try {
      sites.list = (await api('/api/extension/mappings')).mappings || [];
      sites.loaded = true;
      renderSites();
    } catch (e) {
      $('#ext-sites').innerHTML = `<p class="error" role="alert">Couldn't load the job sites: ${esc(e.message)}</p>`;
    }
  }

  async function forget(btn) {
    const site = btn.dataset.extForget;
    setBusy(btn, true);
    try {
      await api(`/api/extension/mappings/${encodeURIComponent(site)}`, { method: 'DELETE' });
      say(`Forgot the layout of ${site}.`);
    } catch (err) {
      say(`Couldn't forget it: ${err.message}`);
    }
    await loadSites();
    $('#ext-sites-heading').focus();
  }

  async function copy(sel, done) {
    const field = $(sel);
    try {
      await navigator.clipboard.writeText(field.value);
      say(done);
    } catch {
      field.focus();
      field.select();
      say("Your browser didn't allow copying. The text is selected: copy it yourself.");
    }
  }

  function renderList() {
    const box = $('#ext-list');
    if (!keys.loaded) return;
    if (!keys.list.length) {
      box.innerHTML = '<p>No browser extension is connected.</p>';
      return;
    }
    box.innerHTML = `<ul class="ext-keys" aria-label="Connected browser extensions">${keys.list.map((k) => `<li class="ext-key">
      <div><strong>${esc(k.label || 'Browser extension')}</strong>
        <div class="muted">Connected ${esc(day(k.created_at))} · ${k.last_used_at ? `last used ${esc(day(k.last_used_at))}` : 'not used yet'}
          · stops working ${esc(day(k.expires_at))} unless used</div></div>
      <button class="p-button is-dense" type="button" data-ext-revoke="${esc(k.id)}">Disconnect<span class="visually-hidden"> ${esc(k.label || 'this extension')}</span></button>
    </li>`).join('')}</ul>
    <p class="hint">An extension stops working after 60 days without use, a year after you connect it, or when you sign out everywhere else or change your password and sign out the other devices.</p>`;
  }

  async function load() {
    try {
      keys.list = await api('/api/extension/keys');
      keys.loaded = true;
      renderList();
    } catch (e) {
      $('#ext-list').innerHTML = `<p class="error" role="alert">Couldn't load the connected extensions: ${esc(e.message)}</p>`;
    }
  }

  async function connect(e) {
    e.preventDefault();
    const input = $('#ext-label');
    const btn = $('#ext-connect');
    const box = $('#ext-form .ext-error');
    box.hidden = true;
    setFieldError(input, '', { focus: false });
    const label = input.value.trim();
    if (!label) { setFieldError(input, 'Give it a name, such as "Firefox on my laptop".'); return; }
    setBusy(btn, true);
    say('');
    try {
      const made = await withRecentAuth(() => api('/api/extension/keys', { method: 'POST', body: JSON.stringify({ label }) }));
      input.value = '';
      $('#ext-server').value = window.location.origin;
      $('#ext-key').value = made.key;
      $('#ext-result').hidden = false;
      $('#ext-result-title').focus();
      say(`Connected "${made.label}". Copy the server address and the key into the extension's options now.`);
      await load();
    } catch (err) {
      if (err.field === 'label') setFieldError(input, err.message);
      else showError(box, err.code === 'reauth_required' ? NO_RECENT : err.message);
    } finally {
      setBusy(btn, false);
    }
  }

  async function revoke(btn) {
    const id = btn.dataset.extRevoke;
    const item = keys.list.find((k) => k.id === id);
    setBusy(btn, true);
    try {
      await api(`/api/extension/keys/${encodeURIComponent(id)}`, { method: 'DELETE' });
      say(`Disconnected "${item?.label || 'the extension'}". Its key no longer works.`);
    } catch (err) {
      say(`Couldn't disconnect it: ${err.message}`);
    }
    await load();
    $('#ext-heading').focus();
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const slot = document.getElementById('account-extension-slot');
    if (!slot) return;
    mount(slot);
    try { await loadSession(); } catch { return; } // signed out: goes to the sign-in page
    load();
    loadSites();
  });
}());
