'use strict';
/*
 * 狩りバトルのサーバー（依存ゼロ・標準モジュールだけ）。
 * - / で index.html、/ws で WebSocket。Caddy が /hunt/ をここへ回す。
 * - 戦いの計算はすべて engine.js（サーバー側）。ブラウザは「どの技を誰に」を送るだけ。
 * - 1〜5人。ハンター5人を、いる人数で順番に分けて操作する（1人なら5人ぜんぶ）。
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const E = require('./engine.js');
const Accounts = require('./accounts.js');

const PORT = +process.env.PORT || 8080;
const HOST = process.env.HOST || '0.0.0.0';
const FILES = { '/': 'index.html', '/index.html': 'index.html' };
const DATA_DIR = process.env.HUNT_DATA_DIR || path.join(__dirname, 'data');
const accounts = Accounts.create(DATA_DIR);
// ログインの試しすぎを止める（1つの回線から 10分に 10回まで）
const LOGIN_MAX = 10, LOGIN_WINDOW = 10 * 60 * 1000;
const loginTries = new Map();   // ip -> [時刻…]
function loginAllowed(ip) {
  const now = Date.now(), a = (loginTries.get(ip) || []).filter((t) => now - t < LOGIN_WINDOW);
  if (a.length >= LOGIN_MAX) { loginTries.set(ip, a); return false; }
  a.push(now); loginTries.set(ip, a); return true;
}

// 乱用対策（UltraOSERO と同じ考え方）
const MAX_MSG = 8 * 1024;
const RATE = 12, BURST = 24;
const MAX_PER_IP = 10;
const MAX_ROOMS = 200;
const MAX_PLAYERS = E.PARTY.length;
// 持ち時間
const TURN_MS = +process.env.HUNT_TURN_MS || 40000;        // 自分の番で考えられる時間
const OFFLINE_MS = +process.env.HUNT_OFFLINE_MS || 5000;   // 切断中の人の番は、これだけ待ったら自動で通常攻撃
const EVENT_MS = +process.env.HUNT_EVENT_MS || 650;        // 画面の演出1つぶんの目安（演出が終わってから時計を動かす）
const IDLE_ROOM_MS = 10 * 60 * 1000;

const server = http.createServer((req, res) => {
  const url = (req.url || '/').split('?')[0];
  if (url === '/healthz') { res.writeHead(200, { 'Content-Type': 'text/plain' }); return res.end('ok'); }
  const f = FILES[url];
  if (!f) { res.writeHead(404, { 'Content-Type': 'text/plain' }); return res.end('not found'); }
  fs.readFile(path.join(__dirname, f), (err, buf) => {
    if (err) { res.writeHead(500); return res.end(); }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' });
    res.end(buf);
  });
});

/* ---------- minimal WebSocket (RFC 6455, text frames) ---------- */
const ipConns = new Map();
class Conn {
  constructor(sock, ip) {
    this.sock = sock; this.ip = ip; this.buf = Buffer.alloc(0); this.player = null;
    this.tokens = BURST; this.last = Date.now(); this.strikes = 0;
    sock.on('data', (d) => this.onData(d));
    sock.on('close', () => { const n = (ipConns.get(ip) || 1) - 1; if (n > 0) ipConns.set(ip, n); else ipConns.delete(ip); onClose(this); });
    sock.on('error', () => {});
  }
  allow() {
    const now = Date.now();
    this.tokens = Math.min(BURST, this.tokens + ((now - this.last) / 1000) * RATE); this.last = now;
    if (this.tokens < 1) return false;
    this.tokens -= 1; return true;
  }
  onData(d) {
    this.buf = Buffer.concat([this.buf, d]);
    if (this.buf.length > 1 << 17) return this.sock.destroy();
    for (;;) {
      const b = this.buf;
      if (b.length < 2) return;
      const op = b[0] & 15, masked = b[1] & 128;
      let len = b[1] & 127, off = 2;
      if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); off = 10; }
      if (len > MAX_MSG) return this.sock.destroy();
      if (!masked) return this.sock.destroy();
      if (b.length < off + 4 + len) return;
      const mask = b.subarray(off, off + 4); off += 4;
      const payload = Buffer.from(b.subarray(off, off + len));
      for (let i = 0; i < len; i++) payload[i] ^= mask[i & 3];
      this.buf = b.subarray(off + len);
      if (op === 8) { this.frame(8, Buffer.alloc(0)); this.sock.end(); return; }
      if (op === 9) { this.frame(10, payload); continue; }
      if (op === 1) {
        if (!this.allow()) { if (++this.strikes > 60) return this.sock.destroy(); continue; }
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
  const ip = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || req.socket.remoteAddress || '?';
  const n = ipConns.get(ip) || 0;
  if (n >= MAX_PER_IP) { sock.write('HTTP/1.1 429 Too Many Requests\r\nConnection: close\r\n\r\n'); sock.destroy(); return; }
  ipConns.set(ip, n + 1);
  const accept = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  sock.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  sock.setNoDelay(true);
  new Conn(sock, ip);
});

