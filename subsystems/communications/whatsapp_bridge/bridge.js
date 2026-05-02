// Pepper WhatsApp Send Bridge
//
// Send-only HTTP service backed by whatsapp-web.js. Pepper's read path stays on
// the local ChatStorage.sqlite — this process does not poll or persist message
// history. It exists solely so that approved drafts can leave the box.
//
// Lifecycle:
//   1. First run: scan the QR shown in stdout with WhatsApp on your phone.
//   2. Session is persisted under ./.wa_session so subsequent restarts skip QR.
//   3. Bind is 127.0.0.1 only — never expose this port off-host.
//
// Endpoints:
//   GET  /health                     -> { ready, state }
//   POST /send  { chatId, message, replyTo? }  -> { ok, id }
//
// chatId formats accepted:
//   - "<digits>"           bare phone number, normalized to <digits>@c.us
//   - "<digits>@c.us"      direct user JID
//   - "<id>@g.us"          group JID

'use strict';

const express = require('express');
const qrcode = require('qrcode-terminal');
const path = require('path');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT = parseInt(process.env.PORT || '3025', 10);
const HOST = process.env.HOST || '127.0.0.1';
const TOKEN = process.env.PEPPER_WA_TOKEN || '';
const SESSION_DIR = path.join(__dirname, '.wa_session');

const app = express();
app.use(express.json({ limit: '256kb' }));

let ready = false;
let lastState = 'BOOTING';

const puppeteerOpts = {
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
};
if (process.env.PUPPETEER_EXECUTABLE_PATH) {
  puppeteerOpts.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
  puppeteer: puppeteerOpts,
});

client.on('qr', (qr) => {
  ready = false;
  lastState = 'AWAITING_QR';
  console.log('[wa-bridge] scan this QR with WhatsApp -> Linked Devices');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
  lastState = 'AUTHENTICATED';
  console.log('[wa-bridge] authenticated');
});

client.on('ready', () => {
  ready = true;
  lastState = 'READY';
  console.log(`[wa-bridge] ready, listening http://${HOST}:${PORT}`);
});

client.on('change_state', (s) => { lastState = s; });
client.on('disconnected', (r) => {
  ready = false;
  lastState = `DISCONNECTED:${r}`;
  console.warn('[wa-bridge] disconnected:', r);
});

client.initialize();

function authMiddleware(req, res, next) {
  if (!TOKEN) return next();
  const supplied = req.header('x-pepper-token') || '';
  if (supplied !== TOKEN) return res.status(401).json({ error: 'unauthorized' });
  next();
}

function normalizeChatId(raw) {
  if (!raw) return null;
  const s = String(raw).trim();
  if (s.endsWith('@c.us') || s.endsWith('@g.us')) return s;
  const digits = s.replace(/[^\d]/g, '');
  if (!digits) return null;
  return `${digits}@c.us`;
}

app.get('/health', (req, res) => {
  res.json({ ready, state: lastState });
});

app.post('/send', authMiddleware, async (req, res) => {
  if (!ready) {
    return res.status(503).json({ error: `bridge not ready (state=${lastState})` });
  }
  const { chatId, message, replyTo } = req.body || {};
  const target = normalizeChatId(chatId);
  if (!target) return res.status(400).json({ error: 'chatId is required' });
  if (!message || typeof message !== 'string') {
    return res.status(400).json({ error: 'message (string) is required' });
  }
  try {
    const opts = replyTo ? { quotedMessageId: String(replyTo) } : {};
    const sent = await client.sendMessage(target, message, opts);
    return res.json({ ok: true, id: sent && sent.id && sent.id._serialized });
  } catch (err) {
    console.error('[wa-bridge] send failed:', err && err.message);
    return res.status(500).json({ error: err && err.message || 'send failed' });
  }
});

app.listen(PORT, HOST, () => {
  console.log(`[wa-bridge] http listener up on ${HOST}:${PORT} (ready=${ready})`);
});

const shutdown = async (signal) => {
  console.log(`[wa-bridge] ${signal}; shutting down`);
  try { await client.destroy(); } catch (_) {}
  process.exit(0);
};
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
