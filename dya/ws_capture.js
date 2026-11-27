// ダイナマイト野球 — 対戦サーバ（sv2.splaxserver.net/chat, socket.io 0.9）の
// WebSocket フレームを人が読める JSONL に落とす採取器。依存ゼロ（fs/path のみ）。
//
// 目的: 人対人オンライン対戦を VPS 独立化するには、まず本家の試合プロトコルを
//       実通信から観測する必要がある。proxy.js の WS 中継は素の TLS バイト列を
//       そのまま pipe しているだけなので、ここで「盗聴」だけして中継には干渉しない
//       （tee）。RFC6455 のフレームを解き、socket.io 0.9 パケットを種別/イベント名/
//       引数に分解して 1 行ずつ追記する。
//
// 使い方（proxy.js 側）:
//   const cap = require('./ws_capture').createWsCapture(DATA_DIR);
//   const sink = cap.sink(connId);           // 1 接続につき 1 つ
//   clientSocket.on('data', c => sink.feed('up', c));    // client -> server（マスクあり）
//   upstream.on('data',     c => sink.feed('down', c));  // server -> client（マスクなし）
//   if (head && head.length) sink.feed('up', head);      // upgrade に同梱された先頭フレーム
//
// 出力: <DATA_DIR>/ws_capture/chat-<connId>-<ts>.jsonl
//   {"t":<ms>,"dir":"up|down","type":"event","event":"v","args":[...]}
//   {"t":<ms>,"dir":"down","type":"connect"}  など

'use strict';

const fs = require('fs');
const path = require('path');

const MAX_FILES = 12;            // 直近この数だけ残す（古いものから消す）
const MAX_FILE_BYTES = 8 * 1024 * 1024;  // 1 ファイル上限。超えたら以降は書かない
const ARG_CAP = 4000;            // 1 引数/生データの最大文字数（読みやすさのため切り詰め）

function capStr(s) {
  if (typeof s !== 'string') return s;
  return s.length > ARG_CAP ? s.slice(0, ARG_CAP) + '…(+' + (s.length - ARG_CAP) + ')' : s;
}

function capArgs(args) {
  if (!Array.isArray(args)) return args;
  return args.map((a) => (typeof a === 'string' ? capStr(a) : a));
}

// socket.io 0.9 パケット文字列 "<type>:<id>:<endpoint>:<data>" を分解する。
// 種別: 0=disconnect 1=connect 2=heartbeat 3=message 4=json 5=event 6=ack 7=error 8=noop
function parseSocketIo(s) {
  const TYPES = {
    '0': 'disconnect', '1': 'connect', '2': 'heartbeat', '3': 'message',
    '4': 'json', '5': 'event', '6': 'ack', '7': 'error', '8': 'noop',
  };
  const m = /^(\d):([^:]*):([^:]*):?([\s\S]*)$/.exec(s);
  if (!m) return { type: 'raw', raw: capStr(s) };
  const type = m[1];
  const endpoint = m[3];
  const data = m[4];
  const base = endpoint ? { endpoint } : {};
  if (type === '5') {
    // event: data = {"name":"...","args":[...]}
    try {
      const o = JSON.parse(data);
      return Object.assign({ type: 'event', event: o.name, args: capArgs(o.args) }, base);
    } catch (_) {
      return Object.assign({ type: 'event', raw: capStr(data) }, base);
    }
  }
  if (type === '2') return Object.assign({ type: 'heartbeat' }, base);
  if (type === '4') {
    try { return Object.assign({ type: 'json', data: JSON.parse(data) }, base); }
    catch (_) { return Object.assign({ type: 'json', raw: capStr(data) }, base); }
  }
  const out = { type: TYPES[type] || ('t' + type) };
  if (data) out.raw = capStr(data);
  return Object.assign(out, base);
}

