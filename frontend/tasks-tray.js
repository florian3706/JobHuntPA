/* JobHuntPA: the tasks tray in the header (docs/design/open-source.md §1.13), on the dashboard and the job page.

   A disclosure button with the number of AI tasks running, and the list it opens: what each one is, for which job,
   company or document, since when, and how far it is where that is known. The list comes from the server
   (GET /api/tasks, backend/tasks.py), so it survives a reload; it holds kinds and ids, never text, and this page
   names the jobs and documents from what it has loaded (taskNames, filled in by app.js and job.js). A change of the
   count is announced politely (announce() in common.js). Nothing here cancels a task: that is left out on purpose.

   Polling: every few seconds while a task runs or the list is open; otherwise once a minute while the page is in
   view (a scheduled search, another tab), and soon after this page sends a change to the server (common.js fires
   "jobhunt:api-write"), which is how a task this page starts appears at once. */
'use strict';

/* Filled in by the page: (workspaceId, id) -> a name, or null when the page hasn't loaded it. */
const taskNames = {
  job: () => null, // -> { title, company }
  document: () => null, // -> filename
  workspace: () => null, // -> workspace name
};

const TASKS_FAST_MS = 2500;
const TASKS_IDLE_MS = 60000;
const tray = { count: null, tasks: [], timer: null, open: false, loading: false, again: false, drawn: null };

function taskTime(iso) {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  const today = at.toDateString() === new Date().toDateString();
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return today ? time : `${at.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`;
}

function taskWorkspaceNote(task) {
  if (task.workspace_id === currentWorkspace()) return '';
  const name = taskNames.workspace(task.workspace_id);
  return name ? ` (workspace ${name})` : ' (another workspace)';
}

/* What a task is for, as escaped HTML: a link to the job where there is one. */
function taskSubjectHtml(task) {
  const ws = task.workspace_id;
  if (task.subject === 'job' || task.subject === 'company') {
    const id = task.job_ids[0];
    const job = id ? taskNames.job(ws, id) : null;
    if (task.subject === 'company') return job?.company ? `for ${esc(job.company)}` : 'for a company';
    const more = task.job_ids.length > 1 ? ` and ${task.job_ids.length - 1} more job${task.job_ids.length > 2 ? 's' : ''}` : '';
    const name = job?.title ? `“${esc(job.title)}”${job.company ? ` at ${esc(job.company)}` : ''}` : 'a job';
    return `for <a href="job.html?id=${encodeURIComponent(id)}&amp;ws=${encodeURIComponent(ws)}">${name}</a>${more}`;
  }
  if (task.subject === 'document') {
    const name = taskNames.document(ws, task.document_id);
    return name ? `for “${esc(name)}”` : 'for a document';
  }
  return '';
}

function taskItemHtml(task, steps) {
  const what = `${esc(task.what)}${task.part_label ? ` (${esc(task.part_label)})` : ''}`;
  const subject = taskSubjectHtml(task);
  const p = task.progress;
  const progress = p ? `<span class="task-progress">${p.done} of ${p.total} done</span>` : '';
  const since = taskTime(task.started_at);
  return `<li class="task-item">
      <span class="task-what"><strong>${what}</strong>${subject ? ` ${subject}` : ''}${esc(taskWorkspaceNote(task))}</span>
      <span class="task-meta muted">${since ? `since ${esc(since)}` : ''}${progress ? `${since ? ' · ' : ''}${progress}` : ''}</span>
      ${steps.length ? `<ul class="task-steps" aria-label="Steps of this task">${steps.map((s) => taskItemHtml(s, [])).join('')}</ul>` : ''}
    </li>`;
}

function renderTasksList() {
  const list = document.getElementById('tasks-list');
  if (!list) return;
  const top = tray.tasks.filter((t) => t.parent_id === null);
  const html = top.map((t) => taskItemHtml(t, tray.tasks.filter((s) => s.parent_id === t.id))).join('');
  if (tray.drawn !== html) { // redrawn only when it changed, and a focused link keeps its focus
    const focused = list.contains(document.activeElement) ? document.activeElement.getAttribute('href') : null;
    list.innerHTML = html;
    tray.drawn = html;
    const again = focused && Array.from(list.querySelectorAll('a')).find((a) => a.getAttribute('href') === focused);
    if (again) again.focus();
    else if (focused) document.getElementById('tasks-btn').focus(); // its task ended: back to the button
  }
  list.hidden = !top.length;
  document.getElementById('tasks-empty').hidden = Boolean(top.length);
}

function renderTasksCount(count) {
  const badge = document.getElementById('tasks-count');
  setText(badge, String(count));
  badge.classList.toggle('is-busy', count > 0);
  document.getElementById('tasks-btn').classList.toggle('is-busy', count > 0);
  if (tray.count !== null && tray.count !== count) {
    announce(count ? `${count} AI task${count === 1 ? '' : 's'} running.` : 'No AI tasks running.');
  }
  tray.count = count;
}

function scheduleTasks() {
  clearTimeout(tray.timer);
  const busy = tray.count > 0 || tray.open;
  if (document.visibilityState !== 'visible') return; // visibilitychange looks again
  tray.timer = setTimeout(refreshTasks, busy ? TASKS_FAST_MS : TASKS_IDLE_MS);
}

async function refreshTasks() {
  if (tray.loading) { tray.again = true; return; }
  tray.loading = true;
  try {
    const data = await api('/api/tasks');
    tray.tasks = Array.isArray(data?.tasks) ? data.tasks : [];
    renderTasksCount(Number(data?.count) || 0);
    if (tray.open) renderTasksList();
  } catch { /* the server is busy or restarting: the next look */ }
  tray.loading = false;
  if (tray.again) { tray.again = false; refreshTasks(); return; }
  scheduleTasks();
}

function initTasksTray() {
  const menu = document.getElementById('tasks-menu');
  const btn = document.getElementById('tasks-btn');
  const panel = document.getElementById('tasks-panel');
  if (!menu || !btn || !panel) return;
  const open = (yes) => {
    tray.open = yes;
    btn.setAttribute('aria-expanded', String(yes));
    panel.hidden = !yes;
    if (yes) { renderTasksList(); refreshTasks(); }
  };
  btn.addEventListener('click', () => open(panel.hidden));
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); open(false); btn.focus(); } });
  btn.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !panel.hidden) { e.preventDefault(); open(false); } });
  document.addEventListener('click', (e) => { if (!panel.hidden && !e.target.closest('#tasks-menu')) open(false); });
  menu.addEventListener('focusout', (e) => {
    if (!panel.hidden && e.relatedTarget && !menu.contains(e.relatedTarget)) open(false);
  });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshTasks(); });
  document.addEventListener('jobhunt:api-write', () => {
    setTimeout(refreshTasks, 400);
    setTimeout(refreshTasks, 2000); // a run may wait a moment for the worker
  });
  refreshTasks();
}

document.addEventListener('DOMContentLoaded', async () => {
  try { await loadSession(); } catch { return; } // signed out: the page is leaving for the sign-in page
  initTasksTray();
});
