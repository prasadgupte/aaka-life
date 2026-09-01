/* Aaka Taskboard — app.js */
'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

const state = {
  tasks:    [],
  meta:     { owners: [], agents: [], projects: [], tags: [], members: [], counts: {} },
  filters:  { due: 'all', owner: '', agent: '', tag: '', q: '', status: 'open' },
  loading:  true,
  tagSearchOpen: false,
  tagSearch: '',
  selected: new Set(),   // task IDs
  snoozePending: null,   // { id } or { bulk: true }
};

const TAG_SHOW_LIMIT = 10;

// ── API ───────────────────────────────────────────────────────────────────────

const API = (window._TASKBOARD_API_BASE || '') + '/api';

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function fetchTasks() {
  const f = state.filters;
  const params = new URLSearchParams();
  if (f.status)                params.set('status', f.status);
  if (f.owner)                 params.set('owner',  f.owner);
  if (f.agent)                 params.set('agent',  f.agent);
  if (f.tag)                   params.set('tag',    f.tag);
  if (f.q)                     params.set('q',      f.q);
  if (f.due && f.due !== 'all') params.set('due',   f.due);
  const data = await apiFetch(`/tasks?${params}`);
  state.tasks = data.tasks;
  return data;
}

async function fetchMeta() {
  state.meta = await apiFetch('/meta');
}

async function patchTask(id, body) {
  return apiFetch(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
}

async function deleteTask(id) {
  return apiFetch(`/tasks/${id}`, { method: 'DELETE' });
}

async function createTask(body) {
  return apiFetch('/tasks', { method: 'POST', body: JSON.stringify(body) });
}

async function bulkAction(ids, action, extra = {}) {
  return apiFetch('/tasks/bulk-action', {
    method: 'POST',
    body: JSON.stringify({ ids: [...ids], action, ...extra }),
  });
}

// ── Polling ───────────────────────────────────────────────────────────────────

let pollTimer = null;

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      await refresh(false);
      document.getElementById('poll-indicator').classList.add('live');
      setTimeout(() => document.getElementById('poll-indicator').classList.remove('live'), 600);
    } catch (_) { /* silent */ }
  }, 30_000);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function dueBucket(dateStr) {
  if (!dateStr) return 'nodate';
  const d = new Date(dateStr + 'T00:00:00');
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const diff = Math.round((d - t) / 86400000);
  if (diff < 0)   return 'overdue';
  if (diff === 0)  return 'today';
  if (diff <= 7)   return 'week';
  return 'later';
}

function dueLabel(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr + 'T00:00:00');
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const diff = Math.round((d - t) / 86400000);
  if (diff < 0)   return `${Math.abs(diff)}d overdue`;
  if (diff === 0)  return 'Today';
  if (diff === 1)  return 'Tomorrow';
  if (diff <= 6)   return d.toLocaleDateString('en', { weekday: 'short' });
  if (diff <= 30)  return d.toLocaleDateString('en', { day: 'numeric', month: 'short' });
  return d.toLocaleDateString('en', { day: 'numeric', month: 'short', year: 'numeric' });
}

function prioIcon(priority) {
  if (priority === 'high')   return '🔴';
  if (priority === 'medium') return '⭐';
  return '○';   // always render — hidden via CSS until hover
}

