/* The cover letter window, shared by the dashboard (app.js) and the job page (job.js).

   It opens in the page's dialog, widened (.modal--letter): the #modal of the dashboard and of the job page.
   Left: the instructions with Redraft, the letter with its word count and saved state, then Save edits, Copy,
   Download (a menu: PDF, .odt, .docx; the ones the template can't give are disabled and say why), Print (the PDF in
   a new tab) and "Discard my edits, back to the draft" (the model's draft is kept as draft_text). Right rail: the
   template in use (from /api/documents/letter-template), the versions, and "Mark this job as applied when I
   download". Downloads are fetched (not plain links) so the window can read the fonts the PDF substituted and set
   the status afterwards. Uses common.js (api, apiResponse, esc, toast, announce, setBusy, fmtDate, saveBlob,
   rememberFocus, restoreFocus) and the page's currentWorkspace().

   The other way to a letter is "Build from my blocks" (initLetterBlocks): the AI suggests which of the user's own
   paragraphs (cover letter blocks, backend/letter_blocks.py) fit this job and in what order, and which blocks are
   worth writing; the user ticks, orders and writes them, and Assemble joins them word for word. The block helpers
   below are shared with the Documents tab's blocks card (app.js). */
const LETTER_FORMATS = [['pdf', 'PDF', '.pdf', 'What most forms want'], ['odt', 'OpenDocument', '.odt', 'Editable'],
  ['docx', 'Word', '.docx', 'Editable']];
const wordCount = (t) => ((t || '').trim().match(/\S+/g) || []).length;
const CHEVRON = '<svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6l4 4 4-4"></path></svg>';

// A download: its bytes, file name and what the server says about it. Through apiResponse, so a signed-out session
// goes to the sign-in page and the encryption notice shows, as for every other call.
async function fetchFile(url) {
  const { data: blob, headers } = await apiResponse(url, { blob: true });
  const name = /filename="([^"]+)"/.exec(headers.get('Content-Disposition') || '')?.[1] || 'Cover letter';
  let substitutions = [];
  try { substitutions = JSON.parse(headers.get('X-Font-Substitutions') || '[]'); } catch { substitutions = []; }
  return { blob, name, substitutions };
}

/* ---------- cover letter blocks: shared by the Documents tab (app.js) and Build from my blocks ----------
   A block has a kind (openings first, closings last), a title, tags and its text, which may hold {company}, {role},
   {location} and {date}. The form's fields come from blockFormHtml(); `p` prefixes their ids (one form on the
   Documents tab, one in the cover letter window). */
const BLOCK_KINDS = [['opening', 'Opening'], ['body', 'Body'], ['closing', 'Closing']];
const BLOCK_KIND_LABEL = Object.fromEntries(BLOCK_KINDS);
const BLOCK_PLACEHOLDERS = '{company}, {role}, {location} and {date}';
const BLOCK_TAG_MAX = 60;
const BLOCK_TAGS_MAX = 30;
const blockKindIndex = (kind) => Math.max(0, BLOCK_KINDS.findIndex(([k]) => k === kind));
const sortBlocks = (list) => list.slice().sort((a, b) => blockKindIndex(a.kind) - blockKindIndex(b.kind) || a.position - b.position || a.id - b.id);
const blockPreview = (text, n = 120) => { const t = String(text || '').replace(/\s+/g, ' ').trim(); return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t; };
const blockName = (b) => b.title || blockPreview(b.text, 60) || 'Untitled block';
const unknownPlaceholdersText = (names) => (names?.length
  ? `Unknown placeholder${names.length === 1 ? '' : 's'}: ${names.map((n) => `{${n}}`).join(', ')} — only ${BLOCK_PLACEHOLDERS} are filled in.` : '');

