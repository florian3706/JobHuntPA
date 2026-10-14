/* JobHuntPA admin panel (accounts design §7): summary tiles and AI use, people, sign-up and invitations, the
   server's settings, and the audit log. The AI provider and commute-key cards are driven by server-settings.js and the
   dialogs by dialogs.js.

   WCAG 2.2 notes:
   - Tables have captions and <th scope>; the people table sorts with buttons in its headers (aria-sort) and, on
     narrow screens, with a "Sort by" menu, because the header row is then hidden and each row becomes a card.
   - Every action button names its target ("Disable alice@example.com").
   - Dialogs are native <dialog>: focus goes to the dialog's heading, Escape closes, and focus returns to the
     button that opened it (or to where that button is drawn again).
   - Results and errors are said in live regions (the status lines, announce() and toast()).
   Names and emails are other people's text: they only ever go into the page through esc() or textContent. */
'use strict';

function currentWorkspace() { return 0; } // admin routes never use a workspace

const admin = {
  users: null, // GET /api/admin/users
  sort: { key: 'person', dir: 'asc' },
  audit: { entries: [], next: null },
  invites: [],
};

/* ---------- small helpers ---------- */
const num = (n) => Number(n || 0).toLocaleString();
const pct = (rate) => (rate === null || rate === undefined ? '–' : `${Math.round(rate * 100)}%`);
const dateOf = (iso) => (iso ? new Date(iso) : null);
const fmtWhen = (iso) => (iso ? dateOf(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '–');
const fmtYmd = (ymd) => (ymd ? new Date(`${ymd}T00:00:00`).toLocaleDateString([], { dateStyle: 'medium' }) : 'Never');
const label = (u) => (u?.email || u?.name || (u ? `person ${u.id}` : 'someone'));
const sameId = (a, b) => Number(a) === Number(b);
const findUser = (id) => admin.users?.users.find((u) => sameId(u.id, id));
const myId = () => SESSION.me?.user?.id;

function say(el, text) { setText(el, text); }

/* A table that keeps its meaning as cards on narrow screens (styles.css: .stack-table, .admin-table). */
function tableHtml(caption, heads, rows, { id = '', empty = '' } = {}) {
  if (!rows.length) return empty ? `<p class="muted">${esc(empty)}</p>` : '';
  const th = (h) => {
    const text = typeof h === 'string' ? h : h.text;
    if (typeof h === 'string' || !h.sort) return `<th role="columnheader" scope="col">${esc(text)}</th>`;
    const on = admin.sort.key === h.sort;
    const dir = on ? (admin.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none';
    return `<th role="columnheader" scope="col" aria-sort="${dir}"><button class="sort-btn" type="button" data-sort="${esc(h.sort)}">${esc(text)}<span aria-hidden="true">${on ? (admin.sort.dir === 'asc' ? ' ▲' : ' ▼') : ''}</span></button></th>`;
  };
  const plain = heads.map((h) => (typeof h === 'string' ? h : h.text));
  const body = rows.map((cells) => `<tr role="row">${cells.map((html, i) => (i === 0
    ? `<th role="rowheader" scope="row" data-label="${esc(plain[0])}">${html}</th>`
    : `<td role="cell" data-label="${esc(plain[i])}">${html}</td>`)).join('')}</tr>`).join('');
  return `<table class="report stack-table admin-table" role="table"${id ? ` id="${id}"` : ''}><caption class="visually-hidden">${esc(caption)}</caption>
    <thead role="rowgroup"><tr role="row">${heads.map(th).join('')}</tr></thead><tbody role="rowgroup">${body}</tbody></table>`;
}

const failure = (e) => (e?.message || 'That did not work. Try again.');
const reauth = (fn) => withRecentAuth(fn);
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
const del = (path) => api(path, { method: 'DELETE' });

/* ---------- warnings ---------- */
function renderWarnings() {
  const data = admin.users;
  const parts = [];
  if (data?.only_one_admin) {
    parts.push(`<p><strong>You are the only admin.</strong> If you lose your password and your recovery codes, nobody can
      open people's details or run the admin panel. Make a second person an admin (People, then "Make admin").</p>`);
  }
  if (data && !data.admin_key_available && data.admin_key_needs_sign_in) {
    parts.push(`<p><strong>Sign in again to open people's details.</strong> This browser signed in before the admin key got
      its own protection. Names and emails stay hidden, and roles can't be changed, until you sign out and sign in again.</p>`);
  } else if (data && !data.admin_key_available) {
    parts.push(`<p><strong>You can't open people's details right now.</strong> Names and emails are hidden and roles can't be
      changed. Ask another admin to share the admin key with you again (People, then "Share the admin key again"). Signing in
      again may also fix it.</p>`);
  }
  if (data?.details_unavailable && data.admin_key_available) {
    parts.push(`<p>${num(data.details_unavailable)} ${data.details_unavailable === 1 ? 'person has' : 'people have'} details that can't be
      shown until they sign in again.</p>`);
  }
  const box = $('#admin-warnings');
  box.innerHTML = parts.map((p) => `<div class="callout" role="note">${p}</div>`).join('');
}

/* ================= summary and AI use ================= */
async function loadUsage() {
  const days = Number($('#usage-days').value) || 30;
  say($('#usage-status'), 'Loading…');
  let s;
  try { s = await api(`/api/admin/stats?days=${days}`); } catch (e) { say($('#usage-status'), `Could not load the figures: ${failure(e)}`); return; }
  say($('#usage-status'), '');
  const u = s.users;
  const tile = (name, value, hint = '') => `<div class="tile"><dt>${esc(name)}</dt><dd>${num(value)}${hint ? `<span class="tile-hint"> ${esc(hint)}</span>` : ''}</dd></div>`;
  $('#tiles').innerHTML = tile('People', u.total) + tile('Admins', u.admins) + tile('Active in 7 days', u.active_7d)
    + tile('Active in 30 days', u.active_30d) + tile('New in 30 days', u.new_30d);

  const t = s.llm.totals;
  const sumBy = (rows, keyOf) => {
    const map = new Map();
    for (const r of rows) {
      const k = keyOf(r);
      const cur = map.get(k.key) || { ...k, requests: 0, input: 0, output: 0, cached: 0 };
      for (const f of ['requests', 'input', 'output', 'cached']) cur[f] += r[f];
      map.set(k.key, cur);
    }
    return [...map.values()];
  };
  const cells = (r) => [num(r.requests), num(r.input), num(r.output), num(r.cached), pct(r.input ? r.cached / r.input : null)];
  const heads = ['Requests', 'Input tokens', 'Output tokens', 'Cached tokens', 'Cache hit rate'];
  const model = sumBy(s.llm.by_day, (r) => ({ key: `${r.provider}|${r.model}|${r.purpose}`, provider: r.provider, model: r.model, purpose: r.purpose }));
  const person = s.llm.by_user.map((r) => ({ ...r, who: r.user ? (r.user.email || r.user.name || `Person ${r.user.id}`) : 'An account that no longer exists' }));
  $('#usage-tables').innerHTML = `
    <p>${num(t.requests)} requests in the last ${days} days: ${num(t.input)} tokens in, ${num(t.output)} out, ${num(t.cached)} served from the cache (${pct(t.cache_hit_rate)} of input).</p>
    <h3>By model and purpose</h3>
    ${tableHtml(`AI use by model and purpose, last ${days} days`, ['Model and purpose', ...heads],
    model.map((r) => [`${esc(r.model || 'unknown model')}<div class="muted">${esc(r.purpose || 'other')} · ${esc(r.provider || '')}</div>`, ...cells(r)]),
    { empty: 'No AI requests in this period.' })}
    <h3>By person</h3>
    ${tableHtml(`AI use by person, last ${days} days`, ['Person', 'Model', ...heads],
    person.map((r) => [esc(r.who), esc(r.model || 'unknown'), ...cells(r)]), { empty: 'No AI requests in this period.' })}
    ${s.llm.by_day.length ? `<details class="usage-days"><summary>By day</summary>
      ${tableHtml(`AI use by day, last ${days} days`, ['Day', 'Model and purpose', ...heads],
    s.llm.by_day.map((r) => [esc(fmtYmd(r.day)), `${esc(r.model || 'unknown')} · ${esc(r.purpose || 'other')}`, ...cells(r)]))}</details>` : ''}`;
}

/* ================= people ================= */
const SORTERS = {
  person: (u) => label(u).toLowerCase(),
  role: (u) => u.role,
  status: (u) => u.status,
  last_active: (u) => u.last_active_on || '',
};

function sortedUsers() {
  const get = SORTERS[admin.sort.key] || SORTERS.person;
  const sign = admin.sort.dir === 'asc' ? 1 : -1;
  return [...admin.users.users].sort((a, b) => {
    const x = get(a); const y = get(b);
    return (x < y ? -1 : x > y ? 1 : label(a).toLowerCase().localeCompare(label(b).toLowerCase())) * sign;
  });
}

function actionButtons(u, data) {
  const l = label(u);
  const self = sameId(u.id, myId());
  const canRole = data.admin_key_available;
  const btn = (act, text, name, extra = '') => `<button class="btn btn-sm" type="button" data-act="${act}" data-id="${u.id}" aria-label="${esc(name)}"${extra}>${esc(text)}</button>`;
  const out = [u.role === 'admin'
    ? btn('role-user', 'Make regular user', `Make regular user: ${l}`, canRole ? '' : ' disabled')
    : btn('role-admin', 'Make admin', `Make admin: ${l}`, canRole && u.status === 'active' ? '' : ' disabled')];
  if (u.role === 'admin' && !u.admin_key_shared && canRole) out.push(btn('regrant', 'Share the admin key again', `Share the admin key again: ${l}`));
  if (!self) out.push(u.status === 'active' ? btn('disable', 'Disable', `Disable ${l}`) : btn('enable', 'Enable', `Enable ${l}`));
  if (u.locked && u.status === 'active' && !self) out.push(btn('unlock', 'Unlock', `Unlock ${l}, who must wait after wrong passwords`));
  if (!self) out.push(btn('delete', 'Delete', `Delete the account of ${l}`));
  return `<div class="row-actions">${out.join('')}</div>`;
}

function renderUsers() {
  const data = admin.users;
  renderWarnings();
  const sortBox = `<div class="sort-select"><label for="users-sort">Sort by</label>
    <select id="users-sort">${[['person', 'Person'], ['role', 'Role'], ['status', 'Status'], ['last_active', 'Last active']]
    .map(([k, t]) => `<option value="${k}"${admin.sort.key === k ? ' selected' : ''}>${t}</option>`).join('')}</select>
    <button id="users-sort-dir" class="btn btn-sm" type="button">${admin.sort.dir === 'asc' ? 'Ascending' : 'Descending'}</button></div>`;
  const rows = sortedUsers().map((u) => {
    const self = sameId(u.id, myId());
    const person = `<strong>${esc(u.email || `Person ${u.id}`)}</strong>${self ? ' <span class="scope-tag">You</span>' : ''}
      ${u.name ? `<div class="muted">${esc(u.name)}</div>` : ''}${u.details_note ? `<div class="muted">${esc(u.details_note)}</div>` : ''}`;
    const role = `${u.role === 'admin' ? 'Admin' : 'User'}${u.role === 'admin' && !u.admin_key_shared ? '<div class="muted">Admin key not shared with them</div>' : ''}`;
    const status = `${u.status === 'active' ? 'Active' : 'Disabled'}${u.locked ? '<div class="muted">Waiting after wrong passwords</div>' : ''}<div class="muted">Background access ${u.background ? 'on' : 'off'}</div>`;
    return [person, role, status, esc(fmtYmd(u.last_active_on)), actionButtons(u, data)];
  });
  $('#users-table').innerHTML = sortBox + tableHtml('People with an account', [
    { text: 'Person', sort: 'person' }, { text: 'Role', sort: 'role' }, { text: 'Status', sort: 'status' },
    { text: 'Last active', sort: 'last_active' }, 'Actions',
  ], rows, { id: 'users-grid', empty: 'No accounts yet.' });
  say($('#users-note'), `${num(data.users.length)} ${data.users.length === 1 ? 'account' : 'accounts'}. Last active is the day someone last used JobHuntPA.`);
}

async function loadUsers() {
  try {
    admin.users = await api('/api/admin/users');
  } catch (e) {
    showError($('#admin-load-error'), `Could not load the people: ${failure(e)}`);
    return false;
  }
  $('#admin-load-error').hidden = true;
  renderUsers();
  return true;
}

/* After an action the list is drawn again; focus goes back to the same button, or to the person's row if it's gone. */
function focusAction(act, id) {
  const el = document.querySelector(`#users-table [data-act="${act}"][data-id="${id}"]`)
    || document.querySelector(`#users-table [data-id="${id}"]`) || $('#users-title');
  if (el === $('#users-title')) el.tabIndex = -1;
  el.focus();
}

async function personAction(act, id, btn) {
  const u = findUser(id);
  if (!u) return;
  const l = label(u);
  const back = () => document.querySelector(`#users-table [data-act="${act}"][data-id="${id}"]`);
  const status = $('#users-status');
  const run = async (what, doIt, doneText) => {
    setBusy(btn, true);
    say(status, `${what}…`);
    try {
      const out = await doIt();
      say(status, doneText(out));
      await loadUsers();
    } catch (e) {
      say(status, '');
      toast(failure(e), 'err', 9000);
      await loadUsers();
    }
    setBusy(btn, false);
    focusAction(act, id);
  };
  if (act === 'role-admin') {
    if (!(await confirmDialog({ title: `Make ${l} an admin?`, ok: 'Make admin', returnTo: back,
      text: 'Admins can see everyone\'s names and emails, change roles, and run the server settings. They never see anyone\'s jobs, documents or searches. You will be asked for your password.' }))) return;
    await run('Making them an admin', () => reauth(() => post(`/api/admin/users/${id}/role`, { role: 'admin' })), () => `${l} is now an admin.`);
  } else if (act === 'role-user') {
    if (!(await confirmDialog({ title: `Make ${l} a regular user?`, ok: 'Make regular user', returnTo: back,
      text: 'They lose the admin panel. The admin key is replaced, so they can no longer open anyone\'s details, even from an old copy. You will be asked for your password.' }))) return;
    await run('Changing their role', () => reauth(() => post(`/api/admin/users/${id}/role`, { role: 'user' })), () => `${l} is now a regular user.`);
  } else if (act === 'regrant') {
    await run('Sharing the admin key', () => reauth(() => post(`/api/admin/users/${id}/regrant`)), () => `Shared the admin key with ${l}.`);
  } else if (act === 'disable') {
    if (!(await confirmDialog({ title: `Disable ${l}?`, ok: 'Disable', returnTo: back,
      text: `They are signed out everywhere and can't sign in until you enable them again. Their data is kept.${u.role === 'admin' ? ' They are an admin, so the admin key is replaced, and they are not given the new one. If you enable them again, share the admin key with them again.' : ''} You will be asked for your password.` }))) return;
    await run('Disabling', () => reauth(() => post(`/api/admin/users/${id}/disable`)), (r) => `${l} is disabled and signed out.${r?.note ? ' ' + r.note : ''}`);
  } else if (act === 'enable') {
    await run('Enabling', () => post(`/api/admin/users/${id}/enable`), () => `${l} can sign in again.`);
  } else if (act === 'unlock') {
    await run('Unlocking', () => post(`/api/admin/users/${id}/enable`), () => `${l} can try to sign in again.`);
  } else if (act === 'delete') {
    if (!(await confirmDialog({ title: `Delete the account of ${l}?`, ok: 'Delete the account', returnTo: back,
      text: `This removes the account and everything in it: their workspaces, jobs, documents and uploaded files. It can't be undone.${u.role === 'admin' ? ' They are an admin, so the admin key is replaced.' : ''} Their search must not be running. You will be asked for your password.` }))) return;
    await run('Deleting', () => reauth(() => del(`/api/admin/users/${id}`)), () => `The account of ${l} was deleted.`);
  }
}

/* ================= sign-up and invitations ================= */
async function loadSignup() {
  try {
    const s = await api('/api/admin/signup');
    const radio = document.querySelector(`input[name="signup-mode"][value="${s.mode}"]`);
    if (radio) radio.checked = true;
    admin.signupMode = s.mode;
  } catch (e) { say($('#signup-status'), `Could not load the sign-up settings: ${failure(e)}`); }
}

async function saveSignup(e) {
  e.preventDefault();
  const mode = document.querySelector('input[name="signup-mode"]:checked')?.value;
  if (mode === 'open' && admin.signupMode !== 'open' && !(await confirmDialog({
    title: 'Let anyone sign up?', ok: 'Open sign-up', returnTo: () => $('#signup-save'),
    text: 'Anyone who can reach this JobHuntPA will be able to create an account, and each account can use the AI service you pay for. Only do this if the server is not open to the whole internet.' }))) return;
  const btn = $('#signup-save');
  setBusy(btn, true);
  say($('#signup-status'), 'Saving…');
  try {
    const s = await api('/api/admin/signup', { method: 'PUT', body: JSON.stringify({ mode }) });
    admin.signupMode = s.mode;
    say($('#signup-status'), 'Saved.');
  } catch (err) { say($('#signup-status'), ''); toast(failure(err), 'err', 9000); }
  setBusy(btn, false);
}

const INVITE_STATE = { pending: 'Waiting', used: 'Used', expired: 'Expired' };

async function loadInvites() {
  try { admin.invites = (await api('/api/admin/invites')).invites; } catch (e) { $('#invites-table').innerHTML = `<p class="err-text">Could not load the invitations: ${esc(failure(e))}</p>`; return; }
  const by = (id) => label(findUser(id));
  $('#invites-table').innerHTML = tableHtml('Invitations', ['Invitation', 'State', 'Created', 'Expires', 'Used by', 'Actions'], admin.invites.map((i) => {
    const name = `Invitation ${i.id}${i.email ? ` for ${i.email}` : ''}`;
    const act = i.status === 'pending' ? `Cancel the ${name}` : `Remove the ${name} from the list`;
    return [esc(name), esc(INVITE_STATE[i.status] || i.status), esc(fmtWhen(i.created_at)), esc(fmtWhen(i.expires_at)),
      i.used_by ? esc(admin.users ? by(i.used_by) : `person ${i.used_by}`) : '<span class="muted">–</span>',
      `<button class="btn btn-sm" type="button" data-invite="${i.id}" aria-label="${esc(act)}">${i.status === 'pending' ? 'Cancel' : 'Remove'}</button>`];
  }), { empty: 'No invitations yet.' });
}

async function createInvite(e) {
  e.preventDefault();
  const input = $('#invite-email');
  const email = input.value.trim();
  setFieldError(input, '');
  const btn = $('#invite-create');
  setBusy(btn, true);
  say($('#invite-status'), 'Creating…');
  try {
    const out = await post('/api/admin/invites', email ? { email } : {});
    say($('#invite-status'), '');
    $('#invite-link').value = out.link;
    $('#invite-result').hidden = false;
    say($('#invite-copy-status'), '');
    $('#invite-result-title').focus();
    input.value = '';
    await loadInvites();
  } catch (err) {
    say($('#invite-status'), '');
    if (err.field === 'email') setFieldError(input, err.message); else toast(failure(err), 'err', 9000);
  }
  setBusy(btn, false);
}

async function copyInvite() {
  const field = $('#invite-link');
  try { await navigator.clipboard.writeText(field.value); say($('#invite-copy-status'), 'Copied.'); }
  catch { field.focus(); field.select(); say($('#invite-copy-status'), "Your browser didn't allow copying. The link is selected: copy it yourself."); }
}

async function onInviteClick(e) {
  const btn = e.target.closest('[data-invite]');
  if (!btn) return;
  setBusy(btn, true);
  try {
    await api(`/api/admin/invites/${btn.dataset.invite}`, { method: 'DELETE' });
    announce('Invitation removed.');
    await loadInvites();
    ($('#invites-table button') || $('#invite-email')).focus();
  } catch (err) { toast(failure(err), 'err', 9000); setBusy(btn, false); }
}

/* ================= server: key rotation, plaintext files ================= */
async function rotateKey() {
  if (!(await confirmDialog({ title: 'Replace the server key?', ok: 'Replace the key', returnTo: () => $('#rotate-key'),
    text: 'JobHuntPA moves the keys for background searches to a new server key and drops the old one. Nobody is signed out. If it can\'t move everything, the old key is kept and nothing is lost. You will be asked for your password.' }))) return;
  const btn = $('#rotate-key');
  setBusy(btn, true);
  say($('#rotate-status'), 'Replacing the key…');
  try {
    const out = await reauth(() => post('/api/admin/keys/rotate'));
    say($('#rotate-status'), `Done. ${num(out.rewrapped)} ${out.rewrapped === 1 ? 'key was' : 'keys were'} moved to the new server key.`);
    await loadAudit(true);
  } catch (e) { say($('#rotate-status'), `Not replaced: ${failure(e)}`); announce(`Not replaced: ${failure(e)}`, true); }
  setBusy(btn, false);
}

async function loadPlaintext() {
  let p;
  try { p = await api('/api/admin/plaintext-audit'); } catch (e) { say($('#plain-summary'), `Could not check: ${failure(e)}`); return; }
  const cols = Object.entries(p.columns);
  const parts = [];
  if (p.encryption.state !== 'done') {
    say($('#plain-summary'), `Your data is still being encrypted (${num(p.encryption.done)} of ${num(p.encryption.total)} done). Check again when it has finished.`);
  } else if (p.clean) {
    say($('#plain-summary'), 'No unencrypted copies of anyone\'s data were found.');
  } else {
    say($('#plain-summary'), 'Some data is still stored unencrypted.');
  }
  if (cols.length) parts.push(`<h4>Values that aren't encrypted</h4><ul>${cols.map(([k, n]) => `<li><code>${esc(k)}</code>: ${num(n)}</li>`).join('')}</ul>`);
  if (p.files.length) parts.push(`<p>${num(p.files.length)} uploaded ${p.files.length === 1 ? 'file isn\'t' : 'files aren\'t'} encrypted.</p>`);
  if (p.leftovers.length) {
    parts.push(`<h4>Old files that still hold data unencrypted</h4><p class="hint">Backups made before encryption and the old page cache. You only need the backup to go back to an older version of JobHuntPA. Delete the <code>.bak-*</code> files you don't need: they also hold keys from before, and anyone who gets them with the key file can read what they held.</p>
      <ul class="leftover-files">${p.leftovers.map((f) => `<li><code>${esc(f.path)}</code> (${esc(fmtBytes(f.bytes))})</li>`).join('')}</ul>`);
  }
  $('#plain-details').innerHTML = parts.join('');
  $('#plain-delete').hidden = !p.leftovers.length || p.encryption.state !== 'done';
}

async function deletePlaintext() {
  const btn = $('#plain-delete');
  if (!(await confirmDialog({ title: 'Delete the unencrypted files?', ok: 'Delete them', returnTo: () => btn,
    text: 'They are overwritten and removed. Without the backup, you can\'t go back to an older version of JobHuntPA. You will be asked for your password.' }))) return;
  setBusy(btn, true);
  say($('#plain-status'), 'Deleting…');
  try {
    const out = await reauth(() => post('/api/admin/plaintext-files/delete'));
    say($('#plain-status'), out.leftovers.length ? `${out.leftovers.length} file(s) could not be deleted. Check the data folder's permissions.` : `Deleted ${num(out.deleted.length)} file(s).`);
    await loadPlaintext();
    await loadAudit(true);
  } catch (e) { say($('#plain-status'), `Not deleted: ${failure(e)}`); announce(`Not deleted: ${failure(e)}`, true); }
  setBusy(btn, false);
  ($('#plain-delete').hidden ? $('#plain-summary') : $('#plain-delete')).focus?.();
}

/* ================= audit log ================= */
const AUDIT_TEXT = {
  role_changed: 'Changed a role', admin_key_shared: 'Shared the admin key', admin_key_replaced: 'Replaced the admin key',
  // (mfa_disabled, membership_updated and grant_*: actions of features since removed, still in older logs)
  user_disabled: 'Disabled an account', user_enabled: 'Enabled an account', mfa_disabled: 'Turned off two-step sign-in',
  membership_updated: 'Changed membership settings', grant_added: 'Added a free period', grant_changed: 'Changed a free period',
  grant_ended: 'Ended a free period', signup_changed: 'Changed the sign-up settings', invite_created: 'Created an invitation',
  invite_deleted: 'Removed an invitation', plaintext_files_deleted: 'Deleted unencrypted files', server_key_rotated: 'Replaced the server key',
  user_deleted: 'Deleted an account', user_unlocked: 'Unlocked an account', server_settings_changed: 'Changed the server settings',
};

function detailText(d) {
  if (!d || typeof d !== 'object') return '';
  return Object.entries(d).filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${typeof v === 'string' && /^\d{4}-\d\d-\d\dT/.test(v) ? fmtWhen(v) : v}`).join(' · ');
}

function renderAudit() {
  const who = (p) => (p ? esc(p.email || p.name || `Person ${p.id}`) : '<span class="muted">–</span>');
  $('#audit-table').innerHTML = tableHtml('What admins did, newest first', ['When', 'Admin', 'What', 'Person', 'Details'],
    admin.audit.entries.map((a) => [esc(fmtWhen(a.at)), who(a.admin), esc(AUDIT_TEXT[a.action] || a.action), who(a.target),
      a.detail === null ? '<span class="muted">Not readable with your key</span>' : (detailText(a.detail) ? esc(detailText(a.detail)) : '<span class="muted">–</span>')]), { empty: 'Nothing yet.' });
  $('#audit-more').hidden = admin.audit.next === null;
}

async function loadAudit(fresh = false) {
  const more = !fresh && admin.audit.next;
  say($('#audit-status'), 'Loading…');
  try {
    const out = await api(`/api/admin/audit?limit=50${more ? `&before_id=${admin.audit.next}` : ''}`);
    admin.audit.entries = more ? admin.audit.entries.concat(out.entries) : out.entries;
    admin.audit.next = out.next_before_id;
    say($('#audit-status'), more ? `Showing ${admin.audit.entries.length} entries.` : '');
    renderAudit();
    if (more) document.querySelectorAll('#audit-table tbody tr')[admin.audit.entries.length - out.entries.length]?.querySelector('th')?.setAttribute('tabindex', '-1');
  } catch (e) { say($('#audit-status'), `Could not load the log: ${failure(e)}`); }
}

/* ================= boot ================= */
function onUsersClick(e) {
  if (e.target.id === 'users-sort-dir') {
    admin.sort.dir = admin.sort.dir === 'asc' ? 'desc' : 'asc';
    renderUsers();
    $('#users-sort-dir').focus();
    return;
  }
  const sort = e.target.closest('[data-sort]');
  if (sort) {
    const key = sort.dataset.sort;
    admin.sort = { key, dir: admin.sort.key === key && admin.sort.dir === 'asc' ? 'desc' : 'asc' };
    renderUsers();
    document.querySelector(`#users-table [data-sort="${key}"]`)?.focus();
    announce(`Sorted by ${sort.textContent.trim()}, ${admin.sort.dir === 'asc' ? 'ascending' : 'descending'}.`);
    return;
  }
  const act = e.target.closest('[data-act]');
  if (act) personAction(act.dataset.act, act.dataset.id, act);
}

function onUsersChange(e) {
  if (e.target.id === 'users-sort') {
    admin.sort.key = e.target.value;
    renderUsers();
    $('#users-sort').focus();
    announce(`Sorted by ${$('#users-sort').selectedOptions[0].textContent}.`);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  let me;
  try { me = await loadSession(); } catch (e) { showError($('#admin-load-error'), e.message); return; }
  initAccountMenu(me);
  if (me.user.role !== 'admin' || !me.features?.admin_panel) {
    $('#main').innerHTML = '<div class="panel-head"><h1>Admin</h1></div><p class="error" role="alert">Only admins can open this page. <a href="./">Back to the dashboard</a></p>';
    return;
  }
  remindRecoveryCodes(me);
  showSignInNotice();
  initDialog($('#confirm-dialog'));
  $('#users-table').addEventListener('click', onUsersClick);
  $('#users-table').addEventListener('change', onUsersChange);
  $('#usage-days').addEventListener('change', loadUsage);
  $('#signup-form').addEventListener('submit', saveSignup);
  $('#invite-form').addEventListener('submit', createInvite);
  $('#invite-copy').addEventListener('click', copyInvite);
  $('#invites-table').addEventListener('click', onInviteClick);
  $('#rotate-key').addEventListener('click', rotateKey);
  $('#plain-delete').addEventListener('click', deletePlaintext);
  $('#audit-more').addEventListener('click', () => loadAudit());
  initLlmSettings();
  initLlmProvider();
  initCommuteKeys();
  $('#score-test').addEventListener('click', testScorer);
  await loadUsers();
  loadUsage();
  loadSignup();
  loadInvites();
  loadLlmSettings();
  loadLlmProvider();
  loadCommuteKeys();
  loadPlaintext();
  loadAudit(true);
});
