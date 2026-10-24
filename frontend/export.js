/* Download your data (Settings tab, #account-export-slot): a zip of folders of text files made on the server from the
   signed-in account's own data (backend/export.py), in the file types ticked here (Markdown, Word, OpenDocument: at
   least one), with the import file that "Import from an export" (restore.js) reads. The file is not encrypted, and the
   card says so.

   The download is a POST (a link must never start an export) that needs the password entered within 15 minutes, so it
   goes through withRecentAuth. The zip comes back as a Blob and is saved through an object URL; the progress and the
   result are said in a status region (WCAG 4.1.3). */
'use strict';

const EXPORT_TYPES = [['md', 'Markdown (.md)'], ['docx', 'Word (.docx)'], ['odt', 'OpenDocument (.odt)']];

async function fetchExport(formats) {
  const csrf = (SESSION.me || await loadSession())?.csrf || '';
  const res = await fetch('/api/account/export', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf, Accept: 'application/zip', 'Content-Type': 'application/json' },
    body: JSON.stringify({ formats }),
  });
  if (res.status === 401) return signedOut();
  if (!res.ok) {
    let data = {};
    try { data = await res.json(); } catch { /* not JSON */ }
    const err = new Error(typeof data.detail === 'string' ? data.detail : `Error ${res.status}`);
    err.status = res.status;
    err.code = data.code;
    throw err;
  }
  const named = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '');
  return { blob: await res.blob(), name: named ? named[1] : 'JobHuntPA-export.zip' };
}

function initExportCard() {
  const slot = document.getElementById('account-export-slot');
  if (!slot) return;
  slot.innerHTML = `<div class="p-card u-stack" id="export-card">
    <h3 id="export-title">Download your data</h3>
    <p>Get a zip file with everything JobHuntPA keeps for your account, as files in folders you can read in a text
      editor or a word processor: your account details, each workspace's search setup, sources, documents (and the original
      files you uploaded), jobs sorted by your status with their fit scores and cover letters, your chats, search
      history and the company research behind your jobs.</p>
    <p><strong>It is your safety net.</strong> If you lose your password and every recovery code, the reset tool on the
      computer JobHuntPA runs on starts over and deletes your data: a recent copy from here is what you keep.</p>
    <p class="hint">The file is <strong>not encrypted</strong>, unlike your data in JobHuntPA. Keep it somewhere safe and
      delete it when you no longer need it. You may be asked for your password first.</p>
    <fieldset class="form-group export-types" aria-describedby="export-types-hint">
      <legend>File types</legend>
      ${EXPORT_TYPES.map(([value, label], n) => `<label class="check"><input type="checkbox" id="export-type-${value}" value="${value}"${n === 0 ? ' checked' : ''} /> ${label}</label>`).join('')}
    </fieldset>
    <p id="export-types-hint" class="hint">Each text file comes once in every type you tick. The files you uploaded come
      as they are, and so does the import file that <strong>Import from an export</strong> reads to bring your data back.</p>
    <p id="export-types-error" class="field-error" hidden></p>
    <div class="toolbar-row">
      <button id="export-btn" class="p-button p-button--positive" type="button">Download my data (.zip)</button>
      <span id="export-status" class="muted" role="status"></span>
    </div>
  </div>`;
  const btn = document.getElementById('export-btn');
  const status = document.getElementById('export-status');
  const boxes = EXPORT_TYPES.map(([value]) => document.getElementById(`export-type-${value}`));
  const typesError = document.getElementById('export-types-error');
  const fieldset = slot.querySelector('.export-types');
  boxes.forEach((box) => box.addEventListener('change', () => {
    if (!boxes.some((b) => b.checked)) return;
    typesError.hidden = true;
    fieldset.removeAttribute('aria-invalid');
    fieldset.setAttribute('aria-describedby', 'export-types-hint');
  }));
  btn.addEventListener('click', async () => {
    const formats = boxes.filter((b) => b.checked).map((b) => b.value);
    if (!formats.length) {
      typesError.innerHTML = '<span aria-hidden="true">⚠ </span>Tick at least one file type.';
      typesError.hidden = false;
      fieldset.setAttribute('aria-invalid', 'true');
      fieldset.setAttribute('aria-describedby', 'export-types-error export-types-hint');
      announce('Tick at least one file type.', true);
      boxes[0].focus();
      return;
    }
    setBusy(btn, true);
    setText(status, 'Making your file. This can take a minute for a lot of jobs…');
    try {
      const { blob, name } = await withRecentAuth(() => fetchExport(formats));
      saveBlob(blob, name);
      setText(status, `Done. Your browser saved ${name} (${fmtBytes(blob.size)}).`);
    } catch (e) {
      setText(status, e.code === 'reauth_required' ? 'Nothing was downloaded: confirm your password to get your data.'
        : e.status === 429 || e.status === 413 || e.status === 503 ? e.message : `Your data could not be downloaded: ${e.message}`);
    }
    setBusy(btn, false);
  });
}

document.addEventListener('DOMContentLoaded', initExportCard);