// The fields of a block's form: title, kind, tags and text. With `bullets`, the evidence the AI found in the user's
// documents sits beside the text field (.block-write; one above the other on a phone).
function blockFormHtml(p, { title = '', kind = 'body', tags = '', text = '', bullets = null } = {}) {
  const tagText = Array.isArray(tags) ? tags.join(', ') : tags;
  const textField = `<div class="p-field block-text-field"><label class="p-label" for="${p}-text">Text</label>
      <textarea id="${p}-text" class="p-textarea doc-textarea" rows="${bullets ? 9 : 6}" maxlength="8000" data-block="text" data-focus="${p}-text">${esc(text)}</textarea></div>`;
  return `
    <div class="note-grid">
      <div class="p-field"><label class="p-label" for="${p}-title">Title</label>
        <input id="${p}-title" class="p-input" type="text" maxlength="200" autocomplete="off" data-block="title" data-focus="${p}-title" value="${esc(title)}" /></div>
      <div class="p-field"><label class="p-label" for="${p}-kind">Part of the letter</label>
        <select id="${p}-kind" class="p-select" data-block="kind" data-focus="${p}-kind">${BLOCK_KINDS.map(([v, l]) => `<option value="${v}"${v === kind ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></div>
      <div class="p-field"><label class="p-label" for="${p}-tags">Tags (comma-separated)</label>
        <input id="${p}-tags" class="p-input" type="text" autocomplete="off" data-block="tags" data-focus="${p}-tags" value="${esc(tagText)}" /></div>
    </div>
    ${bullets ? `<div class="block-write">
      <div class="block-bullets"><h4 class="block-bullets-title">From your documents</h4>
        ${bullets.length ? `<ul>${bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : '<p class="u-muted">Nothing found in your documents for this one.</p>'}</div>
      ${textField}
    </div>` : textField}`;
}

// What a block form holds, as typed (kept while the window's panel is drawn again).
function blockFormValues(root) {
  const field = (name) => root.querySelector(`[data-block="${name}"]`);
  return { title: field('title').value, kind: field('kind').value, tags: field('tags').value, text: field('text').value };
}

// The block in a form made by blockFormHtml(), or null (with the fields' errors shown) when it can't be saved.
function readBlockForm(root) {
  const field = (name) => root.querySelector(`[data-block="${name}"]`);
  const text = field('text').value;
  const tags = field('tags').value.split(',').map((t) => t.trim()).filter(Boolean);
  setFieldError(field('text'), '');
  setFieldError(field('tags'), '');
  let ok = true;
  const longTag = tags.find((t) => t.length > BLOCK_TAG_MAX);
  if (longTag || tags.length > BLOCK_TAGS_MAX) {
    setFieldError(field('tags'), longTag ? `A tag can be up to ${BLOCK_TAG_MAX} characters: shorten "${blockPreview(longTag, 30)}".`
      : `Up to ${BLOCK_TAGS_MAX} tags a block.`, { focus: false });
    ok = false;
  }
  if (!text.trim()) {
    setFieldError(field('text'), 'Write the paragraph here: a block needs some text.', { focus: false });
    ok = false;
  }
  if (!ok) { root.querySelector('[aria-invalid="true"]')?.focus(); return null; }
  return { title: field('title').value.trim(), kind: field('kind').value, tags, text };
}

// Creates (no id) or changes a block: the server's answer (with unknown_placeholders).
function saveLetterBlock(payload, id = null) {
  return api(id ? `/api/letter-blocks/${id}` : '/api/letter-blocks', { method: id ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
}

// A tag from a requirement the AI named: tags are split at commas and hold up to BLOCK_TAG_MAX characters.
function tagFromRequirement(req) {
  const t = String(req || '').replace(/,/g, ' ').replace(/\s+/g, ' ').trim();
  if (t.length <= BLOCK_TAG_MAX) return t;
  const cut = t.slice(0, BLOCK_TAG_MAX);
  return (cut.lastIndexOf(' ') > 20 ? cut.slice(0, cut.lastIndexOf(' ')) : cut).trim();
}

// "The template asks for Cambria and Garamond, which this computer does not have; the PDF uses Liberation Serif for them."
function fontNote(substitutions, template = true) {
  const byUsed = new Map();
  substitutions.forEach((s) => byUsed.set(s.used, [...(byUsed.get(s.used) || []), s.asked]));
  const list = (names) => names.join(names.length > 2 ? ', ' : ' and ').replace(/, ([^,]*)$/, ' and $1');
  return Array.from(byUsed, ([used, asked]) => `The ${template ? 'template' : 'letter'} asks for ${list(asked)}, which this computer `
    + `does not have; the PDF uses ${used}${asked.length > 1 ? ' for them' : ''}.`).join(' ');
}

// host: { dialog, open(title, html), close(), docsHref, onChange(), onBlocksChanged?() }: the page's dialog, how it
// fills, opens and closes it, where the Documents tab is (for "Change" the template and for writing blocks), what to
// redraw when the letter or the job's status changes, and when the user's blocks have changed (the dashboard's
// Documents tab lists them).
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
          <div class="u-row cl-make">
            <button id="cl-draft" class="p-button p-button--positive" type="button">${letter ? 'Redraft' : 'Draft cover letter'}</button>
            <button id="cl-build" class="p-button" type="button">Build from my blocks</button>
          </div>
        </div>
        <p id="cl-status" class="u-small cl-status" role="status"></p>
        <section id="cl-blocks" class="p-card u-stack cl-blocks" aria-labelledby="cl-blocks-title" hidden></section>
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
          <div class="p-notification__body u-small">Two ways to a letter: <strong>Draft cover letter</strong> has the AI write one from your documents,
            reusing your cover letter blocks where they fit; <strong>Build from my blocks</strong> puts together the paragraphs you wrote on the
            Documents tab, word for word, with the AI only choosing and ordering them. Drafts use only facts from your documents, but always
            check names, dates and numbers before sending. Upload earlier cover letters as "Cover letter" documents and drafts will follow your style.</div>
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
    const verb = letter?.edited ? 'Edited' : letter?.source === 'blocks' ? 'Put together' : 'Drafted';
    const when = letter ? `${verb} ${esc(fmtDate(letter.updated_at))}. ` : '';
    $('#cl-state').innerHTML = `${n} word${n === 1 ? '' : 's'}. ${when}${label}`;
    const canDiscard = Boolean(letter?.draft_text) && text() !== letter.draft_text;
    $('#cl-discard').setAttribute('aria-disabled', canDiscard ? 'false' : 'true');
    $('#cl-discard').title = letter?.draft_text ? '' : letter ? 'No draft from the model to go back to' : 'No draft yet';
    renderMenu(); // a letter typed but not saved can be downloaded (download() saves it first)
  };

  const renderVersions = () => {
    // Where the letter came from: the model's draft, the user's blocks, or the user's own typing (no model, no draft).
    const items = [];
    const written = letter && letter.source !== 'blocks' && !letter.model && !letter.draft_text;
    if (letter?.edited && !written) items.push(`<li class="u-stack cl-version"><strong>Edited by you</strong><span class="u-muted">${esc(fmtDate(letter.updated_at))}. Current.</span></li>`);
    if (letter?.source === 'blocks') {
      items.push(`<li class="u-stack cl-version"><strong>Built from your blocks</strong><span class="u-muted">${esc(fmtDate(letter.created_at))}. Your paragraphs, word for word.${letter.edited ? '' : ' Current.'}</span></li>`);
    } else if (written) {
      items.push(`<li class="u-stack cl-version"><strong>Written by you</strong><span class="u-muted">${esc(fmtDate(letter.updated_at))}. Current. No draft from the model.</span></li>`);
    } else if (letter) {
      const effort = letter.reasoning_effort ? `, reasoning ${esc(letter.reasoning_effort)}` : '';
      const words = letter.draft_text ? `, ${wordCount(letter.draft_text)} words` : '';
      const view = letter.draft_text ? `<button id="cl-view-draft" class="p-button p-button--base is-dense cl-view-draft" type="button" aria-expanded="false" aria-controls="cl-draft-view">View the draft</button>` : '';
      items.push(`<li class="u-stack cl-version"><strong>Drafted by ${esc(letter.model || 'the model')}</strong><span class="u-muted">${esc(fmtDate(letter.created_at))}${effort}${words}.${letter.edited ? '' : ' Current.'}</span>${view}</li>`);
    } else {
      items.push('<li class="u-muted">No letter yet. Draft cover letter writes one from your documents; Build from my blocks puts one together from your own paragraphs.</li>');
    }
    $('#cl-versions').innerHTML = items.join('');
    $('#cl-draft-text').value = letter?.draft_text || '';
    if (!letter?.draft_text) $('#cl-draft-view').hidden = true;
    $('#cl-view-draft')?.addEventListener('click', (e) => {
      const open = $('#cl-draft-view').hidden;
      $('#cl-draft-view').hidden = !open;
      e.currentTarget.setAttribute('aria-expanded', String(open));
      e.currentTarget.textContent = open ? 'Hide the draft' : 'View the draft';
    });
  };

  const renderTemplate = (note = '') => {
    const box = $('#cl-template');
    if (!options) { box.innerHTML = '<p class="u-muted">The template could not be looked up.</p>'; renderMenu(); return; }
    const fmtSize = (n) => Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });
    const font = options.letter_font ? `${options.letter_font.family} ${fmtSize(options.letter_font.size)} pt` : '';
    const change = `<a class="cl-change" href="${host.docsHref}">Change the template<span class="u-sr"> on the Documents tab</span></a>`;
    // The fonts a template asks for that this computer lacks matter only when the app draws the PDF itself.
    const missing = options.pdf === 'reportlab' ? (options.fonts || []).filter((f) => !f.found) : [];
    let html;
    if (options.template) {
      html = `<p><strong>${esc(options.template.filename)}</strong>: your header, footer and signature; the letter goes where its {letter} marker is${font ? `, in that paragraph's font (${esc(font)})` : ''}. To change the font, change that paragraph in the template file and upload it again. ${change}</p>
        <p class="u-muted">A .docx template gives .docx and PDF; an .odt template gives .odt and PDF.</p>`;
    } else {
      // The plain layout's font: the families this computer has (empty until the start-up scan has ended), plus
      // whatever is set or in use, so the select always shows the current one. Saved on change (PUT letter-font).
      const current = options.letter_font?.family || '';
      const families = [...new Set([...(options.font_choices || []), current].filter(Boolean))].sort((a, b) => a.localeCompare(b));
      html = `<p><strong>Plain letter</strong>: A4, one inch margins${font ? `, ${esc(font)}` : ''}. ${change}</p>
        <div class="u-row cl-font-row">
          <label class="cl-font-field">Font <select id="cl-font-family" class="p-select">${families.map((f) => `<option value="${esc(f)}"${f === current ? ' selected' : ''}>${esc(f)}</option>`).join('')}</select></label>
          <label class="cl-font-field">Size <input id="cl-font-size" class="p-input cl-font-size" type="number" min="8" max="18" step="0.5" value="${esc(fmtSize(options.letter_font?.size || 11))}" /> pt</label>
          ${options.font_setting ? '<button id="cl-font-default" class="p-button p-button--base is-dense" type="button">Back to the default</button>' : ''}
        </div>
        <p class="u-muted">To use your own letterhead, upload a .docx or .odt with a {letter} marker as a "Letter template" on the Documents tab.</p>`;
    }
    html += options.pdf === 'libreoffice' ? '<p class="u-muted">PDFs are made with LibreOffice.</p>'
      : `<p class="u-muted">PDFs are drawn by the app${missing.length ? '; the fonts list below says what stood in' : ''}.</p>`;
    if (missing.length) html += `<p class="cl-fonts" role="note">${esc(fontNote(missing))}</p>`;
    if (options.problem) html += `<p class="err-text">${esc(options.problem)}</p>`;
    if (note) html += `<p class="cl-fonts" role="status">${esc(note)}</p>`;
    box.innerHTML = html;
    box.querySelector('.cl-change').addEventListener('click', () => host.close()); // the tab shows behind the dialog
    const saveFont = async (body) => {
      try {
        options = await api('/api/documents/letter-font', { method: 'PUT', body: JSON.stringify(body) });
        renderTemplate(body.family === null && body.size === null ? 'The default font again.' : 'Font saved: every download uses it.');
      } catch (e) { renderTemplate(`The font could not be saved: ${e.message}`); }
    };
    const fromForm = () => ({ family: $('#cl-font-family').value || null, size: Number($('#cl-font-size').value) || null });
    $('#cl-font-family')?.addEventListener('change', () => saveFont(fromForm()));
    $('#cl-font-size')?.addEventListener('change', (e) => { if (e.target.checkValidity()) saveFont(fromForm()); });
    $('#cl-font-default')?.addEventListener('click', () => saveFont({ family: null, size: null }));
    renderMenu();
  };

  // The Download menu: which formats this template gives, once there is a letter (saved, or typed and not yet saved:
  // download() saves it first). The same guard as Print.
  function renderMenu() {
    if (!options) return; // (all stay disabled until the template is known)
    const hasText = Boolean(letter) || text().trim() !== '';
    $$('#cl-menu [data-fmt]').forEach((item) => {
      const fmt = item.dataset.fmt;
      const f = options.formats?.[fmt];
      const ok = hasText && Boolean(f?.available);
      item.setAttribute('aria-disabled', ok ? 'false' : 'true');
      const [, , , note2] = LETTER_FORMATS.find((x) => x[0] === fmt);
      const short = options.template ? `Not with a ${options.template.filetype} template` : 'Not available';
      item.querySelector(`[data-note="${fmt}"]`).textContent = !hasText ? 'Draft or write the letter first' : f?.available ? note2 : short;
      item.title = !hasText || f?.available ? '' : f?.reason || '';
    });
  }

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

  // A letter the server made (a draft, or one put together from blocks): into the editor, then everything follows.
  const showLetter = (l) => {
    $('#cl-text').value = l.text;
    $('#cl-draft').textContent = 'Redraft';
    apply(l);
  };

  renderState();
  renderVersions();
  loadOptions();
  $('#cl-text').addEventListener('input', renderState);
  initLetterBlocks(job, host, showLetter);

  $('#cl-draft').addEventListener('click', async () => {
    const btn = $('#cl-draft');
    if (text().trim() && !confirm('Replace the current letter with a new draft?')) return;
    setBusy(btn, true);
    $('#cl-status').textContent = 'Drafting… this can take a minute.';
    try {
      showLetter(await api(`/api/jobs/${job.id}/cover-letter`, { method: 'POST', body: JSON.stringify({ instructions: $('#cl-instructions').value }) }));
      $('#cl-status').textContent = '';
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

/* "Build from my blocks": the panel (#cl-blocks) of one job's cover letter window. `rows` is the checklist in display
   order ({ block, ticked, why }); `missing` the blocks worth writing ({ title, requirement, bullets, draft? }: `draft`
   keeps what was typed while the panel is drawn again); `writing` the one whose form is open. When the AI can't be
   asked (400: no model set up; 502: it failed), the user ticks and orders the blocks themselves. */
function initLetterBlocks(job, host, showLetter) {
  const panel = $('#cl-blocks');
  const status = $('#cl-status');
  let rows = [];
  let missing = [];
  let writing = null;
  let note = ''; // a message at the top of the panel (HTML, built from escaped text)

  const close = () => {
    panel.hidden = true;
    panel.innerHTML = '';
    rows = [];
    missing = [];
    writing = null;
  };

  // What is typed in the open "Write this block" form, kept before the panel is drawn again or another form opens.
  const keepDraft = () => {
    const form = panel.querySelector('#cl-newblock');
    if (form && writing !== null && missing[writing]) missing[writing].draft = blockFormValues(form);
  };

  const render = () => {
    const saved = rememberFocus(panel);
    const checklist = rows.length ? `
      <ul class="p-list p-list--divided cl-block-list" aria-label="Your blocks, in the order the letter takes them">${rows.map((r, i) => {
        const name = blockName(r.block);
        return `<li class="cl-block-row${r.ticked ? ' is-ticked' : ''}">
          <div class="cl-block-pick">
            <label class="p-check"><input class="p-checkbox" type="checkbox" data-pick="${i}" data-focus="pick-${r.block.id}"${r.ticked ? ' checked' : ''} />
              <span><span class="cl-block-name">${esc(name)}</span> <span class="u-small u-muted">(${esc(BLOCK_KIND_LABEL[r.block.kind] || r.block.kind)})</span></span></label>
            ${r.why ? `<p class="u-small u-muted cl-block-why">${esc(r.why)}</p>` : ''}
          </div>
          <div class="u-row cl-block-move">
            <button class="p-button p-button--base is-dense" type="button" data-move="${i}" data-dir="-1" data-focus="up-${r.block.id}" aria-label="Move ${esc(name)} up"${i === 0 ? ' disabled' : ''}><span aria-hidden="true">↑</span></button>
            <button class="p-button p-button--base is-dense" type="button" data-move="${i}" data-dir="1" data-focus="down-${r.block.id}" aria-label="Move ${esc(name)} down"${i === rows.length - 1 ? ' disabled' : ''}><span aria-hidden="true">↓</span></button>
          </div>
        </li>`;
      }).join('')}</ul>` : '';
    const suggestions = missing.length ? `
      <section class="u-stack cl-missing" aria-labelledby="cl-missing-title">
        <h4 id="cl-missing-title" class="cl-h">Blocks worth writing</h4>
        <p class="p-form-help-text">This job asks for these, and none of your blocks covers them yet.</p>
        <ul class="p-list cl-missing-list">${missing.map((m, i) => `<li class="u-stack cl-missing-item">
          <p class="cl-missing-name"><strong>${esc(m.title)}</strong></p>
          <p class="u-small u-muted">${esc(m.requirement)}</p>
          ${writing === i ? `<form id="cl-newblock" class="u-stack cl-newblock" aria-label="Write the block ${esc(m.title)}" novalidate>
            ${blockFormHtml('cl-nb', { ...(m.draft || { title: m.title, kind: 'body', tags: tagFromRequirement(m.requirement) }), bullets: m.bullets || [] })}
            <p class="p-form-help-text">Write it in your own words; the bullets are facts from your documents, not text to copy.</p>
            <div class="u-row">
              <button id="cl-nb-save" class="p-button p-button--positive is-dense" type="submit" data-focus="nb-save">Save block</button>
              <button id="cl-nb-skip" class="p-button is-dense" type="button" data-focus="skip">Skip</button>
              <span id="cl-nb-status" class="u-small u-muted" role="status"></span>
            </div>
          </form>` : `<button class="p-button is-dense" type="button" data-write="${i}" data-focus="write-${i}">Write this block<span class="u-sr">: ${esc(m.title)}</span></button>`}
        </li>`).join('')}</ul>
      </section>` : '';
    panel.innerHTML = `
      <h3 id="cl-blocks-title" class="cl-h" tabindex="-1">Your blocks for this letter</h3>
      ${note ? `<p class="cl-blocks-note">${note}</p>` : ''}
      ${rows.length ? `<p class="p-form-help-text">Tick the blocks to use and put them in order: they are joined word for word, with ${BLOCK_PLACEHOLDERS} filled in.</p>` : ''}
      ${checklist}
      <div class="u-row">
        ${rows.length ? '<button id="cl-assemble" class="p-button p-button--positive" type="button" data-focus="assemble">Assemble letter</button>' : ''}
        <button id="cl-blocks-cancel" class="p-button" type="button" data-focus="cancel">Cancel</button>
      </div>
      ${suggestions}`;
    panel.hidden = false;
    restoreFocus(panel, saved);
  };

  const open = ({ list, picked = [], unpicked = [], gaps = [], message = '' }) => {
    const byId = new Map(list.map((b) => [b.id, b]));
    const used = new Set();
    const take = (id, ticked, why = '') => {
      const block = byId.get(id);
      if (!block || used.has(id)) return null;
      used.add(id);
      return { block, ticked, why };
    };
    rows = [...picked.map((p) => take(p.id, true, p.why || '')), ...unpicked.map((id) => take(id, false))].filter(Boolean);
    rows.push(...sortBlocks(list.filter((b) => !used.has(b.id))).map((b) => ({ block: b, ticked: false, why: '' }))); // any the answer left out
    missing = gaps.map((g) => ({ ...g }));
    writing = null;
    note = message || (rows.length ? '' : `You have no blocks yet. Write the suggested ones below, or on the <a href="${host.docsHref}" data-act="to-docs">Documents tab</a>.`);
    render();
    $('#cl-blocks-title').focus();
  };

  $('#cl-build').addEventListener('click', async () => {
    const btn = $('#cl-build');
    if ($('#cl-text').value.trim() && !confirm('Replace the current letter with one built from your blocks? It is replaced only when you choose Assemble.')) return;
    setBusy(btn, true);
    status.textContent = 'Choosing blocks…';
    try {
      const res = await api(`/api/jobs/${job.id}/cover-letter/blocks/suggest`, { method: 'POST' });
      status.textContent = '';
      open({ list: res.blocks || [], picked: res.picked || [], unpicked: res.unpicked || [], gaps: res.missing || [] });
    } catch (e) {
      if (e.status === 400 || e.status === 502) {
        // No model to ask (or it failed): the user's own blocks, to tick and order by hand.
        let list = [];
        try { list = await api('/api/letter-blocks'); } catch { list = []; }
        const detail = e.message.replace(/[.\s]+$/, '');
        if (list.length) {
          status.textContent = `${detail}: tick the blocks yourself.`;
          open({ list });
        } else {
          status.textContent = '';
          open({ list: [], message: `${esc(detail)}. Write your blocks on the <a href="${host.docsHref}" data-act="to-docs">Documents tab</a> first.` });
        }
      } else status.textContent = `Failed: ${e.message}`;
    }
    setBusy(btn, false);
  });

  panel.addEventListener('change', (e) => {
    const i = e.target.dataset?.pick;
    if (i === undefined || !rows[i]) return;
    rows[i].ticked = e.target.checked;
    e.target.closest('.cl-block-row').classList.toggle('is-ticked', e.target.checked);
  });

  panel.addEventListener('click', async (e) => {
    const el = e.target.closest('button, a');
    if (!el || !panel.contains(el)) return;
    if (el.dataset.act === 'to-docs') { host.close(); return; } // the link's address then shows the Documents tab
    if (el.dataset.move !== undefined) {
      const i = Number(el.dataset.move);
      const j = i + Number(el.dataset.dir);
      if (j < 0 || j >= rows.length) return;
      [rows[i], rows[j]] = [rows[j], rows[i]];
      const id = rows[j].block.id;
      keepDraft();
      render();
      // At the top or the bottom the button that was pressed is disabled: the other one of the pair takes the focus.
      if (!panel.contains(document.activeElement) || document.activeElement.disabled) {
        panel.querySelector(`[data-focus="${j === 0 ? 'down' : 'up'}-${id}"]`)?.focus();
      }
      announce(`Moved "${blockName(rows[j].block)}" to number ${j + 1} of ${rows.length}.`);
    } else if (el.dataset.write !== undefined) {
      keepDraft();
      writing = Number(el.dataset.write);
      render();
      $('#cl-nb-text').focus();
    } else if (el.id === 'cl-nb-skip') {
      keepDraft();
      const i = writing;
      writing = null;
      render();
      panel.querySelector(`[data-focus="write-${i}"]`)?.focus();
    } else if (el.id === 'cl-blocks-cancel') {
      close();
      status.textContent = '';
      $('#cl-build').focus();
    } else if (el.id === 'cl-assemble') {
      const ids = rows.filter((r) => r.ticked).map((r) => r.block.id);
      if (!ids.length) { status.textContent = 'Tick at least one block.'; announce('Tick at least one block.', true); return; }
      setBusy(el, true);
      status.textContent = 'Putting the letter together…';
      try {
        const l = await api(`/api/jobs/${job.id}/cover-letter/blocks/assemble`, { method: 'POST', body: JSON.stringify({ block_ids: ids }) });
        showLetter(l);
        close();
        const unfilled = l.unfilled || [];
        status.textContent = unfilled.length
          ? `Left as written: ${unfilled.map((n) => `{${n}}`).join(', ')} — fill ${unfilled.length === 1 ? 'it' : 'them'} in before sending.`
          : `Put together from ${ids.length} block${ids.length === 1 ? '' : 's'}. Check it, then save any edits.`;
        $('#cl-text').focus();
      } catch (err) {
        setBusy(el, false);
        status.textContent = `Failed: ${err.message}`;
      }
    }
  });

  panel.addEventListener('submit', async (e) => {
    if (e.target.id !== 'cl-newblock') return;
    e.preventDefault();
    const payload = readBlockForm(e.target);
    if (!payload) return;
    const btn = $('#cl-nb-save');
    setBusy(btn, true);
    $('#cl-nb-status').textContent = 'Saving…';
    try {
      const block = await saveLetterBlock(payload);
      host.onBlocksChanged?.();
      // The new block goes where its kind belongs among the ticked ones: after the openings, the bodies, or last.
      const last = (pred) => rows.reduce((at, r, i) => (r.ticked && pred(r.block.kind) ? i : at), -1);
      let at;
      if (block.kind === 'opening') at = last((k) => k === 'opening') + 1;
      else if (block.kind === 'closing') at = last(() => true) + 1;
      else {
        const body = last((k) => k === 'body');
        const closing = rows.findIndex((r) => r.ticked && r.block.kind === 'closing');
        at = body >= 0 ? body + 1 : closing >= 0 ? closing : last((k) => k === 'opening') + 1;
      }
      rows.splice(at, 0, { block, ticked: true, why: '' });
      missing.splice(writing, 1);
      writing = null;
      const warn = unknownPlaceholdersText(block.unknown_placeholders);
      note = warn ? `<span class="warn-text"><span aria-hidden="true">⚠ </span>${esc(warn)}</span>` : '';
      render();
      panel.querySelector(`[data-focus="pick-${block.id}"]`)?.focus();
      toast('Block saved', 'ok', 2500);
      if (warn) announce(warn);
    } catch (err) {
      setBusy(btn, false);
      $('#cl-nb-status').textContent = `Not saved: ${err.message}`;
      announce(`Not saved: ${err.message}`, true);
    }
  });
}