function nextPrio(p) {
  return p === 'normal' ? 'high' : p === 'high' ? 'medium' : 'normal';
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Render: filter bar ────────────────────────────────────────────────────────

function renderMeta() {
  const m = state.meta;
  const f = state.filters;

  // Due tabs
  const dueEl = document.getElementById('due-chips');
  const dueBuckets = [
    { key: 'all',     label: 'All open',    cls: 'due-all' },
    { key: 'overdue', label: '🔥 Overdue',  cls: 'due-overdue', count: m.counts.overdue },
    { key: 'today',   label: '⏰ Today',    cls: 'due-today',   count: m.counts.today },
    { key: 'week',    label: '📅 This week',cls: 'due-week',    count: m.counts.week },
    { key: 'nodate',  label: '📥 No date',  cls: 'due-nodate',  count: m.counts.nodate },
  ];
  const statusTabs = f.status === 'done'
    ? [{ key: 'back', label: '← Open tasks', cls: 'due-all', isBack: true }]
    : [...dueBuckets, { key: 'done', label: '✓ Done', cls: 'done', isDone: true }];

  dueEl.innerHTML = statusTabs.map(b => {
    const active = b.isDone
      ? f.status === 'done'
      : (!b.isBack && f.due === b.key && f.status !== 'done');
    const countHtml = b.count ? `<span class="chip-count">${b.count}</span>` : '';
    return `<button class="chip ${b.cls}${active ? ' active' : ''}" data-due="${b.key}">${b.label}${countHtml}</button>`;
  }).join('');

  // Owner chips
  const ownerEl = document.getElementById('owner-chips');
  if (m.owners.length) {
    const btns = [{ id: '', label: 'Everyone' }, ...m.owners.map(o => ({ id: o, label: o }))];
    ownerEl.innerHTML = btns.map(o =>
      `<button class="chip owner${f.owner === o.id ? ' active' : ''}" data-owner="${esc(o.id)}">${esc(o.label)}</button>`
    ).join('');
  } else {
    ownerEl.innerHTML = '';
  }

  // Agent chips
  const agentEl  = document.getElementById('agent-chips');
  const sepAgent = document.getElementById('sep-agent');
  const showAgents = m.agents.length > 1 || (m.agents.length === 1 && m.agents[0] !== 'taskboard');
  if (showAgents) {
    sepAgent.style.display = '';
    const btns = [{ id: '', label: 'All agents' }, ...m.agents.map(a => ({ id: a, label: a }))];
    agentEl.innerHTML = btns.map(a =>
      `<button class="chip agent${f.agent === a.id ? ' active' : ''}" data-agent="${esc(a.id)}">${esc(a.label)}</button>`
    ).join('');
  } else {
    sepAgent.style.display = 'none';
    agentEl.innerHTML = '';
  }

  renderTagChips();

  document.getElementById('stats').textContent = `${m.counts.open ?? 0} open`;

  // Owner select in modal
  const ownerSel = document.getElementById('f-owner');
  const curOwner = ownerSel.value;
  ownerSel.innerHTML = '<option value="">— nobody —</option>' +
    (m.members.length ? m.members : m.owners).map(o =>
      `<option value="${esc(o)}"${curOwner === o ? ' selected' : ''}>${esc(o)}</option>`
    ).join('');
}

function renderTagChips() {
  const m     = state.meta;
  const f     = state.filters;
  const tagEl = document.getElementById('tag-chips');
  const sep   = document.getElementById('sep-tag');

  if (!m.tags.length) { sep.style.display = 'none'; tagEl.innerHTML = ''; return; }
  sep.style.display = '';

  const allTags = m.tags;  // [{name, count}] sorted by count desc
  const visibleTags = state.tagSearchOpen
    ? allTags.filter(t => !state.tagSearch || t.name.toLowerCase().includes(state.tagSearch.toLowerCase()))
    : allTags.slice(0, TAG_SHOW_LIMIT);

  const hasMore = !state.tagSearchOpen && allTags.length > TAG_SHOW_LIMIT;

  // If active tag isn't in top-10, prepend it so it's always visible
  const activeNotVisible = f.tag && !state.tagSearchOpen && !visibleTags.find(t => t.name === f.tag);
  const activeChip = activeNotVisible
    ? `<button class="chip tag active" data-tag="${esc(f.tag)}">${esc(f.tag)} ×</button>`
    : '';

  const tagHtml = visibleTags.map(t =>
    `<button class="chip tag${f.tag === t.name ? ' active' : ''}" data-tag="${esc(t.name)}" title="${t.count} task${t.count !== 1 ? 's' : ''}">${esc(t.name)}<span class="chip-count">${t.count}</span></button>`
  ).join('');

  let controlHtml = '';
  if (hasMore) {
    controlHtml = `<button class="chip tag tag-expand" id="tag-expand-btn">+${allTags.length - TAG_SHOW_LIMIT} more</button>`;
  } else if (state.tagSearchOpen) {
    controlHtml = `<div class="tag-search-wrap"><input class="tag-search-input" id="tag-search-input" placeholder="Filter tags…" value="${esc(state.tagSearch)}"><button class="tag-search-close" id="tag-search-close">×</button></div>`;
  }

  tagEl.innerHTML = activeChip + tagHtml + controlHtml;

  // Re-attach inline search listeners (they're recreated each render)
  document.getElementById('tag-expand-btn')?.addEventListener('click', e => {
    e.stopPropagation();
    state.tagSearchOpen = true; state.tagSearch = '';
    renderTagChips();
    document.getElementById('tag-search-input')?.focus();
  });
  document.getElementById('tag-search-close')?.addEventListener('click', e => {
    e.stopPropagation();
    state.tagSearchOpen = false; state.tagSearch = '';
    renderTagChips();
  });
  document.getElementById('tag-search-input')?.addEventListener('input', e => {
    state.tagSearch = e.target.value;
    renderTagChips();
  });
  document.getElementById('tag-search-input')?.addEventListener('keydown', e => {
    if (e.key === 'Escape') { state.tagSearchOpen = false; state.tagSearch = ''; renderTagChips(); }
  });
}

// ── Render: task list ─────────────────────────────────────────────────────────

function renderTasks() {
  const area  = document.getElementById('task-area');
  const tasks = state.tasks;

  if (state.loading) {
    area.innerHTML = '<div class="loading"><div class="spinner"></div> Loading…</div>';
    renderBulkBar();
    return;
  }

  if (!tasks.length) {
    const msgs = { overdue: ['🎉', 'Nothing overdue'], today: ['✅', 'Nothing due today'], done: ['📭', 'No completed tasks'] };
    const [icon, msg] = msgs[state.filters.due] || msgs[state.filters.status] || ['🌤️', 'No tasks here'];
    area.innerHTML = `<div class="empty"><div class="icon">${icon}</div><p>${msg}</p></div>`;
    renderBulkBar();
    return;
  }

  if (state.filters.status === 'done') {
    area.innerHTML = tasks.map(taskRowHtml).join('');
  } else {
    const groups = { overdue: [], today: [], week: [], later: [], nodate: [] };
    for (const t of tasks) groups[dueBucket(t.due_date)]?.push(t);

    const sections = [
      { key: 'overdue', label: '🔥 Overdue' },
      { key: 'today',   label: '⏰ Due today' },
      { key: 'week',    label: '📅 This week' },
      { key: 'later',   label: '🗓 Later' },
      { key: 'nodate',  label: '📥 No due date' },
    ];
    const filterDue = state.filters.due;
    let html = '';
    for (const s of sections) {
      const rows = groups[s.key];
      if (!rows.length) continue;
      if (filterDue && filterDue !== 'all' && filterDue !== s.key) continue;
      html += `<div class="section-head">${s.label}</div>`;
      html += rows.map(taskRowHtml).join('');
    }
    area.innerHTML = html || '<div class="empty"><div class="icon">🌤️</div><p>All clear</p></div>';
  }

  // Restore selected state visually
  for (const id of state.selected) {
    area.querySelector(`.task-select[data-id="${id}"]`)?.classList.add('sel-checked');
    area.querySelector(`.task-row[data-id="${id}"]`)?.classList.add('task-selected');
  }

  attachTaskListeners(area);
  renderBulkBar();
}

function taskRowHtml(t) {
  const bucket = dueBucket(t.due_date);
  const isDone = t.status === 'done';
  const rowCls = isDone ? 'done-row' : bucket === 'overdue' ? 'overdue' : bucket === 'today' ? 'today' : '';
  const prio   = t.priority || 'normal';
  const icon   = prioIcon(prio);
  const prioCls = `prio-badge${prio === 'normal' ? ' prio-normal' : ''}`;

  // Due display
  let dueHtml = '';
  if (t.due_date) {
    const dueCls = bucket === 'overdue' ? 'meta-due overdue due-clickable' : bucket === 'today' ? 'meta-due today due-clickable' : 'meta-due due-clickable';
    dueHtml = `<span class="${dueCls}" data-action="set-due" data-id="${esc(t.id)}" title="Click to change">📅 ${esc(dueLabel(t.due_date))}</span>`;
  } else if (!isDone) {
    dueHtml = `<span class="meta-due due-clickable" data-action="set-due" data-id="${esc(t.id)}" title="Set due date" style="cursor:pointer;opacity:0.4">📅 set date</span>`;
  }

  // Tags — clickable to filter
  const tagHtml = (t.tags || []).map(tag =>
    `<button class="meta-pill meta-tag tag-filter-pill" data-tag="${esc(tag)}" title="Filter by ${esc(tag)}">${esc(tag)}</button>`
  ).join('');

  const ownerHtml   = t.owner   ? `<span class="meta-pill meta-owner">@${esc(t.owner)}</span>` : '';
  const agentHtml   = t.agent && t.agent !== 'taskboard' ? `<span class="meta-pill meta-agent">${esc(t.agent)}</span>` : '';
  const projectHtml = t.project ? `<span class="meta-pill meta-project">${esc(t.project)}</span>` : '';
  const snoozeHtml  = t.snooze_until ? `<span class="meta-pill meta-snooze">💤 ${esc(t.snooze_until)}</span>` : '';
  const recurHtml   = t.recurring ? `<span class="meta-pill meta-recurring">🔁 ${esc(t.repeats || 'recurring')}</span>` : '';

  const checkCls  = isDone ? 'task-check checked' : 'task-check';
  const actionsHtml = isDone ? '' : `
    <div class="task-actions">
      <button class="action-btn" data-action="snooze" data-id="${esc(t.id)}" title="Snooze">💤</button>
      <button class="action-btn danger" data-action="delete" data-id="${esc(t.id)}" data-title="${esc(t.title)}" title="Delete">✕</button>
    </div>`;

  return `
    <div class="task-row ${rowCls}" data-id="${esc(t.id)}">
      <div class="task-select" data-id="${esc(t.id)}" title="Select"></div>
      <div class="${checkCls}" data-id="${esc(t.id)}" data-title="${esc(t.title)}"></div>
      <div class="task-body">
        <div class="task-title" data-id="${esc(t.id)}">${esc(t.title)}</div>
        <div class="task-meta">
          ${dueHtml}${ownerHtml}${projectHtml}${tagHtml}${agentHtml}${snoozeHtml}${recurHtml}
        </div>
      </div>
      <div style="display:flex;align-items:start;gap:4px">
        <span class="${prioCls}" data-id="${esc(t.id)}" data-prio="${esc(prio)}" title="Priority: ${prio} — click to cycle">${icon}</span>
        ${actionsHtml}
      </div>
    </div>`;
}

function attachTaskListeners(area) {
  area.querySelectorAll('.task-select').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); toggleSelect(el.dataset.id); });
  });
  area.querySelectorAll('.task-check').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); onComplete(el.dataset.id, el.dataset.title); });
  });
  area.querySelectorAll('.prio-badge').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); onCyclePriority(el.dataset.id, el.dataset.prio); });
  });
  area.querySelectorAll('.task-title').forEach(el => {
    el.addEventListener('dblclick', e => { e.stopPropagation(); startTitleEdit(el.dataset.id, el); });
  });
  area.querySelectorAll('[data-action="snooze"]').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); showSnoozePopover(el.dataset.id, el); });
  });
  area.querySelectorAll('[data-action="delete"]').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); onDelete(el.dataset.id, el.dataset.title); });
  });
  area.querySelectorAll('[data-action="set-due"]').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); startDueDateEdit(el.dataset.id, el); });
  });
  area.querySelectorAll('.tag-filter-pill').forEach(el => {
    el.addEventListener('click', e => {
      e.stopPropagation();
      state.filters.tag = state.filters.tag === el.dataset.tag ? '' : el.dataset.tag;
      state.filters.status = 'open';
      refresh(true);
    });
  });
}

