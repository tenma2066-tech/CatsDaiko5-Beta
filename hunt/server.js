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
const G = require('./gacha.js');

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
const MAX_PLAYERS = 5;   // 席は5つ。いる人で順番に分ける
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
const DEFS = Object.assign(E.defs(), { gacha: G.info() });
const AUTO_MS = +process.env.HUNT_AUTO_MS || 700;   // オートのとき、演出のあと次の行動までの間（画面から「再生おわり」が来なかったときの目安）
const AUTO_FAST_MS = +process.env.HUNT_AUTO_FAST_MS || 60;   // 画面の再生が終わってから、オートが動くまでの間

const cleanName = (s) => String(s || '').replace(/[\u0000-\u001f<>&"']/g, '').trim().slice(0, 12) || 'ハンター';
function newCode() {
  for (;;) { const c = String(crypto.randomInt(1000, 10000)); if (!rooms.has(c)) return c; }
}
const online = (p) => !!(p && p.conn);
function broadcast(r, o) { for (const p of r.players) if (p.conn) p.conn.send(o); }

/* その人が遊べるステージか（ゲストは草原だけ・ログインしている人は前のステージをクリアしたら） */
function stageOpen(p, stage) {
  if (!E.STAGES[stage]) return false;
  if (!p.user) return E.STAGES[stage].world === 'meadow';
  return accounts.stageOpen(p.user, stage);
}
/* その人の編成（{ key, dup } の並び）。ゲストは R だけ */
function deckOf(p) {
  if (p.user) return accounts.partyOf(p.user) || E.R_KEYS.map((key) => ({ key, dup: 0 }));
  return (p.guestDeck && p.guestDeck.length ? p.guestDeck : E.R_KEYS).map((key) => ({ key, dup: 0 }));
}
/* 5つの席を、いる人に順番に配る（席 k は k 番目の人の、その人の編成の k/人数 番目） */
function buildParty(r) {
  const n = r.players.length, decks = r.players.map(deckOf), used = r.players.map(() => new Set());
  const party = [];
  for (let k = 0; k < 5; k++) {
    for (let t = 0; t < n; t++) {
      const owner = (k + t) % n, deck = decks[owner];
      const c = deck.find((x) => !used[owner].has(x.key));
      if (!c) continue;
      used[owner].add(c.key);
      party.push({ key: c.key, dup: c.dup, owner });
      break;
    }
  }
  return party;
}

function roomView(r, p) {
  return {
    t: 'room', code: r.code, stage: r.stage, public: r.public, phase: r.phase, solo: !!r.solo, host: r.players[0] ? r.players[0].id : null,
    you: p.id, max: MAX_PLAYERS,
    players: r.players.map((x) => ({ id: x.id, name: x.name, online: online(x), member: !!x.user && !accounts.isGuest(x.user), auto: !!x.auto, deck: deckOf(x).map((d) => d.key) })),
  };
}
function sendRoom(r) { for (const p of r.players) if (p.conn) p.conn.send(roomView(r, p)); notifyLobby(); }

function listRooms() {
  const out = [];
  for (const r of rooms.values()) {
    if (!r.public || r.phase !== 'lobby' || r.players.length >= MAX_PLAYERS || !online(r.players[0])) continue;
    out.push({ code: r.code, stage: r.stage, host: r.players[0].name, n: r.players.length });
  }
  return out.slice(0, 30);
}
function notifyLobby() {
  const list = listRooms();
  for (const p of players.values()) if (p.conn && !p.room && p.wantList) p.conn.send({ t: 'rooms', rooms: list });
}

function createRoom(p, stage, isPublic, solo) {
  if (rooms.size >= MAX_ROOMS) { p.conn.send({ t: 'error', msg: 'いま部屋がいっぱいです。少し待ってからお試しください。' }); return null; }
  const now = Date.now();
  if (now - (p.lastCreate || 0) < 1500) { p.conn.send({ t: 'error', msg: '少し待ってからもう一度お試しください。' }); return null; }
  if (!stageOpen(p, stage)) stage = '1-1';
  p.lastCreate = now;
  leaveRoom(p);
  const r = { code: newCode(), stage, public: !!isPublic && !solo, solo: !!solo, phase: 'lobby',
              players: [p], state: null, timer: null, deadline: 0, touched: now };
  rooms.set(r.code, r); p.room = r;
  sendRoom(r);
  return r;
}

function joinRoom(p, code) {
  const r = rooms.get(String(code || ''));
  if (!r || r.solo) return p.conn.send({ t: 'error', msg: 'その合言葉の部屋は見つかりません。' });
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
    let k = 0;
    for (const u of E.hunters(r.state)) {
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
  v.deadline = r.deadline; r.seq = (r.seq | 0) + 1;
  for (const p of r.players) if (p.conn) p.conn.send({ t: 'state', state: v, events, now: Date.now(), seq: r.seq });
}

/* 持ち時間の時計。演出が終わるころから数えはじめる。オートの人の番は、演出のあとすぐ AI が動く */
function armTimer(r, nEvents) {
  clearTimeout(r.timer);
  const s = r.state;
  if (!s || s.phase !== 'input') { r.deadline = 0; return; }
  const p = ownerOf(r);
  const anim = Math.min(nEvents * EVENT_MS, 15000);
  const auto = p && p.auto;
  // オートは演出の目安が過ぎたら動く。画面の再生が先に終わったら（idle）、そこで早める
  const wait = auto ? anim + AUTO_MS : anim + (online(p) ? TURN_MS : OFFLINE_MS);
  r.deadline = auto ? 0 : Date.now() + wait;
  r.autoAct = () => {
    if (r.state !== s || s.phase !== 'input') return;
    const who = E.unit(s, s.turn);
    const res = E.aiTurn(s);
    if (!res.ok) return;
    if (!auto) res.events.unshift({ t: 'auto', u: who.id });
    afterAct(r, res.events);
  };
  r.autoAt = Date.now() + wait;
  r.timer = setTimeout(r.autoAct, wait);
}

function sendMe(p) { if (p.conn) p.conn.send({ t: 'me', me: p.user ? accounts.profile(p.user) : null }); }

/* 狩りが終わった: ログインしている人の戦績・彩石・実績を記録する */
function recordResult(r) {
  const s = r.state;
  for (const p of r.players) {
    if (!p.user) continue;
    const res = accounts.record(p.user, { stage: s.stage, win: !!s.win, turns: s.turns, breaks: r.breaks | 0, players: r.players.length });
    if (res && p.conn) { p.conn.send({ t: 'reward', stones: res.stones, first: res.first, unlocked: res.unlocked }); sendMe(p); }
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
  const party = buildParty(r);
  if (!party.length) return;
  r.state = E.createBattle(r.stage, party, crypto.randomInt(1, 2 ** 31 - 1));
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
      p = { token, id: crypto.randomBytes(4).toString('hex'), name: cleanName(m.name), room: null, conn: null, wantList: false, auto: false };
      players.set(token, p);
    }
    if (m.name) p.name = cleanName(m.name);
    const page = typeof m.page === 'string' ? m.page.slice(0, 40) : '';
    if (p.page !== page && accounts.isGuest(p.user)) { accounts.dropGuest(p.user); p.user = null; }   // 再読み込み: ゲストのデータは消える
    p.page = page;
    p.conn = c; c.player = p; p.seen = Date.now();
    const su = accounts.resume(m.session);
    if (su) { if (accounts.isGuest(p.user)) accounts.dropGuest(p.user); p.user = su; p.session = m.session; p.name = accounts.nameOf(su); } else if (m.session && !accounts.isGuest(p.user)) { p.user = null; p.session = null; }
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
  const battle = () => (r && r.phase === 'battle' && r.state ? r.state : null);
  const mine = (s, uid) => {
    if (!s || s.phase !== 'input' || s.turn !== uid) return false;
    if (ownerOf(r) !== p) { c.send({ t: 'error', msg: 'ほかの人のハンターです。' }); return false; }
    return true;
  };
  switch (m.t) {
    case 'name': if (!p.user || accounts.isGuest(p.user)) { p.name = cleanName(m.name); if (p.user) accounts.renameGuest(p.user, p.name); if (r) sendRoom(r); } break;
    case 'guest': {   // ゲストではじめる: ログインした人と同じように遊べる（再読み込みで消える）
      if (p.user && !accounts.isGuest(p.user)) { c.send({ t: 'auth', me: accounts.profile(p.user) }); break; }
      if (m.name) p.name = cleanName(m.name);
      if (!p.user) p.user = accounts.guest(p.name); else accounts.renameGuest(p.user, p.name);
      p.session = null;
      c.send({ t: 'auth', me: accounts.profile(p.user), guest: true });
      if (r) sendRoom(r);
      break;
    }
    case 'register':
    case 'login': {
      if (p.authBusy) break;
      if (!loginAllowed(c.ip)) { c.send({ t: 'auth', error: '試した回数が多すぎます。10分ほど待ってからお試しください。' }); break; }
      p.authBusy = true;
      const fn = m.t === 'register' ? accounts.register : accounts.login;
      fn(m.user, m.pass).then((res) => {
        p.authBusy = false;
        if (res.error) { if (p.conn) p.conn.send({ t: 'auth', error: res.error }); return; }
        if (accounts.isGuest(p.user)) accounts.dropGuest(p.user);
        p.user = res.user; p.session = res.session; p.name = accounts.nameOf(res.user);
        if (p.conn) p.conn.send({ t: 'auth', session: res.session, me: accounts.profile(res.user), created: m.t === 'register' });
        if (p.room) sendRoom(p.room);
      }).catch(() => { p.authBusy = false; if (p.conn) p.conn.send({ t: 'auth', error: 'うまくいきませんでした。もう一度お試しください。' }); });
      break;
    }
    case 'logout':
      { const was = accounts.isGuest(p.user);
        if (was) accounts.dropGuest(p.user); else if (p.user) accounts.logout(p.user, p.session);
        p.user = null; p.session = null;
        c.send({ t: 'auth', session: null, me: null, left: was }); }
      break;
    case 'me': sendMe(p); break;
    case 'claim': {
      if (!p.user) { c.send({ t: 'error', msg: 'ログインすると受け取れます。' }); break; }
      const res = accounts.claim(p.user, String(m.id || ''));
      if (res.error) c.send({ t: 'error', msg: res.error }); else c.send({ t: 'claimed', id: m.id, reward: res.reward });
      sendMe(p);
      break;
    }
    case 'daily': {
      if (!p.user) { c.send({ t: 'error', msg: 'ログインすると受け取れます。' }); break; }
      const res = accounts.daily(p.user);
      if (res.error) c.send({ t: 'error', msg: res.error }); else c.send({ t: 'claimed', id: 'daily', reward: res.reward });
      sendMe(p);
      break;
    }
    case 'deck': {   // 編成。ゲストは R のキャラだけ
      const keys = Array.isArray(m.keys) ? m.keys.map(String) : [];
      if (p.user) { const res = accounts.setDeck(p.user, keys); if (res.error) c.send({ t: 'error', msg: res.error }); sendMe(p); }
      else {
        const d = [...new Set(keys)].filter((k) => E.R_KEYS.includes(k)).slice(0, 5);
        if (!d.length) c.send({ t: 'error', msg: 'ゲストは R のキャラだけ使えます。' }); else p.guestDeck = d;
        c.send({ t: 'guestDeck', deck: p.guestDeck || E.R_KEYS });
      }
      if (r && r.phase === 'lobby') sendRoom(r);
      break;
    }
    case 'gacha': {
      if (!p.user) { c.send({ t: 'error', msg: 'ガチャはログインすると引けます。' }); break; }
      const now = Date.now();
      if (now - (p.lastPull || 0) < 800) break;   // 連打よけ
      p.lastPull = now;
      const res = accounts.pull(p.user, String(m.banner || ''), m.count === 10 ? 10 : 1);
      if (res.error) c.send({ t: 'error', msg: res.error });
      else c.send({ t: 'gacha', banner: m.banner, results: res.results, stones: res.stones, pity: res.pity, unlocked: res.unlocked });
      sendMe(p);
      break;
    }
    case 'rooms': p.wantList = true; c.send({ t: 'rooms', rooms: listRooms() }); break;
    case 'solo': {   // ひとりで出撃: 自分だけの部屋を作ってすぐ始める
      p.wantList = false;
      const stage = String(m.stage || '');
      if (!stageOpen(p, stage)) { c.send({ t: 'error', msg: 'そのステージはまだ遊べません。' }); break; }
      const nr = createRoom(p, stage, false, true);
      if (nr) startBattle(nr);
      break;
    }
    case 'create': p.wantList = false; createRoom(p, String(m.stage || '1-1'), !!m.public, false); break;
    case 'join': p.wantList = false; joinRoom(p, m.code); break;
    case 'quick': {   // すぐ入る: 待っている部屋があれば入り、なければ公開の部屋を作る
      p.wantList = false;
      const open = listRooms()[0];
      if (open) joinRoom(p, open.code); else createRoom(p, String(m.stage || '1-1'), true, false);
      break;
    }
    case 'leave': leaveRoom(p); c.send({ t: 'left' }); break;
    case 'stage':
      if (r && r.phase !== 'battle' && r.players[0] === p) {
        if (!stageOpen(p, String(m.stage))) { c.send({ t: 'error', msg: 'そのステージはまだ遊べません。' }); break; }
        r.stage = String(m.stage); r.phase = 'lobby'; r.state = null; sendRoom(r);
      }
      break;
    case 'start':
      if (r && r.phase !== 'battle' && r.players[0] === p) startBattle(r);
      break;
    case 'auto':
      p.auto = !!m.on;
      if (r) { sendRoom(r); if (battle() && ownerOf(r) === p) { armTimer(r, 0); if (p.auto && r.autoAct) { clearTimeout(r.timer); r.autoAt = Date.now() + AUTO_FAST_MS; r.timer = setTimeout(r.autoAct, AUTO_FAST_MS); } } }
      break;
    case 'move': {
      const s = battle(); if (!mine(s, String(m.u))) break;
      const res = E.move(s, String(m.u), m.x | 0, m.z | 0);
      if (!res.ok) { c.send({ t: 'error', msg: res.error }); break; }
      r.touched = Date.now();
      sendState(r, res.events);   // 時計は動かしたまま（動いても持ち時間は増えない）
      break;
    }
    case 'act': {
      const s = battle(); if (!mine(s, String(m.u))) break;
      const res = E.act(s, String(m.u), m.si | 0, m.target == null ? null : String(m.target));
      if (!res.ok) { c.send({ t: 'error', msg: res.error }); break; }
      afterAct(r, res.events);
      break;
    }
    case 'wait': {
      const s = battle(); if (!mine(s, String(m.u))) break;
      const res = E.wait(s, String(m.u));
      if (!res.ok) { c.send({ t: 'error', msg: res.error }); break; }
      afterAct(r, res.events);
      break;
    }
    case 'again':   // 結果画面から、同じ仲間で部屋へ戻る（ひとりのときは、もう一度出撃）
      if (r && r.phase === 'result' && r.players[0] === p) {
        if (r.solo) startBattle(r); else { r.phase = 'lobby'; r.state = null; sendRoom(r); }
      }
      break;
    case 'idle': {   // 画面の再生が終わった。いまの番がこの人のオートなら、すぐ動かす
      const s = battle();
      if (!s || s.phase !== 'input' || (m.seq | 0) !== r.seq || ownerOf(r) !== p || !p.auto || !r.autoAct) break;
      if ((r.autoAt || 0) - Date.now() > AUTO_FAST_MS) { clearTimeout(r.timer); r.autoAt = Date.now() + AUTO_FAST_MS; r.timer = setTimeout(r.autoAct, AUTO_FAST_MS); }
      break;
    }
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
    if (!p.conn && now - p.seen > IDLE_ROOM_MS) { leaveRoom(p); if (accounts.isGuest(p.user)) accounts.dropGuest(p.user); players.delete(p.token); }
  }
  for (const r of rooms.values()) {
    if (r.players.every((x) => !online(x)) && now - r.touched > IDLE_ROOM_MS) dropRoom(r);
  }
}, 60000).unref();

process.on('SIGTERM', () => { accounts.flush(); process.exit(0); });
process.on('SIGINT', () => { accounts.flush(); process.exit(0); });
server.listen(PORT, HOST, () => console.log(`hunt server on http://${HOST}:${PORT}/ (accounts: ${accounts.count()})`));

module.exports = { server };