// RFC6455 フレームのストリーミング・パーサ。チャンク境界をまたぐので残差を溜める。
// WebSocket 確立前の HTTP ハンドシェイク（"GET ..." / "HTTP/1.1 101 ..."）は
// 一度だけ読み飛ばす。
function makeFrameParser() {
  let buf = Buffer.alloc(0);
  let stripped = false;   // HTTP プリアンブルを除去済みか
  let fragOpcode = 0;     // 継続フレームの元 opcode
  let fragParts = [];     // 継続フレームの text 片

  function looksHttp(b) {
    // "GET " または "HTTP" で始まればハンドシェイク行
    if (b.length < 4) return null; // まだ判断できない
    const s = b.slice(0, 4).toString('latin1');
    return s === 'GET ' || s === 'HTTP';
  }

  return {
    push(chunk) {
      buf = Buffer.concat([buf, chunk]);
      const out = [];

      if (!stripped) {
        const isHttp = looksHttp(buf);
        if (isHttp === null) return out;        // 4 バイト未満、次を待つ
        if (isHttp) {
          const sep = buf.indexOf('\r\n\r\n');
          if (sep < 0) return out;              // ヘッダ終端がまだ、次を待つ
          buf = buf.slice(sep + 4);
        }
        stripped = true;                        // 以降は素の WS フレーム
      }

      while (true) {
        if (buf.length < 2) break;
        const b0 = buf[0];
        const b1 = buf[1];
        const fin = (b0 & 0x80) !== 0;
        let opcode = b0 & 0x0f;
        const masked = (b1 & 0x80) !== 0;
        let len = b1 & 0x7f;
        let offset = 2;
        if (len === 126) {
          if (buf.length < offset + 2) break;
          len = buf.readUInt16BE(offset);
          offset += 2;
        } else if (len === 127) {
          if (buf.length < offset + 8) break;
          const hi = buf.readUInt32BE(offset);
          const lo = buf.readUInt32BE(offset + 4);
          len = hi * 0x100000000 + lo;
          offset += 8;
        }
        let maskKey = null;
        if (masked) {
          if (buf.length < offset + 4) break;
          maskKey = buf.slice(offset, offset + 4);
          offset += 4;
        }
        if (buf.length < offset + len) break;
        let payload = buf.slice(offset, offset + len);
        if (masked && maskKey) {
          const p = Buffer.allocUnsafe(len);
          for (let i = 0; i < len; i++) p[i] = payload[i] ^ maskKey[i & 3];
          payload = p;
        }
        buf = buf.slice(offset + len);

        // 継続フレーム（opcode 0）の組み立て
        if (opcode === 0x0) {
          fragParts.push(payload.toString('utf8'));
          if (fin) {
            const text = fragParts.join('');
            const op = fragOpcode;
            fragParts = [];
            fragOpcode = 0;
            if (op === 0x1) out.push({ opcode: 0x1, payloadLen: text.length, text });
          }
          continue;
        }
        if (!fin && (opcode === 0x1 || opcode === 0x2)) {
          fragOpcode = opcode;
          fragParts = [payload.toString('utf8')];
          continue;
        }

        if (opcode === 0x1) {
          out.push({ opcode: 0x1, payloadLen: len, text: payload.toString('utf8') });
        } else {
          out.push({ opcode, payloadLen: len, text: null });
        }
      }
      return out;
    },
  };
}

function createWsCapture(dataDir) {
  const dir = path.join(dataDir, 'ws_capture');
  try { fs.mkdirSync(dir, { recursive: true }); } catch (_) {}

  function pruneOld() {
    try {
      const files = fs.readdirSync(dir)
        .filter((f) => f.endsWith('.jsonl'))
        .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
        .sort((a, b) => b.t - a.t);
      for (const { f } of files.slice(MAX_FILES)) {
        try { fs.unlinkSync(path.join(dir, f)); } catch (_) {}
      }
    } catch (_) {}
  }

  function sink(connId) {
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    const file = path.join(dir, `chat-${connId}-${ts}.jsonl`);
    const parsers = { up: makeFrameParser(), down: makeFrameParser() };
    let bytes = 0;
    let stopped = false;
    pruneOld();

    function writeRec(rec) {
      if (stopped) return;
      const line = JSON.stringify(rec) + '\n';
      bytes += Buffer.byteLength(line);
      if (bytes > MAX_FILE_BYTES) { stopped = true; return; }
      try { fs.appendFileSync(file, line); } catch (_) {}
    }

    return {
      file,
      feed(direction, chunk) {
        if (stopped) return;
        const parser = parsers[direction];
        if (!parser) return;
        let frames;
        try { frames = parser.push(chunk); } catch (_) { return; }
        for (const fr of frames) {
          const now = Date.now();
          if (fr.opcode === 0x1) {
            const rec = parseSocketIo(fr.text);
            rec.t = now; rec.dir = direction;
            writeRec(rec);
          } else if (fr.opcode === 0x8) {
            writeRec({ t: now, dir: direction, type: 'ws-close' });
          } else if (fr.opcode === 0x9) {
            writeRec({ t: now, dir: direction, type: 'ws-ping' });
          } else if (fr.opcode === 0xA) {
            writeRec({ t: now, dir: direction, type: 'ws-pong' });
          } else if (fr.opcode === 0x2) {
            writeRec({ t: now, dir: direction, type: 'ws-binary', len: fr.payloadLen });
          }
        }
      },
    };
  }

  // PIN 付きルート /__capture 用: 全採取ファイルを 1 本のテキストにまとめて返す。
  function dump() {
    let files;
    try {
      files = fs.readdirSync(dir)
        .filter((f) => f.endsWith('.jsonl'))
        .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
        .sort((a, b) => a.t - b.t);
    } catch (_) { return '(no capture dir)\n'; }
    if (files.length === 0) return '(採取ファイルなし。オンライン対戦を1試合プレイしてから開いてください)\n';
    let out = '';
    for (const { f } of files) {
      out += `\n===== ${f} =====\n`;
      try { out += fs.readFileSync(path.join(dir, f), 'utf8'); } catch (_) { out += '(read error)\n'; }
    }
    return out;
  }

  function clear() {
    let n = 0;
    try {
      for (const f of fs.readdirSync(dir)) {
        if (f.endsWith('.jsonl')) { try { fs.unlinkSync(path.join(dir, f)); n++; } catch (_) {} }
      }
    } catch (_) {}
    return n;
  }

  return { sink, dump, clear, dir };
}

module.exports = { createWsCapture, parseSocketIo, makeFrameParser };
