/* JobHuntPA sign-up walkthrough: welcome.html (accounts design 6.1, Phase K).

   A new account chooses a theme, then sets up a search one small step at a time. Each step saves through the
   same endpoints as the dashboard's Search setup tab (/api/profile, /api/dealbreakers, /api/pins) and then tells the
   server which step is next (/api/account/onboarding), so leaving and coming back resumes where it was left.

   The first account, whose existing search settings moved into it, gets a short path: theme, then keep the
   settings or review them step by step.

   WCAG 2.2 notes:
   - Each step is a fieldset whose legend holds the step's heading (an h2 that takes focus when the step appears, so
     it is read out, including "Step 2 of 5").
   - Progress is text ("Step 2 of 5") and numbered marks (a tick once done), never colour alone; the current mark
     has aria-current="step".
   - Problems go in an error summary that takes focus and links to each field, and are repeated under the field
     (aria-describedby), as in auth.js (3.3.1, 3.3.3). Nothing is asked twice (3.3.7).
   - Back, Next and Skip are real buttons; controls are at least 44 px tall (24 px is the minimum, 2.5.8).
   - The scheduled-search option appears only when /api/auth/me says it exists: me.features.scheduled_searches
     (Phase G). */
'use strict';

/* The page works on the account's first workspace; the server picks it when none is named. */
function currentWorkspace() { return 0; }

const wiz = {
  me: null,
  short: false, // the first account's short path
  features: {},
  steps: [], // step ids in order
  idx: 0,
  profile: null,
  background: null, // { enabled, since }: background access, when the automatic-searches step is offered
  schedule: null, // this workspace's schedule, for the same step
  dealbreakers: { industries: [], keywords: [] },
  pins: [],
  homeText: '', // the home place as last saved (its text and radius are kept on the pin)
  gotoDocs: false,
  changed: false, // a search setting was saved, so existing jobs are filtered again at the end
  busy: false,
};

/* ---------- errors: the summary at the top and the message under each field (as in auth.js) ---------- */
function clearErrors() {
  $('#error-summary').hidden = true;
  $('#error-summary-list').innerHTML = '';
  $$('#wiz-form .field-error').forEach((el) => el.remove());
  $$('#wiz-form [aria-invalid="true"]').forEach((input) => {
    input.removeAttribute('aria-invalid');
    const ids = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter((x) => x && x !== `${input.id}-error`);
    if (ids.length) input.setAttribute('aria-describedby', ids.join(' ')); else input.removeAttribute('aria-describedby');
  });
}

/* errors: [{ field: input id or null, message }] */
function showErrors(errors) {
  clearErrors();
  const list = $('#error-summary-list');
  for (const { field, message } of errors) {
    const input = field && document.getElementById(field);
    const li = document.createElement('li');
    if (input) {
      li.innerHTML = `<a href="#${esc(field)}">${esc(message)}</a>`;
      li.querySelector('a').addEventListener('click', (e) => { e.preventDefault(); input.focus(); });
      const note = document.createElement('p');
      note.id = `${input.id}-error`;
      note.className = 'field-error';
      note.innerHTML = `<span class="visually-hidden">Error: </span>${esc(message)}`;
      (input.closest('.tag-input') || input).insertAdjacentElement('afterend', note);
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', [note.id, input.getAttribute('aria-describedby')].filter(Boolean).join(' '));
    } else {
      li.textContent = message;
    }
    list.appendChild(li);
  }
  const summary = $('#error-summary');
  summary.hidden = false;
  summary.focus();
}

const status = (text) => setText($('#auth-status'), text);

/* ---------- tag inputs (titles, locations, keywords): list items with a remove button, as on the Search tab ---------- */
const tagHtml = (value) => `<li class="tag" data-value="${esc(value)}"><span>${esc(value)}</span><button type="button" aria-label="Remove ${esc(value)}"><span aria-hidden="true">×</span></button></li>`;

