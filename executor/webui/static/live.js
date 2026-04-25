/* SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0 */
/* Live mode: talks to /webui/messages, supports message selection + save. */

(function () {
  "use strict";

  const chatEl = document.getElementById("chat");
  const input = document.getElementById("composer-input");
  const sendBtn = document.getElementById("composer-send");
  const recBtn = document.getElementById("rec-btn");
  const modeChip = document.getElementById("mode-chip");
  const memberChip = document.getElementById("member-chip");
  const botAvatar = document.getElementById("bot-avatar");
  const botName = document.getElementById("bot-name");
  const dayLabel = document.getElementById("day-label");
  const selectBar = document.getElementById("select-bar");
  const selectCount = document.getElementById("select-count");
  const selectClear = document.getElementById("select-clear");
  const selectSave = document.getElementById("select-save");
  const memberModal = document.getElementById("member-modal");
  const memberList = document.getElementById("member-list");
  const memberClose = document.getElementById("member-close");
  const saveModal = document.getElementById("save-modal");
  const saveTitle = document.getElementById("save-title");
  const saveSummary = document.getElementById("save-summary");
  const saveHint = document.getElementById("save-hint");
  const saveCancel = document.getElementById("save-cancel");
  const saveConfirm = document.getElementById("save-confirm");
  const toast = document.getElementById("toast");
  const attachBtn = document.getElementById("attach-btn");
  const fileInput = document.getElementById("file-input");
  const groupChip = document.getElementById("group-chip");
  const groupModal = document.getElementById("group-modal");
  const groupList = document.getElementById("group-list");
  const groupClose = document.getElementById("group-close");

  let state = null;
  let currentMember = null;
  let currentGroup = "";   // "" = personal session; "notes"|"files"|"notifications" = group
  let selectMode = false;
  let turns = [];         // full session log, in order
  let t0 = performance.now();
  let sessionId = null;   // identifies this browser tab to the server
  let eventSource = null; // SSE subscriber for outbound deliveries

  // ── Helpers ──────────────────────────────────────────────────────────────
  function fmtTime() {
    const d = new Date();
    return d.getHours().toString().padStart(2, "0") + ":" +
           d.getMinutes().toString().padStart(2, "0");
  }

  function showToast(text, ms = 2200) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toast.hidden = true; }, ms);
  }

  // ── Rendering ────────────────────────────────────────────────────────────
  function fmtSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function renderAttachment(att) {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    thumb.textContent = att.type === "image" ? "🖼️"
                      : att.type === "pdf"   ? "📄"
                      : "📎";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = att.name || "attachment";
    const size = document.createElement("div");
    size.className = "size";
    size.textContent = fmtSize(att.size_bytes);
    const metaCol = document.createElement("div");
    metaCol.className = "meta-file";
    metaCol.appendChild(name);
    metaCol.appendChild(size);
    const wrap = document.createElement("div");
    wrap.className = "attach";
    wrap.appendChild(thumb);
    wrap.appendChild(metaCol);
    return wrap;
  }

  function renderTurn(turn) {
    const row = document.createElement("div");
    row.className = "row " + (turn.from === "user" ? "user" : "bot");
    if (selectMode) row.classList.add("selectable");

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (turn.kind === "file" && turn.attachment) {
      bubble.appendChild(renderAttachment(turn.attachment));
    }

    if (turn.text) {
      const body = document.createElement("div");
      body.innerHTML = window.renderMd(turn.text);
      bubble.appendChild(body);
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    const t = document.createElement("span");
    t.textContent = fmtTime();
    meta.appendChild(t);
    if (turn.from === "user") {
      const tick = document.createElement("span");
      tick.className = "tick";
      tick.textContent = "✓✓";
      meta.appendChild(tick);
    }
    bubble.appendChild(meta);

    row.appendChild(bubble);
    row.dataset.turnIdx = String(turns.indexOf(turn));
    row.addEventListener("click", () => {
      if (!selectMode) return;
      const idx = parseInt(row.dataset.turnIdx, 10);
      turns[idx]._selected = !turns[idx]._selected;
      row.classList.toggle("selected", turns[idx]._selected);
      refreshSelectBar();
    });

    chatEl.appendChild(row);
    chatEl.scrollTop = chatEl.scrollHeight;
    return row;
  }

  function renderTyping() {
    const row = document.createElement("div");
    row.className = "row bot typing";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    for (let i = 0; i < 3; i++) {
      const d = document.createElement("span");
      d.className = "dot";
      bubble.appendChild(d);
    }
    row.appendChild(bubble);
    chatEl.appendChild(row);
    chatEl.scrollTop = chatEl.scrollHeight;
    return row;
  }

  // ── Selection ────────────────────────────────────────────────────────────
  function setSelectMode(on) {
    selectMode = on;
    recBtn.classList.toggle("active", on);
    document.querySelectorAll(".row").forEach(r => {
      r.classList.toggle("selectable", on);
      if (!on) r.classList.remove("selected");
    });
    if (!on) {
      turns.forEach(t => { delete t._selected; });
    }
    refreshSelectBar();
  }

  function refreshSelectBar() {
    const n = turns.filter(t => t._selected).length;
    if (n === 0 || !selectMode) {
      selectBar.hidden = true;
      return;
    }
    selectBar.hidden = false;
    selectCount.textContent = `${n} selected`;
  }

  selectClear.addEventListener("click", () => {
    turns.forEach(t => { delete t._selected; });
    document.querySelectorAll(".row.selected").forEach(r => r.classList.remove("selected"));
    refreshSelectBar();
  });

  selectSave.addEventListener("click", () => {
    const picked = turns.filter(t => t._selected);
    if (!picked.length) {
      showToast("Click some messages first to mark them, then Save.");
      return;
    }
    saveHint.textContent = `${picked.length} message(s) will be saved as a cassette.`;
    saveModal.hidden = false;
    saveTitle.focus();
  });

  recBtn.addEventListener("click", () => {
    const newMode = !selectMode;
    setSelectMode(newMode);
    if (newMode) {
      showToast("Click any bubble to mark it. Then tap Save.", 3000);
    }
  });

  // ── Save cassette modal ──────────────────────────────────────────────────
  saveCancel.addEventListener("click", () => { saveModal.hidden = true; });

  saveConfirm.addEventListener("click", async () => {
    const title = saveTitle.value.trim();
    if (!title) {
      saveTitle.focus();
      return;
    }
    const picked = turns.filter(t => t._selected);
    if (!picked.length) {
      saveModal.hidden = true;
      return;
    }
    // Renumber ts_offset_ms so the cassette starts at 0
    const baseOffset = picked[0].ts_offset_ms;
    const cassetteMessages = picked.map(t => {
      const m = {
        from: t.from,
        text: t.text,
        ts_offset_ms: Math.max(0, t.ts_offset_ms - baseOffset),
      };
      if (t.member_id) m.member_id = t.member_id;
      if (t.from === "bot") m.typing_ms = t.typing_ms || 600;
      return m;
    });

    saveConfirm.disabled = true;
    try {
      const r = await fetch("/webui/cassettes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          summary: saveSummary.value.trim(),
          messages: cassetteMessages,
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      saveModal.hidden = true;
      saveTitle.value = "";
      saveSummary.value = "";
      setSelectMode(false);
      showToast(`Saved: ${data.name} · open ${data.play_url}`, 4000);
    } catch (e) {
      showToast(`Save failed: ${e.message}`);
    } finally {
      saveConfirm.disabled = false;
    }
  });

  // ── Member picker ────────────────────────────────────────────────────────
  function openMemberPicker() {
    memberList.innerHTML = "";
    for (const [id, m] of Object.entries(state.members)) {
      const row = document.createElement("div");
      const disabled = !m.telegram;
      row.className = "member-row" + (id === currentMember ? " active" : "") + (disabled ? " disabled" : "");
      row.innerHTML = `
        <div class="avatar" style="width:32px;height:32px;font-size:16px">${m.emoji || m.name[0]}</div>
        <div>
          <div class="name">${m.name}</div>
          <div class="id">${id}${disabled ? " (no telegram id — read-only)" : ""}</div>
        </div>
      `;
      if (!disabled) {
        row.addEventListener("click", () => {
          currentMember = id;
          memberChip.textContent = `as ${m.name}`;
          memberModal.hidden = true;
        });
      }
      memberList.appendChild(row);
    }
    memberModal.hidden = false;
  }

  memberChip.addEventListener("click", openMemberPicker);
  memberClose.addEventListener("click", () => { memberModal.hidden = true; });

  // ── Send a message ───────────────────────────────────────────────────────
  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    if (!currentMember) {
      showToast("Pick a member first");
      openMemberPicker();
      return;
    }
    input.value = "";

    // Render user turn
    const offset = Math.round(performance.now() - t0);
    const userTurn = {
      from: "user",
      member_id: currentMember,
      text,
      ts_offset_ms: offset,
    };
    turns.push(userTurn);
    renderTurn(userTurn);

    // Typing indicator
    const typingRow = renderTyping();
    const tStart = performance.now();
    try {
      const r = await fetch("/webui/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text, member_id: currentMember, session_id: sessionId, group: currentGroup,
        }),
      });
      const data = await r.json();
      typingRow.remove();
      if (!r.ok) {
        const errTurn = {
          from: "bot",
          text: `⚠️ ${data.detail || r.status}`,
          ts_offset_ms: Math.round(performance.now() - t0),
          typing_ms: 0,
        };
        turns.push(errTurn);
        renderTurn(errTurn);
        return;
      }
      const latency = Math.round(performance.now() - tStart);
      const botTurn = {
        from: "bot",
        text: data.reply || "(no reply)",
        ts_offset_ms: Math.round(performance.now() - t0),
        typing_ms: Math.min(900, Math.max(300, latency)),
      };
      turns.push(botTurn);
      renderTurn(botTurn);
    } catch (e) {
      typingRow.remove();
      showToast(`Send failed: ${e.message}`);
    }
  }

  sendBtn.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // ── File upload ──────────────────────────────────────────────────────────
  attachBtn.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    if (!currentMember) {
      showToast("Pick a member first");
      openMemberPicker();
      return;
    }
    const caption = input.value.trim();
    input.value = "";

    // User-side bubble with attachment chip + caption
    const offset = Math.round(performance.now() - t0);
    const userTurn = {
      from: "user",
      member_id: currentMember,
      kind: "file",
      attachment: {
        type: file.type.startsWith("image/") ? "image"
              : file.type === "application/pdf" ? "pdf" : "file",
        name: file.name,
        size_bytes: file.size,
      },
      text: caption,
      ts_offset_ms: offset,
    };
    turns.push(userTurn);
    renderTurn(userTurn);

    const typingRow = renderTyping();
    const form = new FormData();
    form.append("file", file);
    form.append("member_id", currentMember);
    form.append("session_id", sessionId);
    form.append("text", caption);
    form.append("group", currentGroup);

    const tStart = performance.now();
    try {
      const r = await fetch("/webui/upload", { method: "POST", body: form });
      const data = await r.json();
      typingRow.remove();
      if (!r.ok) {
        const errTurn = {
          from: "bot",
          text: `⚠️ ${data.detail || r.status}`,
          ts_offset_ms: Math.round(performance.now() - t0),
          typing_ms: 0,
        };
        turns.push(errTurn);
        renderTurn(errTurn);
        return;
      }
      const latency = Math.round(performance.now() - tStart);
      const botTurn = {
        from: "bot",
        text: data.reply || "(no reply)",
        ts_offset_ms: Math.round(performance.now() - t0),
        typing_ms: Math.min(900, Math.max(300, latency)),
      };
      turns.push(botTurn);
      renderTurn(botTurn);
    } catch (e) {
      typingRow.remove();
      showToast(`Upload failed: ${e.message}`);
    }
  });

  // ── SSE outbound subscriber ──────────────────────────────────────────────
  function openStream() {
    if (eventSource) eventSource.close();
    const qs = new URLSearchParams({ session: sessionId });
    if (currentGroup) qs.set("group", currentGroup);
    eventSource = new EventSource(`/webui/stream?${qs.toString()}`);
    eventSource.addEventListener("message", (ev) => {
      let payload;
      try { payload = JSON.parse(ev.data); } catch { return; }
      const botTurn = {
        from: "bot",
        text: payload.text || "",
        ts_offset_ms: Math.round(performance.now() - t0),
        typing_ms: 0,
        _async: true,    // marker: came from executor, not inline POST
      };
      turns.push(botTurn);
      renderTurn(botTurn);
    });
    eventSource.addEventListener("error", () => {
      // EventSource auto-reconnects; just log.
      console.warn("SSE error, will retry");
    });
  }

  // ── Group picker ─────────────────────────────────────────────────────────
  function applyGroup(gid) {
    currentGroup = gid || "";
    if (currentGroup && state.groups && state.groups[currentGroup]) {
      const g = state.groups[currentGroup];
      groupChip.textContent = currentGroup;
      groupChip.dataset.purpose = g.purpose || "";
      groupChip.title = g.description || "";
      // Disable composer for read-only groups (notifications)
      const readOnly = g.purpose === "notifications";
      input.disabled = readOnly;
      input.placeholder = readOnly
        ? "(read-only — agents post here)"
        : "Message";
    } else {
      groupChip.textContent = "personal";
      groupChip.dataset.purpose = "";
      groupChip.title = "Click to switch group";
      input.disabled = false;
      input.placeholder = "Message";
    }
    // Update URL so reload is sticky
    const url = new URL(location.href);
    if (currentGroup) url.searchParams.set("group", currentGroup);
    else url.searchParams.delete("group");
    history.replaceState(null, "", url.toString());
    // Reopen SSE on the new channel
    openStream();
  }

  function openGroupPicker() {
    if (!state || !state.groups) return;
    groupList.innerHTML = "";
    // "personal" option (no group)
    const personalRow = document.createElement("div");
    personalRow.className = "member-row" + (!currentGroup ? " active" : "");
    personalRow.innerHTML = `
      <div class="avatar" style="width:32px;height:32px;font-size:16px">👤</div>
      <div>
        <div class="name">personal</div>
        <div class="id">your own session — no group default</div>
      </div>
    `;
    personalRow.addEventListener("click", () => {
      applyGroup("");
      groupModal.hidden = true;
    });
    groupList.appendChild(personalRow);

    for (const [gid, g] of Object.entries(state.groups)) {
      const row = document.createElement("div");
      row.className = "member-row" + (gid === currentGroup ? " active" : "");
      const emoji = g.purpose === "notes" ? "📝"
                  : g.purpose === "files" ? "📎"
                  : g.purpose === "notifications" ? "🔔"
                  : "💬";
      row.innerHTML = `
        <div class="avatar" style="width:32px;height:32px;font-size:16px">${emoji}</div>
        <div>
          <div class="name">${gid}</div>
          <div class="id">${g.description || g.purpose}</div>
        </div>
      `;
      row.addEventListener("click", () => {
        applyGroup(gid);
        groupModal.hidden = true;
      });
      groupList.appendChild(row);
    }
    groupModal.hidden = false;
  }

  groupChip.addEventListener("click", openGroupPicker);
  groupClose.addEventListener("click", () => { groupModal.hidden = true; });

  // ── Boot ─────────────────────────────────────────────────────────────────
  async function boot() {
    dayLabel.textContent = new Date().toLocaleDateString(undefined, {
      weekday: "long", month: "short", day: "numeric"
    });

    try {
      const r = await fetch("/webui/state");
      state = await r.json();
    } catch (e) {
      showToast("Failed to reach server");
      return;
    }
    botAvatar.textContent = state.bot.emoji || "🌤️";
    botName.textContent = state.bot.name || "Aaka";
    modeChip.textContent = state.mode;
    modeChip.dataset.mode = state.mode;
    document.title = `${state.bot.name || "Aaka"} — ${state.mode}`;

    // Session: persist per mode so demo/live don't share. Refresh on mode change.
    const lsKey = `aaka.webui.session.${state.mode}`;
    sessionId = localStorage.getItem(lsKey);
    if (!sessionId) {
      sessionId = state.session_id;
      localStorage.setItem(lsKey, sessionId);
    }

    // Pick a default member (first with telegram id)
    for (const [id, m] of Object.entries(state.members)) {
      if (m.telegram) { currentMember = id; break; }
    }
    if (currentMember) {
      memberChip.textContent = `as ${state.members[currentMember].name}`;
    } else {
      memberChip.textContent = "no senders";
    }

    // Apply ?group= from URL (sticky across reload).
    const urlGroup = new URLSearchParams(location.search).get("group") || "";
    if (urlGroup && state.groups && state.groups[urlGroup]) {
      applyGroup(urlGroup);    // also opens stream
    } else {
      applyGroup("");
    }

    // Welcome bubble — context-aware on group + member.
    const memberName = currentMember ? state.members[currentMember].name : "there";
    let hint;
    if (currentGroup && state.groups[currentGroup]) {
      const g = state.groups[currentGroup];
      if (g.purpose === "notes") {
        hint = `**notes** group · as ${memberName}. Anything you type lands as a note tagged \`#${g.default_tag || "inbox"}\`. Slash commands still work.`;
      } else if (g.purpose === "files") {
        hint = `**files** group · as ${memberName}. Attach a file with 📎 — it'll route via Smart Drop. Plain text falls through to normal commands.`;
      } else if (g.purpose === "notifications") {
        hint = `**notifications** group. Agents and system health post here. Composer is read-only — use the *personal* group or a different one to talk back.`;
      } else {
        hint = `**${currentGroup}** group · as ${memberName}.`;
      }
    } else if (state.mode === "demo") {
      hint = `Hi ${memberName}! You're chatting against the **demo** family. Try \`/today\`, \`/tasks\`, \`/menu\`, or \`/buy grocery\`. Tap the group chip to try **notes** / **files** / **notifications**.`;
    } else {
      hint = `Hi ${memberName}! You're chatting **live**. Try \`/today\` or just type. Tap the group chip to switch context.`;
    }
    const welcome = {
      from: "bot",
      text: hint,
      ts_offset_ms: 0,
      typing_ms: 0,
    };
    turns.push(welcome);
    renderTurn(welcome);

    if (!input.disabled) input.focus();
  }

  boot();
})();
