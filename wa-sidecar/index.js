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
 *
 * STUB PHASE: the HTTP server + route handlers exist but Baileys is not wired
 * yet — every route returns 503 until startSession() lands.
 */

const http = require('http');
const path = require('path');
const fs = require('fs');

// ── Env / config ───────────────────────────────────────────────────────────
const WA_AUTH_DIR =
  process.env.WA_AUTH_DIR ||
  (process.env.AAKA_CONFIG_DIR
    ? path.join(process.env.AAKA_CONFIG_DIR, 'whatsapp-auth')
    : path.join(__dirname, 'whatsapp-auth'));
const WA_SIDECAR_PORT = parseInt(process.env.WA_SIDECAR_PORT || '18792', 10);
const WA_RECEIVER_URL =
  process.env.WA_RECEIVER_URL || 'http://127.0.0.1:18793/inbound';

// ── Session state ──────────────────────────────────────────────────────────
let sock = null;
let currentQrDataUri = null;
let connectionStatus = 'connecting'; // "connecting" | "qr" | "connected"
let connectedJid = null;
let reconnectAttempt = 0;

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

// ── Route handlers (STUB — Baileys not wired yet) ──────────────────────────
async function handleSend(_req, res) {
  sendJson(res, 503, { ok: false, error: 'session not ready' });
}

async function handleSendMedia(_req, res) {
  sendJson(res, 503, { ok: false, error: 'session not ready' });
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

module.exports = { server };
