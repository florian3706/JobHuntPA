/* Import from an export (Settings tab, #account-import-slot): brings workspaces back from a zip that "Download your
   data" made (backend/data_import.py, docs/design/open-source.md §1.7).

   Two steps, each a POST of the zip itself (never a link), with the CSRF token and the password entered within 15
   minutes (withRecentAuth): "Look inside" lists the workspaces in the export with their counts; the person ticks the
   ones to bring in and imports them. Each becomes a new workspace. Everything the server sends back comes from
   someone else's file, so it is written into the page escaped (esc) or as text. Progress and results are said in a
   status region (setText, WCAG 4.1.3); a missing file is a field error (setFieldError). */
'use strict';

async function postZip(url, file) {
  const csrf = (SESSION.me || await loadSession())?.csrf || '';
  const res = await fetch(url, { method: 'POST', headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/zip' }, body: file });
  if (res.status === 401) return signedOut();
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(typeof data.detail === 'string' ? data.detail : `Error ${res.status}`);
    err.status = res.status;
    err.code = data.code;
    throw err;
  }
  return data;
}

function plural(n, one, many = `${one}s`) { return `${n} ${n === 1 ? one : many}`; }

function restoreCounts(w) {
  return [plural(w.jobs, 'job'), plural(w.documents, 'document'), plural(w.chats, 'chat'),
    plural(w.cover_letters, 'cover letter'), plural(w.sources, 'source')].join(', ');
}

function initRestoreCard() {
  const slot = document.getElementById('account-import-slot');
  if (!slot) return;
  slot.innerHTML = `<div class="card form-card" id="restore-card">
    <h3 id="restore-title">Import from an export</h3>
    <p>Bring workspaces back from a zip file that <strong>Download your data</strong> made, here or on another
      JobHuntPA. Each workspace you pick becomes a new workspace with its search setup, sources, documents, jobs, cover
      letters and chats: nothing is merged into the workspaces you have. A name you already use gets "(imported" and
      today's date added.</p>
    <p class="hint">Company research from the file is only used where this JobHuntPA has none, or older. The file stays
      on your computer until you choose to look inside it. You may be asked for your password first.</p>
    <div class="field">
      <label for="restore-file">Export file (.zip)</label>
      <input id="restore-file" type="file" accept=".zip,application/zip" />
    </div>
    <div class="toolbar-row">
      <button id="restore-check" class="btn" type="button">Look inside</button>
      <span id="restore-status" class="muted" role="status"></span>
    </div>
    <div id="restore-pick" hidden>
      <fieldset class="form-group restore-list">
        <legend id="restore-legend">Workspaces in this export</legend>
        <div id="restore-options" class="restore-options"></div>
      </fieldset>
      <div class="toolbar-row">
        <button id="restore-go" class="btn btn-primary" type="button">Import the ticked workspaces</button>
      </div>
    </div>
  </div>`;
  const input = document.getElementById('restore-file');
  const check = document.getElementById('restore-check');
  const go = document.getElementById('restore-go');
  const status = document.getElementById('restore-status');
  const pick = document.getElementById('restore-pick');
  const options = document.getElementById('restore-options');
  let checked = null; // the file the list below belongs to

  const say = (text) => setText(status, text); // (a status region: setText says it once)
  const failed = (e, what) => {
    if (e.code === 'reauth_required') return say(`Nothing was ${what}: confirm your password to go on.`);
    return say(e.message || `The file could not be ${what}.`);
  };

  input.addEventListener('change', () => {
    setFieldError(input, '');
    pick.hidden = true;
    options.innerHTML = '';
    checked = null;
    setText(status, '');
  });

  check.addEventListener('click', async () => {
    const file = input.files[0];
    if (!file) { setFieldError(input, 'Choose the .zip file that Download your data saved first.'); return; }
    setBusy(check, true);
    say('Reading the file…');
    try {
      const found = await withRecentAuth(() => postZip('/api/account/import/check', file));
      checked = file;
      options.innerHTML = found.workspaces.map((w) => `<label class="check restore-option">
          <input type="checkbox" value="${Number(w.index)}" checked />
          <span><strong>${esc(w.name)}</strong> <span class="muted">${esc(restoreCounts(w))}</span></span></label>`).join('');
      pick.hidden = false;
      const made = found.exported_at ? ` made on ${fmtDate(found.exported_at)}` : '';
      say(`This export${made} holds ${plural(found.workspaces.length, 'workspace')}. Tick the ones to import.`);
      options.querySelector('input')?.focus();
    } catch (e) {
      pick.hidden = true;
      failed(e, 'read');
    }
    setBusy(check, false);
  });

  go.addEventListener('click', async () => {
    const ticked = Array.from(options.querySelectorAll('input:checked')).map((box) => box.value);
    if (!ticked.length) { say('Tick at least one workspace to import.'); options.querySelector('input')?.focus(); return; }
    if (!checked) { say('Choose the file again and look inside it first.'); return; }
    setBusy(go, true);
    say('Importing. This can take a minute for a lot of jobs…');
    try {
      const done = await withRecentAuth(() => postZip(`/api/account/import?pick=${encodeURIComponent(ticked.join(','))}`, checked));
      const names = done.workspaces.map((w) => w.name).join(', ');
      say(`Imported ${plural(done.workspaces.length, 'workspace')}: ${names}. Pick one in the workspace menu to open it.`);
      pick.hidden = true;
      options.innerHTML = '';
      input.value = '';
      checked = null;
      if (typeof loadWorkspaces === 'function') loadWorkspaces().catch(() => {});
    } catch (e) {
      failed(e, 'imported');
    }
    setBusy(go, false);
  });
}

document.addEventListener('DOMContentLoaded', initRestoreCard);
