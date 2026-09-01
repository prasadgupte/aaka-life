'use strict';
/*
 * wa-sidecar/index.js — Baileys WhatsApp sidecar for aaka.
 *
 * Owns a single WhatsApp Web session so aaka's WhatsApp channel no longer needs
 * OpenClaw. Localhost only. Self-contained: only runtime deps are
 * @whiskeysockets/baileys + qrcode (no express, no pino, no sqlite).
 *
 * Responsibilities:
 *   • Pair via a live web page at GET / (auto-refreshing QR, like wa-backup's
 *     frontend) — or pairing code when WA_PAIRING_NUMBER is set. Raw: /qr, /pair.
 *   • Persist auth to $WA_AUTH_DIR via useMultiFileAuthState (survives restart)
 *   • Forward inbound WA messages → POST to the Python receiver (WA_RECEIVER_URL)
 *   • Accept outbound sends: POST /send (text), POST /send-media (image/document)
 *   • Reconnect with exponential backoff (cap 30s); exit(1) on loggedOut / 440
 *
 * Env:
 *   WA_AUTH_DIR      auth state dir  (default: $AAKA_CONFIG_DIR/whatsapp-auth or ./whatsapp-auth)
 *   WA_SIDECAR_PORT  HTTP API port   (default: 18792)
 *   WA_RECEIVER_URL  inbound forward  (default: http://127.0.0.1:18793/inbound)
 *   WA_MEDIA_DIR     inbound media    (default: <auth-parent>/wa-media)
 *   WA_PAIRING_NUMBER  E.164 digits to link by pairing code instead of QR (optional)
 *
 * Baileys patterns lifted from /Users/Shared/tools/wa-backup/server.js
 * (makeWASocket + useMultiFileAuthState + connection.update + reconnect),
 * stripped of the SQLite/Socket.io/multi-account/history machinery.
 */

const http = require('http');
const path = require('path');
const fs = require('fs');

const {
  default: makeWASocket,
  useMultiFileAuthState,
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
} = require('@whiskeysockets/baileys');
const QRCode = require('qrcode');

// ── Env / config ───────────────────────────────────────────────────────────
const WA_AUTH_DIR =
  process.env.WA_AUTH_DIR ||
  (process.env.AAKA_CONFIG_DIR
    ? path.join(process.env.AAKA_CONFIG_DIR, 'whatsapp-auth')
    : path.join(__dirname, 'whatsapp-auth'));
const WA_SIDECAR_PORT = parseInt(process.env.WA_SIDECAR_PORT || '18792', 10);
const WA_RECEIVER_URL =
  process.env.WA_RECEIVER_URL || 'http://127.0.0.1:18793/inbound';
// Optional: link by pairing code instead of QR. Set to the E.164 digits of the
// WhatsApp number being linked (country code + number, NO '+', spaces stripped).
// When set (and not yet registered), the sidecar requests an 8-char pairing code
// the user types into WhatsApp → Linked Devices → Link with phone number.
const WA_PAIRING_NUMBER = (process.env.WA_PAIRING_NUMBER || '').replace(/[^0-9]/g, '');
// Inbound media is saved next to the auth dir (sibling), never inside it.
const WA_MEDIA_DIR =
  process.env.WA_MEDIA_DIR || path.join(path.dirname(WA_AUTH_DIR), 'wa-media');

// ── Session state ──────────────────────────────────────────────────────────
let sock = null;
let currentQrDataUri = null;
let pairingCode = null;
let connectionStatus = 'connecting'; // "connecting" | "qr" | "pairing" | "connected" | "rate_limited"
let connectedJid = null;
let statusMessage = null;   // human-readable note surfaced at /status and /pair
let reconnectAttempt = 0;
let pairAttempt = 0;        // fresh-pairing (unregistered) close count — bounded, unlike reconnect

const RECONNECT_CAP_MS = 30000;
const CONNECTION_REPLACED = 440; // DisconnectReason.connectionReplaced numeric

// Fresh pairing (unregistered) must NOT hammer WhatsApp — rapid retries earn an
// IP-level pairing ban (428 "Connection Terminated" before any QR/code). So when
// unpaired we back off HARD and stop after a few tries, surfacing "rate_limited"
// instead of looping. Established-session reconnects keep the fast backoff above.
const MAX_PAIR_ATTEMPTS = 3;
const PAIR_BACKOFF_MS = 90000;      // 90s between fresh-pairing retries
const PAIR_BACKOFF_CAP_MS = 300000; // cap 5 min

