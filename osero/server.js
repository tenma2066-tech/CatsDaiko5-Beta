// UltraOSERO online server — zero dependencies (Node 18+).
// Serves index.html + engine.js and runs authoritative game rooms over WebSocket at /ws.
//   PORT=8080 OSERO_DATA_DIR=./data node server.js
'use strict';
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { Worker } = require('worker_threads');
const E = require('./engine.js');

const PORT = +process.env.PORT || 8080;
const HOST = process.env.HOST || '0.0.0.0';
const DATA_DIR = process.env.OSERO_DATA_DIR || path.join(__dirname, 'data');
const FILES = { '/': 'index.html', '/index.html': 'index.html', '/engine.js': 'engine.js' };
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };

// 乱用対策
const MAX_MSG = 16 * 1024;     // 1 メッセージの上限
const RATE = 15, BURST = 30;   // 1 接続あたり 毎秒15・瞬間30 メッセージ
const MAX_PER_IP = 10;         // 1 IP あたりの同時接続
const MAX_ROOMS = 300;
// 持ち時間
const TIMES = [0, 15, 30, 60];  // 秒。0 = なし（切断中だけ代打ち）
const OFFLINE_MS = +process.env.OSERO_OFFLINE_MS || 12000; // 切断中の人の手番は、これだけ待ったら CPU が代わりに打つ
const RECONNECT_MIN_MS = 10000; // 再接続した人には最低これだけ残す（全回復はしない）
const AFK_LIMIT = 3;            // 代打ちがこの回数続いたら、その席を CPU に交代
const STAMPS = 8;

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
const ipConns = new Map(); // ip -> 接続数
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
    if (this.buf.length > 1 << 18) return this.sock.destroy();
    for (;;) {
      const b = this.buf;
      if (b.length < 2) return;
      const op = b[0] & 15, masked = b[1] & 128;
      let len = b[1] & 127, off = 2;
      if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); off = 10; }
      if (len > MAX_MSG) return this.sock.destroy();
      if (!masked) return this.sock.destroy(); // clients must mask
      if (b.length < off + 4 + len) return;
      const mask = b.subarray(off, off + 4); off += 4;
      const payload = Buffer.from(b.subarray(off, off + len));
      for (let i = 0; i < len; i++) payload[i] ^= mask[i & 3];
      this.buf = b.subarray(off + len);
      if (op === 8) { this.frame(8, Buffer.alloc(0)); this.sock.end(); return; }
      if (op === 9) { this.frame(10, payload); continue; }
      if (op === 1) {
        if (!this.allow()) { if (++this.strikes > 60) return this.sock.destroy(); continue; } // 送りすぎは捨て、続くなら切る
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
  // Caddy の後ろでは X-Forwarded-For の先頭が利用者の IP
  const ip = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || req.socket.remoteAddress || '?';
  const n = ipConns.get(ip) || 0;
  if (n >= MAX_PER_IP) { sock.write('HTTP/1.1 429 Too Many Requests\r\nConnection: close\r\n\r\n'); sock.destroy(); return; }
  ipConns.set(ip, n + 1);
  const accept = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  sock.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  sock.setNoDelay(true);
  new Conn(sock, ip);
});

/* ---------- CPU: worker_threads のプールで考える（メインのループを止めない） ---------- */
const POOL_SIZE = Math.max(1, Math.min(2, (os.cpus() || []).length - 1));
const pool = [], jobs = [];
const quickMove = (data) => { try { return E.think(E.fromData(data), 1); } catch (e) { return -1; } };
function spawnWorker() {
  let w;
  try { w = new Worker(path.join(__dirname, 'cpu_worker.js')); } catch (e) { console.error('cpu worker unavailable', e.message); return null; }
  w.job = null;
  w.on('message', (msg) => { const j = w.job; w.job = null; if (j) { clearTimeout(j.timer); j.resolve(msg.move); } pump(); });
  w.on('error', (e) => console.error('cpu worker error:', e.message));
  w.on('exit', () => {
    const k = pool.indexOf(w); if (k >= 0) pool.splice(k, 1);
    const j = w.job; w.job = null;
    if (j) { clearTimeout(j.timer); j.resolve(quickMove(j.data)); }
    setTimeout(() => { const nw = spawnWorker(); if (nw) pool.push(nw); pump(); }, 500);
  });
  return w;
}
for (let k = 0; k < POOL_SIZE; k++) { const w = spawnWorker(); if (w) pool.push(w); }
function pump() {
  for (const w of pool) {
    if (w.job || !jobs.length) continue;
    const j = jobs.shift();
    w.job = j;
    j.timer = setTimeout(() => w.terminate(), (j.time || 1000) + 4000); // 応答しない worker は作り直す
    w.postMessage({ id: 0, data: j.data, level: j.level, time: j.time });
  }
}
function cpuThink(data, level, time) {
  return new Promise((resolve) => {
    if (!pool.length || jobs.length > 200) return resolve(quickMove(data));
    jobs.push({ data, level, time, resolve });
    pump();
  });
}

