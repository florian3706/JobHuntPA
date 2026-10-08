/* JobHuntPA admin panel (accounts design §7): summary tiles and AI use, people, sign-up and invitations, the
   server's settings, and the audit log. The AI provider and commute-key cards are driven by server-settings.js and the
   dialogs by dialogs.js.

   WCAG 2.2 notes:
   - Tables have captions and <th scope>; the people table sorts with buttons in its headers (aria-sort) and, on
     narrow screens, with a "Sort by" menu, because the header row is then hidden and each row becomes a card.
   - Every action button names its target ("Send reset email to alice@example.com").
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
const fmtDay = (iso) => (iso ? dateOf(iso).toLocaleDateString([], { dateStyle: 'medium' }) : '–');
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
/* While a dialog is open the page behind it (toasts, live regions) is inert and unseen, so a failure inside one is
   written into an alert element inside the dialog. */
function dialogError(id, msg) { const el = document.getElementById(id); if (el) { el.textContent = msg; el.hidden = false; } }
function clearDialogError(...ids) { ids.forEach((id) => { const el = document.getElementById(id); if (el) { el.textContent = ''; el.hidden = true; } }); }
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
  if (data && !data.admin_key_available) {
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
function membershipText(u) {
  const m = u.membership;
  const bits = [];
  if (m.exempt) bits.push('Exempt from payment');
  if (m.active_grants) bits.push(`${m.active_grants} free period${m.active_grants === 1 ? '' : 's'} running`);
  if (m.stripe_status) bits.push(`Subscription: ${m.stripe_status}`);
  if (!bits.length) bits.push('No membership');
  bits.push(`${num(m.free_searches_used)} free searches used`);
  return bits;
}

const membershipRank = (u) => (u.membership.exempt ? 3 : 0) + (u.membership.active_grants ? 2 : 0) + (u.membership.stripe_status ? 1 : 0);
const SORTERS = {
  person: (u) => label(u).toLowerCase(),
  role: (u) => u.role,
  status: (u) => u.status,
  last_active: (u) => u.last_active_on || '',
  membership: (u) => membershipRank(u),
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
  const out = [btn('membership', 'Membership', `Edit membership of ${l}`)];
  out.push(u.role === 'admin'
    ? btn('role-user', 'Make regular user', `Make ${l} a regular user`, canRole ? '' : ' disabled')
    : btn('role-admin', 'Make admin', `Make ${l} an admin`, canRole && u.status === 'active' ? '' : ' disabled'));
  if (u.role === 'admin' && !u.admin_key_shared && canRole) out.push(btn('regrant', 'Share the admin key again', `Share the admin key with ${l} again`));
  if (!self) out.push(u.status === 'active' ? btn('disable', 'Disable', `Disable ${l}`) : btn('enable', 'Enable', `Enable ${l}`));
  if (u.locked && u.status === 'active' && !self) out.push(btn('unlock', 'Unlock', `Unlock ${l}, who must wait after wrong passwords`));
  if (u.mfa && !self) out.push(btn('mfa-off', 'Turn off two-step sign-in', `Turn off two-step sign-in for ${l}`));
  out.push(btn('reset', 'Send reset email', `Send reset email to ${l}`));
  if (!self) out.push(btn('delete', 'Delete', `Delete the account of ${l}`));
  return `<div class="row-actions">${out.join('')}</div>`;
}

function renderUsers() {
  const data = admin.users;
  renderWarnings();
  const sortBox = `<div class="sort-select"><label for="users-sort">Sort by</label>
    <select id="users-sort">${[['person', 'Person'], ['role', 'Role'], ['status', 'Status'], ['last_active', 'Last active'], ['membership', 'Membership']]
    .map(([k, t]) => `<option value="${k}"${admin.sort.key === k ? ' selected' : ''}>${t}</option>`).join('')}</select>
    <button id="users-sort-dir" class="btn btn-sm" type="button">${admin.sort.dir === 'asc' ? 'Ascending' : 'Descending'}</button></div>`;
  const rows = sortedUsers().map((u) => {
    const self = sameId(u.id, myId());
    const person = `<strong>${esc(u.email || `Person ${u.id}`)}</strong>${self ? ' <span class="scope-tag">You</span>' : ''}
      ${u.name ? `<div class="muted">${esc(u.name)}</div>` : ''}${u.details_note ? `<div class="muted">${esc(u.details_note)}</div>` : ''}`;
    const role = `${u.role === 'admin' ? 'Admin' : 'User'}${u.role === 'admin' && !u.admin_key_shared ? '<div class="muted">Admin key not shared with them</div>' : ''}`;
    const status = `${u.status === 'active' ? 'Active' : 'Disabled'}${u.locked ? '<div class="muted">Waiting after wrong passwords</div>' : ''}<div class="muted">Two-step sign-in ${u.mfa ? 'on' : 'off'} · background access ${u.background ? 'on' : 'off'}</div>`;
    return [person, role, status, esc(fmtYmd(u.last_active_on)), membershipText(u).map(esc).join('<br>'), actionButtons(u, data)];
  });
  $('#users-table').innerHTML = sortBox + tableHtml('People with an account', [
    { text: 'Person', sort: 'person' }, { text: 'Role', sort: 'role' }, { text: 'Status', sort: 'status' },
    { text: 'Last active', sort: 'last_active' }, { text: 'Membership', sort: 'membership' }, 'Actions',
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
  if (act === 'membership') { openMembership(id, btn); return; }
  if (act === 'role-admin') {
    if (!(await confirmDialog({ title: `Make ${l} an admin?`, ok: 'Make admin', returnTo: back,
      text: 'Admins can see everyone\'s names, emails and membership details, change roles and memberships, and run the server settings. They never see anyone\'s jobs, documents or searches. You will be asked for your password.' }))) return;
    await run('Making them an admin', () => reauth(() => post(`/api/admin/users/${id}/role`, { role: 'admin' })), () => `${l} is now an admin.`);
  } else if (act === 'role-user') {
    if (!(await confirmDialog({ title: `Make ${l} a regular user?`, ok: 'Make regular user', returnTo: back,
      text: 'They lose the admin panel. The admin key is replaced, so they can no longer open anyone\'s details, even from an old copy. You will be asked for your password.' }))) return;
    await run('Changing their role', () => reauth(() => post(`/api/admin/users/${id}/role`, { role: 'user' })), () => `${l} is now a regular user.`);
  } else if (act === 'regrant') {
    await run('Sharing the admin key', () => reauth(() => post(`/api/admin/users/${id}/regrant`)), () => `Shared the admin key with ${l}.`);
  } else if (act === 'disable') {
    if (!(await confirmDialog({ title: `Disable ${l}?`, ok: 'Disable', returnTo: back,
      text: `They are signed out everywhere and can't sign in until you enable them again. Their data is kept.${LIVE_SUB.includes(u.membership.stripe_status) ? ' They pay by subscription, which keeps renewing while they are off, and they can\'t cancel it themselves.' : ''}${u.role === 'admin' ? ' They are an admin, so the admin key is replaced, and they are not given the new one. If you enable them again, share the admin key with them again.' : ''} You will be asked for your password.` }))) return;
    await run('Disabling', () => reauth(() => post(`/api/admin/users/${id}/disable`)), (r) => `${l} is disabled and signed out.${r?.subscription === 'still_billing' ? ' Their subscription is still active and they are still charged. Open Membership to exempt them and end it at the period end, or cancel it in Stripe.' : ''}${r?.note ? ' ' + r.note : ''}`);
  } else if (act === 'enable') {
    await run('Enabling', () => post(`/api/admin/users/${id}/enable`), () => `${l} can sign in again.`);
  } else if (act === 'unlock') {
    await run('Unlocking', () => post(`/api/admin/users/${id}/enable`), () => `${l} can try to sign in again.`);
  } else if (act === 'delete') {
    if (!(await confirmDialog({ title: `Delete the account of ${l}?`, ok: 'Delete the account', returnTo: back,
      text: `This removes the account and everything in it: their workspaces, jobs, documents and uploaded files. It can't be undone.${u.role === 'admin' ? ' They are an admin, so the admin key is replaced.' : ''} Their search must not be running. You will be asked for your password.` }))) return;
    await run('Deleting', () => reauth(() => del(`/api/admin/users/${id}`)), (r) => `The account of ${l} was deleted.${r?.subscription === 'failed' ? ' Their subscription may still be active: cancel it in the Stripe dashboard.' : ''}`);
  } else if (act === 'mfa-off') {
    if (!(await confirmDialog({ title: `Turn off two-step sign-in for ${l}?`, ok: 'Turn it off', returnTo: back,
      text: 'Do this only when you are sure the request is really from them: it removes a protection from their account. They are signed out and can set it up again. You will be asked for your password.' }))) return;
    await run('Turning it off', () => reauth(() => post(`/api/admin/users/${id}/mfa/disable`)), (out) => `Two-step sign-in is off for ${l}, and they are signed out.${out?.notice_sent ? ' They were told by email.' : ''}`);
  } else if (act === 'reset') {
    await run('Sending', () => post(`/api/admin/users/${id}/reset-email`), () => `Sent a password reset email to ${l}.`);
  }
}

/* ================= membership dialog ================= */
let memberId = null;
let editingGrant = null;
let editingDates = null; // the dates the change form was filled with: only a date that was edited is sent

const GRANT_KIND = { free_period: 'Free period', manual: 'Manual extension' };

function grantState(g) {
  if (g.revoked_at) return `Ended ${fmtDay(g.revoked_at)}`;
  if (g.active) return 'Running';
  return dateOf(g.starts_at) > new Date() ? 'Not started' : 'Finished';
}

function renderGrants() {
  const u = findUser(memberId);
  if (!u) return;
  const l = label(u);
  const grants = u.membership.grants;
  const rows = grants.map((g) => {
    const when = `${fmtDay(g.starts_at)} to ${fmtDay(g.ends_at)}`;
    const buttons = g.revoked_at ? '' : `<div class="row-actions">
      <button class="btn btn-sm" type="button" data-grant-edit="${g.id}" aria-label="Change the ${esc(GRANT_KIND[g.kind] || g.kind).toLowerCase()} ${esc(when)}">Change</button>
      <button class="btn btn-sm" type="button" data-grant-end="${g.id}" aria-label="End the ${esc(GRANT_KIND[g.kind] || g.kind).toLowerCase()} ${esc(when)} now">End now</button></div>`;
    return [`${esc(GRANT_KIND[g.kind] || g.kind)}`, esc(fmtDay(g.starts_at)), esc(fmtDay(g.ends_at)), esc(grantState(g)),
      g.note ? esc(g.note) : '<span class="muted">–</span>', buttons];
  });
  $('#grants-table').innerHTML = tableHtml(`Free periods and extensions for ${l}`, ['Kind', 'From', 'Until', 'State', 'Note', 'Actions'], rows,
    { empty: 'No free periods yet.' });
  const total = u.membership.active_grants;
  say($('#grants-count'), grants.length ? `${total} running. Showing the newest ${grants.length}.` : '');
}

/* "Ends on" includes the whole day, and the server wants the moment it ends: midnight at the end of that day. */
function addDay(ymd, by) {
  const d = new Date(`${ymd}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + by);
  return d.toISOString().slice(0, 10);
}
/* The last day a period includes: its end date, or the day before when it ends exactly at midnight. */
function lastDayOf(iso) {
  const day = iso.slice(0, 10);
  return /T00:00:00/.test(iso) ? addDay(day, -1) : day;
}

function resetGrantForm() {
  editingGrant = null;
  editingDates = null;
  $('#grant-form').reset();
  $('#grant-kind').disabled = false;
  $('#grant-form-title').textContent = 'Add a free period';
  $('#grant-save').textContent = 'Add free period';
  $('#grant-cancel').hidden = true;
  ['grant-start', 'grant-end', 'grant-days', 'grant-note'].forEach((id) => setFieldError(document.getElementById(id), '', { focus: false }));
}

const LIVE_SUB = ['active', 'trialing', 'past_due'];
const BILLING_EVERY = { month: 'month', year: 'year', week: 'week', day: 'day' };

/* How someone pays, for the membership dialog: the subscription's state, then (from the admin-only billing details)
   card brand and last 4, plan and price. Stripe's own ids never reach this page. */
function subscriptionHtml(m, b) {
  if (!m.stripe_status) return '';
  const bits = [`Subscription: ${m.stripe_status.replace('_', ' ')}`];
  const plan = b?.plan || m.plan;
  if (plan) bits.push(`plan ${plan}${b?.amount != null ? ` (${(b.amount / 100).toFixed(2)} ${String(b.currency || '').toUpperCase()} per ${BILLING_EVERY[b.interval] || b.interval || 'period'})` : ''}`);
  if (b?.card_last4) bits.push(`${b.card_brand ? `${b.card_brand} card` : 'card'} ending ${b.card_last4}`);
  if (m.current_period_end) bits.push(m.cancel_at_period_end ? `ends ${fmtDay(m.current_period_end)}` : `next payment ${fmtDay(m.current_period_end)}`);
  if (m.stripe_status === 'past_due' && m.grace_until) bits.push(`keeps access until ${fmtDay(m.grace_until)}`);
  return `<p class="muted">${esc(bits.join(', '))}.</p>`;
}

function openMembership(id, opener) {
  const u = findUser(id);
  memberId = id;
  const l = label(u);
  const m = u.membership;
  $('#person-body').innerHTML = `
    <p class="muted">${esc(u.name || '')}${u.name && u.email ? ' · ' : ''}${esc(u.created_via ? `joined by ${u.created_via === 'invite' ? 'invitation' : u.created_via === 'open' ? 'open sign-up' : u.created_via}` : '')}${u.email_verified ? ' · email verified' : ''}</p>
    <form id="mem-form" class="admin-form" novalidate>
      <h3>Payment</h3>
      <label class="check"><input id="mem-exempt" type="checkbox"${m.exempt ? ' checked' : ''} /> Exempt from payment (never asked for a membership)</label>
      <div class="field narrow">
        <label class="field-label" for="mem-free">Free searches used</label>
        <input id="mem-free" type="number" min="0" max="100000" step="1" inputmode="numeric" value="${esc(m.free_searches_used)}" />
      </div>
      ${subscriptionHtml(m, u.billing)}
      <label id="mem-cancel-row" class="check" hidden><input id="mem-cancel" type="checkbox" aria-describedby="mem-cancel-hint" /> Cancel their subscription at the period end</label>
      <p id="mem-cancel-hint" class="hint" hidden>They keep what they paid for until ${esc(fmtDay(m.current_period_end))}, then are not charged again. Stripe is told first: if it can't be reached, nothing is saved.</p>
      <div class="toolbar-row"><button id="mem-save" class="btn btn-primary" type="submit">Save payment settings</button><span id="mem-status" class="muted" role="status"></span></div>
      <p id="mem-error" class="error" role="alert" hidden></p>
    </form>
    <h3>Free periods and extensions</h3>
    <p class="hint">Saving a payment setting or changing a free period asks for your password if you haven't entered it in the last 15 minutes.</p>
    <p class="hint">Dates are in UTC. A free period begins at the start of its first day and "Or ends on" includes that whole day. It gives full access until it ends; "End now" keeps it in the history.${LIVE_SUB.includes(m.stripe_status) ? ' This person pays, so a free period also moves their next payment to when it ends.' : ''}</p>
    <p id="grants-count" class="muted" role="status"></p>
    <p id="grants-error" class="error" role="alert" hidden></p>
    <div id="grants-table"></div>
    <form id="grant-form" class="admin-form" novalidate>
      <h4 id="grant-form-title">Add a free period</h4>
      <div class="form-grid">
        <label class="field"><span class="field-label">Kind</span><select id="grant-kind"><option value="free_period">Free period</option><option value="manual">Manual extension</option></select></label>
        <div class="field"><label class="field-label" for="grant-start">Starts on <span class="muted">(blank: now)</span></label><input id="grant-start" type="date" /></div>
        <div class="field"><label class="field-label" for="grant-days">Lasts for (days)</label><input id="grant-days" type="number" min="1" max="3660" step="1" inputmode="numeric" /></div>
        <div class="field"><label class="field-label" for="grant-end">Or ends on</label><input id="grant-end" type="date" /></div>
      </div>
      <div class="field"><label class="field-label" for="grant-note">Note <span class="muted">(only admins see it)</span></label><input id="grant-note" type="text" maxlength="500" autocomplete="off" /></div>
      <div class="toolbar-row">
        <button id="grant-save" class="btn btn-primary" type="submit">Add free period</button>
        <button id="grant-cancel" class="btn" type="button" hidden>Cancel the change</button>
        <span id="grant-status" class="muted" role="status"></span>
      </div>
      <p id="grant-error" class="error" role="alert" hidden></p>
    </form>`;
  renderGrants();
  $('#mem-form').addEventListener('submit', saveMembership);
  // Exempting someone who pays offers to end their subscription when the period they paid for is over.
  const syncCancel = () => {
    const offer = $('#mem-exempt').checked && LIVE_SUB.includes(m.stripe_status) && !m.cancel_at_period_end;
    $('#mem-cancel-row').hidden = !offer;
    $('#mem-cancel-hint').hidden = !offer;
    if (!offer) $('#mem-cancel').checked = false;
  };
  $('#mem-exempt').addEventListener('change', syncCancel);
  syncCancel();
  $('#grant-form').addEventListener('submit', saveGrant);
  $('#grant-cancel').addEventListener('click', () => { resetGrantForm(); $('#grant-kind').focus(); });
  $('#grants-table').addEventListener('click', onGrantClick);
  openDialog($('#person-dialog'), `Membership: ${l}`, () => document.querySelector(`#users-table [data-act="membership"][data-id="${id}"]`) || opener);
}

const GRANT_FIELD = { starts_at: 'grant-start', ends_at: 'grant-end', days: 'grant-days', note: 'grant-note' };

function grantProblem(e) {
  const input = document.getElementById(GRANT_FIELD[e.field] || '');
  if (input) setFieldError(input, e.message); else dialogError('grant-error', failure(e));
}

async function saveMembership(e) {
  e.preventDefault();
  const free = $('#mem-free');
  const n = Number(free.value);
  if (free.value === '' || !Number.isInteger(n) || n < 0 || n > 100000) { setFieldError(free, 'Enter a whole number from 0 to 100,000.'); return; }
  setFieldError(free, '');
  clearDialogError('mem-error');
  const btn = $('#mem-save');
  setBusy(btn, true);
  say($('#mem-status'), 'Saving…');
  try {
    const body = { exempt: $('#mem-exempt').checked, free_searches_used: n };
    if (body.exempt && $('#mem-cancel').checked) body.cancel_subscription_at_period_end = true;
    const r = await reauth(() => api(`/api/admin/users/${memberId}/membership`, { method: 'PUT', body: JSON.stringify(body) }));
    say($('#mem-status'), r.subscription_ending ? 'Saved. Their subscription ends when the period they paid for is over.' : 'Saved.');
    await loadUsers();
  } catch (err) { say($('#mem-status'), ''); dialogError('mem-error', failure(err)); }
  setBusy(btn, false);
}

async function saveGrant(e) {
  e.preventDefault();
  const start = $('#grant-start').value;
  const days = $('#grant-days').value.trim();
  const end = $('#grant-end').value;
  ['grant-start', 'grant-end', 'grant-days', 'grant-note'].forEach((id) => setFieldError(document.getElementById(id), '', { focus: false }));
  clearDialogError('grant-error');
  const endChanged = !editingGrant || end !== editingDates.end; // a date left as it was filled in is not sent
  const startChanged = !editingGrant || start !== editingDates.start;
  if (!editingGrant || days || endChanged) {
    if (!days && !end) { setFieldError($('#grant-days'), 'Enter how many days it lasts, or the date it ends.'); return; }
    if (days && endChanged && end) { setFieldError($('#grant-days'), 'Fill in either the number of days or the end date, not both.'); return; }
  }
  const body = { note: $('#grant-note').value.trim() };
  if (start && startChanged) body.starts_at = start;
  if (days) body.days = Number(days);
  else if (end && endChanged) body.ends_at = addDay(end, 1);
  const btn = $('#grant-save');
  setBusy(btn, true);
  say($('#grant-status'), 'Saving…');
  try {
    const r = await reauth(() => (editingGrant ? api(`/api/admin/users/${memberId}/grants/${editingGrant}`, { method: 'PATCH', body: JSON.stringify(body) })
      : post(`/api/admin/users/${memberId}/grants`, { kind: $('#grant-kind').value, ...body })));
    const moved = r?.stripe?.extended ? (r.stripe.trial_end ? ` Their next payment moves to ${fmtDay(r.stripe.trial_end)}.` : ' They are charged again now.') : '';
    say($('#grant-status'), `${editingGrant ? 'Changed.' : 'Added.'}${moved}`);
    announce(`${editingGrant ? 'Free period changed.' : 'Free period added.'}${moved}`);
    resetGrantForm();
    await loadUsers();
    renderGrants();
    // The free period stands even when Stripe couldn't be told: say so, so it can be fixed in Stripe.
    if (r?.stripe?.problem) dialogError('grant-error', `The free period is saved, but their next payment was not changed: ${r.stripe.problem}`);
  } catch (err) { say($('#grant-status'), ''); grantProblem(err); }
  setBusy(btn, false);
}

async function onGrantClick(e) {
  const editBtn = e.target.closest('[data-grant-edit]');
  const endBtn = e.target.closest('[data-grant-end]');
  const u = findUser(memberId);
  if (editBtn) {
    const g = u.membership.grants.find((x) => sameId(x.id, editBtn.dataset.grantEdit));
    editingGrant = g.id;
    $('#grant-kind').value = g.kind;
    $('#grant-kind').disabled = true;
    editingDates = { start: g.starts_at.slice(0, 10), end: lastDayOf(g.ends_at) };
    $('#grant-start').value = editingDates.start;
    $('#grant-end').value = editingDates.end;
    $('#grant-days').value = '';
    $('#grant-note').value = g.note || '';
    $('#grant-form-title').textContent = 'Change this free period';
    $('#grant-save').textContent = 'Save changes';
    $('#grant-cancel').hidden = false;
    $('#grant-form-title').scrollIntoView({ block: 'nearest', behavior: scrollBehavior() });
    $('#grant-start').focus();
  } else if (endBtn) {
    setBusy(endBtn, true);
    clearDialogError('grants-error');
    try {
      const r = await reauth(() => del(`/api/admin/users/${memberId}/grants/${endBtn.dataset.grantEnd}`));
      const billed = r?.stripe?.extended ? (r.stripe.trial_end ? ` Their next payment moves to ${fmtDay(r.stripe.trial_end)}.` : ' They are charged again now.') : '';
      announce(`Free period ended.${billed}`);
      await loadUsers();
      renderGrants();
      ($('#grants-table button') || $('#grant-kind')).focus();
      // The free period is over either way: say so when Stripe still has it, so it can be fixed there.
      if (r?.stripe?.problem) dialogError('grants-error', `The free period is ended, but Stripe still has it as free: ${r.stripe.problem}`);
    } catch (err) { dialogError('grants-error', failure(err)); setBusy(endBtn, false); }
  }
}

/* ================= sign-up and invitations ================= */
async function loadSignup() {
  try {
    const s = await api('/api/admin/signup');
    const radio = document.querySelector(`input[name="signup-mode"][value="${s.mode}"]`);
    if (radio) radio.checked = true;
    $('#signup-free').value = s.free_searches;
    admin.signupMode = s.mode;
  } catch (e) { say($('#signup-status'), `Could not load the sign-up settings: ${failure(e)}`); }
}

async function saveSignup(e) {
  e.preventDefault();
  const mode = document.querySelector('input[name="signup-mode"]:checked')?.value;
  const free = Number($('#signup-free').value);
  if ($('#signup-free').value === '' || !Number.isInteger(free) || free < 0 || free > 1000) { setFieldError($('#signup-free'), 'Enter a whole number from 0 to 1,000.'); return; }
  setFieldError($('#signup-free'), '');
  if (mode === 'open' && admin.signupMode !== 'open' && !(await confirmDialog({
    title: 'Let anyone sign up?', ok: 'Open sign-up', returnTo: () => $('#signup-save'),
    text: 'Anyone who can reach this JobHuntPA will be able to create an account, and each account can use the AI service you pay for. Only do this if the server is not open to the whole internet.' }))) return;
  const btn = $('#signup-save');
  setBusy(btn, true);
  say($('#signup-status'), 'Saving…');
  try {
    const s = await api('/api/admin/signup', { method: 'PUT', body: JSON.stringify({ mode, free_searches: free }) });
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

const INVITE_NOTE = {
  queued: 'The link is being emailed to them. Copy it too if you like: it is shown only once.',
  not_set_up: "Email isn't set up, so nothing was sent. Copy the link now: it is shown only once. Send it to the person yourself.",
  no_address: 'Nothing was sent, because the invitation has no address. Copy the link now: it is shown only once.',
  busy: 'Email is busy, so nothing was sent. Copy the link now: it is shown only once. Send it to the person yourself.',
  off: 'Copy it now: it is shown only once. Send it to the person yourself.',
};

async function createInvite(e) {
  e.preventDefault();
  const input = $('#invite-email');
  const email = input.value.trim();
  setFieldError(input, '');
  const btn = $('#invite-create');
  setBusy(btn, true);
  say($('#invite-status'), 'Creating…');
  try {
    // With email set up the link is emailed to the address, unless the box is unticked.
    const body = email ? { email } : {};
    if (email && emailIsOn()) body.send = $('#invite-send').checked;
    const out = await post('/api/admin/invites', body);
    say($('#invite-status'), '');
    $('#invite-result-note').textContent = INVITE_NOTE[out.email_status] || INVITE_NOTE.off;
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
    text: 'JobHuntPA moves the keys for background searches and two-step sign-in to a new server key and drops the old one. Nobody is signed out. If it can\'t move everything, the old key is kept and nothing is lost. You will be asked for your password.' }))) return;
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
    parts.push(`<h4>Old files that still hold data unencrypted</h4><p class="hint">Backups made before encryption and the old page cache. You only need the backup to go back to an older version of JobHuntPA.</p>
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
  ['#person-dialog', '#confirm-dialog'].forEach((s) => initDialog($(s)));
  $('#person-close').addEventListener('click', () => $('#person-dialog').close());
  $('#person-dialog').addEventListener('close', () => { $('#person-body').innerHTML = ''; memberId = null; editingGrant = null; });
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
  initEmailSettings();
  initStripeSettings();
  $('#score-test').addEventListener('click', testScorer);
  await loadUsers();
  loadUsage();
  loadSignup();
  loadInvites();
  loadLlmSettings();
  loadLlmProvider();
  loadCommuteKeys();
  loadEmailSettings();
  loadStripeSettings();
  loadPlaintext();
  loadAudit(true);
});