// ── JID helpers (lifted from wa-backup) ────────────────────────────────────
function normalizeJid(jid) {
  const [userPart, server] = String(jid).split('@');
  return userPart.split(':')[0] + '@' + (server || 's.whatsapp.net');
}

// ── Baileys session ────────────────────────────────────────────────────────
async function startSession() {
  fs.mkdirSync(WA_AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(WA_AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    // No pino dependency — a silent no-op logger keeps Baileys quiet.
    logger: makeSilentLogger(),
    browser: Browsers.macOS('Desktop'),
  });

  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('connection.update', handleConnectionUpdate);
  sock.ev.on('messages.upsert', handleMessagesUpsert);

  // Pairing-code path: if a target number is configured and we're not yet
  // registered, ask WhatsApp for an 8-char code instead of a QR. Requested a
  // few seconds after socket creation so the noise handshake can open first.
  if (WA_PAIRING_NUMBER && !sock.authState.creds.registered) {
    setTimeout(async () => {
      try {
        if (sock && !sock.authState.creds.registered && !pairingCode) {
          const code = await sock.requestPairingCode(WA_PAIRING_NUMBER);
          pairingCode = code;
          connectionStatus = 'pairing';
          console.log(`wa-sidecar: PAIRING CODE = ${code}`);
          console.log('  → WhatsApp → Settings → Linked Devices → Link a device → Link with phone number → enter this code.');
        }
      } catch (e) {
        console.error('wa-sidecar: requestPairingCode failed:', e && e.message);
      }
    }, 3000);
  }
}

function makeSilentLogger() {
  const noop = () => {};
  const logger = {
    level: 'silent',
    trace: noop, debug: noop, info: noop, warn: noop, error: noop, fatal: noop,
  };
  logger.child = () => logger;
  return logger;
}

async function handleConnectionUpdate(update) {
  const { connection, lastDisconnect, qr } = update;

  // In pairing-code mode WhatsApp still emits a `qr` field — ignore it so the
  // code-based flow (status "pairing") isn't clobbered by "qr".
  if (qr && !WA_PAIRING_NUMBER) {
    try {
      currentQrDataUri = await QRCode.toDataURL(qr);
    } catch (e) {
      console.error('wa-sidecar: QR encode failed:', e && e.message);
    }
    connectionStatus = 'qr';
    reconnectAttempt = 0;
    console.log('wa-sidecar: QR pending — scan to pair. (data-URI available at GET /qr)');
    console.log(qr);
  }

  if (connection === 'open') {
    connectionStatus = 'connected';
    connectedJid = sock && sock.user ? normalizeJid(sock.user.id) : null;
    currentQrDataUri = null;
    pairingCode = null;
    statusMessage = null;
    reconnectAttempt = 0;
    pairAttempt = 0;
    console.log(`wa-sidecar: connected as ${connectedJid}`);
  }

  if (connection === 'close') {
    const code =
      lastDisconnect &&
      lastDisconnect.error &&
      lastDisconnect.error.output &&
      lastDisconnect.error.output.statusCode;

    if (code === DisconnectReason.loggedOut || code === CONNECTION_REPLACED) {
      // Session evicted (logged out elsewhere, or another Web session stole it).
      // Do NOT loop — exit non-zero so the supervisor (launchd) alerts PG.
      console.error(
        `wa-sidecar: session evicted (code ${code}) — exiting so supervisor can alert.`
      );
      process.exit(1);
    }

    if (code === DisconnectReason.restartRequired) {
      // Normal final step of pairing (and stream restarts): WhatsApp accepted the
      // scan and asks us to reconnect to finish login. Must reconnect IMMEDIATELY —
      // a backoff here makes the phone's "linking…" flow time out ("couldn't link
      // device"). This is NOT a refusal, so it does not count as a pair attempt.
      console.log('wa-sidecar: restart required (515) — reconnecting now to finish login.');
      connectionStatus = 'connecting';
      reconnectAttempt = 0;
      pairAttempt = 0;
      setTimeout(() => {
        startSession().catch((e) =>
          console.error('wa-sidecar: restart reconnect failed:', e && e.message)
        );
      }, 250);
      return;
    }

    const registered = !!(sock && sock.authState && sock.authState.creds && sock.authState.creds.registered);

    if (!registered) {
      // Fresh pairing was refused (no session yet). Retrying fast = an IP ban.
      // Back off hard and give up after a few tries, surfacing rate_limited.
      pairAttempt += 1;
      currentQrDataUri = null;
      pairingCode = null;
      if (pairAttempt >= MAX_PAIR_ATTEMPTS) {
        connectionStatus = 'rate_limited';
        statusMessage =
          'WhatsApp refused pairing from this IP (code ' + code + '). This is a pairing-rate block — ' +
          'wait a few hours or pair from a different network (e.g. phone hotspot), then restart the sidecar.';
        console.error('wa-sidecar: ' + statusMessage + ' — NOT retrying (avoids deepening the ban).');
        return; // stay alive so /status reports rate_limited; no more attempts
      }
      connectionStatus = 'connecting';
      const pdelay = Math.min(PAIR_BACKOFF_MS * pairAttempt, PAIR_BACKOFF_CAP_MS);
      console.log(
        `wa-sidecar: pairing refused (code ${code}) — backing off ${pdelay}ms (pair attempt ${pairAttempt}/${MAX_PAIR_ATTEMPTS}).`
      );
      setTimeout(() => {
        startSession().catch((e) =>
          console.error('wa-sidecar: pairing retry failed:', e && e.message)
        );
      }, pdelay);
      return;
    }

    // Established session dropped — legitimate reconnect, fast backoff is fine.
    connectionStatus = 'connecting';
    connectedJid = null;
    const delay = Math.min(1000 * 2 ** reconnectAttempt, RECONNECT_CAP_MS);
    reconnectAttempt += 1;
    console.log(
      `wa-sidecar: connection closed (code ${code}) — reconnecting in ${delay}ms (attempt ${reconnectAttempt}).`
    );
    setTimeout(() => {
      startSession().catch((e) =>
        console.error('wa-sidecar: reconnect failed:', e && e.message)
      );
    }, delay);
  }
}

