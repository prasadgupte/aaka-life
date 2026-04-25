/* SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0 */
/* Cassette player. Loads ../cassettes/<name>.json and animates it inside
   the .scroll container, with optional autoplay. */

(function () {
  "use strict";

  const params = new URLSearchParams(location.search);
  const cassetteName = params.get("play") || "tour";
  const autoplay = params.get("autoplay") !== "0";
  const speed = parseFloat(params.get("speed") || "1") || 1;

  const chatEl = document.getElementById("chat");
  const captionEl = document.getElementById("caption");
  const botAvatarEl = document.getElementById("bot-avatar");
  const botNameEl = document.getElementById("bot-name");
  const dayLabelEl = document.getElementById("day-label");

  const today = new Date().toLocaleDateString(undefined, {
    weekday: "long", month: "short", day: "numeric"
  });
  dayLabelEl.textContent = today;

  function fmtTime() {
    const d = new Date();
    return d.getHours().toString().padStart(2, "0") + ":" +
           d.getMinutes().toString().padStart(2, "0");
  }

  function fmtSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function attachmentChip(att) {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    if (att.type === "image" && att.url) {
      thumb.style.backgroundImage = `url('../cassettes/${att.url}')`;
    } else {
      thumb.textContent = att.type === "pdf" ? "📄" : "📎";
    }
    const meta = document.createElement("div");
    meta.className = "meta-file";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = att.name || "attachment";
    const size = document.createElement("div");
    size.className = "size";
    size.textContent = fmtSize(att.size_bytes);
    meta.appendChild(name);
    meta.appendChild(size);
    const wrap = document.createElement("div");
    wrap.className = "attach";
    wrap.appendChild(thumb);
    wrap.appendChild(meta);
    return wrap;
  }

  function renderTurn(turn) {
    const row = document.createElement("div");
    row.className = "row " + (turn.from === "user" ? "user" : "bot");

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (turn.kind === "file" && turn.attachment) {
      bubble.appendChild(attachmentChip(turn.attachment));
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

  function sleep(ms) {
    return new Promise(res => setTimeout(res, ms / speed));
  }

  async function play(cassette) {
    if (cassette.bot) {
      botAvatarEl.textContent = cassette.bot.emoji || "🌤️";
      botNameEl.textContent = cassette.bot.name || "Aaka";
    }
    document.title = `${cassette.bot?.name || "Aaka"} — ${cassette.title}`;

    if (cassette.summary) {
      captionEl.textContent = cassette.summary;
      captionEl.hidden = false;
    }

    let prevOffset = 0;
    for (const turn of cassette.messages) {
      const wait = Math.max(0, (turn.ts_offset_ms || 0) - prevOffset);
      await sleep(wait);
      prevOffset = turn.ts_offset_ms || 0;

      if (turn.from === "bot" && turn.typing_ms) {
        const typingRow = renderTyping();
        await sleep(turn.typing_ms);
        typingRow.remove();
      }
      renderTurn(turn);
    }
  }

  async function load() {
    const url = `../cassettes/${cassetteName}.json`;
    let cassette;
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      cassette = await res.json();
    } catch (err) {
      const row = document.createElement("div");
      row.className = "row bot";
      row.innerHTML = `<div class="bubble">⚠️ Could not load <code>${cassetteName}</code>.<br>${err.message}<br><br>Looked in <code>${url}</code>.<br><br>Append <code>?play=&lt;name&gt;</code> to the URL.</div>`;
      chatEl.appendChild(row);
      return;
    }

    if (autoplay) {
      play(cassette);
    } else {
      // Step mode: render all at once for now (manual stepping TBD)
      play(cassette);
    }
  }

  load();
})();