function tagField(field, label, hint, placeholder, values) {
  const id = `wz-${field}`;
  return `<div class="form-group">
      <label for="${id}">${label}</label>
      <div class="tag-input" data-field="${field}"><ul class="tags" aria-label="${label}">${values.map(tagHtml).join('')}</ul>
        <input id="${id}" type="text" placeholder="${placeholder}" aria-describedby="${id}-hint" enterkeyhint="done" autocomplete="off" /></div>
      <small id="${id}-hint" class="hint">${hint}</small>
    </div>`;
}

const tagList = (field) => document.querySelector(`.tag-input[data-field="${field}"] .tags`);

function addTag(field, text) {
  const value = String(text || '').trim();
  if (!value) return;
  const list = tagList(field);
  if ([...list.querySelectorAll('.tag')].some((t) => t.dataset.value.toLowerCase() === value.toLowerCase())) {
    announce(`${value} is already in the list.`);
    return;
  }
  list.insertAdjacentHTML('beforeend', tagHtml(value));
  announce(`Added ${value}.`);
}

/* The tags, including what was typed but not yet confirmed with Enter. */
function collectTags(field) {
  const input = document.querySelector(`.tag-input[data-field="${field}"] input`);
  if (input && input.value.trim()) { addTag(field, input.value); input.value = ''; }
  return [...tagList(field).querySelectorAll('.tag')].map((t) => t.dataset.value);
}

function initTagInputs(root) {
  root.addEventListener('keydown', (e) => {
    const input = e.target.closest('.tag-input input');
    if (!input || e.key !== 'Enter') return;
    e.preventDefault(); // Enter adds the tag; it doesn't move to the next step
    addTag(input.closest('.tag-input').dataset.field, input.value);
    input.value = '';
  });
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('.tag button');
    if (!btn) return;
    const tag = btn.closest('.tag');
    const input = tag.closest('.tag-input').querySelector('input');
    const value = tag.dataset.value;
    tag.remove();
    input.focus();
    announce(`Removed ${value}.`);
  });
}

/* ---------- saving ---------- */
async function putProfile(patch) {
  wiz.profile = await api('/api/profile', { method: 'PUT', body: JSON.stringify({ ...wiz.profile, ...patch }) });
  wiz.changed = true;
}

async function putDealbreakers(patch) {
  wiz.dealbreakers = await api('/api/dealbreakers', { method: 'PUT', body: JSON.stringify({ ...wiz.dealbreakers, ...patch }) });
  wiz.changed = true;
}

/* Where the next step is, so a person who leaves resumes there. */
function rememberStep(position) {
  return api('/api/account/onboarding', { method: 'PUT', body: JSON.stringify({ step: position }) }).catch(() => {});
}

/* ---------- the steps ---------- */
const WORK_MODES = [
  ['wz-remote-aus', 'remote_aus_ok', 'Remote (Australia)'],
  ['wz-remote-global', 'remote_global_ok', 'Remote (overseas employers)'],
  ['wz-hybrid', 'allow_hybrid', 'Hybrid (some days in an office)'],
  ['wz-onsite', 'allow_onsite', 'Onsite'],
];