/* ---------- 戦績・レート（ファイルに保存） ---------- */
const STATS_FILE = path.join(DATA_DIR, 'stats.json');
let stats = { v: 1, players: {} }, statsTimer = null, statsWarned = false;
try { const d = JSON.parse(fs.readFileSync(STATS_FILE, 'utf8')); if (d && d.players) stats = d; } catch (e) { /* 初回は空 */ }
function saveStats() {
  if (statsTimer) return;
  statsTimer = setTimeout(() => {
    statsTimer = null;
    try {
      fs.mkdirSync(DATA_DIR, { recursive: true });
      const tmp = STATS_FILE + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(stats));
      fs.renameSync(tmp, STATS_FILE); // 書きかけのファイルを残さない
    } catch (e) { if (!statsWarned) { statsWarned = true; console.error('stats: 保存できません（メモリ上で続けます）:', e.message); } }
  }, 1000);
}
const profile = (token, name) => {
  let s = stats.players[token];
  if (!s) s = stats.players[token] = { name: name || 'プレイヤー', games: 0, wins: 0, losses: 0, draws: 0, rating: 1500 };
  if (name) s.name = name;
  return s;
};
function rankingList() {
  return Object.values(stats.players).filter((s) => s.games > 0)
    .sort((a, b) => (b.rating - a.rating) || (b.games - a.games));
}
const pubStat = (s, rank) => ({ name: s.name, games: s.games, wins: s.wins, losses: s.losses, draws: s.draws, rating: Math.round(s.rating), rank });
// 人間が2人以上いた対局だけ記録。人間どうしの総当たりで Elo。途中で抜けた人は最下位あつかい
function recordResult(r) {
  if (r.recorded || !r.game || !r.game.over) return;
  r.recorded = true;
  const hs = r.startHumans || [];
  if (hs.length < 2) return;
  const rank = E.ranking(r.game);
  const stonesOf = (p) => { const x = rank.find((y) => y.p === p); return x ? x.stones : 0; };
  const es = hs.map((h) => ({ token: h.token, s: profile(h.token, h.name), score: r.quitters.has(h.token) ? -1 : stonesOf(h.p) }));
  const top = Math.max(...es.map((e) => e.score));
  const nTop = es.filter((e) => e.score === top).length;
  const K = 32 / (es.length - 1), delta = es.map(() => 0);
  for (let a = 0; a < es.length; a++) for (let b = a + 1; b < es.length; b++) {
    const A = es[a], B = es[b];
    const ea = 1 / (1 + Math.pow(10, (B.s.rating - A.s.rating) / 400));
    const sa = A.score > B.score ? 1 : A.score < B.score ? 0 : 0.5;
    delta[a] += K * (sa - ea); delta[b] -= K * (sa - ea);
  }
  es.forEach((e, k) => {
    e.s.games++;
    if (e.score === top && nTop === 1) e.s.wins++; else if (e.score === top) e.s.draws++; else e.s.losses++;
    e.s.rating = Math.max(100, e.s.rating + delta[k]);
    const p = players.get(e.token);
    if (p && p.conn) p.conn.send({ t: 'rated', delta: Math.round(delta[k]), rating: Math.round(e.s.rating), stats: pubStat(e.s) });
  });
  saveStats();
}