/* ---------- プレイヤーと部屋 ---------- */
const players = new Map();   // token -> player
const rooms = new Map();     // code -> room
const DEFS = E.defs();

const cleanName = (s) => String(s || '').replace(/[\u0000-\u001f<>&"']/g, '').trim().slice(0, 12) || 'ハンター';
function newCode() {
  for (;;) { const c = String(crypto.randomInt(1000, 10000)); if (!rooms.has(c)) return c; }
}
const online = (p) => !!(p && p.conn);
function broadcast(r, o) { for (const p of r.players) if (p.conn) p.conn.send(o); }

function roomView(r, p) {
  return {
    t: 'room', code: r.code, quest: r.quest, public: r.public, phase: r.phase, host: r.players[0] ? r.players[0].id : null,
    you: p.id, max: MAX_PLAYERS,
    players: r.players.map((x) => ({ id: x.id, name: x.name, online: online(x), member: !!x.user })),
  };
}
function sendRoom(r) { for (const p of r.players) if (p.conn) p.conn.send(roomView(r, p)); notifyLobby(); }

function listRooms() {
  const out = [];
  for (const r of rooms.values()) {
    if (!r.public || r.phase !== 'lobby' || r.players.length >= MAX_PLAYERS || !online(r.players[0])) continue;
    out.push({ code: r.code, quest: r.quest, host: r.players[0].name, n: r.players.length });
  }
  return out.slice(0, 30);
}
function notifyLobby() {
  const list = listRooms();
  for (const p of players.values()) if (p.conn && !p.room && p.wantList) p.conn.send({ t: 'rooms', rooms: list });
}

function createRoom(p, quest, isPublic) {
  if (rooms.size >= MAX_ROOMS) return p.conn.send({ t: 'error', msg: 'いま部屋がいっぱいです。少し待ってからお試しください。' });
  const now = Date.now();
  if (now - (p.lastCreate || 0) < 2000) return p.conn.send({ t: 'error', msg: '少し待ってからもう一度お試しください。' });
  p.lastCreate = now;
  leaveRoom(p);
  const r = { code: newCode(), quest: E.QUESTS[quest] ? quest : 'hibashiri', public: !!isPublic, phase: 'lobby',
              players: [p], state: null, timer: null, deadline: 0, touched: now };
  rooms.set(r.code, r); p.room = r;
  sendRoom(r);
}

function joinRoom(p, code) {
  const r = rooms.get(String(code || ''));
  if (!r) return p.conn.send({ t: 'error', msg: 'その合言葉の部屋は見つかりません。' });
  if (r.players.includes(p)) return sendRoom(r);
  if (r.phase !== 'lobby') return p.conn.send({ t: 'error', msg: 'その部屋はもう狩りに出ています。' });
  if (r.players.length >= MAX_PLAYERS) return p.conn.send({ t: 'error', msg: 'その部屋は満員です。' });
  leaveRoom(p);
  r.players.push(p); p.room = r; r.touched = Date.now();
  sendRoom(r);
}

function dropRoom(r) {
  clearTimeout(r.timer);
  for (const p of r.players) p.room = null;
  rooms.delete(r.code);
  notifyLobby();
}

function leaveRoom(p) {
  const r = p.room;
  if (!r) return;
  p.room = null;
  const idx = r.players.indexOf(p);
  r.players = r.players.filter((x) => x !== p);
  if (!r.players.length) return dropRoom(r);
  // 狩りの途中で抜けた人のハンターは、残った人に配り直す
  if (r.phase === 'battle' && r.state) {
    const hs = E.hunters(r.state);
    let k = 0;
    for (const u of hs) {
      if (u.owner === idx) u.owner = k++ % r.players.length;
      else if (u.owner > idx) u.owner--;
    }
    broadcast(r, { t: 'notice', msg: `${p.name} さんが抜けました。ハンターを引き継ぎます。` });
    sendState(r, []);
    armTimer(r, 0);
  }
  sendRoom(r);
}

/* ---------- 戦いの進行 ---------- */
function ownerOf(r) {
  const s = r.state;
  if (!s || s.phase !== 'input' || !s.turn) return null;
  return r.players[E.unit(s, s.turn).owner] || null;
}

function sendState(r, events) {
  const v = E.view(r.state);
  v.deadline = r.deadline;
  for (const p of r.players) if (p.conn) p.conn.send({ t: 'state', state: v, events, now: Date.now() });
}

/* 持ち時間の時計。演出が終わるころから数えはじめる */
function armTimer(r, nEvents) {
  clearTimeout(r.timer);
  const s = r.state;
  if (!s || s.phase !== 'input') { r.deadline = 0; return; }
  const p = ownerOf(r);
  const wait = Math.min(nEvents * EVENT_MS, 12000) + (online(p) ? TURN_MS : OFFLINE_MS);
  r.deadline = Date.now() + wait;
  r.timer = setTimeout(() => {
    if (r.state !== s || s.phase !== 'input') return;
    const who = E.unit(s, s.turn);
    const res = E.autoAct(s);
    if (!res.ok) return;
    res.events.unshift({ t: 'auto', u: who.id });
    afterAct(r, res.events);
  }, wait);
}

function sendMe(p) { if (p.conn) p.conn.send({ t: 'me', me: p.user ? accounts.profile(p.user) : null }); }

/* 狩りが終わった: ログインしている人の戦績・彩石・実績を記録する */
function recordResult(r) {
  const s = r.state;
  for (const p of r.players) {
    if (!p.user) continue;
    const res = accounts.record(p.user, { quest: s.quest, win: !!s.win, turns: s.turns, breaks: r.breaks | 0, players: r.players.length });
    if (res && p.conn) { p.conn.send({ t: 'reward', stones: res.stones, unlocked: res.unlocked }); sendMe(p); }
  }
}

function afterAct(r, events) {
  r.touched = Date.now();
  r.breaks = (r.breaks | 0) + events.filter((e) => e.t === 'break').length;
  if (r.state.phase === 'over') { clearTimeout(r.timer); r.deadline = 0; r.phase = 'result'; sendState(r, events); recordResult(r); sendRoom(r); return; }
  armTimer(r, events.length);
  sendState(r, events);
}

function startBattle(r) {
  r.state = E.createBattle(r.quest, r.players.length, crypto.randomInt(1, 2 ** 31 - 1));
  r.phase = 'battle'; r.breaks = 0;
  const ev = E.start(r.state);
  sendRoom(r);
  afterAct(r, ev);
}

/* ---------- メッセージ ---------- */
function handle(c, m) {
  if (!m || typeof m.t !== 'string') return;
  if (m.t === 'hello') {
    let p = typeof m.token === 'string' ? players.get(m.token) : null;
    if (p) {
      if (p.conn && p.conn !== c) { p.conn.player = null; p.conn.sock.destroy(); }   // 同じ人の古い接続は切る
    } else {
      const token = crypto.randomBytes(16).toString('hex');
      p = { token, id: crypto.randomBytes(4).toString('hex'), name: cleanName(m.name), room: null, conn: null, wantList: false };
      players.set(token, p);
    }
    if (m.name) p.name = cleanName(m.name);
    p.conn = c; c.player = p; p.seen = Date.now();
    const su = accounts.resume(m.session);
    if (su) { p.user = su; p.session = m.session; p.name = accounts.nameOf(su); } else if (m.session) { p.user = null; p.session = null; }
    c.send({ t: 'welcome', token: p.token, id: p.id, name: p.name, defs: DEFS, me: p.user ? accounts.profile(p.user) : null, badSession: !!(m.session && !su) });
    if (p.room) {
      const r = p.room;
      sendRoom(r);
      if (r.state) {
        // 戻ってきた: 時計が止まっていたか、自分の番なら数え直す
        if (r.phase === 'battle' && (!r.deadline || ownerOf(r) === p)) armTimer(r, 0);
        sendState(r, []);
      }
    }
    return;
  }
  const p = c.player;
  if (!p) return;
  p.seen = Date.now();
  const r = p.room;
  switch (m.t) {
    case 'name': if (!p.user) { p.name = cleanName(m.name); if (r) sendRoom(r); } break;
    case 'register':
    case 'login': {
      if (p.authBusy) break;
      if (!loginAllowed(c.ip)) { c.send({ t: 'auth', error: '試した回数が多すぎます。10分ほど待ってからお試しください。' }); break; }
      p.authBusy = true;
      const fn = m.t === 'register' ? accounts.register : accounts.login;
      fn(m.user, m.pass).then((res) => {
        p.authBusy = false;
        if (res.error) { if (p.conn) p.conn.send({ t: 'auth', error: res.error }); return; }
        p.user = res.user; p.session = res.session; p.name = accounts.nameOf(res.user);
        if (p.conn) p.conn.send({ t: 'auth', session: res.session, me: accounts.profile(res.user), created: m.t === 'register' });
        if (p.room) sendRoom(p.room);
      }).catch(() => { p.authBusy = false; if (p.conn) p.conn.send({ t: 'auth', error: 'うまくいきませんでした。もう一度お試しください。' }); });
      break;
    }
    case 'logout':
      if (p.user) { accounts.logout(p.user, p.session); p.user = null; p.session = null; }
      c.send({ t: 'auth', session: null, me: null });
      break;
    case 'me': sendMe(p); break;
    case 'claim': {
      if (!p.user) { c.send({ t: 'error', msg: 'ログインすると受け取れます。' }); break; }
      const res = accounts.claim(p.user, String(m.id || ''));
      if (res.error) c.send({ t: 'error', msg: res.error });
      else c.send({ t: 'claimed', id: m.id, reward: res.reward });
      sendMe(p);
      break;
    }
    case 'rooms': p.wantList = true; c.send({ t: 'rooms', rooms: listRooms() }); break;
    case 'create': p.wantList = false; createRoom(p, String(m.quest || ''), !!m.public); break;
    case 'join': p.wantList = false; joinRoom(p, m.code); break;
    case 'quick': {   // すぐ入る: 待っている部屋があれば入り、なければ公開の部屋を作る
      p.wantList = false;
      const open = listRooms()[0];
      if (open) joinRoom(p, open.code); else createRoom(p, String(m.quest || 'hibashiri'), true);
      break;
    }
    case 'leave': leaveRoom(p); c.send({ t: 'left' }); break;
    case 'quest':
      if (r && r.phase !== 'battle' && r.players[0] === p && E.QUESTS[m.quest]) { r.quest = m.quest; r.phase = 'lobby'; r.state = null; sendRoom(r); }
      break;
    case 'start':
      if (r && r.phase !== 'battle' && r.players[0] === p) startBattle(r);
      break;
    case 'act': {
      if (!r || r.phase !== 'battle' || !r.state) break;
      const s = r.state;
      if (s.phase !== 'input' || s.turn !== m.u) break;
      if (ownerOf(r) !== p) { c.send({ t: 'error', msg: 'ほかの人のハンターです。' }); break; }
      const res = E.act(s, String(m.u), m.si | 0, m.target == null ? null : String(m.target));
      if (!res.ok) { c.send({ t: 'error', msg: res.error }); break; }
      afterAct(r, res.events);
      break;
    }
    case 'again':   // 結果画面から、同じ仲間で部屋へ戻る
      if (r && r.phase === 'result' && r.players[0] === p) { r.phase = 'lobby'; r.state = null; sendRoom(r); }
      break;
    case 'ping': c.send({ t: 'pong' }); break;
    default: break;
  }
}

function onClose(c) {
  const p = c.player;
  if (!p || p.conn !== c) return;
  p.conn = null; p.seen = Date.now();
  const r = p.room;
  if (!r) return;
  if (r.players.every((x) => !online(x))) { clearTimeout(r.timer); r.deadline = 0; }   // だれもいない間は止めておく
  else if (r.phase === 'battle' && ownerOf(r) === p) { armTimer(r, 0); sendState(r, []); }
  sendRoom(r);
}

// 片づけ: 長く戻ってこない人と、だれもいない部屋
setInterval(() => {
  const now = Date.now();
  for (const p of players.values()) {
    if (!p.conn && now - p.seen > IDLE_ROOM_MS) { leaveRoom(p); players.delete(p.token); }
  }
  for (const r of rooms.values()) {
    if (r.players.every((x) => !online(x)) && now - r.touched > IDLE_ROOM_MS) dropRoom(r);
  }
}, 60000).unref();

process.on('SIGTERM', () => { accounts.flush(); process.exit(0); });
process.on('SIGINT', () => { accounts.flush(); process.exit(0); });
server.listen(PORT, HOST, () => console.log(`hunt server on http://${HOST}:${PORT}/ (accounts: ${accounts.count()})`));

module.exports = { server };