function extractText(msg) {
  const m = msg && msg.message;
  if (!m) return '';
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.documentMessage && m.documentMessage.caption) ||
    ''
  );
}

function mediaKind(msg) {
  const m = msg && msg.message;
  if (!m) return null;
  if (m.imageMessage) return { field: 'imageMessage', mime: m.imageMessage.mimetype || 'image/jpeg', ext: '.jpg' };
  if (m.documentMessage) return { field: 'documentMessage', mime: m.documentMessage.mimetype || 'application/octet-stream', ext: extFromName(m.documentMessage.fileName) };
  return null;
}

function extFromName(name) {
  if (!name) return '';
  const e = path.extname(name);
  return e || '';
}

async function handleMessagesUpsert({ messages, type }) {
  if (!Array.isArray(messages)) return;
  for (const msg of messages) {
    try {
      if (!msg || !msg.key || !msg.key.remoteJid || !msg.key.id) continue;
      // Skip history/backfill — only forward live "notify" events.
      if (type !== 'notify') continue;

      const text = extractText(msg);
      const media = mediaKind(msg);
      if (!text && !media) continue; // nothing routable

      const body = {
        sender_id: msg.key.remoteJid,
        channel_id: msg.key.remoteJid,
        message_id: msg.key.id,
        text: text,
        from_me: !!msg.key.fromMe,
        timestamp: new Date(Number(msg.messageTimestamp || 0) * 1000).toISOString(),
      };

      if (media) {
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {});
          fs.mkdirSync(WA_MEDIA_DIR, { recursive: true });
          const fname = `${msg.key.id}${media.ext || ''}`;
          const fpath = path.join(WA_MEDIA_DIR, fname);
          fs.writeFileSync(fpath, buf);
          body.media_path = fpath;
          body.mime_type = media.mime;
        } catch (e) {
          console.error('wa-sidecar: media download failed:', e && e.message);
        }
      }

      postInbound(body).catch((e) =>
        console.error('wa-sidecar: inbound POST failed (discarded):', e && e.message)
      );
    } catch (e) {
      console.error('wa-sidecar: upsert handler error (discarded):', e && e.message);
    }
  }
}

// POST an inbound message body to the Python receiver. Discards on failure.
function postInbound(body) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body), 'utf8');
    const u = new URL(WA_RECEIVER_URL);
    const client = u.protocol === 'https:' ? require('https') : require('http');
    const req = client.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': data.length,
        },
      },
      (res) => {
        res.resume(); // drain
        res.on('end', resolve);
      }
    );
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

// ── HTTP helpers ───────────────────────────────────────────────────────────
function sendJson(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(e);
      }
    });
    req.on('error', reject);
  });
}

