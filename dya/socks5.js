// 依存ゼロの SOCKS5 コネクタ（Tor 用）。
// username/password 認証つきで CONNECT し、トンネル済みの生 TCP ソケットを返す。
// Tor の IsolateSOCKSAuth と組み合わせると、username/password が違えば別サーキット
// （＝別の出口IP）になる。これで「利用者ごとに別IP」を実現する。
//
// 使い方:
//   socksConnect({host:'127.0.0.1',port:9050, dstHost:'db.dya.jp', dstPort:443,
//                 user:'<sessionKey>', pass:'<sessionKey>'}, (err, socket) => {...});
//   成功時 socket は dstHost:dstPort へつながった生 TCP。TLS は呼び出し側で被せる。

'use strict';

const net = require('net');

function socksConnect(opts, cb) {
  const {
    host, port, dstHost, dstPort,
    user = '', pass = '', timeout = 20000,
  } = opts;
  let done = false;
  const finish = (err, sock) => {
    if (done) return;
    done = true;
    if (err) { try { if (sock) sock.destroy(); } catch (_) {} cb(err); }
    else cb(null, sock);
  };

  const sock = net.connect(port, host);
  sock.setTimeout(timeout, () => finish(new Error('socks timeout')));
  sock.once('error', (e) => finish(e));

  let stage = 'greeting';
  let buf = Buffer.alloc(0);

  sock.once('connect', () => {
    // 認証方式を提示: 0x00(no-auth) と 0x02(user/pass)。Tor は user/pass を
    // 検証しないが、IsolateSOCKSAuth のため必ず 0x02 を選ばせたいので user/pass も載せる。
    sock.write(Buffer.from([0x05, 0x02, 0x00, 0x02]));
  });

  sock.on('data', (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    try {
      if (stage === 'greeting') {
        if (buf.length < 2) return;
        if (buf[0] !== 0x05) return finish(new Error('socks: bad version ' + buf[0]));
        const method = buf[1];
        buf = buf.slice(2);
        if (method === 0x02) {
          // user/pass 認証（RFC1929）
          const u = Buffer.from(String(user), 'utf8').slice(0, 255);
          const p = Buffer.from(String(pass), 'utf8').slice(0, 255);
          const authReq = Buffer.concat([
            Buffer.from([0x01, u.length]), u,
            Buffer.from([p.length]), p,
          ]);
          stage = 'auth';
          sock.write(authReq);
        } else if (method === 0x00) {
          stage = 'request';
          sendConnect();
        } else {
          return finish(new Error('socks: no acceptable auth method (' + method + ')'));
        }
      } else if (stage === 'auth') {
        if (buf.length < 2) return;
        const ok = buf[1] === 0x00;
        buf = buf.slice(2);
        if (!ok) return finish(new Error('socks: auth failed'));
        stage = 'request';
        sendConnect();
      } else if (stage === 'request') {
        // 応答: VER REP RSV ATYP BND.ADDR BND.PORT
        if (buf.length < 4) return;
        if (buf[1] !== 0x00) return finish(new Error('socks: connect failed (rep=' + buf[1] + ')'));
        const atyp = buf[3];
        let need = 4;
        if (atyp === 0x01) need += 4 + 2;         // IPv4
        else if (atyp === 0x04) need += 16 + 2;   // IPv6
        else if (atyp === 0x03) {
          if (buf.length < 5) return;
          need += 1 + buf[4] + 2;                 // domain
        } else return finish(new Error('socks: bad atyp ' + atyp));
        if (buf.length < need) return;
        // トンネル確立。以降のバイトは宛先からのデータ。呼び出し側へ綺麗に渡す。
        const leftover = buf.slice(need);
        stage = 'tunnel';
        sock.setTimeout(0);
        sock.removeAllListeners('data');
        sock.removeAllListeners('error');
        sock.removeAllListeners('timeout');
        finish(null, sock);
        if (leftover.length) sock.unshift(leftover);
      }
    } catch (e) { finish(e); }
  });

  function sendConnect() {
    const h = Buffer.from(String(dstHost), 'utf8');
    if (h.length > 255) return finish(new Error('socks: dst host too long'));
    const req = Buffer.concat([
      Buffer.from([0x05, 0x01, 0x00, 0x03, h.length]), h,
      Buffer.from([(dstPort >> 8) & 0xff, dstPort & 0xff]),
    ]);
    sock.write(req);
  }
}

// socks5h://host:port → {host, port}
function parseSocksUrl(url) {
  const m = /^socks5h?:\/\/([^:/]+):(\d+)/i.exec(String(url || '').trim());
  if (!m) return null;
  return { host: m[1], port: parseInt(m[2], 10) };
}

module.exports = { socksConnect, parseSocksUrl };
