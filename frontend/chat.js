/* JobHuntPA job chat: one module for the dashboard's side panel (index.html, beside the job cards) and the job
   page's Chats tab (job.html). Both pages hold the same controls, by id: #chat-title, #chat-threads (a select of the
   earlier chats), #chat-new, #chat-delete, #chat-jobs, #chat-messages, #chat-form, #chat-input, #chat-web,
   #chat-status, #chat-send. The job page adds #chat-thread-list (the chats as a list, beside the conversation) and
   #chat-starters (the first questions, in their own card); the panel adds #chat-close.
   Every call to the API is here: /api/chats (the threads about a set of jobs), POST /api/chats (a new one),
   GET /api/chats/{id}, POST /api/chats/{id}/messages, DELETE /api/chats/{id}. Needs common.js. */
'use strict';

const CHAT_MAX_JOBS = 12;
const CHAT_STARTERS = {
  one: ['What would I actually be doing day to day?', 'Honestly, how well do I fit? What are my biggest gaps?',
    'How should I address my gaps in an interview?', 'What should I ask them in an interview?'],
  many: ['Compare these jobs for me.', 'Which should I apply to first, and why?', 'Which one fits my experience best?'],
};

/* The open chat: { jobIds, jobs, threadId, threads, messages, busy }, or null. */
const chatState = {
  chat: null,
  token: 0, // bumps when the chat switches jobs, so late answers for old jobs are dropped
  returnFocus: null, // finds the control that opened the panel, for focus when it closes
  // What the page provides: findJob(id) -> a job to label; showJob(id) shows a job's card (the dashboard);
  // panel: the chat is the dashboard's side panel (it can close, and covers the page on narrow screens).
  host: { findJob: () => null, showJob: null, panel: false },
};

/* Beside the cards on wide screens the panel is a complementary region; on narrower screens it covers the
   page, so there it is a modal dialog: the page behind is inert and Escape closes it (WCAG 2.4.3, 2.4.11). */
const chatCovers = window.matchMedia('(max-width: 1099px)');
function syncChatMode() {
  const panel = $('#chat-panel');
  if (!panel) return;
  const modal = !panel.hidden && chatCovers.matches;
  panel.setAttribute('role', modal ? 'dialog' : 'complementary');
  if (modal) panel.setAttribute('aria-modal', 'true'); else panel.removeAttribute('aria-modal');
  ['.skip-link', '.app-header', '#main'].forEach((sel) => { const el = $(sel); if (el) el.inert = modal; });
}

function chatJobLabel(j) { return { id: j.id, title: j.title, company: j.company, url: j.url }; }

/* The finder for the control that had focus when the chat opened (the dashboard redraws its cards, so the same
   control may be a new element by the time the chat closes). */
function chatFocusFinder(el) {
  if (!el || el === document.body) return () => null;
  const act = el.dataset?.act;
  const jobId = el.closest?.('[data-job-id]')?.dataset.jobId;
  const id = el.id;
  return () => {
    if (el.isConnected && !el.closest('[hidden]')) return el;
    if (id && document.getElementById(id)) return document.getElementById(id);
    if (act && jobId) return document.querySelector(`.job-card[data-job-id="${jobId}"] [data-act="${act}"]`);
    return null;
  };
}

/* Open the chat about these jobs (one or several). `jobs` are labels for jobs the page can't find itself;
   `focus` false leaves the keyboard focus where it is (the job page opens its tab without stealing it). */