// ── Route handlers ─────────────────────────────────────────────────────────
async function handleSend(req, res) {
  if (!sock || connectionStatus !== 'connected') {
    return sendJson(res, 503, { ok: false, error: 'session not ready' });
  }
  let body;
  try {
    body = await readBody(req);
  } catch (e) {
    return sendJson(res, 400, { ok: false, error: 'invalid JSON' });
  }
  const { jid, text } = body;
  if (!jid || typeof text !== 'string') {
    return sendJson(res, 400, { ok: false, error: 'jid and text required' });
  }
  try {
    await sock.sendMessage(jid, { text });
    return sendJson(res, 200, { ok: true });
  } catch (e) {
    return sendJson(res, 500, { ok: false, error: String(e && e.message ? e.message : e) });
  }
}

async function handleSendMedia(req, res) {
  if (!sock || connectionStatus !== 'connected') {
    return sendJson(res, 503, { ok: false, error: 'session not ready' });
  }
  let body;
  try {
    body = await readBody(req);
  } catch (e) {
    return sendJson(res, 400, { ok: false, error: 'invalid JSON' });
  }
  const { jid, mime_type, file_path, caption } = body;
  if (!jid || !file_path) {
    return sendJson(res, 400, { ok: false, error: 'jid and file_path required' });
  }
  let buf;
  try {
    buf = fs.readFileSync(file_path);
  } catch (e) {
    return sendJson(res, 400, { ok: false, error: `cannot read file: ${e && e.message}` });
  }
  try {
    const cap = caption || '';
    let content;
    if (mime_type && String(mime_type).startsWith('image/')) {
      content = { image: buf, mimetype: mime_type, caption: cap };
    } else {
      content = {
        document: buf,
        mimetype: mime_type || 'application/octet-stream',
        fileName: path.basename(file_path),
        caption: cap,
      };
    }
    await sock.sendMessage(jid, content);
    return sendJson(res, 200, { ok: true });
  } catch (e) {
    return sendJson(res, 500, { ok: false, error: String(e && e.message ? e.message : e) });
  }
}

// ── Pairing web page (live QR / code, like wa-backup's frontend) ────────────
// Self-contained: polls /status every 2s and renders whatever the sidecar is
// doing — a live (auto-rotating) QR by default, or the 8-char pairing code when
// WA_PAIRING_NUMBER is set. Solves code/QR expiry: the page always shows current.
const PAIR_PAGE = `<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link WhatsApp · aaka</title>
<style>
  :root{--teal:#12b5a6;--ink:#0f1419;--muted:#6b7784;--bg:#f7f9fa}
  *{box-sizing:border-box}body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:var(--bg);color:var(--ink);display:flex;min-height:100vh;align-items:center;justify-content:center}
  .card{background:#fff;border-radius:20px;box-shadow:0 8px 40px rgba(15,20,25,.10);padding:32px;max-width:420px;width:92%;text-align:center}
  .brand{font-weight:800;font-size:22px;letter-spacing:-.5px}.brand b{color:var(--teal)}
  h1{font-size:19px;margin:.6em 0 .2em}p.sub{color:var(--muted);margin:.2em 0 1.2em;font-size:14px}
  .qr{width:264px;height:264px;margin:8px auto;border-radius:14px;background:#fff;display:flex;align-items:center;justify-content:center;border:1px solid #eef1f3}
  .qr img{width:256px;height:256px;image-rendering:pixelated}
  .code{font:700 34px/1.1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:6px;color:var(--ink);
    background:#f0fbfa;border:1px dashed var(--teal);border-radius:12px;padding:18px;margin:8px 0}
  .steps{text-align:left;font-size:13.5px;color:var(--muted);margin:14px 4px 0;padding-left:18px}
  .steps li{margin:3px 0}.spin{width:34px;height:34px;border:3px solid #e3e8eb;border-top-color:var(--teal);
    border-radius:50%;animation:s .8s linear infinite;margin:26px auto}@keyframes s{to{transform:rotate(360deg)}}
  .ok{color:var(--teal);font-size:44px}.warn{color:#c2410c;font-size:14px;background:#fff7ed;border-radius:10px;padding:12px;margin-top:8px}
  .pill{display:inline-block;font-size:12px;color:var(--muted);margin-top:14px}
</style></head><body>
<div class="card">
  <div class="brand">&amp; aaka<b>.</b></div>
  <div id="view"><div class="spin"></div><p class="sub">Starting…</p></div>
  <span class="pill" id="pill"></span>
</div>
<script>
const view=document.getElementById('view'),pill=document.getElementById('pill');
let lastQr=null;
function steps(kind){return '<ol class="steps"><li>Open <b>WhatsApp</b> on your phone</li>'+
  '<li>Tap <b>Settings → Linked Devices</b></li>'+
  '<li>Tap <b>Link a device</b>'+(kind==='code'?' → <b>Link with phone number</b>':'')+'</li>'+
  '<li>'+(kind==='code'?'Enter the code above':'Point your camera at this QR')+'</li></ol>';}
async function tick(){
  try{
    const s=await (await fetch('/status')).json();
    pill.textContent='status: '+s.status;
    if(s.status==='connected'){view.innerHTML='<div class="ok">✓</div><h1>WhatsApp linked</h1>'+
      '<p class="sub">'+(s.jid||'')+'</p><p class="sub">You can close this page. aaka is now on WhatsApp.</p>';return;}
    if(s.status==='rate_limited'){view.innerHTML='<h1>Try another network</h1>'+
      '<div class="warn">'+(s.message||'WhatsApp is rate-limiting pairing from this network.')+'</div>'+
      '<p class="sub" style="margin-top:12px">Reconnect over your phone hotspot and restart the sidecar.</p>';return;}
    if(s.status==='pairing'){const p=await (await fetch('/pair')).json();
      view.innerHTML='<h1>Link with phone number</h1><p class="sub">Enter this code in WhatsApp</p>'+
      '<div class="code">'+((p.code||'········').replace(/(.{4})(.{4})/,'$1 $2'))+'</div>'+steps('code');return;}
    if(s.status==='qr'){const q=await (await fetch('/qr')).json();
      if(q.qr){lastQr=q.qr;view.innerHTML='<h1>Scan to link WhatsApp</h1><p class="sub">Point your phone camera at the code</p>'+
        '<div class="qr"><img src="'+q.qr+'"></div>'+steps('qr');}return;}
    view.innerHTML='<div class="spin"></div><p class="sub">Connecting to WhatsApp…</p>';
  }catch(e){pill.textContent='sidecar offline — retrying…';}
}
tick();setInterval(tick,2000);
</script></body></html>`;

