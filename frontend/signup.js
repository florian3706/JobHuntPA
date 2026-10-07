/* JobHuntPA sign-up page (signup.html, accounts design §6.1): the form for open sign-up and for invitation links
   (signup.html?invite=…), then the recovery codes, then the walkthrough (welcome.html), as on first run.
   It builds on auth.js (loaded first): authCall, showErrors, showCodes, the password toggle and the error summary.
   The WCAG notes in auth.js apply here too. */
'use strict';

const INVITE = new URLSearchParams(location.search).get('invite') || '';

/* A reason sign-up can't go ahead, in place of the form. */
function showClosed(text) {
  clearErrors();
  status('');
  $('#setup-step').hidden = true;
  $('#closed-text').textContent = text;
  $('#closed-step').hidden = false;
  $('#page-title').focus();
}

function whyNotAllowed(signup) {
  if (signup.encrypting) return 'JobHuntPA is encrypting its data and can\'t make accounts for a few minutes. Try this link again soon.';
  if (signup.mode === 'closed') return 'Sign-up is closed on this JobHuntPA. Ask the person who runs it.';
  if (INVITE) return 'This invitation link has already been used or has expired. Ask the person who runs this JobHuntPA for a new one.';
  return 'You need an invitation link to create an account here. Ask the person who runs this JobHuntPA to send you one.';
}

async function initSignup() {
  const state = await authCall(`/api/auth/state${INVITE ? `?invite=${encodeURIComponent(INVITE)}` : ''}`);
  if (state.status !== 200) { status(''); showErrors(answerProblem(state, "JobHuntPA isn't answering. Check that it's running, then reload this page.")); return; }
  if (state.data.setup_required) { location.replace('first-run.html'); return; }
  if (state.data.signed_in) {
    // Signed in already: only the recovery codes of a brand-new account are left to show.
    const codes = await authCall('/api/auth/recovery-codes');
    if (codes.status !== 200) { location.replace('./'); return; }
    const me = await authCall('/api/auth/me');
    showCodes(codes.data.codes, me.data.csrf);
    return;
  }
  const signup = state.data.signup;
  if (!signup || !signup.allowed) { showClosed(whyNotAllowed(signup || {})); return; }
  status('');
  if (signup.invite?.email) $('#email').value = signup.invite.email;
  $('#setup-step').hidden = false;
  $('#email').focus();
  $('#signup-form').addEventListener('submit', submitSignup);
}

async function submitSignup(e) {
  e.preventDefault();
  const email = $('#email').value.trim();
  const password = $('#password').value;
  const problems = [];
  if (!email) problems.push({ field: 'email', message: 'Enter your email address.' });
  if (!password) problems.push({ field: 'password', message: 'Choose a password.' });
  else if (password.length < 10) problems.push({ field: 'password', message: 'Use at least 10 characters. A few random words work well.' });
  if (problems.length) { showErrors(problems); return; }
  const btn = $('#signup-submit');
  setBusy(btn, true);
  status('Creating your account…');
  hidePasswords();
  const body = { email, password, name: $('#name').value.trim(), remember: $('#remember').checked };
  if (INVITE) body.invite = INVITE;
  const res = await authCall('/api/auth/signup', { method: 'POST', body });
  setBusy(btn, false);
  if (res.status === 201) {
    status('');
    showCodes(res.data.recovery_codes, res.data.csrf);
    return;
  }
  status('');
  const code = res.data.code;
  if (code === 'signup_closed' || code === 'invite_needed' || code === 'invite_invalid') { showClosed(whyNotAllowed({ mode: code === 'signup_closed' ? 'closed' : 'invite' })); return; }
  const errors = answerProblem(res, `Creating the account didn't work (error ${res.status}). Try again.`);
  if (code === 'email_taken') errors[0].field = 'email';
  showErrors(errors);
}

document.addEventListener('DOMContentLoaded', () => {
  if (PAGE === 'signup') initSignup();
});