// ── Selection ─────────────────────────────────────────────────────────────────

function toggleSelect(id) {
  const row    = document.querySelector(`.task-row[data-id="${id}"]`);
  const box    = document.querySelector(`.task-select[data-id="${id}"]`);
  if (state.selected.has(id)) {
    state.selected.delete(id);
    row?.classList.remove('task-selected');
    box?.classList.remove('sel-checked');
  } else {
    state.selected.add(id);
    row?.classList.add('task-selected');
    box?.classList.add('sel-checked');
  }
  renderBulkBar();
}

function selectAll() {
  for (const t of state.tasks) state.selected.add(t.id);
  document.querySelectorAll('.task-select').forEach(el => el.classList.add('sel-checked'));
  document.querySelectorAll('.task-row').forEach(el => el.classList.add('task-selected'));
  renderBulkBar();
}

function clearSelection() {
  state.selected.clear();
  document.querySelectorAll('.task-select').forEach(el => el.classList.remove('sel-checked'));
  document.querySelectorAll('.task-row').forEach(el => el.classList.remove('task-selected'));
  renderBulkBar();
}

function renderBulkBar() {
  let bar = document.getElementById('bulk-bar');
  const n = state.selected.size;

  if (n === 0) { bar?.remove(); return; }

  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'bulk-bar';
    bar.className = 'bulk-bar';
    document.body.appendChild(bar);
  }

  const allVisible   = state.tasks.every(t => state.selected.has(t.id));
  const selectLabel  = allVisible ? 'Deselect all' : `Select all ${state.tasks.length}`;

  bar.innerHTML = `
    <span class="bulk-count">${n} selected</span>
    <button class="bulk-select-all" id="bulk-sel-toggle">${selectLabel}</button>
    <div class="bulk-sep"></div>
    <button class="bulk-btn bulk-done"  id="bulk-done">✓ Done</button>
    <button class="bulk-btn"            id="bulk-snooze-btn">💤 Tomorrow</button>
    <button class="bulk-btn"            id="bulk-snooze3">💤 3 days</button>
    <button class="bulk-btn"            id="bulk-snooze7">💤 1 week</button>
    <div class="bulk-sep"></div>
    <button class="bulk-btn bulk-del"   id="bulk-delete">✕ Delete</button>
    <button class="bulk-btn bulk-clear" id="bulk-clear">× clear</button>
  `;

  bar.querySelector('#bulk-sel-toggle').addEventListener('click', () => {
    allVisible ? clearSelection() : selectAll();
  });
  bar.querySelector('#bulk-done').addEventListener('click', onBulkDone);
  bar.querySelector('#bulk-snooze-btn').addEventListener('click', () => onBulkSnooze(1));
  bar.querySelector('#bulk-snooze3').addEventListener('click', () => onBulkSnooze(3));
  bar.querySelector('#bulk-snooze7').addEventListener('click', () => onBulkSnooze(7));
  bar.querySelector('#bulk-delete').addEventListener('click', onBulkDelete);
  bar.querySelector('#bulk-clear').addEventListener('click', clearSelection);
}