async function openChat(ids, { jobs = [], focus = true } = {}) {
  const jobIds = [...new Set(ids)].sort((a, b) => a - b);
  if (!jobIds.length) return;
  if (jobIds.length > CHAT_MAX_JOBS) { toast(`Chat about up to ${CHAT_MAX_JOBS} jobs at a time; select fewer.`, 'err'); return; }
  const token = ++chatState.token;
  const given = new Map(jobs.map((j) => [j.id, j]));
  chatState.chat = {
    jobIds, threadId: null, threads: [], messages: [], busy: false,
    jobs: jobIds.map((id) => given.get(id) || chatState.host.findJob(id)).filter(Boolean).map(chatJobLabel),
  };
  const panel = $('#chat-panel');
  if (panel) {
    if (panel.hidden) chatState.returnFocus = chatFocusFinder(document.activeElement);
    panel.hidden = false;
    document.body.classList.add('chat-open');
    syncChatMode();
  }
  setChatStatus('');
  renderChat();
  if (focus) $('#chat-input').focus();
  try {
    const threads = await api(`/api/chats?job_ids=${jobIds.join(',')}`);
    if (token !== chatState.token) return;
    chatState.chat.threads = threads;
    if (threads.length) await loadChatThread(threads[0].id, token);
    else renderChat();
  } catch (e) { if (token === chatState.token) setChatStatus(`Could not load earlier chats: ${e.message}`, true); }
}

async function loadChatThread(id, token = chatState.token) {
  const t = await api(`/api/chats/${id}`);
  if (token !== chatState.token) return;
  applyThread(t);
  renderChat();
}

function applyThread(t) {
  const c = chatState.chat;
  c.threadId = t.id;
  c.messages = t.messages;
  if (t.jobs.length) c.jobs = t.jobs;
  const summary = { id: t.id, title: t.title, updated_at: t.updated_at };
  c.threads = [summary, ...c.threads.filter((x) => x.id !== t.id)];
}

/* Close the panel (the dashboard). On the job page the chat stays with its tab; this only forgets it. */
function closeChat({ restoreFocus = true } = {}) {
  const panel = $('#chat-panel');
  const wasOpen = !!panel && !panel.hidden;
  const hadFocus = !!panel && panel.contains(document.activeElement);
  chatState.token++;
  chatState.chat = null;
  if (panel) {
    panel.hidden = true;
    document.body.classList.remove('chat-open');
    syncChatMode();
  }
  if (wasOpen && restoreFocus && hadFocus) (chatState.returnFocus?.() || $('#main')).focus();
  chatState.returnFocus = null;
}

function setChatStatus(text, isError = false) {
  const el = $('#chat-status');
  el.textContent = text;
  el.classList.toggle('err-text', isError);
}

/* How many chats there are about these jobs, for a page that shows a count (the job page's tab). */
function chatCount() { return chatState.chat ? chatState.chat.threads.length : 0; }

function renderChat() {
  const c = chatState.chat;
  if (!c) return;
  const n = c.jobIds.length;
  const current = c.threads.find((t) => t.id === c.threadId);
  $('#chat-title').textContent = current ? (current.title || 'Chat') : n === 1 ? 'Ask about this job' : `Ask about ${n} jobs`;
  $('#chat-input').placeholder = `Ask anything about ${n === 1 ? 'this job' : 'these jobs'}…`;
  const jobsList = $('#chat-jobs');
  if (jobsList) {
    jobsList.innerHTML = c.jobs.map((j, i) => `<li><button class="chat-job" type="button" data-id="${j.id}" title="Show this job's card">
      ${n > 1 ? `<strong>${i + 1}</strong> ` : ''}${esc(j.title || '—')} · ${esc(j.company || '—')}<span class="visually-hidden">: show its card</span></button></li>`).join('');
    jobsList.hidden = !chatState.host.showJob;
    if (chatState.host.showJob) $$('#chat-jobs .chat-job').forEach((b) => b.addEventListener('click', () => chatState.host.showJob(Number(b.dataset.id))));
  }
  const sel = $('#chat-threads');
  const opts = c.threads.map((t) => `<option value="${t.id}" ${t.id === c.threadId ? 'selected' : ''}>${esc(t.title || 'Chat')} · ${esc(fmtDate(t.updated_at).split(',')[0])}</option>`);
  if (!c.threadId) opts.unshift('<option value="" selected>New chat</option>');
  sel.innerHTML = opts.join('');
  sel.disabled = c.busy || c.threads.length === 0;
  renderChatThreadList();
  // A button that is about to be hidden or disabled hands keyboard focus to the question box.
  const strand = [$('#chat-delete'), $('#chat-new'), $('#chat-send'), sel].includes(document.activeElement)
    || $('#chat-messages').contains(document.activeElement);
  $('#chat-delete').hidden = !c.threadId;
  $('#chat-new').disabled = c.busy || !c.threadId;
  $('#chat-send').disabled = c.busy;
  renderChatMessages();
  renderChatStarters();
  if (strand && (!document.activeElement || document.activeElement === document.body || document.activeElement.disabled
      || document.activeElement.closest('[hidden]'))) $('#chat-input').focus();
}