// ── HTTP server ────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && (req.url === '/' || req.url === '/pair.html')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(PAIR_PAGE);
    }
    if (req.method === 'GET' && req.url === '/status') {
      return sendJson(res, 200, { status: connectionStatus, jid: connectedJid, message: statusMessage });
    }
    if (req.method === 'GET' && req.url === '/qr') {
      if (connectionStatus === 'connected') {
        return sendJson(res, 409, { error: 'already connected' });
      }
      return sendJson(res, 200, { qr: currentQrDataUri });
    }
    if (req.method === 'GET' && req.url === '/pair') {
      if (connectionStatus === 'connected') {
        return sendJson(res, 409, { error: 'already connected' });
      }
      return sendJson(res, 200, { code: pairingCode, status: connectionStatus, message: statusMessage });
    }
    if (req.method === 'GET' && req.url === '/health') {
      return sendJson(res, 200, { ok: true });
    }
    if (req.method === 'POST' && req.url === '/send') {
      return handleSend(req, res);
    }
    if (req.method === 'POST' && req.url === '/send-media') {
      return handleSendMedia(req, res);
    }
    return sendJson(res, 404, { error: 'not found' });
  } catch (err) {
    return sendJson(res, 500, { error: String(err && err.message ? err.message : err) });
  }
});

server.listen(WA_SIDECAR_PORT, '127.0.0.1', () => {
  console.log(`wa-sidecar listening on 127.0.0.1:${WA_SIDECAR_PORT}`);
  console.log(`wa-sidecar auth dir: ${WA_AUTH_DIR}`);
  console.log(`wa-sidecar receiver: ${WA_RECEIVER_URL}`);
  console.log(`wa-sidecar: open http://127.0.0.1:${WA_SIDECAR_PORT}/ to link WhatsApp (live QR / code page)`);
  if (WA_PAIRING_NUMBER) {
    const masked = WA_PAIRING_NUMBER.slice(0, 4) + '****' + WA_PAIRING_NUMBER.slice(-2);
    console.log(`wa-sidecar: pairing-code mode for ${masked} (GET /pair)`);
  }
});

// WA_NO_CONNECT=1 serves the HTTP surface without touching WhatsApp (page/route tests).
if (process.env.WA_NO_CONNECT === '1') {
  console.log('wa-sidecar: WA_NO_CONNECT=1 — HTTP only, not connecting to WhatsApp.');
} else {
  startSession().catch((e) =>
    console.error('wa-sidecar: startSession failed:', e && e.message)
  );
}

module.exports = { server };
