/* Careers pages in bulk (docs/design/open-source.md §1.14): the "Bulk add" dialog and "Export sources", on the
   Search setup tab's Company sources card.

   1. The person pastes a list or chooses a file (CSV: company,url; the file's text is read here, as UTF-8, into the
      box for them to see).
   2. The server reads the rows and flags the unusual addresses (POST /api/sources/bulk/preview). That call fetches
      nothing and looks nothing up: no request reaches any of the sites until the person clicks Add. The rows are
      shown as a list; each can be unticked or removed. Flagged rows start unticked; rows that can't be added have no
      tick box. Addresses are shown as text, never as links, so nothing in the list can be opened by mistake.
   3. The ticked rows go to POST /api/sources/bulk, which adds each the way a single add does, and every row gets a
      result line.

   A native <dialog>: focus moves in, stays inside and goes back to "Bulk add" when it closes; Escape closes it. Each
   step's heading takes focus when the step changes, and counts and results are said in status regions. */
'use strict';

(() => {
  const MAX_FILE = 512 * 1024; // bytes; the server takes at most this many characters
  const RESULT = {
    added: 'Added', already: 'Already there', repeat: 'Repeated in the file', refused: 'Not added',
    unreadable: 'Could not be read', left: 'Left out',
  };
  let rows = []; // the preview's rows, each with `key` (stable while the list is open) and `ticked`
  let opener = null;

  const el = (id) => document.getElementById(id);
  const name = (r) => r.label || r.company || r.host || r.url || `line ${r.line}`;
  const canAdd = (r) => r.status === 'new' || r.status === 'already' || r.status === 'repeat';

  function showStep(step, { focus = 'heading' } = {}) {
    ['input', 'review', 'done'].forEach((s) => { el(`srcbulk-step-${s}`).hidden = s !== step; });
    el('srcbulk-dialog').querySelector('.srcbulk-body').scrollTop = 0;
    (focus === 'text' ? el('srcbulk-text') : el(`srcbulk-${step}-heading`)).focus();
  }

  function open() {
    opener = document.activeElement;
    const dlg = el('srcbulk-dialog');
    if (!dlg.open) dlg.showModal();
    if (rows.length && !el('srcbulk-step-review').hidden) showStep('review'); // reopened with the list still there
    else showStep('input', { focus: 'text' });
  }

  /* -- step 1: the text --------------------------------------------------------------------------------------- */
  async function readFile() {
    const input = el('srcbulk-file');
    const file = input.files && input.files[0];
    if (!file) return;
    setFieldError(input, '', { focus: false });
    if (file.size > MAX_FILE) {
      setFieldError(input, `That file is larger than ${MAX_FILE / 1024} KB. Split it into smaller files.`);
      return;
    }
    let text;
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
    } catch {
      setFieldError(input, 'That file isn\'t UTF-8 text. In a spreadsheet, save it as "CSV UTF-8" and choose it again.');
      return;
    }
    el('srcbulk-text').value = text.replace(/^\uFEFF/, '');
    setFieldError(el('srcbulk-text'), '', { focus: false });
    announce(`Read ${file.name}. Its text is in the box: check it, then choose Show the list.`);
  }

  async function preview() {
    const box = el('srcbulk-text');
    const text = box.value;
    if (!text.trim()) { setFieldError(box, 'Paste the list, or choose a file.'); return; }
    setFieldError(box, '', { focus: false });
    const btn = el('srcbulk-preview');
    setBusy(btn, true);
    try {
      const r = await api('/api/sources/bulk/preview', { method: 'POST', body: JSON.stringify({ text }) });
      if (!r.rows.length) { setFieldError(box, 'No rows were found in that text. Each line needs a company and an address.'); return; }
      rows = r.rows.map((row, i) => ({ ...row, key: i, ticked: canAdd(row) && !row.flags.length }));
      renderRows();
      showStep('review');
    } catch (e) {
      setFieldError(box, e.message);
    } finally {
      setBusy(btn, false);
    }
  }

  /* -- step 2: the list ----------------------------------------------------------------------------------------- */
  function noteOf(r) {
    if (r.status === 'already') return 'Already a source: it stays as it is.';
    if (r.status === 'repeat') return `Repeats line ${r.repeat_of}: it will be skipped.`;
    return '';
  }

  function rowHtml(r) {
    const id = `srcbulk-r-${r.key}`;
    const label = esc(name(r));
    const blocked = !canAdd(r);
    const reasons = r.status === 'unreadable' ? [r.problem] : r.flags.map((f) => f.reason);
    const mark = blocked ? 'Can\'t be added' : 'Unusual';
    const flags = reasons.length
      ? `<ul class="srcbulk-flags">${reasons.map((why) => `<li><span aria-hidden="true">⚠ </span><strong>${mark}:</strong> ${esc(why)}</li>`).join('')}</ul>`
      : '';
    const note = noteOf(r);
    const head = blocked
      ? `<span class="srcbulk-name">${label}</span>`
      : `<input type="checkbox" id="${id}" aria-describedby="${id}-info" ${r.ticked ? 'checked' : ''} />
         <label class="srcbulk-name" for="${id}">${label}</label>`;
    return `<div class="srcbulk-head">${head}</div>
      <div class="srcbulk-info" id="${id}-info">
        <span class="srcbulk-url">${esc(r.url || '(no address)')}</span>
        <span class="doc-meta">Line ${r.line}${note ? ` · ${esc(note)}` : ''}</span>
        ${flags}
      </div>
      <button class="btn btn-ghost btn-sm" type="button" data-act="remove">Remove<span class="visually-hidden"> ${label}, line ${r.line}</span></button>`;
  }

  function renderRows() {
    const list = el('srcbulk-rows');
    list.innerHTML = '';
    rows.forEach((r) => {
      const li = document.createElement('li');
      li.className = `srcbulk-row${r.flags.length || !canAdd(r) ? ' is-flagged' : ''}`;
      li.dataset.key = String(r.key);
      li.innerHTML = rowHtml(r);
      li.querySelector('input[type="checkbox"]')?.addEventListener('change', (e) => { r.ticked = e.target.checked; count(); });
      li.querySelector('[data-act="remove"]').addEventListener('click', () => remove(r));
      list.appendChild(li);
    });
    count();
  }

  function count() {
    const ticked = rows.filter((r) => r.ticked).length;
    const unusual = rows.filter((r) => canAdd(r) && r.flags.length).length;
    const blocked = rows.filter((r) => !canAdd(r)).length;
    const parts = [`${rows.length} page${rows.length === 1 ? '' : 's'}`, `${ticked} ticked`];
    if (unusual) parts.push(`${unusual} unusual`);
    if (blocked) parts.push(`${blocked} can't be added`);
    setText(el('srcbulk-count'), `${parts.join(', ')}.`);
    el('srcbulk-add').textContent = ticked ? `Add the ${ticked} ticked page${ticked === 1 ? '' : 's'}` : 'Add the ticked pages';
  }

  function remove(r) {
    const at = rows.indexOf(r);
    rows.splice(at, 1);
    renderRows();
    announce(`Removed ${name(r)}. ${rows.length} left.`);
    const items = el('srcbulk-rows').querySelectorAll('[data-act="remove"]');
    (items[Math.min(at, items.length - 1)] || el('srcbulk-review-heading')).focus();
  }

  async function add() {
    const kept = rows.filter((r) => r.ticked);
    if (!kept.length) { announce('Nothing is ticked. Tick the pages to add, or go back to the text.', true); return; }
    const btn = el('srcbulk-add');
    setBusy(btn, true);
    try {
      const r = await api('/api/sources/bulk', {
        method: 'POST', body: JSON.stringify({ rows: kept.map((row) => ({ company: row.company, url: row.url })) }),
      });
      const results = [...r.results];
      showResults(rows.map((row) => (row.ticked ? { ...results.shift(), line: row.line }
        : { line: row.line, company: name(row), url: row.url,
          result: canAdd(row) ? 'left' : row.status, detail: canAdd(row) ? 'You left it unticked.' : (row.problem || row.flags.map((f) => f.reason).join(' ')) })));
      rows = [];
      if (typeof loadSources === 'function') loadSources();
      if (typeof loadSchedule === 'function') loadSchedule({ controls: false });
    } catch (e) {
      announce(`Nothing was added: ${e.message}`, true);
      setText(el('srcbulk-count'), `Nothing was added: ${e.message}`);
    } finally {
      setBusy(btn, false);
    }
  }

  /* -- step 3: the results --------------------------------------------------------------------------------------- */
  function showResults(lines) {
    const list = el('srcbulk-results');
    list.innerHTML = lines.map((x) => `<li class="srcbulk-row srcbulk-result is-${esc(x.result)}">
      <span><strong>${esc(RESULT[x.result] || x.result)}:</strong> ${esc(x.company || x.url || '')}</span>
      <span class="srcbulk-url">${esc(x.url || '')}</span>
      <span class="doc-meta">Line ${esc(x.line)}${x.detail ? ` · ${esc(x.detail)}` : ''}</span></li>`).join('');
    const tally = Object.keys(RESULT).map((k) => [k, lines.filter((x) => x.result === k).length]).filter(([, n]) => n);
    const summary = tally.map(([k, n]) => `${RESULT[k]}: ${n}`).join('. ');
    setText(el('srcbulk-summary'), `${summary}.`);
    showStep('done');
    announce(`${summary}.`);
  }

  function again() {
    el('srcbulk-text').value = '';
    el('srcbulk-file').value = '';
    rows = [];
    showStep('input', { focus: 'text' });
  }

  /* -- export ----------------------------------------------------------------------------------------------- */
  async function exportSources() {
    const btn = el('sources-export');
    const status = el('sources-export-status');
    setBusy(btn, true);
    try {
      const ws = currentWorkspace();
      const res = await fetch('/api/sources/export', { headers: ws ? { 'X-Workspace': String(ws) } : {} });
      if (res.status === 401) { signedOut(); return; }
      if (!res.ok) {
        let detail = `Error ${res.status}`;
        try { const data = await res.json(); if (typeof data.detail === 'string') detail = data.detail; } catch { /* not JSON */ }
        throw new Error(detail);
      }
      const named = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '');
      const file = named ? named[1] : 'sources.csv';
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement('a'), { href: url, download: file });
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      setText(status, `Done. Your browser saved ${file}. Bulk add takes it as it is.`);
    } catch (e) {
      setText(status, `The sources could not be exported: ${e.message}`);
    } finally {
      setBusy(btn, false);
    }
  }

  function init() {
    const dlg = el('srcbulk-dialog');
    if (!dlg || !el('sources-bulk-open')) return;
    el('sources-bulk-open').addEventListener('click', open);
    el('sources-export').addEventListener('click', exportSources);
    el('srcbulk-close').addEventListener('click', () => dlg.close());
    el('srcbulk-done').addEventListener('click', () => dlg.close());
    el('srcbulk-file').addEventListener('change', readFile);
    el('srcbulk-preview').addEventListener('click', preview);
    el('srcbulk-back').addEventListener('click', () => showStep('input', { focus: 'text' }));
    el('srcbulk-add').addEventListener('click', add);
    el('srcbulk-again').addEventListener('click', again);
    dlg.addEventListener('close', () => {
      if (!el('srcbulk-step-done').hidden) again(); // a finished list isn't shown again next time
      const back = opener && opener.isConnected ? opener : el('sources-bulk-open');
      opener = null;
      back.focus();
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