/* The job page lists the chats beside the conversation (the select stays for narrow screens). */
function renderChatThreadList() {
  const list = $('#chat-thread-list');
  if (!list) return;
  const c = chatState.chat;
  list.innerHTML = c.threads.map((t) => `<li><button class="chat-thread${t.id === c.threadId ? ' is-current' : ''}" type="button" data-id="${t.id}" aria-current="${t.id === c.threadId ? 'true' : 'false'}" ${c.busy ? 'disabled' : ''}>
      <span class="chat-thread__title">${esc(t.title || 'Chat')}</span><span class="u-small u-muted">${esc(fmtDate(t.updated_at).split(',')[0])}</span></button></li>`).join('')
    || '<li class="u-small u-muted chat-thread-none">No chats about this job yet.</li>';
  $$('#chat-thread-list .chat-thread').forEach((b) => b.addEventListener('click', async () => {
    try { await loadChatThread(Number(b.dataset.id)); } catch (err) { setChatStatus(`Could not load that chat: ${err.message}`, true); }
  }));
}

function chatStartersHtml(c) {
  const starters = CHAT_STARTERS[c.jobIds.length === 1 ? 'one' : 'many'];
  return `<p class="muted" id="chat-starters-intro">The model sees ${c.jobIds.length === 1 ? 'the ad' : 'each ad'}, your documents, the fit assessment,
      the company info and any cover-letter draft. Try:</p>
      <ul class="chat-starters" aria-labelledby="chat-starters-intro">${starters.map((q) => `<li><button class="btn btn-ghost btn-sm chat-starter" type="button">${esc(q)}</button></li>`).join('')}</ul>`;
}

/* The job page keeps the first questions in their own card, whatever the conversation shows. */
function renderChatStarters() {
  const box = $('#chat-starters');
  if (!box) return;
  box.innerHTML = chatStartersHtml(chatState.chat);
  box.querySelectorAll('.chat-starter').forEach((b) => b.addEventListener('click', () => sendChat(b.textContent)));
}

