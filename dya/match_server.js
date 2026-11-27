// ダイナマイト野球 — 独立マッチサーバ（本家 sv2.splaxserver.net/chat の置き換え）
//
// 役割: dya.js クライアントの対戦通信を、本家サーバ無しで成立させる。
//   - socket.io 0.9.16 / protocol 1 のサーバ側（resource=/socket.io, namespace=/chat）
//   - マッチメイク: s3{r:N}/s4{r:id} でルームに入れ、2人そろったら組ませる
//   - 試合中は純粋な中継（v/r/m/e/s2/s3/vr/anc をそのまま相手へ転送）
//     → 試合ロジック・パケット内容は一切解釈しない。2 クライアントが互いに喋る。
//
// プロトコルは同梱の dya.js / socket.js から復元（ライブキャプチャ不要）:
//   接続   : io.connect('.../chat', {reconnection:false, transports:['websocket']})
//   送信   : sj_emt(name,obj) = socket.json.emit(name,obj)  → 5::/chat:{"name","args":[obj]}
//   入室   : s3{r:N}（N=0:1イニング / 1,2,3:3イニング） / s4{r:id}（再戦）
//   組成   : server→ s1{t,n}（t=0/1 の打順側, n=s_tdm(match_number)）を各自へ
//   自己紹介: client→ s2{v:game_ver, d:tm_data} → server が相手へ転送
//   試合中 : v{f,...} / r{f,...} / m / e / vr / anc を相互転送
//   相手落ち: server→ vr（生存通知）。client 側 watchdog がこれで後始末する。
//
// game_ver=46 / s_n(base62)="ByDExOQGWUFkfwMK5trYASJpnRzhjosLu8NVi0TZC4a1IPHl2c3Xm6vqb7e9gd"

'use strict';

const crypto = require('crypto');

const S_N = 'ByDExOQGWUFkfwMK5trYASJpnRzhjosLu8NVi0TZC4a1IPHl2c3Xm6vqb7e9gd';
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

// dya.js の s_tdm と同じ: 整数 → base62 文字列。クライアントは s_hrk で復号する。
function s_tdm(u) {
  u = Math.floor(u);
  if (u <= 0) return S_N[1]; // 正の値を保証（match_number==0 は lobby, -1 は失敗の意味）
  let r = '';
  while (u > 0) { r = S_N[u % 62] + r; u = Math.floor(u / 62); }
  return r;
}

// ===== RFC6455: サーバ送信フレーム（マスク無し・text）=====
function encodeTextFrame(str) {
  const payload = Buffer.from(str, 'utf8');
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.from([0x81, len]);
  } else if (len < 65536) {
    header = Buffer.from([0x81, 126, (len >> 8) & 0xff, len & 0xff]);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81; header[1] = 127;
    header.writeUInt32BE(Math.floor(len / 0x100000000), 2);
    header.writeUInt32BE(len >>> 0, 6);
  }
  return Buffer.concat([header, payload]);
}
function encodeCloseFrame() { return Buffer.from([0x88, 0x00]); }
function encodePong(payload) {
  const p = payload || Buffer.alloc(0);
  return Buffer.concat([Buffer.from([0x8A, p.length & 0x7f]), p]);
}

// ===== RFC6455: クライアント受信フレームのストリーミング復号（マスクあり）=====
function makeFrameDecoder() {
  let buf = Buffer.alloc(0);
  let fragOpcode = 0;
  let fragParts = [];
  return function push(chunk) {
    buf = Buffer.concat([buf, chunk]);
    const out = [];
    while (true) {
      if (buf.length < 2) break;
      const b0 = buf[0];
      const b1 = buf[1];
      const fin = (b0 & 0x80) !== 0;
      const opcode = b0 & 0x0f;
      const masked = (b1 & 0x80) !== 0;
      let len = b1 & 0x7f;
      let offset = 2;
      if (len === 126) {
        if (buf.length < offset + 2) break;
        len = buf.readUInt16BE(offset); offset += 2;
      } else if (len === 127) {
        if (buf.length < offset + 8) break;
        const hi = buf.readUInt32BE(offset);
        const lo = buf.readUInt32BE(offset + 4);
        len = hi * 0x100000000 + lo; offset += 8;
      }
      let maskKey = null;
      if (masked) {
        if (buf.length < offset + 4) break;
        maskKey = buf.slice(offset, offset + 4); offset += 4;
      }
      if (buf.length < offset + len) break;
      let payload = buf.slice(offset, offset + len);
      if (masked && maskKey) {
        const p = Buffer.allocUnsafe(len);
        for (let i = 0; i < len; i++) p[i] = payload[i] ^ maskKey[i & 3];
        payload = p;
      }
      buf = buf.slice(offset + len);

      if (opcode === 0x0) { // continuation
        fragParts.push(payload);
        if (fin) {
          const whole = Buffer.concat(fragParts); const op = fragOpcode;
          fragParts = []; fragOpcode = 0;
          if (op === 0x1) out.push({ opcode: 0x1, text: whole.toString('utf8') });
        }
        continue;
      }
      if (!fin && (opcode === 0x1 || opcode === 0x2)) {
        fragOpcode = opcode; fragParts = [payload]; continue;
      }
      if (opcode === 0x1) out.push({ opcode: 0x1, text: payload.toString('utf8') });
      else if (opcode === 0x8) out.push({ opcode: 0x8 });
      else if (opcode === 0x9) out.push({ opcode: 0x9, payload });
      else if (opcode === 0xA) out.push({ opcode: 0xA });
    }
    return out;
  };
}