// ── Bulk actions ──────────────────────────────────────────────────────────────

async function onBulkDone() {
  const ids   = [...state.selected];
  const count = ids.length;
  try {
    clearSelection();
    state.tasks = state.tasks.filter(t => !ids.includes(t.id));
    renderTasks();
    showToast(`✓ ${count} task${count !== 1 ? 's' : ''} done`, 'Undo', async () => {
      await bulkAction(ids, 'snooze', { snooze_until: '2099-01-01' }).catch(() => {});
      // Actually undo done by reopening - use individual patches
      await Promise.all(ids.map(id => patchTask(id, { status: 'open' }).catch(() => {})));
      await refresh(true);
    });
    await bulkAction(ids, 'done');
    await fetchMeta(); renderMeta();
  } catch (e) {
    showToast(`Error: ${e.message}`); await refresh(true);
  }
}

async function onBulkSnooze(days) {
  const ids   = [...state.selected];
  const count = ids.length;
  const label = days === 1 ? 'tomorrow' : `${days} days`;
  try {
    clearSelection();
    // Remove snoozed tasks from current visible list (they'll disappear from "open" view)
    state.tasks = state.tasks.filter(t => !ids.includes(t.id));
    renderTasks();
    showToast(`💤 ${count} task${count !== 1 ? 's' : ''} snoozed for ${label}`);
    await bulkAction(ids, 'snooze', { snooze_days: days });
    await fetchMeta(); renderMeta();
  } catch (e) {
    showToast(`Error: ${e.message}`); await refresh(true);
  }
}