const STEPS = {
  theme: {
    title: 'Choose a theme',
    intro: 'Pick how JobHuntPA looks. The page changes as soon as you choose, and you can change it later on the Settings tab. The themes labelled for dyslexia or colour vision are there if you want them.',
    skippable: true,
    html: () => '<div id="wz-theme-slot"></div>',
    mount() {
      $('#wz-theme-slot').replaceWith(createThemePicker({ legend: 'Theme', name: 'wz-theme', onChange: saveAccountTheme }));
    },
  },

  keep: {
    title: 'Your current search settings',
    intro: 'Your job titles, locations and other search settings came with your existing data. You can keep them as they are, or look through them one step at a time.',
    nextLabel: 'Keep my settings',
    html() {
      const p = wiz.profile;
      const modes = WORK_MODES.filter(([, key]) => p[key]).map(([, , label]) => label).join(', ') || 'none chosen';
      const row = (name, value) => `<div><dt>${name}</dt><dd>${esc(value)}</dd></div>`;
      return `<dl class="wiz-summary">
          ${row('Job titles', p.titles.join(', ') || 'none yet')}
          ${row('Locations', p.seek_locations.join(', ') || 'all of Australia')}
          ${row('Work types', modes)}
          ${row('Salary floor', p.salary_floor ? `$${p.salary_floor.toLocaleString()} a year` : 'none')}
        </dl>
        <p><button id="wz-review" class="btn" type="button">Review them step by step</button></p>`;
    },
    mount() {
      $('#wz-review').addEventListener('click', () => {
        wiz.short = false;
        wiz.steps = buildSteps();
        go(wiz.steps.indexOf('titles'));
      });
    },
  },

  titles: {
    title: 'What jobs are you looking for?',
    intro: 'Add the job titles you would apply for. JobHuntPA searches for each one and shows jobs whose title contains one of them. Add a title and press Enter; add as many as you like.',
    skippable: true,
    html: () => tagField('titles', 'Target job titles', 'For example Product Manager or Business Analyst. Use the titles employers advertise.', 'Add a title and press Enter', wiz.profile.titles),
    async save() {
      const titles = collectTags('titles');
      if (!titles.length) { showErrors([{ field: 'wz-titles', message: 'Add at least one job title, or choose "Skip for now".' }]); return false; }
      await putProfile({ titles });
      return true;
    },
  },

  where: {
    title: 'Where do you want to work?',
    intro: 'Say which places to search, whether you want remote, hybrid or onsite work, and how far you will travel from home.',
    skippable: true,
    html() {
      const p = wiz.profile;
      const home = wiz.pins.find((x) => x.kind === 'home');
      return `${tagField('seek_locations', 'Places to search', 'Write places the way SEEK does, for example All Sydney NSW or Melbourne VIC. Leave empty to search all of Australia.', 'Add a place and press Enter', p.seek_locations)}
        <fieldset class="form-group wiz-group">
          <legend>Kinds of work you would take</legend>
          ${WORK_MODES.map(([id, key, label]) => `<label class="check"><input type="checkbox" id="${id}" ${p[key] ? 'checked' : ''} /> ${label}</label>`).join('')}
        </fieldset>
        <div class="form-group">
          <label for="wz-office-days">Hybrid: most days a week in the office</label>
          <select id="wz-office-days" aria-describedby="wz-office-days-hint">
            <option value="">Any</option>
            ${[1, 2, 3, 4].map((n) => `<option value="${n}" ${p.max_office_days === n ? 'selected' : ''}>${n} day${n > 1 ? 's' : ''}</option>`).join('')}
          </select>
          <small id="wz-office-days-hint" class="hint">Only used for hybrid jobs that say how many days they need you in.</small>
        </div>
        <div class="form-group">
          <label for="wz-home">Your home suburb or town <span class="muted">(optional)</span></label>
          <input id="wz-home" type="text" autocomplete="off" placeholder="For example Chatswood NSW" value="${esc(home ? home.label : '')}" aria-describedby="wz-home-hint" />
          <small id="wz-home-hint" class="hint">Hybrid and onsite jobs are kept only if the office is within your travel distance of home. JobHuntPA looks the place up on OpenStreetMap; it doesn't keep your address. Without a home place, those jobs can't be checked for distance.</small>
        </div>
        <div class="form-group">
          <label for="wz-radius">How far will you travel (kilometres)?</label>
          <input id="wz-radius" type="number" min="1" max="500" step="1" inputmode="numeric" value="${home ? Math.round(home.radius_km) : 25}" />
        </div>`;
    },
    mount() {
      const hybrid = $('#wz-hybrid');
      const days = $('#wz-office-days');
      const sync = () => { days.disabled = !hybrid.checked; };
      hybrid.addEventListener('change', sync);
      sync();
    },
    async save() {
      const problems = [];
      const picked = Object.fromEntries(WORK_MODES.map(([id, key]) => [key, $(`#${id}`).checked]));
      if (!Object.values(picked).some(Boolean)) {
        problems.push({ field: 'wz-remote-aus', message: 'Choose at least one kind of work, or choose "Skip for now".' });
      }
      const radius = Number($('#wz-radius').value);
      const homeText = $('#wz-home').value.trim();
      if (homeText && !(radius >= 1 && radius <= 500)) {
        problems.push({ field: 'wz-radius', message: 'Enter a travel distance between 1 and 500 kilometres.' });
      }
      if (problems.length) { showErrors(problems); return false; }

      let place = null;
      const home = wiz.pins.find((x) => x.kind === 'home');
      if (homeText && (!home || home.label !== homeText || Math.round(home.radius_km) !== radius)) {
        if (!home || home.label !== homeText) {
          status('Looking up your home place…');
          const found = await api(`/api/geo/lookup?q=${encodeURIComponent(homeText)}`);
          status('');
          if (!found.found) {
            showErrors([{ field: 'wz-home', message: "JobHuntPA couldn't find that place. Try a suburb or town and its state, for example Chatswood NSW, or leave it empty and add a home pin on the Map tab later." }]);
            return false;
          }
          place = { label: homeText, lat: found.lat, lng: found.lng };
        } else {
          place = { label: home.label, lat: home.lat, lng: home.lng };
        }
      }
      await putProfile({
        seek_locations: collectTags('seek_locations'),
        ...picked,
        max_office_days: picked.allow_hybrid ? ($('#wz-office-days').value === '' ? null : Number($('#wz-office-days').value)) : wiz.profile.max_office_days,
      });
      if (place) {
        const body = JSON.stringify({ ...place, kind: 'home', radius_km: radius });
        const saved = home
          ? await api(`/api/pins/${home.id}`, { method: 'PUT', body })
          : await api('/api/pins', { method: 'POST', body });
        wiz.pins = home ? wiz.pins.map((x) => (x.id === home.id ? saved : x)) : [...wiz.pins, saved];
      }
      return true;
    },
  },

  extras: {
    title: 'Salary and words to include or avoid',
    intro: 'Everything on this step is optional. A salary floor, words you want, and words or industries you never want help leave out jobs that are not worth your time.',
    skippable: true,
    html() {
      return `<div class="form-group">
          <label for="wz-salary">Lowest salary you would accept (AUD a year)</label>
          <input id="wz-salary" type="number" min="0" step="1000" inputmode="numeric" placeholder="For example 140000" autocomplete="off" aria-describedby="wz-salary-hint" value="${wiz.profile.salary_floor ?? ''}" />
          <small id="wz-salary-hint" class="hint">Only applied to jobs that state a salary. Hourly and daily rates are turned into a yearly amount.</small>
        </div>
        ${tagField('keywords_include', 'Words you want to see', 'A job also passes if its title or description mentions one of these.', 'Add a word and press Enter', wiz.profile.keywords_include)}
        ${tagField('keywords_exclude', 'Words you do not want', 'Jobs that use one of these as a whole word in the title or description are left out.', 'Add a word and press Enter', wiz.profile.keywords_exclude)}
        ${tagField('industries', 'Industries you never want', 'For example Gambling or Tobacco.', 'Add an industry and press Enter', wiz.dealbreakers.industries)}`;
    },
    async save() {
      const input = $('#wz-salary');
      if (!input.validity.valid) {
        showErrors([{ field: 'wz-salary', message: 'Enter the salary as a whole number of dollars a year, 0 or more (for example 140000), or leave it empty.' }]);
        return false;
      }
      await putProfile({
        salary_floor: input.value === '' ? null : Number(input.value),
        keywords_include: collectTags('keywords_include'),
        keywords_exclude: collectTags('keywords_exclude'),
      });
      await putDealbreakers({ industries: collectTags('industries') });
      return true;
    },
  },

  /* Shown only when me.features.scheduled_searches is true (background access, Phase G). Searching by itself needs
     the server to hold a key (background access), so ticking this turns that on (it asks for the password again if
     it was entered a while ago) and the explanation says what that means. */
  auto: {
    title: 'Automatic searches',
    intro: 'JobHuntPA can look for new jobs by itself while you are away, so they are waiting when you sign in.',
    skippable: true,
    html() {
      const on = !!(wiz.background?.enabled && wiz.schedule?.enabled);
      return `<label class="check"><input type="checkbox" id="wz-auto" aria-describedby="wz-auto-hint" ${on ? 'checked' : ''} /> Search for new jobs automatically</label>
        <div id="wz-auto-hint" class="hint">
          <p><strong>If you tick this:</strong> ${esc(BACKGROUND_ON_COPY)}</p>
          <p><strong>If you leave it empty:</strong> ${esc(BACKGROUND_OFF_COPY)}</p>
          <p>You can change this later in Settings: the days and times under Scheduled searches, and Background access.</p>
        </div>`;
    },
    async save() {
      const want = $('#wz-auto').checked;
      if (want && !wiz.background?.enabled) {
        wiz.background = await withRecentAuth(() => api('/api/account/background', { method: 'POST', body: JSON.stringify({ enabled: true }) }));
      }
      const s = wiz.schedule || await api('/api/schedule');
      wiz.schedule = await api('/api/schedule', {
        method: 'PUT',
        body: JSON.stringify({ enabled: want, times: s.times, days: s.days, skip_holidays: s.skip_holidays, state: s.state }),
      });
      return true;
    },
  },

  resume: {
    title: 'Add your résumé (optional)',
    intro: "JobHuntPA scores how well each job fits you by comparing what the job asks for with what your résumé says you have done. Without a résumé it still finds and filters jobs, but there are no fit scores. You add it on the Documents tab, and it stays in your account.",
    nextLabel: 'Finish',
    html: () => `<label class="check"><input type="checkbox" id="wz-goto-docs" ${wiz.gotoDocs ? 'checked' : ''} /> Take me to the Documents tab when I finish, so I can add it now</label>
      <p class="hint">Or leave this unticked and add it later: it is the Documents tab on the dashboard.</p>`,
    async save() {
      wiz.gotoDocs = $('#wz-goto-docs').checked;
      return true;
    },
  },
};

