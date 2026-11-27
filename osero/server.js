// UltraOSERO online server — zero dependencies (Node 18+).
// Serves index.html + engine.js and runs authoritative game rooms over WebSocket at /ws.
//   PORT=8080 node server.js
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const E = require('./engine.js');

const PORT = +process.env.PORT || 8080;
const HOST = process.env.HOST || '0.0.0.0';
const FILES = { '/': 'index.html', '/index.html': 'index.html', '/engine.js': 'engine.js' };
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };

const server = http.createServer((req, res) => {
  const f = FILES[(req.url || '/').split('?')[0]];
  if (!f) { res.writeHead(404, { 'Content-Type': 'text/plain' }); return res.end('not found'); }
  fs.readFile(path.join(__dirname, f), (err, buf) => {
    if (err) { res.writeHead(500); return res.end(); }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(f)], 'Cache-Control': 'no-cache' });
    res.end(buf);
  });
});

/* ---------- minimal WebSocket (RFC 6455, text frames) ---------- */
class Conn {
  constructor(sock) {
    this.sock = sock; this.buf = Buffer.alloc(0); this.player = null;
    sock.on('data', (d) => this.onData(d));
    sock.on('close', () => onClose(this));
    sock.on('error', () => {});
  }
  onData(d) {
    this.buf = Buffer.concat([this.buf, d]);
    if (this.buf.length > 1 << 20) return this.sock.destroy();
    for (;;) {
      const b = this.buf;
      if (b.length < 2) return;
      const op = b[0] & 15, masked = b[1] & 128;
      let len = b[1] & 127, off = 2;
      if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); off = 10; }
      if (len > 65536) return this.sock.destroy();
      if (!masked) return this.sock.destroy(); // clients must mask
      if (b.length < off + 4 + len) return;
      const mask = b.subarray(off, off + 4); off += 4;
      const payload = Buffer.from(b.subarray(off, off + len));
      for (let i = 0; i < len; i++) payload[i] ^= mask[i & 3];
      this.buf = b.subarray(off + len);
      if (op === 8) { this.frame(8, Buffer.alloc(0)); this.sock.end(); return; }
      if (op === 9) { this.frame(10, payload); continue; }
      if (op === 1) {
        let msg; try { msg = JSON.parse(payload.toString('utf8')); } catch (e) { continue; }
        try { handle(this, msg); } catch (e) { console.error(e); }
      }
    }
  }
  frame(op, payload) {
    if (this.sock.destroyed) return;
    const len = payload.length; let head;
    if (len < 126) head = Buffer.from([128 | op, len]);
    else if (len < 65536) { head = Buffer.alloc(4); head[0] = 128 | op; head[1] = 126; head.writeUInt16BE(len, 2); }
    else { head = Buffer.alloc(10); head[0] = 128 | op; head[1] = 127; head.writeBigUInt64BE(BigInt(len), 2); }
    this.sock.write(Buffer.concat([head, payload]));
  }
  send(o) { this.frame(1, Buffer.from(JSON.stringify(o))); }
}

server.on('upgrade', (req, sock) => {
  const key = req.headers['sec-websocket-key'];
  if ((req.url || '').split('?')[0] !== '/ws' || (req.headers.upgrade || '').toLowerCase() !== 'websocket' || !key) { sock.destroy(); return; }
  const accept = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  sock.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  sock.setNoDelay(true);
  new Conn(sock);
});

/* ---------- rooms ---------- */
const players = new Map(); // token -> {token, name, conn, room}
const rooms = new Map();   // code -> room
const cleanName = (s) => String(s || '').replace(/[\u0000-\u001f<>]/g, '').trim().slice(0, 12);

function newCode() {
  const A = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  for (;;) { let c = ''; for (let i = 0; i < 4; i++) c += A[crypto.randomInt(A.length)]; if (!rooms.has(c)) return c; }
}
function members(r) {
  const list = r.seats.filter((s) => s.token).map((s) => s.token);
  return [...new Set(list.concat([...r.watchers]))].map((t) => players.get(t)).filter(Boolean);
}
function broadcast(r, o) { for (const p of members(r)) if (p.conn) p.conn.send(o); }
function publicRoom(r) {
  return {
    code: r.code, cfg: r.cfg, started: !!r.game, over: !!(r.game && r.game.over),
    host: r.seats.findIndex((s) => s.token === r.host),
    seats: r.seats.map((s) => {
      const p = s.token && players.get(s.token);
      return { type: s.type, level: s.level, name: s.type === 'cpu' ? 'CPU' : p ? p.name : '', open: s.type === 'human' && !s.token, online: !!(p && p.conn) };
    })
  };
}
function sendRoom(r) {
  const pub = publicRoom(r);
  for (const p of members(r)) if (p.conn) p.conn.send({ t: 'room', room: pub, you: r.seats.findIndex((s) => s.token === p.token) });
}
function leaveRoom(p) {
  const r = p.room && rooms.get(p.room);
  p.room = null;
  if (!r) return;
  r.watchers.delete(p.token);
  for (const s of r.seats) if (s.token === p.token) {
    s.token = null;
    if (r.game) { s.type = 'cpu'; s.level = 2; } // the game goes on with a CPU in that seat
  }
  if (r.host === p.token) { const next = r.seats.find((s) => s.token); r.host = next ? next.token : null; }
  if (!r.host) { clearTimeout(r.cpuTimer); rooms.delete(r.code); return; }
  sendRoom(r);
  scheduleCpu(r, 800);
}