async function onBulkDelete() {
  const ids   = [...state.selected];
  const count = ids.length;
  if (!confirm(`Delete ${count} task${count !== 1 ? 's' : ''}? This cannot be undone.`)) return;
  try {
    clearSelection();
    state.tasks = state.tasks.filter(t => !ids.includes(t.id));
    renderTasks();
    showToast(`Deleted ${count} task${count !== 1 ? 's' : ''}`);
    await bulkAction(ids, 'delete');
    await fetchMeta(); renderMeta();
  } catch (e) {
    showToast(`Error: ${e.message}`); await refresh(true);
  }
}

// ── Single task actions ───────────────────────────────────────────────────────

async function onComplete(id, title) {
  state.tasks = state.tasks.filter(t => t.id !== id);
  state.selected.delete(id);
  renderTasks();

  showToast(`✓ "${title}" done`, 'Undo', async () => {
    await patchTask(id, { status: 'open' }).catch(() => {});
    await refresh(true);
  });

  try {
    await patchTask(id, { status: 'done' });
    await fetchMeta(); renderMeta();
  } catch (e) {
    showToast(`Error: ${e.message}`); await refresh(true);
  }
}

async function onDelete(id, title) {
  state.tasks = state.tasks.filter(t => t.id !== id);
  state.selected.delete(id);
  renderTasks();

  showToast(`Deleted "${title}"`, 'Undo', async () => {
    await patchTask(id, { status: 'open' }).catch(() => {});
    await refresh(true);
  });

  try {
    await deleteTask(id);
    await fetchMeta(); renderMeta();
  } catch (e) {
    showToast(`Error: ${e.message}`); await refresh(true);
  }
}

