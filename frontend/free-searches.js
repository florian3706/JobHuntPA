/* The note beside "Run search" on the Jobs tab: how many free searches a search uses, or that a membership is
   needed (Phase I). Loaded after app.js, which calls refreshFreeSearchNote() when a search is queued or finished. */
'use strict';

async function refreshFreeSearchNote() {
  const note = document.getElementById('free-search-note');
  if (!note) return;
  let st;
  try { st = await api('/api/billing/status'); } catch { return; } // the note is a convenience: say nothing when it fails
  const me = st.me;
  if (!st.billing.enabled || me.member) { note.hidden = true; note.textContent = ''; return; }
  note.hidden = false;
  if (me.can_search) {
    note.innerHTML = `Running a search uses 1 of your ${me.free_searches_left} free ${me.free_searches_left === 1 ? 'search' : 'searches'}, and scores the jobs it finds. Rescoring, Fill missing info, chat and cover letters are for members. <a href="billing.html">See membership options</a>`;
  } else {
    note.innerHTML = `${esc(me.message || 'You have no free searches left.')} <a href="billing.html">See membership options</a>`;
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  try { await loadSession(); } catch { return; }
  refreshFreeSearchNote();
});
