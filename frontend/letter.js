/* The cover letter window, shared by the dashboard (app.js) and the job page (job.js).

   It opens in the page's dialog, widened (.modal--letter): the dashboard's #modal, the job page's #letter-dialog.
   Left: the instructions with Redraft, the letter with its word count and saved state, then Save edits, Copy,
   Download (a menu: PDF, .odt, .docx; the ones the template can't give are disabled and say why), Print (the PDF in
   a new tab) and "Discard my edits, back to the draft" (the model's draft is kept as draft_text). Right rail: the
   template in use (from /api/documents/letter-template), the versions, and "Mark this job as applied when I
   download". Downloads are fetched (not plain links) so the window can read the fonts the PDF substituted and set
   the status afterwards. Uses common.js (api, esc, toast, announce, setBusy, fmtDate, saveBlob) and the page's
   currentWorkspace(). */
const LETTER_FORMATS = [['pdf', 'PDF', '.pdf', 'What most forms want'], ['odt', 'OpenDocument', '.odt', 'Editable'],
  ['docx', 'Word', '.docx', 'Editable']];
const wordCount = (t) => ((t || '').trim().match(/\S+/g) || []).length;
const CHEVRON = '<svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6l4 4 4-4"></path></svg>';

async function fetchFile(url) { // a download: its bytes, file name and what the server says about it
  const ws = currentWorkspace();
  const res = await fetch(url, { headers: ws ? { 'X-Workspace': String(ws) } : {} });
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch { detail = ''; }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const name = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '')?.[1] || 'Cover letter';
  let substitutions = [];
  try { substitutions = JSON.parse(res.headers.get('X-Font-Substitutions') || '[]'); } catch { substitutions = []; }
  return { blob, name, substitutions };
}

// "The template asks for Cambria and Garamond, which this computer does not have; the PDF uses Liberation Serif for them."
function fontNote(substitutions, template = true) {
  const byUsed = new Map();
  substitutions.forEach((s) => byUsed.set(s.used, [...(byUsed.get(s.used) || []), s.asked]));
  const list = (names) => names.join(names.length > 2 ? ', ' : ' and ').replace(/, ([^,]*)$/, ' and $1');
  return Array.from(byUsed, ([used, asked]) => `The ${template ? 'template' : 'letter'} asks for ${list(asked)}, which this computer `
    + `does not have; the PDF uses ${used}${asked.length > 1 ? ' for them' : ''}.`).join(' ');
}

