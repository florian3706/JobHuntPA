/* JobHuntPA: the "Background access" card on the Settings tab (accounts design 6.7, Phase G).

   With background access on, the server keeps a key that can open your data, so scheduled searches run while you
   are signed out. Turning it on needs the password entered in the last 15 minutes (withRecentAuth); turning it off
   deletes the server's copy at once. The card is in index.html (#account-background-slot).

   WCAG 2.2 notes: the control is a switch (a checkbox with role="switch", so its state is read as on or off) with a
   visible label, described by the explanation under it. A change is announced (common.js announce) and a refusal
   (or a failed load, with a Try again button) shown in an alert; a refused change puts the switch back as it was. The switch is never disabled while it
   works (that would drop keyboard focus); further changes are ignored until the first is done. */
'use strict';

const BACKGROUND_COPY = {
  off: BACKGROUND_OFF_COPY,
  on: BACKGROUND_ON_COPY,
  moreOff: 'Turning it on asks for your password. Scheduled searches are skipped until you do.',
  moreOn: 'Turning it off deletes the key the server holds. Scheduled searches are then skipped until you turn it on again.',
};

const background = { enabled: false, since: null, busy: false, loaded: false };

function fmtBackgroundSince(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

/* The card's text for a state; the switch itself is set by renderBackground. */
function backgroundText(state) {
  const since = state.enabled && state.since ? fmtBackgroundSince(state.since) : '';
  return {
    explain: state.enabled ? BACKGROUND_COPY.on : BACKGROUND_COPY.off,
    more: `${state.enabled ? BACKGROUND_COPY.moreOn : BACKGROUND_COPY.moreOff}${since ? ` It has been on since ${since}.` : ''}`,
    status: state.enabled ? 'On' : 'Off',
  };
}

function renderBackground() {
  const box = $('#background-switch');
  if (!box) return;
  const text = backgroundText(background);
  box.checked = background.enabled;
  box.disabled = !background.loaded;
  setText($('#background-explain'), text.explain);
  setText($('#background-more'), background.loaded ? text.more : '');
  setText($('#background-status'), background.loaded ? text.status : '');
}

function backgroundError(message) {
  const el = $('#background-error');
  if (!message) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = message;
}

/* The schedule card says whether searches are being skipped; refresh it after a change. */
function refreshScheduleCard() {
  if (typeof loadSchedule === 'function') loadSchedule({ controls: false });
}

async function changeBackground(want) {
  if (background.busy) { renderBackground(); return; }
  background.busy = true;
  backgroundError('');
  try {
    const answer = await withRecentAuth(() => api('/api/account/background', { method: 'POST', body: JSON.stringify({ enabled: want }) }));
    Object.assign(background, { enabled: !!answer.enabled, since: answer.since || null });
    renderBackground();
    announce(background.enabled ? 'Background access is on.' : 'Background access is off. The server no longer holds your key.');
    refreshScheduleCard();
  } catch (e) {
    renderBackground(); // the switch goes back to what the server says
    backgroundError(`${want ? "Couldn't turn background access on" : "Couldn't turn background access off"}: ${e.message}`);
    announce(`Background access did not change: ${e.message}`, true);
  } finally {
    background.busy = false;
  }
}

async function loadBackground() {
  const retry = $('#background-retry');
  try {
    const state = await api('/api/account/background');
    Object.assign(background, { enabled: !!state.enabled, since: state.since || null, loaded: true });
    backgroundError('');
    if (retry) retry.hidden = true;
    renderBackground();
  } catch (e) {
    backgroundError(`Couldn't load this setting: ${e.message}`);
    if (retry) retry.hidden = false;
    announce(`Couldn't load background access: ${e.message}`, true);
  }
}

/* The schedule card's "Turn it on" button takes you to the switch. */
function focusBackgroundSwitch() {
  const box = $('#background-switch');
  if (!box) return;
  box.scrollIntoView({ block: 'center', behavior: scrollBehavior() });
  box.focus();
}

document.addEventListener('DOMContentLoaded', async () => {
  const box = $('#background-switch');
  if (!box) return;
  box.addEventListener('change', () => changeBackground(box.checked));
  $('#background-retry')?.addEventListener('click', () => loadBackground());
  document.addEventListener('click', (e) => {
    if (e.target.closest?.('[data-focus-background]')) focusBackgroundSwitch();
  });
  try { await loadSession(); } catch { return; } // signed out: goes to the sign-in page
  loadBackground();
});