function renderChatMessages() {
  const c = chatState.chat;
  const box = $('#chat-messages');
  if (!c.messages.length && !c.busy) {
    if ($('#chat-starters')) {
      box.innerHTML = '<p class="muted chat-empty">No messages yet. Ask a question below, or pick one of the starters.</p>';
      return;
    }
    box.innerHTML = chatStartersHtml(c);
    box.querySelectorAll('.chat-starter').forEach((b) => b.addEventListener('click', () => sendChat(b.textContent)));
    return;
  }
  box.innerHTML = c.messages.map((m) => {
    if (m.role === 'user') {
      return `<div class="chat-msg user"><span class="visually-hidden">You asked: </span>${esc(m.content)}${
        m.web_search ? '<div class="chat-meta"><span aria-hidden="true">🌐 </span>with web search</div>' : ''}</div>`;
    }
    const links = m.sources || [];
    const sources = links.length
      ? `<div class="chat-sources"><span class="muted">${links.some((x) => x.cited) ? 'Sources:' : 'Pages the web search found:'}</span>
          ${links.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title || x.url)}</a>`).join(' · ')}</div>` : '';
    return `<div class="chat-msg assistant"><span class="visually-hidden">Answer: </span>${mdToHtml(m.content)}${sources}
      <div class="chat-meta">${esc(m.model || '')}${m.created_at ? ` · ${esc(fmtDate(m.created_at))}` : ''}
        <button class="link chat-copy" type="button" data-id="${m.id}">Copy<span class="visually-hidden"> this answer</span></button></div></div>`;
  }).join('') + (c.busy ? '<div class="chat-msg assistant chat-thinking" aria-hidden="true"><span></span><span></span><span></span></div>' : '');
  box.querySelectorAll('.chat-copy').forEach((b) => b.addEventListener('click', async () => {
    const msg = c.messages.find((m) => String(m.id) === b.dataset.id);
    try { await navigator.clipboard.writeText(msg.content); toast('Copied', 'ok', 1500); } catch { toast('Could not copy', 'err', 2500); }
  }));
  box.scrollTop = box.scrollHeight;
}

async function sendChat(text) {
  const c = chatState.chat;
  text = String(text || '').trim();
  if (!c || !text || c.busy) return;
  const web = $('#chat-web').checked;
  const token = chatState.token;
  c.busy = true;
  c.messages.push({ role: 'user', content: text, web_search: web });
  $('#chat-input').value = '';
  setChatStatus(web ? 'Searching the web… this can take a minute or two.' : 'Thinking…');
  renderChat();
  try {
    const body = JSON.stringify(c.threadId ? { message: text, web_search: web } : { job_ids: c.jobIds, message: text, web_search: web });
    const t = await api(c.threadId ? `/api/chats/${c.threadId}/messages` : '/api/chats', { method: 'POST', body });
    if (token !== chatState.token) return;
    applyThread(t);
    setChatStatus('');
    announce('Answer received: it is the last message in the conversation.');
  } catch (e) {
    if (token !== chatState.token) return;
    c.messages.pop();
    if (!$('#chat-input').value) $('#chat-input').value = text; // nothing typed is lost (WCAG 3.3.7)
    setChatStatus(`Failed: ${e.message}`, true);
  }
  c.busy = false;
  renderChat();
  document.dispatchEvent(new CustomEvent('chat:changed'));
}

/* Put a question in the box without sending it (the job page's "Ask about this company" prompts). */
function draftChatQuestion(text) {
  const input = $('#chat-input');
  input.value = text;
  input.focus();
}

/* Wire the controls. `host`: see chatState.host. */
function initChat(host = {}) {
  Object.assign(chatState.host, host);
  const panel = $('#chat-panel');
  if (panel) {
    $('#chat-close').addEventListener('click', () => closeChat());
    panel.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); closeChat(); }
    });
    chatCovers.addEventListener('change', syncChatMode);
  }
  $('#chat-form').addEventListener('submit', (e) => { e.preventDefault(); sendChat($('#chat-input').value); });
  $('#chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat($('#chat-input').value); }
  });
  $('#chat-new').addEventListener('click', () => {
    const c = chatState.chat;
    if (!c || c.busy) return;
    c.threadId = null;
    c.messages = [];
    setChatStatus('');
    renderChat();
    $('#chat-input').focus();
  });
  $('#chat-threads').addEventListener('change', async (e) => {
    if (!e.target.value) return;
    try { await loadChatThread(Number(e.target.value)); } catch (err) { setChatStatus(`Could not load that chat: ${err.message}`, true); }
  });
  $('#chat-delete').addEventListener('click', async () => {
    const c = chatState.chat;
    if (!c?.threadId || !confirm('Delete this conversation?')) return;
    try {
      await api(`/api/chats/${c.threadId}`, { method: 'DELETE' });
      c.threads = c.threads.filter((t) => t.id !== c.threadId);
      c.threadId = null;
      c.messages = [];
      if (c.threads.length) await loadChatThread(c.threads[0].id); else renderChat();
      document.dispatchEvent(new CustomEvent('chat:changed'));
    } catch (e) { setChatStatus(`Delete failed: ${e.message}`, true); }
  });
}
