/* Native <dialog> helpers shared by the admin page's scripts (admin.js, server-settings.js).
   Focus goes to the dialog's heading when it opens, Escape closes it, and focus returns to the element that opened
   it (or to where that element is drawn again). Needs the page's $ helper (common.js). */
'use strict';

const openers = new Map(); // dialog id -> a function that finds the element to focus when it closes

function openDialog(dlg, title, returnTo) {
  dlg.querySelector('.modal-title').textContent = title;
  openers.set(dlg.id, returnTo);
  dlg.showModal();
  dlg.querySelector('.modal-title').focus();
}

function initDialog(dlg) {
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); }); // the backdrop
  dlg.addEventListener('close', () => {
    const back = openers.get(dlg.id)?.();
    openers.delete(dlg.id);
    (back && document.contains(back) ? back : $('#main')).focus();
  });
}

/* Asks before something that matters. Resolves true when confirmed. */
function confirmDialog({ title, text, ok = 'Confirm', returnTo = null }) {
  const dlg = $('#confirm-dialog');
  $('#confirm-text').textContent = text;
  $('#confirm-ok').textContent = ok;
  return new Promise((resolve) => {
    let answer = false;
    const done = () => { dlg.removeEventListener('close', done); resolve(answer); };
    dlg.addEventListener('close', done);
    $('#confirm-form').onsubmit = (e) => { e.preventDefault(); answer = true; dlg.close(); };
    $('#confirm-cancel').onclick = () => dlg.close();
    openDialog(dlg, title, returnTo);
  });
}