function scheduleCpu(r, delay) {
  clearTimeout(r.cpuTimer);
  const g = r.game;
  if (!g || g.over || r.seats[g.turn - 1].type !== 'cpu') return;
  r.cpuTimer = setTimeout(() => {
    if (r.game !== g || g.over) return;
    const seat = r.seats[g.turn - 1];
    if (seat.type !== 'cpu') return;
    // capped so one room's CPU never blocks the server for long
    const m = E.think(E.fromData(E.toData(g)), seat.level, { time: seat.level >= 4 ? 1500 : undefined });
    if (m >= 0) applyMove(r, m);
  }, delay);
}
function applyMove(r, i) {
  const g = r.game, p = g.turn;
  if (!E.isLegal(g, i, p)) return false;
  const rec = E.play(g, i);
  r.active = Date.now();
  broadcast(r, { t: 'move', i, p });
  const flips = rec.flips.length / 4;
  // give clients time to play the animation before the CPU answers
  scheduleCpu(r, Math.min(4500, 1100 + flips * 110 + (rec.gens > 1 ? 1200 : 0)));
  if (g.over) sendRoom(r);
  return true;
}

function handle(c, m) {
  if (!m || typeof m.t !== 'string') return;
  if (m.t === 'hello') {
    let p = typeof m.token === 'string' && players.get(m.token);
    if (!p) { p = { token: crypto.randomBytes(12).toString('hex'), name: '', conn: null, room: null }; players.set(p.token, p); }
    if (p.conn && p.conn !== c) { const old = p.conn; old.player = null; try { old.sock.end(); } catch (e) {} }
    p.conn = c; c.player = p;
    p.name = cleanName(m.name) || p.name || 'プレイヤー';
    c.send({ t: 'welcome', token: p.token });
    const r = p.room && rooms.get(p.room);
    if (r) { sendRoom(r); if (r.game) c.send({ t: 'state', data: E.toData(r.game) }); }
    return;
  }
  const p = c.player;
  if (!p) return c.send({ t: 'error', msg: '先に接続してください' });
  const r = p.room && rooms.get(p.room);
  switch (m.t) {
    case 'create': {
      leaveRoom(p);
      const cfg = E.sanitize(m.cfg);
      delete cfg.seed;
      const defs = Array.isArray(m.seats) ? m.seats : [];
      const seats = [];
      for (let k = 0; k < cfg.players; k++) {
        const d = defs[k] || {};
        seats.push(d.type === 'cpu' ? { type: 'cpu', level: Math.max(1, Math.min(4, d.level | 0 || 2)), token: null } : { type: 'human', level: 0, token: null });
      }
      if (!seats.some((s) => s.type === 'human')) seats[0] = { type: 'human', level: 0, token: null };
      seats.find((s) => s.type === 'human').token = p.token;
      const room = { code: newCode(), cfg, seats, host: p.token, game: null, watchers: new Set(), cpuTimer: null, active: Date.now() };
      rooms.set(room.code, room); p.room = room.code;
      sendRoom(room);
      return;
    }
    case 'join': {
      const t = rooms.get(String(m.code || '').toUpperCase());
      if (!t) return c.send({ t: 'error', msg: 'その部屋は見つかりません' });
      if (r !== t) leaveRoom(p);
      p.room = t.code;
      if (!t.seats.some((s) => s.token === p.token)) {
        const open = !t.game && t.seats.find((s) => s.type === 'human' && !s.token);
        if (open) open.token = p.token; else t.watchers.add(p.token); // full or running: watch
      }
      sendRoom(t);
      if (t.game) c.send({ t: 'state', data: E.toData(t.game) });
      return;
    }
    case 'leave': leaveRoom(p); c.send({ t: 'left' }); return;
    case 'start': {
      if (!r || r.host !== p.token) return c.send({ t: 'error', msg: 'ホストだけが開始できます' });
      for (const s of r.seats) if (s.type === 'human' && !s.token) { s.type = 'cpu'; s.level = 2; }
      r.game = E.createGame(Object.assign({}, r.cfg, { seed: crypto.randomInt(1, 2 ** 31) }));
      r.active = Date.now();
      sendRoom(r);
      broadcast(r, { t: 'state', data: E.toData(r.game) });
      scheduleCpu(r, 1200);
      return;
    }
    case 'move': {
      if (!r || !r.game || r.game.over) return c.send({ t: 'error', msg: '対局中ではありません' });
      const seat = r.seats.findIndex((s) => s.token === p.token);
      if (seat !== r.game.turn - 1) return c.send({ t: 'error', msg: 'あなたの番ではありません' });
      if (!applyMove(r, m.i | 0)) c.send({ t: 'error', msg: 'そこには置けません' });
      return;
    }
    case 'sync': if (r && r.game) c.send({ t: 'state', data: E.toData(r.game) }); return;
  }
}
function onClose(c) {
  const p = c.player;
  if (!p || p.conn !== c) return;
  p.conn = null;
  const r = p.room && rooms.get(p.room);
  if (r) sendRoom(r);
}

// drop rooms nobody has been connected to for 15 minutes
setInterval(() => {
  const t = Date.now();
  for (const r of rooms.values()) {
    const online = members(r).some((p) => p.conn);
    if (online) r.active = t;
    else if (t - r.active > 15 * 60 * 1000) { clearTimeout(r.cpuTimer); rooms.delete(r.code); }
  }
  for (const p of players.values()) if (!p.conn && !(p.room && rooms.has(p.room))) players.delete(p.token);
}, 60 * 1000).unref();

server.listen(PORT, HOST, () => console.log(`UltraOSERO: http://${HOST}:${PORT}`));