function buildSteps() {
  if (wiz.short) return ['theme', 'keep'];
  const steps = ['theme', 'titles', 'where', 'extras'];
  if (wiz.features.scheduled_searches) steps.push('auto'); // Phase G
  steps.push('resume');
  return steps;
}

/* ---------- showing a step ---------- */
function renderProgress() {
  const total = wiz.steps.length;
  const def = STEPS[wiz.steps[wiz.idx]];
  setText($('#wiz-count'), `Step ${wiz.idx + 1} of ${total}: ${def.title}`);
  $('#wiz-steps').innerHTML = wiz.steps.map((id, i) => {
    const state = i < wiz.idx ? 'done' : i === wiz.idx ? 'current' : 'todo';
    const word = { done: 'done', current: 'current step', todo: 'not started' }[state];
    return `<li class="wiz-mark is-${state}"${state === 'current' ? ' aria-current="step"' : ''}><span aria-hidden="true">${state === 'done' ? '✓' : i + 1}</span><span class="visually-hidden">Step ${i + 1}: ${esc(STEPS[id].title)}, ${word}</span></li>`;
  }).join('');
  $('#wiz-progress').hidden = false;
}

function go(idx, { focus = true, remember = true } = {}) {
  clearErrors();
  wiz.idx = Math.max(0, Math.min(idx, wiz.steps.length - 1));
  const id = wiz.steps[wiz.idx];
  const def = STEPS[id];
  const total = wiz.steps.length;
  $('#wiz-body').innerHTML = `<fieldset class="wiz-fieldset">
      <legend><h2 id="step-title" tabindex="-1"><span class="visually-hidden">Step ${wiz.idx + 1} of ${total}: </span>${esc(def.title)}</h2></legend>
      <p class="wiz-intro">${esc(def.intro)}</p>
      ${def.html()}
    </fieldset>`;
  def.mount?.();
  renderProgress();
  document.title = `Step ${wiz.idx + 1} of ${total}: ${def.title} · JobHuntPA`;
  $('#wiz-back').hidden = wiz.idx === 0;
  $('#wiz-skip').hidden = !def.skippable;
  setText($('#wiz-next'), def.nextLabel || 'Next');
  $('#wiz-form').hidden = false;
  if (remember) rememberStep(wiz.idx + 1);
  if (focus) $('#step-title').focus();
}

