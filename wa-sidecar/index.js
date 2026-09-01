'use strict';
/*
 * wa-sidecar/index.js — Baileys WhatsApp sidecar for aaka.
 *
 * Owns a single WhatsApp Web session so aaka's WhatsApp channel no longer needs
 * OpenClaw. Localhost only. Self-contained: only runtime deps are
 * @whiskeysockets/baileys + qrcode (no express, no pino, no sqlite).
 *
 * Responsibilities:
 *   • Pair via QR (printed to stdout + served as a data-URI at GET /qr)
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
// Inbound media is saved next to the auth dir (sibling), never inside it.
const WA_MEDIA_DIR =
  process.env.WA_MEDIA_DIR || path.join(path.dirname(WA_AUTH_DIR), 'wa-media');

// ── Session state ──────────────────────────────────────────────────────────
let sock = null;
let currentQrDataUri = null;
let connectionStatus = 'connecting'; // "connecting" | "qr" | "connected"
let connectedJid = null;
let reconnectAttempt = 0;

const RECONNECT_CAP_MS = 30000;
const CONNECTION_REPLACED = 440; // DisconnectReason.connectionReplaced numeric

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

  if (qr) {
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
    reconnectAttempt = 0;
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

// ── HTTP server ────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/status') {
      return sendJson(res, 200, { status: connectionStatus, jid: connectedJid });
    }
    if (req.method === 'GET' && req.url === '/qr') {
      if (connectionStatus === 'connected') {
        return sendJson(res, 409, { error: 'already connected' });
      }
      return sendJson(res, 200, { qr: currentQrDataUri });
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
});

startSession().catch((e) =>
  console.error('wa-sidecar: startSession failed:', e && e.message)
);

module.exports = { server };