async function onCyclePriority(id, currentPrio) {
  const newPrio = nextPrio(currentPrio);
  const task = state.tasks.find(t => t.id === id);
  if (task) {
    task.priority = newPrio;
    // Update badge in-place without full re-render
    const badge = document.querySelector(`.prio-badge[data-id="${id}"]`);
    if (badge) {
      badge.textContent = prioIcon(newPrio);
      badge.dataset.prio = newPrio;
      badge.className = `prio-badge${newPrio === 'normal' ? ' prio-normal' : ''}`;
      badge.title = `Priority: ${newPrio} — click to cycle`;
    }
  }
  try {
    await patchTask(id, { priority: newPrio });
  } catch (e) {
    showToast(`Error: ${e.message}`); await refresh(true);
  }
}

function startTitleEdit(id, el) {
  const task = state.tasks.find(t => t.id === id);
  if (!task) return;
  const input = document.createElement('input');
  input.className = 'task-title-input';
  input.value = task.title;
  el.replaceWith(input);
  input.focus(); input.select();

  const commit = async () => {
    const newTitle = input.value.trim();
    if (!newTitle || newTitle === task.title) { input.replaceWith(el); return; }
    task.title = newTitle;
    el.textContent = newTitle;
    input.replaceWith(el);
    try { await patchTask(id, { title: newTitle }); }
    catch (e) { showToast(`Error: ${e.message}`); await refresh(true); }
  };

  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') input.replaceWith(el);
  });
}