async function advance() {
  if (wiz.idx >= wiz.steps.length - 1) { await finish(); return; }
  go(wiz.idx + 1);
}

async function finish() {
  status('Opening your dashboard…');
  try {
    await api('/api/account/onboarding', { method: 'PUT', body: JSON.stringify({ done: true }) });
    if (wiz.changed) await api('/api/jobs/refilter', { method: 'POST' }).catch(() => {});
  } catch (e) {
    status('');
    showErrors([{ field: null, message: `Finishing didn't work: ${e.message}. Try again.` }]);
    return;
  }
  location.replace(wiz.gotoDocs ? './#docs' : './');
}

async function leave() {
  try { await api('/api/account/onboarding', { method: 'PUT', body: JSON.stringify({ done: true }) }); }
  catch (e) { toast(`That didn't work: ${e.message}`, 'err'); return; }
  location.replace('./');
}

/* ---------- buttons ---------- */
async function onNext(e) {
  e.preventDefault();
  if (wiz.busy) return;
  wiz.busy = true;
  setBusy($('#wiz-next'), true);
  try {
    const def = STEPS[wiz.steps[wiz.idx]];
    clearErrors();
    const ok = def.save ? await def.save() : true;
    if (ok !== false) await advance();
  } catch (err) {
    status('');
    showErrors([{ field: null, message: `Saving didn't work: ${err.message}. Try again, or choose "Skip for now".` }]);
  } finally {
    wiz.busy = false;
    setBusy($('#wiz-next'), false);
  }
}

