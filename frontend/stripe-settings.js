/* JobHuntPA admin panel, Server section: the "Payments (Stripe)" card (accounts design §8, Phase I). Whether the
   secret key and webhook secret are set (and test or live), the price lookup key, the prices Stripe has for it,
   setup steps while memberships are off, and "Sync now". The server never sends a key back; saving one needs a
   recent password. Loaded by admin.html after server-settings.js; the markup is in admin.html. */
'use strict';

const STRIPE_LABEL = { secret_key: 'Stripe secret key', webhook_secret: 'Webhook signing secret', price_lookup_key: 'Price lookup key' };
const stripeField = (name) => document.querySelector(`#stripe-card [data-stripe="${name}"]`);
const PRICE_WORD = { month: 'per month', year: 'per year', week: 'per week', day: 'per day' };

function renderStripe(st) {
  const modeTag = $('#stripe-mode');
  modeTag.hidden = !st.configured || !st.mode;
  modeTag.textContent = st.mode === 'test' ? 'Test mode' : st.mode === 'live' ? 'Live' : '';
  const keyState = (box, text) => { box.querySelector('.key-state').innerHTML = text; };
  keyState(stripeField('secret_key'), st.secret_key.set
    ? `<span class="ok-text">Saved</span> <span class="muted">(${st.secret_key.mode === 'test' ? 'test mode' : 'live mode'})</span>` : '<span class="muted">Not set</span>');
  keyState(stripeField('webhook_secret'), st.webhook.secret_set ? '<span class="ok-text">Saved</span>' : '<span class="muted">Not set</span>');
  stripeField('secret_key').querySelector('.key-clear').disabled = !st.secret_key.set;
  stripeField('webhook_secret').querySelector('.key-clear').disabled = !st.webhook.secret_set;
  $('#stripe-webhook-url').value = st.webhook.url;
  $('#stripe-lookup-key').value = st.price_lookup_key || '';
  let summary;
  if (!st.configured) summary = 'Off: no Stripe key is saved, so everyone has full access and nobody is charged.';
  else if (!st.enabled) summary = 'Off for now: Stripe has no active price for the lookup key, so everyone has full access and nobody is charged.';
  else summary = `On${st.test_mode ? ' in test mode' : ''}: people use their free searches, then need a membership.`;
  setText($('#stripe-summary'), summary);
  $('#stripe-setup').hidden = !st.setup.length;
  $('#stripe-steps').innerHTML = st.setup.map((t) => `<li>${esc(t)}</li>`).join('');
  const rows = st.prices.map((p) => [esc(p.plan),
    p.found ? `<span class="ok-text">Found<span aria-hidden="true"> ✓</span></span>` : '<span class="muted">Not found</span>',
    p.found && p.amount != null ? esc(`${(p.amount / 100).toFixed(2)} ${String(p.currency || '').toUpperCase()} ${PRICE_WORD[p.interval] || p.interval || ''}`.trim()) : '<span class="muted">–</span>']);
  $('#stripe-prices').innerHTML = rows.length
    ? tableHtml('Prices in Stripe', ['Lookup key', 'State', 'Price'], rows) : '';
  if (st.problem) setText($('#stripe-status'), st.problem);
}

async function loadStripeSettings(refresh = false) {
  try { renderStripe(await api(`/api/admin/stripe${refresh ? '?refresh=true' : ''}`)); }
  catch (e) { setText($('#stripe-summary'), `Could not load the payment settings: ${e.message}`); }
}

async function putStripe(name, value) {
  const box = stripeField(name);
  const save = box.querySelector('.key-save');
  const clear = box.querySelector('.key-clear');
  const input = box.querySelector('.key-input');
  setBusy(save, true);
  if (clear) setBusy(clear, true);
  setText($('#stripe-status'), 'Saving…');
  try {
    renderStripe(await withRecentAuth(() => api('/api/admin/stripe', { method: 'PUT', body: JSON.stringify({ [name]: value }) })));
    if (name !== 'price_lookup_key') input.value = '';
    toast(`${STRIPE_LABEL[name]} ${value ? 'saved' : 'removed'}`, 'ok', 2500);
    if ($('#stripe-status').textContent === 'Saving…') setText($('#stripe-status'), 'Saved.');
  } catch (e) {
    setText($('#stripe-status'), '');
    if (e.status === 422 && e.field === name) setFieldError(input, e.message);
    else toast(`Not saved: ${e.message}`, 'err', 8000);
  }
  setBusy(save, false);
  if (clear) setBusy(clear, false);
}

async function syncStripe() {
  const btn = $('#stripe-sync');
  setBusy(btn, true);
  setText($('#stripe-status'), 'Asking Stripe…');
  try {
    renderStripe(await api('/api/admin/stripe?refresh=true'));
    if ($('#stripe-status').textContent === 'Asking Stripe…') setText($('#stripe-status'), 'Checked just now.');
  } catch (e) { setText($('#stripe-status'), `Sync failed: ${e.message}`); }
  setBusy(btn, false);
}

function initStripeSettings() {
  ['secret_key', 'webhook_secret', 'price_lookup_key'].forEach((name) => {
    const box = stripeField(name);
    const input = box.querySelector('.key-input');
    box.addEventListener('submit', (e) => {
      e.preventDefault();
      const value = input.value.trim(); // pasted keys often carry a space or line break
      if (!value) { setFieldError(input, name === 'price_lookup_key' ? 'Type the lookup key, for example jobhunt_monthly.' : `Paste the ${STRIPE_LABEL[name].toLowerCase()} first.`); return; }
      setFieldError(input, '');
      putStripe(name, value);
    });
    box.querySelector('.key-clear')?.addEventListener('click', async (e) => {
      const clear = e.currentTarget;
      const text = name === 'secret_key'
        ? 'Memberships switch off: everyone gets full access and nobody is charged until you save a key again. People who already pay are not cancelled in Stripe.'
        : 'Stripe can no longer tell JobHuntPA about payments straight away. Memberships still update every 6 hours.';
      if (await confirmDialog({ title: `Remove the ${STRIPE_LABEL[name].toLowerCase()}?`, ok: 'Remove', returnTo: () => clear, text })) putStripe(name, '');
    });
  });
  $('#stripe-sync').addEventListener('click', syncStripe);
}