// host: { dialog, open(title, html), close(), docsHref, onChange() }: the page's dialog, how it fills, opens and
// closes it, where the Documents tab is (for "Change" the template), and what to redraw when the letter or the job's
// status changes.
async function openLetterWindow(job, host) {
  let letter = null;
  try { letter = await api(`/api/jobs/${job.id}/cover-letter`); } catch { letter = null; }
  let options = null; // the template in use and the formats it gives (loaded below)
  const dlg = host.dialog;
  dlg.classList.add('modal--letter');
  const formatItems = LETTER_FORMATS.map(([fmt, name, ext, note]) => `
    <li role="none"><button class="cl-menu-item" role="menuitem" type="button" tabindex="-1" data-fmt="${fmt}" aria-disabled="true">
      <span class="cl-menu-item__name"><strong>${name}</strong> <span class="u-small u-muted">${ext}</span></span>
      <span class="cl-menu-item__note u-small u-muted" data-note="${fmt}">${note}</span></button></li>`).join('');
  host.open(`Cover letter: ${job.title} at ${job.company}`, `
    <div class="cl-window">
      <div class="cl-main">
        <div class="cl-instructions">
          <div class="p-field"><label class="p-label" for="cl-instructions">Extra instructions for the draft (optional)</label>
            <input id="cl-instructions" class="p-input" type="text" maxlength="2000" autocomplete="off" value="${esc(letter?.instructions || '')}" placeholder="e.g. Mention I can start in two weeks. Keep it under 300 words."></div>
          <button id="cl-draft" class="p-button p-button--positive" type="button">${letter ? 'Redraft' : 'Draft cover letter'}</button>
        </div>
        <p id="cl-status" class="u-small cl-status" role="status"></p>
        <div class="p-field">
          <div class="cl-letter-head"><label class="p-label" for="cl-text">The letter</label><span id="cl-state" class="u-small u-muted" role="status"></span></div>
          <textarea id="cl-text" class="p-textarea cl-textarea" rows="17" placeholder="Your draft appears here. You can edit it before saving.">${esc(letter?.text || '')}</textarea>
        </div>
        <div class="cl-actions">
          <div class="u-row cl-actions-main">
            <button id="cl-save" class="p-button p-button--positive" type="button">Save edits</button>
            <button id="cl-copy" class="p-button" type="button">Copy<span class="u-sr"> the letter</span></button>
            <div class="cl-menu-wrap">
              <button id="cl-download" class="p-button cl-download" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="cl-menu">Download ${CHEVRON}</button>
              <ul id="cl-menu" class="cl-menu" role="menu" aria-label="Download as" hidden>${formatItems}</ul>
            </div>
            <button id="cl-print" class="p-button p-button--base" type="button">Print</button>
          </div>
          <button id="cl-discard" class="p-button p-button--base" type="button">Discard my edits, back to the draft</button>
        </div>
        <div class="p-notification p-notification--information" role="note">
          <svg class="p-notification__icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v6M12 7.5v.5"></path></svg>
          <div class="p-notification__body u-small">Drafts use only facts from your documents, but always check names, dates and numbers before sending.
            Upload earlier cover letters as "Cover letter" documents and drafts will follow your style.</div>
        </div>
      </div>
      <aside class="cl-rail u-stack" aria-label="About this draft">
        <section class="u-stack cl-section" aria-labelledby="cl-tpl-h">
          <h3 id="cl-tpl-h" class="cl-h">Template</h3>
          <div id="cl-template" class="u-small u-stack cl-template"><p class="u-muted">Looking up the template…</p></div>
        </section>
        <section class="u-stack cl-section" aria-labelledby="cl-ver-h">
          <h3 id="cl-ver-h" class="cl-h">Versions</h3>
          <ul id="cl-versions" class="p-list p-list--divided u-small cl-versions"></ul>
          <div id="cl-draft-view" class="cl-draft-view" hidden>
            <label class="p-label" for="cl-draft-text">The draft as the model wrote it</label>
            <textarea id="cl-draft-text" class="p-textarea cl-textarea cl-textarea--draft" rows="12" readonly></textarea>
          </div>
        </section>
        <section class="u-stack cl-section" aria-labelledby="cl-next-h">
          <h3 id="cl-next-h" class="cl-h">Next</h3>
          <label class="p-check"><input id="cl-applied" class="p-checkbox" type="checkbox"> Mark this job as applied when I download</label>
          <p class="u-small u-muted">Download offers PDF first, then .odt and .docx. Arrow keys move through the menu; Escape closes it.</p>
        </section>
      </aside>
    </div>`);
  const text = () => $('#cl-text').value;
  const saved = () => letter?.text ?? '';
  const dirty = () => (letter ? text() !== letter.text : text().trim() !== '');

  const renderState = () => {
    const n = wordCount(text());
    let label = '';
    if (dirty()) label = '<span class="p-status-label p-status-label--caution">Unsaved changes</span>';
    else if (letter) label = '<span class="p-status-label p-status-label--positive">Saved</span>';
    const when = letter ? `${letter.edited ? 'Edited' : 'Drafted'} ${esc(fmtDate(letter.updated_at))}. ` : '';
    $('#cl-state').innerHTML = `${n} word${n === 1 ? '' : 's'}. ${when}${label}`;
    const canDiscard = Boolean(letter?.draft_text) && text() !== letter.draft_text;
    $('#cl-discard').setAttribute('aria-disabled', canDiscard ? 'false' : 'true');
    $('#cl-discard').title = letter?.draft_text ? '' : letter ? 'This letter was drafted before drafts were kept' : 'No draft yet';
  };

  const renderVersions = () => {
    const items = [];
    if (letter?.edited) items.push(`<li class="u-stack cl-version"><strong>Edited by you</strong><span class="u-muted">${esc(fmtDate(letter.updated_at))}. Current.</span></li>`);
    if (letter?.model || letter?.created_at) {
      const effort = letter.reasoning_effort ? `, reasoning ${esc(letter.reasoning_effort)}` : '';
      const words = letter.draft_text ? `, ${wordCount(letter.draft_text)} words` : '';
      const view = letter.draft_text ? `<button id="cl-view-draft" class="p-button p-button--base is-dense cl-view-draft" type="button" aria-expanded="false" aria-controls="cl-draft-view">View the draft</button>` : '';
      items.push(`<li class="u-stack cl-version"><strong>Drafted by ${esc(letter.model || 'the model')}</strong><span class="u-muted">${esc(fmtDate(letter.created_at))}${effort}${words}.${letter.edited ? '' : ' Current.'}</span>${view}</li>`);
    } else if (letter) {
      items.push('<li class="u-stack cl-version"><strong>Written by you</strong><span class="u-muted">No draft from the model yet.</span></li>');
    } else {
      items.push('<li class="u-muted">No draft yet. Draft cover letter writes one from your documents.</li>');
    }
    $('#cl-versions').innerHTML = items.join('');
    $('#cl-draft-text').value = letter?.draft_text || '';
    $('#cl-view-draft')?.addEventListener('click', (e) => {
      const open = $('#cl-draft-view').hidden;
      $('#cl-draft-view').hidden = !open;
      e.currentTarget.setAttribute('aria-expanded', String(open));
      e.currentTarget.textContent = open ? 'Hide the draft' : 'View the draft';
    });
  };

  const renderTemplate = (note = '') => {
    const box = $('#cl-template');
    if (!options) { box.innerHTML = '<p class="u-muted">The template could not be looked up.</p>'; return; }
    const font = options.letter_font ? `${options.letter_font.family} ${Number(options.letter_font.size).toLocaleString(undefined, { maximumFractionDigits: 1 })} pt` : '';
    const change = `<a class="cl-change" href="${host.docsHref}">Change<span class="u-sr"> the template on the Documents tab</span></a>`;
    let html;
    if (options.template) {
      html = `<p><strong>${esc(options.template.filename)}</strong>: your header, footer and signature; the letter goes where its {{letter}} marker is${font ? `, in that paragraph's font (${esc(font)})` : ''}. ${change}</p>`;
      const missing = (options.fonts || []).filter((f) => !f.found);
      if (missing.length) html += `<p class="cl-fonts" role="note">${esc(fontNote(missing))}</p>`;
      if (options.problem) html += `<p class="err-text">${esc(options.problem)}</p>`;
      html += '<p class="u-muted">A .docx template gives .docx and PDF; an .odt template gives .odt and PDF.</p>';
    } else {
      html = `<p><strong>Plain letter</strong>: A4, one inch margins${font ? `, ${esc(font)}` : ''}. ${change}</p>
        <p class="u-muted">To use your own letterhead, upload a .docx or .odt with a {{letter}} marker as a "Letter template" on the Documents tab.</p>`;
    }
    if (note) html += `<p class="cl-fonts" role="status">${esc(note)}</p>`;
    box.innerHTML = html;
    box.querySelector('.cl-change').addEventListener('click', () => host.close()); // the tab shows behind the dialog
    // the menu: which formats this template gives
    $$('#cl-menu [data-fmt]').forEach((item) => {
      const fmt = item.dataset.fmt;
      const f = options.formats?.[fmt];
      const ok = Boolean(letter) && Boolean(f?.available);
      item.setAttribute('aria-disabled', ok ? 'false' : 'true');
      const [, , , note2] = LETTER_FORMATS.find((x) => x[0] === fmt);
      const short = options.template ? `Not with a ${options.template.filetype} template` : 'Not available';
      item.querySelector(`[data-note="${fmt}"]`).textContent = !letter ? 'Draft or write the letter first' : f?.available ? note2 : short;
      item.title = !letter || f?.available ? '' : f?.reason || '';
    });
  };

  const loadOptions = async () => {
    try { options = await api('/api/documents/letter-template'); } catch { options = null; }
    renderTemplate();
  };

  const apply = (l) => {
    letter = l;
    renderState();
    renderVersions();
    renderTemplate();
    job.has_cover_letter = true;
    host.onChange();
  };

  const save = async () => {
    const l = await api(`/api/jobs/${job.id}/cover-letter`, { method: 'PUT', body: JSON.stringify({ text: text() }) });
    apply(l);
    return l;
  };

  renderState();
  renderVersions();
  loadOptions();
  $('#cl-text').addEventListener('input', renderState);

  $('#cl-draft').addEventListener('click', async () => {
    const btn = $('#cl-draft');
    if (text().trim() && !confirm('Replace the current letter with a new draft?')) return;
    setBusy(btn, true);
    $('#cl-status').textContent = 'Drafting… this can take a minute.';
    try {
      const l = await api(`/api/jobs/${job.id}/cover-letter`, { method: 'POST', body: JSON.stringify({ instructions: $('#cl-instructions').value }) });
      $('#cl-text').value = l.text;
      $('#cl-status').textContent = '';
      btn.textContent = 'Redraft';
      apply(l);
    } catch (e) { $('#cl-status').textContent = `Failed: ${e.message}`; }
    setBusy(btn, false);
  });
  $('#cl-save').addEventListener('click', async () => {
    try { await save(); toast('Cover letter saved', 'ok', 2000); }
    catch (e) { toast(`Save failed: ${e.message}`, 'err'); }
  });
  $('#cl-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text()); toast('Copied', 'ok', 1500); }
    catch { $('#cl-text').select(); toast('Press Ctrl+C to copy', '', 2500); }
  });
  $('#cl-discard').addEventListener('click', async (e) => {
    if (e.currentTarget.getAttribute('aria-disabled') === 'true') return;
    if (!confirm("Discard your edits and go back to the model's draft?")) return;
    $('#cl-text').value = letter.draft_text;
    try { await save(); toast('Back to the draft', 'ok', 2000); }
    catch (err) { toast(`Could not save: ${err.message}`, 'err'); }
  });

  // Downloads: save first when there are unsaved edits, fetch the file, then the "applied" status if asked.
  const download = async (fmt) => {
    if (!letter && !text().trim()) { toast('Draft or write the letter first', 'err'); return; }
    try {
      if (dirty()) await save();
      const { blob, name, substitutions } = await fetchFile(`/api/jobs/${job.id}/cover-letter.${fmt}`);
      saveBlob(blob, name);
      // fonts the PDF stood in that the Template section doesn't already say (the plain layout never substitutes)
      const known = new Set((options?.fonts || []).filter((f) => !f.found).map((f) => f.asked));
      const note = fontNote(substitutions.filter((s) => !known.has(s.asked)), Boolean(options?.template));
      renderTemplate(note);
      toast(`Downloaded ${name}`, 'ok', 2500);
      if (note) announce(note);
      if ($('#cl-applied').checked && job.status !== 'applied') {
        const res = await api(`/api/jobs/${job.id}/status`, { method: 'PATCH', body: JSON.stringify({ status: 'applied' }) });
        Object.assign(job, res && typeof res === 'object' && 'status' in res ? res : { status: 'applied' });
        host.onChange();
        toast('Marked as applied', 'ok', 2000);
      }
    } catch (e) { toast(`Download failed: ${e.message}`, 'err'); }
  };

  // The Download menu (ARIA menu button): arrow keys, Home/End, Escape; closes on a click elsewhere or Tab.
  const button = $('#cl-download');
  const menu = $('#cl-menu');
  const items = () => $$('#cl-menu [role="menuitem"]');
  const openMenu = (focusLast = false) => {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    const list = items();
    (focusLast ? list[list.length - 1] : list[0]).focus();
    menu.scrollIntoView({ block: 'nearest' }); // the dialog scrolls when the menu opens near its bottom
  };
  const closeMenu = (refocus = true) => {
    if (menu.hidden) return;
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (refocus) button.focus();
  };
  button.addEventListener('click', () => (menu.hidden ? openMenu() : closeMenu()));
  button.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); openMenu(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); openMenu(true); }
  });
  menu.addEventListener('keydown', (e) => {
    const list = items();
    const i = list.indexOf(document.activeElement);
    const go = (n) => { e.preventDefault(); list[(n + list.length) % list.length].focus(); };
    if (e.key === 'ArrowDown') go(i + 1);
    else if (e.key === 'ArrowUp') go(i - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(list.length - 1);
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(); }
    else if (e.key === 'Tab') closeMenu(false);
  });
  menu.addEventListener('click', (e) => {
    const item = e.target.closest('[role="menuitem"]');
    if (!item) return;
    if (item.getAttribute('aria-disabled') === 'true') { announce(item.title || item.querySelector('.cl-menu-item__note').textContent); return; }
    closeMenu();
    download(item.dataset.fmt);
  });
  const outside = (e) => { if (!menu.hidden && !e.target.closest('.cl-menu-wrap')) closeMenu(false); };
  document.addEventListener('click', outside);
  dlg.addEventListener('close', () => { document.removeEventListener('click', outside); dlg.classList.remove('modal--letter'); }, { once: true });

  $('#cl-print').addEventListener('click', async () => {
    if (!letter && !text().trim()) { toast('Draft or write the letter first', 'err'); return; }
    const win = window.open('', '_blank'); // opened on the click itself, so the browser allows it
    try {
      if (dirty()) await save();
      const { blob } = await fetchFile(`/api/jobs/${job.id}/cover-letter.pdf`);
      const url = URL.createObjectURL(blob);
      if (win) win.location = url; else saveBlob(blob, 'Cover letter.pdf');
      toast('The PDF is open in a new tab: print it from there', '', 4000);
    } catch (e) { win?.close(); toast(`Could not make the PDF: ${e.message}`, 'err'); }
  });
}