function startDueDateEdit(id, el) {
  const task = state.tasks.find(t => t.id === id);
  if (!task) return;
  const input = document.createElement('input');
  input.type = 'date'; input.className = 'due-input';
  input.value = task.due_date || '';
  el.replaceWith(input); input.focus();

  const commit = async () => {
    const newDate = input.value;
    if (newDate === task.due_date) { input.replaceWith(el); return; }
    task.due_date = newDate;
    el.textContent = newDate ? `📅 ${dueLabel(newDate)}` : '📅 set date';
    input.replaceWith(el);
    renderTasks();
    try {
      await patchTask(id, { due_date: newDate || null });
      await fetchMeta(); renderMeta();
    } catch (e) { showToast(`Error: ${e.message}`); await refresh(true); }
  };

  input.addEventListener('change', () => input.blur());
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', e => { if (e.key === 'Escape') input.replaceWith(el); });
}

// ── Snooze ────────────────────────────────────────────────────────────────────

function showSnoozePopover(id, anchorEl) {
  const pop  = document.getElementById('snooze-pop');
  const rect = anchorEl.getBoundingClientRect();
  pop.style.display = '';
  pop.style.top  = `${rect.bottom + 4}px`;
  pop.style.left = `${rect.left}px`;
  state.snoozePending = { id };

  const outside = e => {
    if (!pop.contains(e.target)) { pop.style.display = 'none'; document.removeEventListener('click', outside, true); }
  };
  setTimeout(() => document.addEventListener('click', outside, true), 0);
}

document.getElementById('snooze-pop').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn || !state.snoozePending) return;
  document.getElementById('snooze-pop').style.display = 'none';

  const { id } = state.snoozePending;
  const days   = parseInt(btn.dataset.days, 10);

  if (days === 0) {
    const dateStr = prompt('Snooze until (YYYY-MM-DD):');
    if (!dateStr) return;
    try { await patchTask(id, { snooze_until: dateStr }); showToast(`💤 Snoozed until ${dateStr}`); await refresh(true); }
    catch (e2) { showToast(`Error: ${e2.message}`); }
    return;
  }

  try {
    await patchTask(id, { snooze_days: days });
    showToast(`💤 Snoozed for ${days === 1 ? 'tomorrow' : days + ' days'}`);
    state.tasks = state.tasks.filter(t => t.id !== id);
    state.selected.delete(id);
    renderTasks();
    await fetchMeta(); renderMeta();
  } catch (e2) { showToast(`Error: ${e2.message}`); await refresh(true); }
});

// ── Add task modal ────────────────────────────────────────────────────────────

function openModal() {
  document.getElementById('f-title').value = '';
  document.getElementById('f-due').value   = '';
  document.getElementById('f-owner').value = '';
  document.getElementById('f-priority').value = 'normal';
  document.getElementById('f-tags').value  = state.filters.tag || '';
  document.getElementById('f-desc').value  = '';
  document.getElementById('modal').style.display = 'flex';
  document.getElementById('f-title').focus();
}

function closeModal() { document.getElementById('modal').style.display = 'none'; }

document.getElementById('btn-add').addEventListener('click', openModal);
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-cancel').addEventListener('click', closeModal);
document.getElementById('modal').addEventListener('click', e => { if (e.target === document.getElementById('modal')) closeModal(); });

