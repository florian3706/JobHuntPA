/* The membership page (billing.html): who is a member and why, free searches left, the plan and when it renews or
   ends, the plans to choose from (Stripe Checkout), the return from Checkout (verified on the server) and Stripe's
   customer portal (accounts design §8). Card details never reach this page or this server. */
'use strict';

const REASON_TEXT = {
  admin: "You're an admin, so you don't need a membership.",
  exempt: 'Your account is exempt from payment. You have full access.',
  billing_off: "This JobHuntPA doesn't charge, so everyone has full access.",
  grant: 'You have a free period, so you have full access.',
  subscribed: "You're a member.",
  grace: "You're still a member, but your last payment didn't go through.",
};

let billingState = null; // the last /api/billing/status answer

const money = (cents, currency) => {
  try { return new Intl.NumberFormat([], { style: 'currency', currency: String(currency || 'usd').toUpperCase() }).format(cents / 100); }
  catch { return `${(cents / 100).toFixed(2)} ${esc(String(currency || '').toUpperCase())}`; }
};
const day = (iso) => (iso ? new Date(iso).toLocaleDateString([], { dateStyle: 'long' }) : '');
const INTERVAL_WORD = { month: 'month', year: 'year', week: 'week', day: 'day' };
const planName = (p) => (p.interval === 'year' ? 'Yearly' : p.interval === 'month' ? 'Monthly' : p.plan);

function priceText(p) {
  const every = INTERVAL_WORD[p.interval] || p.interval;
  return `${money(p.amount, p.currency)} per ${every}`;
}

function renderMe(st) {
  const me = st.me;
  $('#me-loading').hidden = true;
  $('#test-badge').hidden = !st.billing.test_mode;
  const lines = [];
  let summary;
  if (me.member) {
    summary = REASON_TEXT[me.reason] || "You're a member.";
    if (me.reason === 'grant' && me.free_period_until) lines.push(['Free period until', day(me.free_period_until)]);
    if (me.reason === 'grace' && me.grace_until) lines.push(['Update your card by', day(me.grace_until)]);
  } else if (me.stripe_status === 'past_due') {
    summary = "Your last payment didn't go through, and the time to fix it is over. Update your payment method to get your membership back.";
  } else {
    summary = me.free_searches_left > 0
      ? `You're not a member yet. You have ${me.free_searches_left} of ${me.free_searches_limit} free searches left.`
      : "You're not a member yet, and your free searches are used up.";
  }
  if (me.member && me.free_searches_left > 0 && !['admin', 'exempt', 'billing_off'].includes(me.reason)) {
    lines.push(['Free searches left', `${me.free_searches_left} of ${me.free_searches_limit}`]);
  }
  if (me.plan) {
    const known = st.billing.prices.find((p) => p.plan === me.plan);
    lines.push(['Plan', known ? `${planName(known)} (${priceText(known)})` : me.plan]);
  }
  if (me.current_period_end && me.stripe_status) {
    if (me.cancel_at_period_end) lines.push(['Ends on', `${day(me.current_period_end)}: you cancelled, so it won't renew`]);
    else if (['active', 'trialing', 'past_due'].includes(me.stripe_status)) {
      lines.push([me.stripe_status === 'trialing' ? 'First payment on' : 'Renews on', day(me.current_period_end)]);
    } else lines.push(['Subscription', `${me.stripe_status.replace('_', ' ')}, last paid period ended ${day(me.current_period_end)}`]);
  }
  setText($('#me-summary'), summary);
  $('#me-summary').hidden = false;
  const facts = $('#me-details');
  facts.innerHTML = lines.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  facts.hidden = !lines.length;
  const fixCard = !me.member && me.stripe_status === 'past_due'; // the plans are hidden: the card is what to fix
  $('#portal-btn').textContent = fixCard ? 'Update your payment method' : 'Manage payment and plan';
  $('#me-actions').hidden = !me.can_manage;
  $('#me-actions-hint').hidden = !me.can_manage;
}