// socket.io 0.9 パケット文字列を分解: "<type>:<id>:<endpoint>:<data>"
function decodePacket(str) {
  const m = /^(\d+):([^:]*):([^:]*):?([\s\S]*)?$/.exec(str);
  if (!m) return null;
  return { type: parseInt(m[1], 10), id: m[2], endpoint: m[3] || '', data: m[4] || '' };
}

function createMatchServer(opts) {
  opts = opts || {};
  const log = opts.log || function () {};
  const HEARTBEAT_MS = 20000;   // サーバ発ハートビート間隔（handshake の timeout=60 より十分短く）
  const HANDSHAKE_TTL = 60000;

  const pending = new Map();    // sid -> created ms（handshake 済・WS 未接続）
  const rooms = new Map();      // roomKey -> [conn, ...]（待機列）
  const all = new Set();        // 生存中の conn
  let matchCounter = 0;
  let connSeq = 0;

  function handleHandshake(req, res) {
    // 期限切れ sid を掃除
    const now = Date.now();
    for (const [sid, t] of pending) if (now - t > HANDSHAKE_TTL) pending.delete(sid);
    const sid = crypto.randomBytes(16).toString('hex');
    pending.set(sid, now);
    // <sid>:<heartbeat timeout>:<close timeout>:<transports>
    const body = `${sid}:60:60:websocket`;
    res.writeHead(200, {
      'content-type': 'text/plain; charset=UTF-8',
      'access-control-allow-origin': '*',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(body),
    });
    res.end(body);
  }

  function handleUpgrade(req, socket, head, sid) {
    if (sid) pending.delete(sid);
    const key = req.headers['sec-websocket-key'];
    if (!key) { try { socket.destroy(); } catch (_) {} return; }
    const accept = crypto.createHash('sha1').update(key + WS_GUID).digest('base64');
    socket.write(
      'HTTP/1.1 101 Switching Protocols\r\n' +
      'Upgrade: websocket\r\n' +
      'Connection: Upgrade\r\n' +
      'Sec-WebSocket-Accept: ' + accept + '\r\n\r\n'
    );

    const conn = makeConn(socket);
    all.add(conn);
    log('MATCH', conn.id, `WS connected (sid=${sid ? sid.slice(0, 8) : '-'})  total=${all.size}`);

    conn.sendRaw('1::'); // ベース namespace 接続確定
    if (head && head.length) conn.feed(head);
    socket.on('data', (c) => conn.feed(c));
    socket.on('close', () => conn.onClose('close'));
    socket.on('error', () => conn.onClose('error'));
  }

  function makeConn(socket) {
    const decode = makeFrameDecoder();
    const conn = {
      id: ++connSeq,
      socket,
      joinedChat: false,
      room: null,     // 待機中のルームキー
      peer: null,     // 対戦相手 conn
      tb: -1,
      closed: false,
      hb: null,
      sendRaw(str) {
        if (this.closed) return;
        try { this.socket.write(encodeTextFrame(str)); } catch (_) {}
      },
      sendEvent(name, arg) {
        // socket.json.emit 相当: 5::/chat:{"name":..,"args":[arg]}
        const args = (arg === undefined) ? [] : [arg];
        this.sendRaw('5::/chat:' + JSON.stringify({ name, args }));
      },
      feed(chunk) {
        let frames;
        try { frames = decode(chunk); } catch (_) { return; }
        for (const f of frames) {
          if (f.opcode === 0x1) this.onPacket(f.text);
          else if (f.opcode === 0x8) this.onClose('ws-close');
          else if (f.opcode === 0x9) { try { this.socket.write(encodePong(f.payload)); } catch (_) {} }
          // 0xA(pong) は無視
        }
      },
      onPacket(str) {
        const p = decodePacket(str);
        if (!p) return;
        switch (p.type) {
          case 2: // heartbeat（クライアント発）→ 返す
            this.sendRaw('2::');
            break;
          case 1: // connect（namespace 接続要求）
            if (p.endpoint === '/chat') {
              this.joinedChat = true;
              this.sendRaw('1::/chat');
              this.startHeartbeat();
              log('MATCH', this.id, 'joined /chat namespace');
            }
            break;
          case 0: // disconnect
            this.onClose('client-disconnect');
            break;
          case 5: { // event
            let ev;
            try { ev = JSON.parse(p.data); } catch (_) { return; }
            this.onEvent(ev.name, (ev.args && ev.args[0]) !== undefined ? ev.args[0] : undefined);
            break;
          }
          default:
            break;
        }
      },
      onEvent(name, arg) {
        // すでに対戦中なら全イベントを相手へ素通し中継
        if (this.peer && !this.peer.closed) {
          this.peer.sendEvent(name, arg);
          return;
        }
        // 未マッチ: 入室要求だけ処理
        if ((name === 's3' || name === 's4') && arg && arg.r !== undefined) {
          joinRoom(this, name + ':' + String(arg.r));
        }
        // それ以外（マッチ前の雑多なイベント）は無視
      },
      startHeartbeat() {
        if (this.hb) return;
        this.hb = setInterval(() => { this.sendRaw('2::'); }, HEARTBEAT_MS);
        if (this.hb.unref) this.hb.unref();
      },
      onClose(reason) {
        if (this.closed) return;
        this.closed = true;
        if (this.hb) { clearInterval(this.hb); this.hb = null; }
        all.delete(this);
        // 待機列から除去
        if (this.room && rooms.has(this.room)) {
          const q = rooms.get(this.room).filter((c) => c !== this);
          if (q.length) rooms.set(this.room, q); else rooms.delete(this.room);
        }
        // 対戦相手へ「相手落ち」を通知
        if (this.peer && !this.peer.closed) {
          log('MATCH', this.id, `closed (${reason}) -> notify peer #${this.peer.id} (vr)`);
          this.peer.sendEvent('vr', {});
          this.peer.peer = null;
        } else {
          log('MATCH', this.id, `closed (${reason})  total=${all.size}`);
        }
        this.peer = null;
        try { this.socket.write(encodeCloseFrame()); } catch (_) {}
        try { this.socket.end(); } catch (_) {}
      },
    };
    return conn;
  }

  function joinRoom(conn, roomKey) {
    if (conn.peer) return; // すでに対戦中
    // 既存の待機から移動
    if (conn.room && conn.room !== roomKey && rooms.has(conn.room)) {
      const q = rooms.get(conn.room).filter((c) => c !== conn);
      if (q.length) rooms.set(conn.room, q); else rooms.delete(conn.room);
    }
    conn.room = roomKey;
    let q = rooms.get(roomKey);
    if (!q) { q = []; rooms.set(roomKey, q); }
    if (q.indexOf(conn) < 0) q.push(conn);
    log('MATCH', conn.id, `queue room=${roomKey}  waiting=${q.length}`);
    // ロビー番号（待機人数）を通知（dya.js: 's' は run_mode 6→8、server_p_number 表示）
    conn.sendEvent('s', { n: q.length });

    // 2 人そろったら組ませる（先頭 2 人）
    while (q.length >= 2) {
      const a = q.shift();
      const b = q.shift();
      if (a.closed || a.peer) { if (b && !b.closed) q.unshift(b); continue; }
      if (b.closed || b.peer) { if (a && !a.closed) q.unshift(a); continue; }
      startMatch(a, b);
    }
    if (q.length === 0) rooms.delete(roomKey);
  }

  function startMatch(a, b) {
    matchCounter++;
    const n = s_tdm(matchCounter);
    a.peer = b; b.peer = a;
    a.room = null; b.room = null;
    a.tb = 0; b.tb = 1;
    log('MATCH', a.id, `paired #${a.id}(t=0) <-> #${b.id}(t=1)  match=${matchCounter} n=${n}`);
    // s1{t,n}: client は my_tb=data.t, match_number=s_hrk(data.n) にして s2 を返す
    a.sendEvent('s1', { t: 0, n });
    b.sendEvent('s1', { t: 1, n });
  }

  // socket.io のパスか判定（handshake と websocket）
  function isHandshakePath(pathname) {
    return /^\/socket\.io\/1\/?$/.test(pathname);
  }
  function wsSidFromPath(pathname) {
    const m = /^\/socket\.io\/1\/websocket\/([^/?]+)/.exec(pathname);
    return m ? m[1] : null;
  }

  function stats() {
    return { conns: all.size, rooms: rooms.size, matches: matchCounter };
  }

  return { handleHandshake, handleUpgrade, isHandshakePath, wsSidFromPath, stats, s_tdm };
}

module.exports = { createMatchServer, s_tdm, encodeTextFrame, makeFrameDecoder, decodePacket };
