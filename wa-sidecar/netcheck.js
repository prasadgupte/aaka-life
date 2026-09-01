'use strict';
/*
 * wa-sidecar/netcheck.js — is this NETWORK able to pair WhatsApp Web?
 *
 *   node wa-sidecar/netcheck.js
 *
 * Runs two layered probes and prints a verdict:
 *   1. Raw WebSocket to WhatsApp's socket endpoint (no handshake). If this
 *      closes immediately, a firewall / corporate TLS-inspection proxy is
 *      breaking WhatsApp Web on this network — pairing cannot work here.
 *   2. A real Baileys handshake. If the raw WS holds but this is dropped
 *      (428 before any QR), WhatsApp is rejecting the handshake from this IP
 *      (rate-limit / block) — try a different network (phone hotspot).
 *
 * A "clean" network: raw WS holds AND the handshake yields a QR.
 */
const WS = require('ws');
const B = require('@whiskeysockets/baileys');
const makeWASocket = B.default;

const WS_URL = 'wss://web.whatsapp.com/ws/chat';
const silent = { level: 'silent', trace(){}, debug(){}, info(){}, warn(){}, error(){}, fatal(){}, child(){ return this; } };

function rawProbe() {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const ws = new WS(WS_URL, { origin: 'https://web.whatsapp.com', headers: { 'User-Agent': 'Mozilla/5.0' } });
    let opened = false;
    const done = (verdict) => { try { ws.close(); } catch (_) {} resolve(verdict); };
    ws.on('open', () => { opened = true; });
    ws.on('error', (e) => done({ ok: false, ms: Date.now() - t0, why: 'error: ' + e.message }));
    ws.on('close', (code) => { if (!opened) done({ ok: false, ms: Date.now() - t0, why: 'closed before open (code ' + code + ')' }); });
    setTimeout(() => done(opened ? { ok: true, ms: Date.now() - t0 } : { ok: false, ms: Date.now() - t0, why: 'never opened' }), 6000);
  });
}

function handshakeProbe() {
  return new Promise(async (resolve) => {
    const os = require('os'), path = require('path'), fs = require('fs');
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-netcheck-'));
    const { version } = await B.fetchLatestBaileysVersion();
    const { state, saveCreds } = await B.useMultiFileAuthState(dir);
    const sock = makeWASocket({ version, auth: state, browser: B.Browsers.macOS('Desktop'), logger: silent });
    sock.ev.on('creds.update', saveCreds);
    let settled = false;
    const done = (v) => { if (settled) return; settled = true; try { sock.end(); } catch (_) {} try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_) {} resolve(Object.assign({ version }, v)); };
    sock.ev.on('connection.update', (u) => {
      if (u.qr) return done({ ok: true });
      if (u.connection === 'close') return done({ ok: false, code: u.lastDisconnect?.error?.output?.statusCode, why: u.lastDisconnect?.error?.message });
    });
    setTimeout(() => done({ ok: false, why: 'timeout, no QR in 20s' }), 20000);
  });
}

(async () => {
  console.log('wa-sidecar netcheck — is this network able to pair WhatsApp?\n');
  process.stdout.write('1) raw WebSocket to WhatsApp … ');
  const raw = await rawProbe();
  console.log(raw.ok ? `OPEN & held ${raw.ms}ms ✓  (network layer is clean)` : `FAILED (${raw.why}) ✗`);

  if (!raw.ok) {
    console.log('\nVERDICT: a firewall/proxy on THIS network is blocking WhatsApp Web.');
    console.log('→ Pairing cannot work here. Use a different network (phone hotspot) or open the WhatsApp Web endpoints.');
    process.exit(1);
  }

  process.stdout.write('2) Baileys handshake (expect a QR) … ');
  const hs = await handshakeProbe();
  if (hs.ok) {
    console.log('QR issued ✓');
    console.log('\nVERDICT: this network is CLEAN — pairing works here. Run:  node wa-sidecar/index.js  → open http://127.0.0.1:18792/');
    process.exit(0);
  }
  console.log(`dropped (code ${hs.code || '?'}: ${hs.why}) ✗`);
  console.log(`   version tried: ${JSON.stringify(hs.version)}`);
  console.log('\nVERDICT: network reaches WhatsApp, but WhatsApp REJECTS the pairing handshake from this IP.');
  console.log('→ Not a code/version problem (raw socket held). This IP is rate-limited/blocked for new-device pairing.');
  console.log('→ Try a different network (phone hotspot), or wait several hours.');
  process.exit(2);
})().catch((e) => { console.error('netcheck error:', e && e.message); process.exit(3); });