/* ---------- rooms ---------- */
const players = new Map(); // token -> {token, name, conn, room}
const rooms = new Map();   // code -> room
const cleanName = (s) => String(s || '').replace(/[\u0000-\u001f<>]/g, '').trim().slice(0, 12);
const cleanTime = (t) => (TIMES.includes(t | 0) ? t | 0 : 30);

function newCode() {
  const A = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  for (;;) { let c = ''; for (let i = 0; i < 4; i++) c += A[crypto.randomInt(A.length)]; if (!rooms.has(c)) return c; }
}
function members(r) {
  const list = r.seats.filter((s) => s.token).map((s) => s.token);
  return [...new Set(list.concat([...r.watchers]))].map((t) => players.get(t)).filter(Boolean);
}
const anyOnline = (r) => members(r).some((p) => p.conn);
function broadcast(r, o) { for (const p of members(r)) if (p.conn) p.conn.send(o); }
function publicRoom(r) {
  return {
    code: r.code, cfg: r.cfg, public: !!r.public, time: r.time, started: !!r.game, over: !!(r.game && r.game.over),
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
  if (r.public) notifyLobby();
}

/* ---------- free rooms (public lobby) ---------- */
const lobbyWatch = new Set(); // conns looking at the free-room list
let lobbyTimer = null;
const isOpen = (r) => !r.game && r.seats.some((s) => s.type === 'human' && !s.token);
const hostOnline = (r) => { const h = players.get(r.host); return !!(h && h.conn); };
// 一覧に出すのは、開始前ならホストが接続中、対局中ならだれかが接続中の公開部屋だけ（抜け殻の部屋を出さない）
const listable = (r) => r.public && r.host && (r.game ? anyOnline(r) : hostOnline(r));
function roomSummary(r) {
  const host = players.get(r.host), humans = r.seats.filter((s) => s.type === 'human');
  return {
    code: r.code, host: host ? host.name : '', time: r.time,
    cfg: { shape: r.cfg.shape, w: r.cfg.w, h: r.cfg.h, players: r.cfg.players, walls: r.cfg.walls, chain: !!r.cfg.chain },
    joined: humans.filter((s) => s.token).length, humans: humans.length,
    open: isOpen(r), started: !!r.game, over: !!(r.game && r.game.over), watchers: r.watchers.size
  };
}
function listRooms() {
  return [...rooms.values()].filter(listable)
    .sort((a, b) => (isOpen(b) - isOpen(a)) || (b.created - a.created))
    .slice(0, 50).map(roomSummary);
}
function notifyLobby() {
  if (lobbyTimer || !lobbyWatch.size) return;
  lobbyTimer = setTimeout(() => {
    lobbyTimer = null;
    const list = listRooms();
    for (const c of lobbyWatch) c.send({ t: 'rooms', rooms: list });
  }, 250);
}
// keep at least one seat (besides the host's) open for humans, so others can actually join
function ensureGuestSeat(seats) {
  if (seats.filter((s) => s.type === 'human').length >= 2) return;
  const host = seats.findIndex((s) => s.type === 'human');
  const k = seats.findIndex((s, i) => i !== host && s.type !== 'human');
  if (k >= 0) seats[k] = { type: 'human', level: 0, token: null };
}
function createRoom(p, cfgIn, defsIn, isPublic, time) {
  const now = Date.now();
  if (now - (p.lastCreate || 0) < 2000) return p.conn && p.conn.send({ t: 'error', msg: '少し待ってからもう一度どうぞ' });
  if (rooms.size >= MAX_ROOMS) return p.conn && p.conn.send({ t: 'error', msg: 'いまは部屋がいっぱいです。少し待ってから試してください' });
  p.lastCreate = now;
  leaveRoom(p);
  const cfg = E.sanitize(cfgIn);
  delete cfg.seed;
  const defs = Array.isArray(defsIn) ? defsIn : [];
  const seats = [];
  for (let k = 0; k < cfg.players; k++) {
    const d = defs[k] || {};
    seats.push(d.type === 'cpu' ? { type: 'cpu', level: Math.max(1, Math.min(4, d.level | 0 || 2)), token: null } : { type: 'human', level: 0, token: null });
  }
  if (!seats.some((s) => s.type === 'human')) seats[0] = { type: 'human', level: 0, token: null };
  if (isPublic) ensureGuestSeat(seats);
  seats.find((s) => s.type === 'human').token = p.token;
  const room = {
    code: newCode(), cfg, seats, host: p.token, game: null, watchers: new Set(),
    cpuTimer: null, clockTimer: null, deadline: 0, limitMs: 0, seq: 0, paused: false,
    lastOnline: now, created: now, public: !!isPublic, time: cleanTime(time),
    startHumans: [], quitters: new Set(), recorded: false
  };
  rooms.set(room.code, room); p.room = room.code;
  sendRoom(room);
}
function leaveRoom(p) {
  const r = p.room && rooms.get(p.room);
  p.room = null;
  if (!r) return;
  r.watchers.delete(p.token);
  for (const s of r.seats) if (s.token === p.token) {
    s.token = null;
    if (r.game) { s.type = 'cpu'; s.level = 2; if (!r.game.over) r.quitters.add(p.token); } // 対局は CPU が引き継ぐ
  }
  if (r.host === p.token) { const next = r.seats.find((s) => s.token); r.host = next ? next.token : null; }
  if (!r.host) { dropRoom(r); return; }
  sendRoom(r);
  advance(r, 800);
}
function dropRoom(r) {
  clearTimeout(r.cpuTimer); clearTimeout(r.clockTimer);
  rooms.delete(r.code);
  if (r.public) notifyLobby();
}

/* ---------- 手番の進行（CPU / 持ち時間） ---------- */
const seatOnline = (s) => { const p = s.token && players.get(s.token); return !!(p && p.conn); };
function advance(r, delay) {
  clearTimeout(r.cpuTimer); clearTimeout(r.clockTimer); r.deadline = 0; r.seq++;
  const g = r.game;
  if (!g || g.over) return;
  if (r.seats[g.turn - 1].type === 'cpu') scheduleCpu(r, delay);
  else startClock(r, delay);
}
// ms を渡すとその残り時間で、無ければ持ち時間（切断中なら OFFLINE_MS）で時計を始める
function startClock(r, delay, ms) {
  const g = r.game, seat = r.seats[g.turn - 1];
  clearTimeout(r.clockTimer);
  const seq = ++r.seq;
  const limit = ms != null ? ms : seatOnline(seat) ? r.time * 1000 : OFFLINE_MS;
  r.limitMs = seatOnline(seat) ? r.time * 1000 : OFFLINE_MS;
  if (!limit) { r.deadline = 0; broadcast(r, { t: 'clock', p: g.turn, ms: 0, limit: 0 }); return; }
  r.deadline = Date.now() + delay + limit;
  broadcast(r, { t: 'clock', p: g.turn, ms: delay + limit, limit: r.limitMs, offline: !seatOnline(seat) });
  r.clockTimer = setTimeout(() => timeUp(r, seq), delay + limit);
}
async function timeUp(r, seq) {
  const g = r.game;
  if (!g || g.over || r.seq !== seq) return;
  if (!anyOnline(r)) { r.paused = true; r.deadline = 0; return; } // だれもいない部屋は止めておく
  const turn = g.turn, moveNo = g.moveNo, seat = r.seats[turn - 1];
  if (seat.type !== 'human') return;
  const m = await cpuThink(E.toData(g), 2);
  if (r.game !== g || g.over || g.turn !== turn || g.moveNo !== moveNo || r.seq !== seq) return;
  const mv = m >= 0 && E.isLegal(g, m, turn) ? m : E.moves(g, turn)[0];
  if (mv == null) return;
  seat.auto = (seat.auto || 0) + 1;
  broadcast(r, { t: 'auto', p: turn });
  if (seat.auto >= AFK_LIMIT && seat.token) {
    // 放置が続く席は CPU に交代し、本人は観戦にまわす
    const tok = seat.token;
    r.watchers.add(tok); r.quitters.add(tok);
    seat.token = null; seat.type = 'cpu'; seat.level = 2;
    if (r.host === tok) { const next = r.seats.find((s) => s.token); r.host = next ? next.token : tok; }
    broadcast(r, { t: 'afk', p: turn });
    sendRoom(r);
  }
  applyMove(r, mv, false);
}
function scheduleCpu(r, delay) {
  clearTimeout(r.cpuTimer);
  const g = r.game;
  if (!g || g.over || r.seats[g.turn - 1].type !== 'cpu') return;
  const seq = ++r.seq;
  r.cpuTimer = setTimeout(async () => {
    if (r.game !== g || g.over || r.seq !== seq) return;
    const seat = r.seats[g.turn - 1];
    if (seat.type !== 'cpu') return;
    const turn = g.turn, moveNo = g.moveNo;
    const m = await cpuThink(E.toData(g), seat.level, seat.level >= 4 ? 1500 : undefined);
    if (r.game !== g || g.over || g.turn !== turn || g.moveNo !== moveNo || r.seq !== seq) return;
    const mv = m >= 0 && E.isLegal(g, m, turn) ? m : E.moves(g, turn)[0];
    if (mv != null) applyMove(r, mv, false);
  }, delay);
}
function applyMove(r, i, manual) {
  const g = r.game, p = g.turn;
  if (!E.isLegal(g, i, p)) return false;
  if (manual) r.seats[p - 1].auto = 0;
  const rec = E.play(g, i);
  broadcast(r, { t: 'move', i, p });
  if (g.over) { advance(r, 0); sendRoom(r); recordResult(r); return true; }
  const flips = rec.flips.length / 4;
  // give clients time to play the animation before the next turn starts
  advance(r, Math.min(4500, 1100 + flips * 110 + (rec.gens > 1 ? 1200 : 0)));
  return true;
}

function handle(c, m) {
  if (!m || typeof m.t !== 'string') return;
  if (m.t === 'hello') {
    let p = typeof m.token === 'string' && players.get(m.token);
    if (!p) {
      const tok = typeof m.token === 'string' && /^[0-9a-f]{24}$/.test(m.token) && stats.players[m.token] ? m.token : crypto.randomBytes(12).toString('hex');
      p = { token: tok, name: '', conn: null, room: null }; players.set(p.token, p);
    }
    if (p.conn && p.conn !== c) { const old = p.conn; old.player = null; try { old.sock.end(); } catch (e) {} }
    p.conn = c; c.player = p;
    p.name = cleanName(m.name) || p.name || 'プレイヤー';
    c.send({ t: 'welcome', token: p.token });
    const r = p.room && rooms.get(p.room);
    if (r) {
      r.lastOnline = Date.now();
      sendRoom(r);
      if (r.game) {
        c.send({ t: 'state', data: E.toData(r.game) });
        const g = r.game;
        if (r.paused && !g.over) { r.paused = false; advance(r, 0); }
        else if (!g.over && r.seats[g.turn - 1].token === p.token && r.seats[g.turn - 1].type === 'human') {
          // 自分の手番で戻ってきた: 残りを最低 RECONNECT_MIN_MS にする（全回復はしない）
          const rem = r.deadline ? r.deadline - Date.now() : 0;
          const lim = r.time * 1000;
          startClock(r, 0, lim ? Math.min(lim, Math.max(rem, RECONNECT_MIN_MS)) : 0);
        } else if (r.deadline) c.send({ t: 'clock', p: g.turn, ms: Math.max(0, r.deadline - Date.now()), limit: r.limitMs });
      }
    }
    return;
  }
  const p = c.player;
  if (!p) return c.send({ t: 'error', msg: '先に接続してください' });
  const r = p.room && rooms.get(p.room);
  switch (m.t) {
    case 'create': createRoom(p, m.cfg, m.seats, !!m.public, m.time); return;
    case 'lobby': {
      if (m.on) { lobbyWatch.add(c); c.send({ t: 'rooms', rooms: listRooms() }); } else lobbyWatch.delete(c);
      return;
    }
    case 'quick': {
      // 募集中のフリールームのうち、いちばん長く待っている部屋に入る。無ければ自分で公開部屋を作る
      if (r && r.public && isOpen(r)) { sendRoom(r); return; }
      const t = [...rooms.values()].filter((x) => listable(x) && isOpen(x)).sort((a, b) => a.created - b.created)[0];
      if (!t) { createRoom(p, m.cfg, m.seats, true, m.time); return; }
      leaveRoom(p);
      p.room = t.code;
      t.seats.find((s) => s.type === 'human' && !s.token).token = p.token;
      sendRoom(t);
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
      if (t.game) {
        c.send({ t: 'state', data: E.toData(t.game) });
        if (t.deadline) c.send({ t: 'clock', p: t.game.turn, ms: Math.max(0, t.deadline - Date.now()), limit: t.limitMs });
      }
      return;
    }
    case 'leave': leaveRoom(p); c.send({ t: 'left' }); return;
    case 'start': {
      if (!r || r.host !== p.token) return c.send({ t: 'error', msg: 'ホストだけが開始できます' });
      if (r.game && !r.game.over) return;
      for (const s of r.seats) { if (s.type === 'human' && !s.token) { s.type = 'cpu'; s.level = 2; } s.auto = 0; }
      r.game = E.createGame(Object.assign({}, r.cfg, { seed: crypto.randomInt(1, 2 ** 31) }));
      r.startHumans = r.seats.map((s, k) => ({ s, k })).filter((x) => x.s.type === 'human' && x.s.token)
        .map((x) => ({ p: x.k + 1, token: x.s.token, name: (players.get(x.s.token) || {}).name }));
      r.quitters = new Set(); r.recorded = false; r.paused = false;
      sendRoom(r);
      broadcast(r, { t: 'state', data: E.toData(r.game) });
      advance(r, 1200);
      return;
    }
    case 'move': {
      if (!r || !r.game || r.game.over) return c.send({ t: 'error', msg: '対局中ではありません' });
      const seat = r.seats.findIndex((s) => s.token === p.token);
      if (seat !== r.game.turn - 1) return c.send({ t: 'error', msg: 'あなたの番ではありません' });
      if (!applyMove(r, m.i | 0, true)) c.send({ t: 'error', msg: 'そこには置けません' });
      return;
    }
    case 'stamp': {
      if (!r) return;
      const k = m.k | 0, now = Date.now();
      if (k < 0 || k >= STAMPS || now - (p.lastStamp || 0) < 1500) return;
      p.lastStamp = now;
      broadcast(r, { t: 'stamp', p: r.seats.findIndex((s) => s.token === p.token) + 1, name: p.name, k });
      return;
    }
    case 'stats': {
      const list = rankingList();
      const mine = stats.players[p.token];
      c.send({ t: 'stats', me: mine && mine.games ? pubStat(mine, list.indexOf(mine) + 1) : null, top: list.slice(0, 30).map((s, k) => pubStat(s, k + 1)), total: list.length });
      return;
    }
    case 'sync': if (r && r.game) c.send({ t: 'state', data: E.toData(r.game) }); return;
  }
}
function onClose(c) {
  lobbyWatch.delete(c);
  const p = c.player;
  if (!p || p.conn !== c) return;
  p.conn = null;
  const r = p.room && rooms.get(p.room);
  if (!r) return;
  sendRoom(r);
  // 手番の人が切れたら、持ち時間を OFFLINE_MS まで縮める
  const g = r.game;
  if (g && !g.over && r.seats[g.turn - 1].token === p.token && r.seats[g.turn - 1].type === 'human') {
    const rem = r.deadline ? r.deadline - Date.now() : Infinity;
    if (rem > OFFLINE_MS) startClock(r, 0, OFFLINE_MS);
  }
}

// drop rooms nobody has been connected to for 15 minutes
setInterval(() => {
  const t = Date.now();
  for (const r of rooms.values()) {
    if (anyOnline(r)) r.lastOnline = t;
    else if (t - r.lastOnline > 15 * 60 * 1000) dropRoom(r);
  }
  for (const p of players.values()) if (!p.conn && !(p.room && rooms.has(p.room))) players.delete(p.token);
}, 60 * 1000).unref();

server.listen(PORT, HOST, () => console.log(`UltraOSERO: http://${HOST}:${PORT}  (cpu workers: ${pool.length}, data: ${DATA_DIR})`));