document.getElementById('modal-save').addEventListener('click', async () => {
  const title = document.getElementById('f-title').value.trim();
  if (!title) { document.getElementById('f-title').focus(); return; }

  const tags = document.getElementById('f-tags').value.split(',').map(s => s.trim()).filter(Boolean);
  const body = {
    title,
    due_date:    document.getElementById('f-due').value || null,
    owner:       document.getElementById('f-owner').value || '',
    priority:    document.getElementById('f-priority').value,
    tags,
    description: document.getElementById('f-desc').value.trim(),
  };

  const btn = document.getElementById('modal-save');
  btn.disabled = true; btn.textContent = 'Adding…';
  try {
    await createTask(body);
    closeModal(); showToast(`✓ Task added: "${title}"`);
    await refresh(true);
  } catch (e) { showToast(`Error: ${e.message}`); }
  finally { btn.disabled = false; btn.textContent = 'Add task'; }
});

document.getElementById('f-title').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('modal-save').click();
});

// ── Filter events ─────────────────────────────────────────────────────────────

document.getElementById('filterbar').addEventListener('click', async e => {
  const chip = e.target.closest('.chip');
  if (!chip || chip.id === 'tag-expand-btn') return;

  if (chip.dataset.due !== undefined) {
    const key = chip.dataset.due;
    if      (key === 'done') { state.filters.status = 'done'; state.filters.due = 'all'; }
    else if (key === 'back') { state.filters.status = 'open'; state.filters.due = 'all'; }
    else                     { state.filters.status = 'open'; state.filters.due = key;   }
  } else if (chip.dataset.owner !== undefined) {
    state.filters.owner = chip.dataset.owner === state.filters.owner ? '' : chip.dataset.owner;
  } else if (chip.dataset.agent !== undefined) {
    state.filters.agent = chip.dataset.agent === state.filters.agent ? '' : chip.dataset.agent;
  } else if (chip.dataset.tag !== undefined) {
    state.filters.tag = chip.dataset.tag === state.filters.tag ? '' : chip.dataset.tag;
    if (!state.filters.tag) { state.tagSearchOpen = false; state.tagSearch = ''; }
  }

  clearSelection();
  await refresh(true);
});

let searchTimer = null;
document.getElementById('search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    state.filters.q = e.target.value.trim();
    clearSelection();
    await refresh(true);
  }, 250);
});

// ── Toast ─────────────────────────────────────────────────────────────────────

let toastTimer = null;

function showToast(msg, undoLabel, undoFn) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  clearTimeout(toastTimer);

  const div = document.createElement('div');
  div.className = 'toast';
  div.innerHTML = `<span>${esc(msg)}</span>`;

  if (undoLabel && undoFn) {
    const btn = document.createElement('button');
    btn.className = 'toast-undo'; btn.textContent = undoLabel;
    btn.addEventListener('click', () => { div.remove(); clearTimeout(toastTimer); undoFn(); });
    div.appendChild(btn);
  }

  document.body.appendChild(div);
  toastTimer = setTimeout(() => {
    div.classList.add('out');
    setTimeout(() => div.remove(), 200);
  }, undoFn ? 5000 : 2500);
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'n' && !e.metaKey && !e.ctrlKey
      && document.activeElement.tagName !== 'INPUT'
      && document.activeElement.tagName !== 'TEXTAREA') {
    e.preventDefault(); openModal();
  }
  if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
    e.preventDefault(); document.getElementById('search').focus();
  }
  if (e.key === 'Escape') {
    closeModal();
    document.getElementById('snooze-pop').style.display = 'none';
    if (state.tagSearchOpen) { state.tagSearchOpen = false; state.tagSearch = ''; renderTagChips(); }
    if (state.selected.size) clearSelection();
  }
});

// ── Refresh ───────────────────────────────────────────────────────────────────

async function refresh(renderMetaToo = false) {
  if (renderMetaToo) {
    await Promise.all([fetchTasks(), fetchMeta()]);
    renderMeta();
  } else {
    await fetchTasks();
  }
  state.loading = false;
  renderTasks();
}

// ── Init ──────────────────────────────────────────────────────────────────────

(async () => {
  state.loading = true;
  renderTasks();
  await refresh(true);
  startPolling();
})();
