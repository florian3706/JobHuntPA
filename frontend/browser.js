/* The browser for JavaScript-only careers pages (backend/browser.py): the offer on the Search setup tab, shown once a
   careers page has needed it, and the Settings card. Nothing is downloaded until the person chooses "Download".

   WCAG 2.2 notes: the offer is a headed region with real buttons; progress and results are said in status regions
   (4.1.3); "Not now" moves focus back to the careers page field, so focus never lands on a hidden control (2.4.3). */
'use strict';

let browserState = null;
let browserPoll = null;

function renderBrowser(st) {
  browserState = st;
  $$('.browser-size').forEach((el) => setText(el, String(st.size_mb)));
  const offer = $('#browser-offer');
  if (offer) offer.hidden = !(st.needed && !st.declined && !st.available) && !st.installing;
  const state = st.available ? 'Ready: JavaScript-only careers pages can be read.'
    : st.installing ? 'Downloading the browser… This takes a few minutes; you can keep using JobHuntPA.'
      : st.error ? st.error
        : 'Not downloaded. Careers pages that need JavaScript say so in their test result until it is.';
  setText($('#browser-state'), state);
  if ($('#browser-offer') && !offer.hidden) setText($('#browser-offer-status'), st.installing ? 'Downloading…' : (st.error || ''));
  const btn = $('#browser-download');
  if (btn) {
    btn.hidden = st.available;
    btn.disabled = st.installing;
    setText(btn, st.error ? 'Try the download again' : 'Download the browser');
  }
  clearTimeout(browserPoll);
  if (st.installing) browserPoll = setTimeout(refreshBrowserOffer, 3000);
  else if (st.available && $('#browser-offer-yes')?.dataset.asked) {
    announce('The browser is downloaded. Test the careers page again.');
    delete $('#browser-offer-yes').dataset.asked;
  }
}

async function refreshBrowserOffer() {
  try { renderBrowser(await api('/api/settings/browser')); } catch { /* (the page shows other problems itself) */ }
}

async function downloadBrowser(e) {
  const btn = e.currentTarget;
  setBusy(btn, true);
  btn.dataset.asked = '1';
  try { renderBrowser(await api('/api/settings/browser/install', { method: 'POST', body: '{}' })); }
  catch (err) { setText($('#browser-state'), `Not started: ${err.message}`); }
  setBusy(btn, false);
}

async function declineBrowser() {
  try {
    renderBrowser(await api('/api/settings/browser/decline', { method: 'POST', body: '{}' }));
    announce('Not downloaded. You can download it later in Settings, under Browser for JavaScript pages.');
    $('#source-url')?.focus();
  } catch (err) { setText($('#browser-offer-status'), `That didn't work: ${err.message}`); }
}

document.addEventListener('DOMContentLoaded', async () => {
  if (!$('#browser-card')) return;
  $('#browser-download').addEventListener('click', downloadBrowser);
  $('#browser-offer-yes').addEventListener('click', downloadBrowser);
  $('#browser-offer-no').addEventListener('click', declineBrowser);
  try { if (!(await loadSession())) return; } catch { return; }
  refreshBrowserOffer();
});