function renderPlans(st) {
  const on = st.billing.enabled;
  $('#off-card').hidden = on || st.me.reason === 'admin';
  $('#without-card').hidden = !on || st.me.member;
  const showPlans = on && st.me.can_subscribe && st.billing.prices.length > 0;
  $('#plans-card').hidden = !showPlans;
  if (!showPlans) return;
  $('#plans-list').innerHTML = st.billing.prices.map((p) => `<li class="plan-item">
    <span><span class="plan-price">${esc(priceText(p))}</span><br><span class="muted">${esc(planName(p))}</span></span>
    <button class="btn btn-primary" type="button" data-plan="${esc(p.plan)}" aria-label="Become a member: ${esc(planName(p))}, ${esc(priceText(p))}">Become a member</button>
  </li>`).join('');
}

function render(st) {
  billingState = st;
  renderMe(st);
  renderPlans(st);
}

async function loadStatus() {
  try {
    render(await api('/api/billing/status'));
    $('#billing-error').hidden = true;
  } catch (e) {
    $('#me-loading').hidden = true;
    showError($('#billing-error'), `Could not load your membership: ${e.message}`);
  }
}

function say(text) {
  const el = $('#billing-result');
  el.textContent = text;
  el.hidden = false;
  announce(text);
}

/* Back from Stripe Checkout: ask the server to check the payment, then say clearly how it went. */
async function checkReturn(sessionId) {
  say('Checking your payment…');
  try {
    render(await api('/api/billing/verify', { method: 'POST', body: JSON.stringify({ session_id: sessionId }) }));
    say("Thank you. Your payment went through and you're a member now.");
    return true;
  } catch (e) {
    const why = {
      not_complete: "Your payment hasn't finished yet. Nothing has been taken so far. If you paid, wait a minute and reload this page.",
      session_mismatch: "That payment belongs to another account, so it wasn't applied here. Sign in with the account that paid.",
      billing_off: "This JobHuntPA doesn't charge, so there is nothing to check.",
      no_session: "We couldn't find that payment, so nothing was applied.",
    }[e.code];
    say(`${why || `We couldn't confirm your payment: ${e.message}`} If you were charged and your membership doesn't show up, tell the person who runs this JobHuntPA.`);
    return false;
  }
}

async function startCheckout(btn) {
  setBusy(btn, true);
  setText($('#plans-status'), 'Taking you to Stripe…');
  try {
    const r = await api('/api/billing/checkout', { method: 'POST', body: JSON.stringify({ plan: btn.dataset.plan }) });
    location.assign(r.url); // Stripe's page; it brings the person back here
    return;
  } catch (e) {
    setText($('#plans-status'), '');
    if (e.code === 'already_subscribed') {
      say(e.message);
      setBusy(btn, false);
      await loadStatus(); // the plans are hidden now, so the focused button is gone: move focus to what to do next
      const portal = $('#portal-btn');
      (portal && !$('#me-actions').hidden ? portal : $('#billing-result')).focus();
      return;
    }
    else showError($('#billing-error'), `Could not start the payment: ${e.message}`);
  }
  setBusy(btn, false);
}

async function openPortal() {
  const btn = $('#portal-btn');
  setBusy(btn, true);
  setText($('#portal-status'), 'Taking you to Stripe…');
  try {
    const r = await api('/api/billing/portal', { method: 'POST', body: '{}' });
    location.assign(r.url);
    return;
  } catch (e) {
    setText($('#portal-status'), '');
    showError($('#billing-error'), `Could not open the payment page: ${e.message}`);
  }
  setBusy(btn, false);
}

document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  let me;
  try { me = await loadSession(); } catch (e) { showError($('#billing-error'), e.message); return; }
  initAccountMenu(me);
  remindRecoveryCodes(me);
  showSignInNotice();
  $('#portal-btn').addEventListener('click', openPortal);
  $('#plans-list').addEventListener('click', (e) => { const b = e.target.closest('[data-plan]'); if (b) startCheckout(b); });
  const params = new URLSearchParams(location.search);
  const sessionId = params.get('session_id');
  const cancelled = params.get('cancelled');
  // The address is cleaned so a reload doesn't check the same payment again.
  if (sessionId || cancelled) { try { history.replaceState(null, '', 'billing.html'); } catch { /* old browser */ } }
  if (sessionId) {
    if (await checkReturn(sessionId)) return;
  } else if (cancelled) {
    say('You cancelled the payment. Nothing was charged.');
  }
  await loadStatus();
});
