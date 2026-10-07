/* JobHuntPA admin panel, Server section: the "Email" card (accounts design §9 and §16): the mail server (SMTP), the
   From address, the public address that links in emails point to, and "Send a test email". Loaded by admin.html after
   common.js and before admin.js, which calls initEmailSettings() and loadEmailSettings(); the markup is in admin.html.

   - The server never sends the password back, only whether one is set ("Saved" or "Not set").
   - Saving needs a recent sign-in (withRecentAuth asks for the password); the settings decide where reset links go.
   - A refusal names the field it is about ({field}); it is written under that field (setFieldError, WCAG 3.3.1).
   - The test email is sent in the background: the page asks for the result about once a second and says it in a
     status region (WCAG 4.1.3). */
'use strict';

let emailCfg = null; // GET /api/settings/email
const EMAIL_FIELDS = { host: 'email-host', port: 'email-port', security: 'email-security', user: 'email-user',
  password: 'email-password', from_address: 'email-from', public_base_url: 'email-public' };
const SECURITY_LABEL = { starttls: 'STARTTLS (port 587)', ssl: 'SSL (port 465)', none: 'None (a mail server on this computer)' };

/* Invitations offer "Email it" only when email is set up (admin.js asks). */
const emailIsOn = () => Boolean(emailCfg?.enabled);

function renderEmailSettings(st) {
  emailCfg = st;
  $('#email-security').innerHTML = st.security_modes.map((m) => `<option value="${esc(m)}">${esc(SECURITY_LABEL[m] || m)}</option>`).join('');
  $('#email-host').value = st.host || '';
  $('#email-port').value = st.port || '';
  $('#email-security').value = st.security;
  $('#email-user').value = st.user || '';
  $('#email-password').value = '';
  $('#email-from').value = st.from_address || '';
  $('#email-public').value = st.public_base_url || '';
  $('#email-password-state').innerHTML = st.password.set ? '<span class="ok-text">Saved</span>' : '<span class="muted">Not set</span>';
  $('#email-password-clear').disabled = !st.password.set;
  $('#email-state').textContent = st.text;
  updateEmailPort();
  $('#email-origin').textContent = location.origin;
  syncInviteEmail();
}

function updateEmailPort() {
  if (!emailCfg) return;
  $('#email-port').placeholder = emailCfg.default_ports[$('#email-security').value] || '';
}

/* Shown in the invitations form: the "email the link" tick, only while email is on. */
function syncInviteEmail() {
  const box = document.getElementById('invite-send-field');
  if (box) box.hidden = !emailIsOn();
}

async function loadEmailSettings() {
  try { renderEmailSettings(await api('/api/settings/email')); }
  catch (e) { $('#email-state').textContent = `Could not load the email settings: ${e.message}`; }
}

async function saveEmailSettings(e) {
  e.preventDefault();
  const err = $('#email-error');
  err.hidden = true;
  Object.values(EMAIL_FIELDS).forEach((id) => setFieldError($(`#${id}`), ''));
  const body = {
    host: $('#email-host').value.trim(), port: $('#email-port').value.trim(), security: $('#email-security').value,
    user: $('#email-user').value.trim(), from_address: $('#email-from').value.trim(),
    public_base_url: $('#email-public').value.trim(),
  };
  if ($('#email-password').value.trim()) body.password = $('#email-password').value; // empty keeps the saved one
  const btn = $('#email-save');
  setBusy(btn, true);
  $('#email-save-status').textContent = 'Saving…';
  try {
    renderEmailSettings(await withRecentAuth(() => api('/api/settings/email', { method: 'PUT', body: JSON.stringify(body) })));
    $('#email-save-status').textContent = 'Saved.';
    toast('Email settings saved', 'ok', 2500);
  } catch (ex) {
    $('#email-save-status').textContent = '';
    const id = EMAIL_FIELDS[ex.field];
    if (id) setFieldError($(`#${id}`), ex.message); else showError(err, `Not saved: ${ex.message}`);
  }
  setBusy(btn, false);
}

async function removeEmailPassword() {
  const btn = $('#email-password-clear');
  if (!(await confirmDialog({ title: 'Remove the saved mail password?', ok: 'Remove password', returnTo: () => btn,
    text: 'JobHuntPA then connects to the mail server without a password. Email stops working if your server needs one.' }))) return;
  setBusy(btn, true);
  try {
    renderEmailSettings(await withRecentAuth(() => api('/api/settings/email', { method: 'PUT', body: JSON.stringify({ password: '' }) })));
    toast('Mail password removed', 'ok', 2500);
  } catch (ex) { toast(`Not removed: ${ex.message}`, 'err', 8000); }
  setBusy(btn, false);
  $('#email-password').focus();
}

function useThisAddress() {
  $('#email-public').value = location.origin;
  setFieldError($('#email-public'), '');
  announce(`Public address set to ${location.origin}. Save the email settings to keep it.`);
}

async function testEmail() {
  const btn = $('#email-test');
  const out = $('#email-test-status');
  setBusy(btn, true);
  out.textContent = 'Sending…';
  try {
    await api('/api/settings/email/test', { method: 'POST' });
    for (let i = 0; i < 90; i += 1) {
      await new Promise((r) => setTimeout(r, 1000));
      const r = await api('/api/settings/email/test');
      if (r.status === 'sent') { out.innerHTML = '<span class="ok-text">Sent. Check your own inbox (and spam).<span aria-hidden="true"> ✓</span></span>'; break; }
      if (r.status === 'failed') {
        out.innerHTML = `<span class="err-text"><span aria-hidden="true">✕ </span>The mail server refused it${r.error ? ` (${esc(r.error)})` : ''}. Check the server, port, encryption, user name and password.</span>`;
        break;
      }
      if (r.status !== 'sending') { out.textContent = ''; break; }
      if (i === 89) out.textContent = 'Still sending. Check again in a minute.';
    }
  } catch (e) { out.innerHTML = `<span class="err-text">Test failed: ${esc(e.message)}</span>`; }
  setBusy(btn, false);
}

function initEmailSettings() {
  $('#email-form').addEventListener('submit', saveEmailSettings);
  $('#email-security').addEventListener('change', updateEmailPort);
  $('#email-password-clear').addEventListener('click', removeEmailPassword);
  $('#email-use-origin').addEventListener('click', useThisAddress);
  $('#email-test').addEventListener('click', testEmail);
}