function onSkip() {
  if (wiz.busy) return;
  advance();
}

function onBack() {
  if (wiz.busy || wiz.idx === 0) return;
  go(wiz.idx - 1);
}

/* ---------- start ---------- */
async function start() {
  let me;
  try { me = await loadSession(); } // signed out: goes to the sign-in page and never returns
  catch (e) { status(''); showErrors([{ field: null, message: e.message }]); return; }
  if (!me.onboarding_step) { location.replace('./'); return; } // finished or skipped already
  if (me.recovery_codes_pending) { location.replace('first-run.html'); return; } // save the codes first
  wiz.me = me;
  try {
    const [info, profile, dealbreakers, pins] = await Promise.all([
      api('/api/account/onboarding'), api('/api/profile'), api('/api/dealbreakers'), api('/api/pins'),
    ]);
    wiz.features = info.features || {};
    wiz.short = info.path === 'short';
    wiz.profile = profile;
    wiz.dealbreakers = dealbreakers;
    wiz.pins = pins;
    if (wiz.features.scheduled_searches) {
      [wiz.background, wiz.schedule] = await Promise.all([api('/api/account/background'), api('/api/schedule')]);
    }
    wiz.steps = buildSteps();
    status('');
    go((info.step || 1) - 1, { remember: false });
  } catch (e) {
    status('');
    showErrors([{ field: null, message: `Couldn't load your setup: ${e.message}. Reload the page to try again.` }]);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initTagInputs($('#wiz-form'));
  $('#wiz-form').addEventListener('submit', onNext);
  $('#wiz-skip').addEventListener('click', onSkip);
  $('#wiz-back').addEventListener('click', onBack);
  $('#wiz-leave').addEventListener('click', leave);
  start();
});
