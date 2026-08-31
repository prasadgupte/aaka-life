/* executor/console/static/console.js
 * Tab routing + Console tab (chat/SSE/upload) + Status tab (capability board + logs).
 * Tasks tab is an <iframe> loading /tasks/ (reskinned taskboard).
 */
'use strict';

(function () {
  const TABS = ['console', 'tasks', 'status'];
  let statusFetched = false;
  let tasksLoaded = false;

  // ── Tab routing ─────────────────────────────────────────────────────────
  function activateTab(name) {
    if (!TABS.includes(name)) name = 'console';
    document.querySelectorAll('.console-tab').forEach((btn) => {
      const active = btn.dataset.tab === name;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', String(active));
    });
    document.querySelectorAll('.console-panel').forEach((panel) => {
      panel.style.display = panel.id === `panel-${name}` ? 'flex' : 'none';
    });
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);

    if (name === 'tasks' && !tasksLoaded) {
      const f = document.getElementById('tasks-frame');
      if (f) { f.src = '/tasks/'; tasksLoaded = true; }
    }
    if (name === 'status' && !statusFetched) {
      statusFetched = true;
      fetchStatus();
      fetchLogs();
    }
  }

  document.querySelectorAll('.console-tab').forEach((btn) => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });
  window.addEventListener('hashchange', () => {
    activateTab((location.hash || '#console').slice(1));
  });

  // ════════════════════════ CONSOLE TAB ════════════════════════
  const chat = document.getElementById('console-chat');
  const emptyState = document.getElementById('console-empty');
  const memberSel = document.getElementById('console-member');
  const input = document.getElementById('console-input');
  const sendBtn = document.getElementById('console-send');
  const attachBtn = document.getElementById('console-attach');
  const fileInput = document.getElementById('console-file');
  const errorTip = document.getElementById('console-error-tip');
  const pill = document.getElementById('console-pill');

  let sessionId = null;
  let messageCount = 0;
  let eventSource = null;

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function nowTime() {
    return new Date().toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' });
  }

  // Render bot text as markdown via the shared md.js renderer (links open in a
  // new tab, images render inline). Falls back to plain text if md.js is absent.
  function renderBubbleText(span, text) {
    if (typeof window.renderMd === 'function') {
      span.innerHTML = window.renderMd(text);
    } else {
      span.textContent = text || '';
    }
  }

  function hideEmpty() {
    if (emptyState) emptyState.style.display = 'none';
  }

  function addBubble(role, text, opts = {}) {
    hideEmpty();
    messageCount++;
    const row = document.createElement('div');
    row.className = `console-msg console-msg--${role}`;
    const bubble = document.createElement('div');
    bubble.className = 'console-bubble' + (opts.error ? ' console-bubble--error' : '');
    if (opts.attachment) {
      bubble.appendChild(renderConsoleAttachment(opts.attachment));
    }
    const span = document.createElement('span');
    span.className = 'console-bubble-text';
    // Bot replies render markdown (links → new-tab anchors, images inline);
    // user/echo/error messages stay plain text to avoid any injection surface.
    if (role === 'bot' && !opts.error && text) {
      renderBubbleText(span, text);
    } else {
      span.textContent = text || '';
    }
    bubble.appendChild(span);
    const meta = document.createElement('span');
    meta.className = 'console-bubble-meta';
    meta.innerHTML = esc(nowTime()) + (role === 'you' ? ' <span class="console-tick">✓✓</span>' : '');
    bubble.appendChild(meta);
    row.appendChild(bubble);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return span;
  }

  function renderConsoleAttachment(att) {
    const wrap = document.createElement('div');
    wrap.className = 'console-attach';
    const thumb = document.createElement('div');
    thumb.className = 'console-attach-thumb';
    thumb.textContent = '📄';
    const meta = document.createElement('div');
    meta.className = 'console-attach-meta';
    meta.innerHTML = `${esc(att.name)}<br>${esc(att.size || '')}`;
    wrap.appendChild(thumb);
    wrap.appendChild(meta);
    if (att.uploading) {
      const bar = document.createElement('div');
      bar.className = 'console-attach-bar';
      wrap.appendChild(bar);
    }
    return wrap;
  }

  function addTyping() {
    hideEmpty();
    const row = document.createElement('div');
    row.className = 'console-msg console-msg--bot';
    row.id = 'console-typing-row';
    row.innerHTML =
      '<div class="console-bubble"><span class="console-spark">✻</span>' +
      '<span class="console-typing"><i></i><i></i><i></i></span></div>';
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
  }
  function removeTyping() {
    document.getElementById('console-typing-row')?.remove();
  }

  function setPillOffline(off) {
    if (!pill) return;
    pill.textContent = off ? 'offline' : 'live';
    pill.style.color = off ? 'var(--aaka-berry)' : '';
  }

  async function loadState() {
    try {
      const r = await fetch('/webui/state');
      const data = await r.json();
      sessionId = data.session_id;
      const members = data.members || {};
      memberSel.innerHTML = '<option value="">— who? —</option>' +
        Object.keys(members).map((id) =>
          `<option value="${esc(id)}">${esc(members[id].name || id)}</option>`).join('');
      openStream();
    } catch (e) {
      setPillOffline(true);
    }
  }

  function openStream() {
    if (!sessionId) return;
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/webui/stream?session=' + encodeURIComponent(sessionId));
    eventSource.addEventListener('message', (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        removeTyping();
        streamInto(addBubble('bot', ''), payload.text || '');
      } catch (_) { /* ignore */ }
    });
    eventSource.onerror = () => setPillOffline(true);
    eventSource.onopen = () => setPillOffline(false);
  }

  function streamInto(span, text) {
    // Stream plain text char-by-char (markdown can't be parsed mid-token), then
    // swap in the rendered markdown on the last frame so links/images appear.
    const chars = Array.from(text);
    let i = 0;
    function tick() {
      if (i >= chars.length) {
        renderBubbleText(span, text);
        chat.scrollTop = chat.scrollHeight;
        return;
      }
      const step = Math.max(1, Math.round(chars.length / 60));
      span.textContent += chars.slice(i, i + step).join('');
      i += step;
      chat.scrollTop = chat.scrollHeight;
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  function showMemberError() {
    if (!errorTip) return;
    errorTip.hidden = false;
    memberSel.classList.add('console-member--error');
    setTimeout(() => {
      errorTip.hidden = true;
      memberSel.classList.remove('console-member--error');
    }, 3000);
  }

  function setSending(on) {
    input.disabled = on;
    sendBtn.disabled = on;
    sendBtn.innerHTML = on ? '<span class="console-send-spinner"></span>' : '➤';
  }

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    if (!memberSel.value) { showMemberError(); return; }

    addBubble('you', text);
    input.value = '';
    setSending(true);
    addTyping();

    try {
      const r = await fetch('/webui/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text, member_id: memberSel.value, session_id: sessionId, group: '',
        }),
      });
      if (!r.ok) throw new Error('http ' + r.status);
      const data = await r.json();
      removeTyping();
      if (data.reply) streamInto(addBubble('bot', ''), data.reply);
    } catch (e) {
      removeTyping();
      addBubble('bot', 'Something went wrong — try again.', { error: true });
    } finally {
      setSending(false);
      input.focus();
    }
  }

  async function uploadFile(file) {
    if (!memberSel.value) { showMemberError(); return; }
    addBubble('you', '', { attachment: { name: file.name, size: humanSize(file.size), uploading: true } });
    setSending(true);
    addTyping();
    const fd = new FormData();
    fd.append('file', file);
    fd.append('member_id', memberSel.value);
    fd.append('session_id', sessionId);
    fd.append('text', input.value.trim());
    input.value = '';
    try {
      const r = await fetch('/webui/upload', { method: 'POST', body: fd });
      if (!r.ok) throw new Error('http ' + r.status);
      const data = await r.json();
      removeTyping();
      if (data.reply) streamInto(addBubble('bot', ''), data.reply);
    } catch (e) {
      removeTyping();
      addBubble('bot', 'Upload failed — try again.', { error: true });
    } finally {
      setSending(false);
    }
  }

  function humanSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  // Chip handling
  document.body.addEventListener('click', (e) => {
    const chip = e.target.closest('.console-chip');
    if (!chip) return;
    if (chip.dataset.tabJump) { activateTab(chip.dataset.tabJump); return; }
    if (chip.dataset.cmd) {
      input.value = chip.dataset.cmd;
      input.focus();
    }
  });

  sendBtn?.addEventListener('click', sendMessage);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  attachBtn?.addEventListener('click', () => fileInput.click());
  fileInput?.addEventListener('change', () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
    fileInput.value = '';
  });

  // Drag-to-chat
  if (chat) {
    chat.addEventListener('dragover', (e) => { e.preventDefault(); chat.classList.add('drag-over'); });
    chat.addEventListener('dragleave', () => chat.classList.remove('drag-over'));
    chat.addEventListener('drop', (e) => {
      e.preventDefault();
      chat.classList.remove('drag-over');
      if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
    });
  }

  // ════════════════════════ STATUS TAB ════════════════════════
  const TIER_ORDER = ['0', '1', '2', '3'];
  let lastStatusData = null;
  let lastStatusTime = 0;

  function toast(msg) {
    const host = document.getElementById('console-toast-host');
    if (!host) return;
    const div = document.createElement('div');
    div.className = 'console-toast';
    div.textContent = msg;
    host.appendChild(div);
    setTimeout(() => div.remove(), 2000);
  }

  function renderCard(check) {
    const ok = !!check.ok;
    const iconCls = ok ? 'status-icon--ok' : 'status-icon--off';
    const icon = ok ? '✓' : '🔌';
    const cardCls = 'card status-card' + (ok ? ' status-card--ok' : '');
    let fixHtml = '';
    if (!ok && check.fix) {
      fixHtml =
        `<details class="status-fix"><summary class="mono-label">What to do</summary>` +
        `<div class="status-fix-body well" title="Click to copy" data-fix="${esc(check.fix)}">` +
        `<code>${esc(check.fix)}</code>` +
        `<span class="status-copy-hint mono-label">tap to copy</span></div></details>`;
    }
    return `<div class="${cardCls}">
      <div class="status-card-head">
        <span class="status-icon ${iconCls}">${icon}</span>
        <span class="status-label">${esc(check.label || check.id || '')}</span>
      </div>
      ${check.note ? `<p class="status-note">${esc(check.note)}</p>` : ''}
      ${fixHtml}
    </div>`;
  }

  function renderStatus(data) {
    const body = document.getElementById('status-body');
    if (!body) return;
    if (data.error) {
      body.innerHTML =
        `<div class="card status-card status-card--err">
          <div class="status-card-head">
            <span class="status-icon status-icon--err">✕</span>
            <span class="status-label">Status check failed</span>
          </div>
          <p class="status-note" style="font-family:var(--aaka-font-mono);font-size:12px;">${esc(data.error)}</p>
          <button class="btn btn--secondary" id="status-retry" style="margin-top:12px;">Try again</button>
        </div>`;
      document.getElementById('status-retry')?.addEventListener('click', () => fetchStatus(true));
      return;
    }
    const checks = data.checks || [];
    const tiers = data.tiers || {};
    const byTier = {};
    for (const c of checks) (byTier[String(c.tier)] = byTier[String(c.tier)] || []).push(c);

    let html = '';
    const seen = new Set();
    for (const t of TIER_ORDER.concat(Object.keys(byTier))) {
      if (seen.has(t) || !byTier[t]) continue;
      seen.add(t);
      const label = (tiers[t] && tiers[t].label) ? tiers[t].label : `Tier ${t}`;
      html += `<section><h3 class="status-tier-head">Tier ${t} — ${esc(label)}</h3>`;
      html += `<div class="status-grid">${byTier[t].map(renderCard).join('')}</div></section>`;
    }
    body.innerHTML = html || '<p class="status-note">No checks returned.</p>';
    setupCopyFix();
  }

  function setupCopyFix() {
    document.querySelectorAll('.status-fix-body').forEach((el) => {
      el.addEventListener('click', () => {
        const fix = el.dataset.fix || '';
        if (navigator.clipboard) navigator.clipboard.writeText(fix).then(() => toast('✓ Copied'));
      });
    });
  }

  function showSkeleton() {
    const body = document.getElementById('status-body');
    if (body) {
      body.innerHTML =
        '<span class="gradient-bar gradient-bar--wide"></span>' +
        '<div class="status-grid" style="margin-top:16px;">' +
        '<div class="status-skeleton"></div><div class="status-skeleton"></div><div class="status-skeleton"></div></div>';
    }
  }

  async function fetchStatus(force) {
    const banner = document.getElementById('status-cache-banner');
    const now = Date.now();
    if (!force && lastStatusData && now - lastStatusTime < 60000) {
      if (banner) banner.innerHTML = '<div class="well status-cache">Showing cached result · Refresh for live data</div>';
      renderStatus(lastStatusData);
      return;
    }
    if (banner) banner.innerHTML = '';
    showSkeleton();
    try {
      const r = await fetch('/console/status/data');
      const data = await r.json();
      lastStatusData = data;
      lastStatusTime = Date.now();
      renderStatus(data);
    } catch (e) {
      renderStatus({ error: 'could not reach status endpoint' });
    }
    const lc = document.getElementById('status-last-checked');
    if (lc) lc.textContent = 'Last checked: ' + nowTime();
  }

  function highlightLog(lines) {
    if (!lines || !lines.length) return '<span class="status-log-empty">— no log file —</span>';
    return lines.map((line) => {
      const safe = esc(line);
      if (/ERROR|Exception/.test(line)) return `<span class="status-log-err">${safe}</span>`;
      if (/WARNING|WARN/.test(line)) return `<span class="status-log-warn">${safe}</span>`;
      return safe;
    }).join('\n');
  }

  async function fetchLogs() {
    const qw = document.getElementById('log-queueworker');
    const sensor = document.getElementById('log-sensor');
    try {
      const r = await fetch('/console/status/logs');
      const data = await r.json();
      if (qw) qw.innerHTML = highlightLog(data.queueworker);
      if (sensor) sensor.innerHTML = highlightLog(data.sensor);
      const age = 'updated ' + nowTime();
      document.getElementById('log-age-qw').textContent = age;
      document.getElementById('log-age-sensor').textContent = age;
    } catch (e) {
      if (qw) qw.innerHTML = '<span class="status-log-err">— could not fetch logs —</span>';
      if (sensor) sensor.innerHTML = '<span class="status-log-err">— could not fetch logs —</span>';
    }
  }

  document.getElementById('status-refresh')?.addEventListener('click', () => {
    fetchStatus(true);
    fetchLogs();
  });

  // ── Init ──────────────────────────────────────────────────────────────
  loadState();
  activateTab((location.hash || '#console').slice(1));
})();
