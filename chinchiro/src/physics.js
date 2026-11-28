// ---- Dice physics (self-made, deterministic) ----
const PHYS = (() => {
  const R = 5, T = 0.28, CY = 5, RIM = 2.7, FOOT = 2.3, TAB = -0.5, H = 0.42, SR = 0.08;
  const G = -250, DT = 1 / 480, BOUND = 13;
  const RIMR = Math.sqrt(R * R - (CY - RIM) ** 2);
  const INVM = 1, INVI = 6;
  const HAND = [0, 4.6, 4.2];
  const S = [];
  for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) S.push([x * H, y * H, z * H]);
  for (let a = 0; a < 3; a++) for (const s1 of [-1, 1]) for (const s2 of [-1, 1]) {
    const v = [0, 0, 0]; v[(a + 1) % 3] = s1 * H; v[(a + 2) % 3] = s2 * H; S.push(v);
  }
  const AX = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]];

  function rng(seed) {
    let a = seed >>> 0;
    return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  function qrot(q, v, o) {
    const w = q[0], x = q[1], y = q[2], z = q[3], vx = v[0], vy = v[1], vz = v[2];
    const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
    o[0] = vx + w * tx + (y * tz - z * ty); o[1] = vy + w * ty + (z * tx - x * tz); o[2] = vz + w * tz + (x * ty - y * tx); return o;
  }
  function qmul(a, b) {
    return [a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
      a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
      a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
      a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0]];
  }
  function qnorm(q) { const l = Math.hypot(q[0], q[1], q[2], q[3]); q[0] /= l; q[1] /= l; q[2] /= l; q[3] /= l; return q; }
  function randQuat(r) {
    const u1 = r(), u2 = r() * 6.2832, u3 = r() * 6.2832, a = Math.sqrt(1 - u1), b = Math.sqrt(u1);
    return [a * Math.sin(u2), a * Math.cos(u2), b * Math.sin(u3), b * Math.cos(u3)];
  }
  function sdBowl(x, y, z) {
    const dc = Math.hypot(x, y - CY, z);
    const shell = Math.max(dc - (R + T), y - RIM);
    const foot = Math.max(Math.hypot(x, z) - FOOT, TAB - y, y - 0.1);
    return Math.max(Math.min(shell, foot), R - dc);
  }
  const gtmp = [0, 0, 0];
  function gradBowl(x, y, z) {
    const e = 1e-3;
    gtmp[0] = sdBowl(x + e, y, z) - sdBowl(x - e, y, z);
    gtmp[1] = sdBowl(x, y + e, z) - sdBowl(x, y - e, z);
    gtmp[2] = sdBowl(x, y, z + e) - sdBowl(x, y, z - e);
    const l = Math.hypot(gtmp[0], gtmp[1], gtmp[2]) || 1; gtmp[0] /= l; gtmp[1] /= l; gtmp[2] /= l; return gtmp;
  }

  // a throw setup: dice held in hand
  function setup(seed) {
    const r = rng(seed);
    const off = [[-0.62, 0, 0.1], [0.62, 0.05, 0.05], [0, 0.55, -0.35]];
    const bodies = off.map(o => ({
      p: [HAND[0] + o[0] + (r() - .5) * .1, HAND[1] + o[1] + (r() - .5) * .1, HAND[2] + o[2] + (r() - .5) * .1],
      q: randQuat(r)
    }));
    return { seed, bodies, r };
  }

  function simulate(su, power, aim, opt = {}) {
    const r = rng(su.seed * 7 + 13);
    power = Math.max(0, Math.min(1, power));
    const B = su.bodies.map(b => ({ p: b.p.slice(), q: b.q.slice(), v: [0, 0, 0], w: [0, 0, 0], calm: 0 }));
    const tgt = [aim * 1.6, 0.6, -0.8 - power * 1.6];
    const speed = 7 + power * 24 + power ** 3 * 19;
    for (const b of B) {
      let d = [tgt[0] - b.p[0], tgt[1] - b.p[1], tgt[2] - b.p[2]]; const l = Math.hypot(...d); d = d.map(x => x / l);
      const s = speed * (0.9 + r() * 0.2);
      b.v = [d[0] * s + (r() - .5) * 2.5, d[1] * s + 3 + power * 6, d[2] * s + (r() - .5) * 2];
      const wm = 8 + power * 30 + r() * 10;
      let w = [r() - .5, r() - .5, r() - .5]; const wl = Math.hypot(...w); b.w = w.map(x => x / wl * wm);
    }
    const FPS = 120, every = 4, maxSteps = Math.round((opt.maxT || 7) / DT);
    const frames = []; const events = []; const lastEv = {};
    const settle = [0, 0, 0];
    const wp = [0, 0, 0], tmp = [0, 0, 0];
    let step = 0, calmAll = 0;
    const rec = () => { for (const b of B) frames.push(b.p[0], b.p[1], b.p[2], b.q[0], b.q[1], b.q[2], b.q[3]); };
    rec();
    for (; step < maxSteps; step++) {
      for (const b of B) b.v[1] += G * DT;
      const C = [];
      // static contacts
      for (let i = 0; i < 3; i++) {
        const b = B[i];
        for (const s of S) {
          qrot(b.q, s, wp); const x = b.p[0] + wp[0], y = b.p[1] + wp[1], z = b.p[2] + wp[2];
          const sd = sdBowl(x, y, z);
          if (sd < SR) {
            const n = gradBowl(x, y, z);
            C.push({ a: i, b: -1, ra: [wp[0] - n[0] * SR, wp[1] - n[1] * SR, wp[2] - n[2] * SR], n: [n[0], n[1], n[2]], d: SR - sd, e: 0.42, mu: 0.3, k: 'bowl' });
          }
          if (y - TAB < SR) C.push({ a: i, b: -1, ra: [wp[0], wp[1] - SR, wp[2]], n: [0, 1, 0], d: SR - (y - TAB), e: 0.3, mu: 0.55, k: 'table' });
          const hd = Math.hypot(x, z);
          if (hd > BOUND - SR) C.push({ a: i, b: -1, ra: [wp[0], wp[1], wp[2]], n: [-x / hd, 0, -z / hd], d: hd - BOUND + SR, e: 0.2, mu: 0.4, k: 'table' });
        }
      }
      // die-die
      for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
        if (i === j) continue;
        const A = B[i], Bb = B[j];
        const dx = A.p[0] - Bb.p[0], dy = A.p[1] - Bb.p[1], dz = A.p[2] - Bb.p[2];
        if (dx * dx + dy * dy + dz * dz > 3.2) continue;
        const qi = [Bb.q[0], -Bb.q[1], -Bb.q[2], -Bb.q[3]];
        for (const s of S) {
          qrot(A.q, s, wp);
          tmp[0] = A.p[0] + wp[0] - Bb.p[0]; tmp[1] = A.p[1] + wp[1] - Bb.p[1]; tmp[2] = A.p[2] + wp[2] - Bb.p[2];
          const lp = qrot(qi, tmp, [0, 0, 0]);
          const c = [Math.max(-H, Math.min(H, lp[0])), Math.max(-H, Math.min(H, lp[1])), Math.max(-H, Math.min(H, lp[2]))];
          let d = [lp[0] - c[0], lp[1] - c[1], lp[2] - c[2]]; let dist = Math.hypot(d[0], d[1], d[2]);
          if (dist < 1e-6) { // deep: push along axis of least penetration
            let best = 0, bv = 1e9; for (let a = 0; a < 3; a++) { const pen = H - Math.abs(lp[a]); if (pen < bv) { bv = pen; best = a; } }
            d = [0, 0, 0]; d[best] = Math.sign(lp[best]) || 1; dist = -bv;
          } else { d = d.map(x => x / dist); }
          if (dist < 2 * SR) {
            const n = qrot(Bb.q, d, [0, 0, 0]);
            C.push({ a: i, b: j, ra: [wp[0] - n[0] * SR, wp[1] - n[1] * SR, wp[2] - n[2] * SR], n, d: (2 * SR - dist) * 0.5, e: 0.5, mu: 0.25, k: 'die' });
          }
        }
      }
      // prepare
      const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
      const velAt = (b, rr) => { const c = cross(b.w, rr); return [b.v[0] + c[0], b.v[1] + c[1], b.v[2] + c[2]]; };
      const kOf = (c, dir) => {
        const x = cross(c.ra, dir); let k = INVM + INVI * (x[0] * x[0] + x[1] * x[1] + x[2] * x[2]);
        if (c.b >= 0) { const y = cross(c.rb, dir); k += INVM + INVI * (y[0] * y[0] + y[1] * y[1] + y[2] * y[2]); }
        return k;
      };
      const rel = c => { const va = velAt(B[c.a], c.ra); if (c.b < 0) return va; const vb = velAt(B[c.b], c.rb); return [va[0] - vb[0], va[1] - vb[1], va[2] - vb[2]]; };
      const apply = (c, J) => {
        const A = B[c.a]; A.v[0] += J[0] * INVM; A.v[1] += J[1] * INVM; A.v[2] += J[2] * INVM;
        const ta = cross(c.ra, J); A.w[0] += ta[0] * INVI; A.w[1] += ta[1] * INVI; A.w[2] += ta[2] * INVI;
        if (c.b >= 0) {
          const Bb = B[c.b]; Bb.v[0] -= J[0] * INVM; Bb.v[1] -= J[1] * INVM; Bb.v[2] -= J[2] * INVM;
          const tb = cross(c.rb, J); Bb.w[0] -= tb[0] * INVI; Bb.w[1] -= tb[1] * INVI; Bb.w[2] -= tb[2] * INVI;
        }
      };
      const inContact = [0, 0, 0];
      for (const c of C) {
        if (c.b >= 0) { const A = B[c.a], Bb = B[c.b]; c.rb = [A.p[0] + c.ra[0] - Bb.p[0], A.p[1] + c.ra[1] - Bb.p[1], A.p[2] + c.ra[2] - Bb.p[2]]; }
        c.kn = kOf(c, c.n);
        const v = rel(c); const vn = v[0] * c.n[0] + v[1] * c.n[1] + v[2] * c.n[2];
        c.bias = vn < -1.2 ? -c.e * vn : 0; c.jn = 0; c.jt = [0, 0, 0];
        inContact[c.a] = 1; if (c.b >= 0) inContact[c.b] = 1;
        if (vn < -2.5) {
          const key = c.k + c.a + '_' + c.b; const t = step * DT;
          if (!lastEv[key] || t - lastEv[key] > 0.035) { lastEv[key] = t; events.push({ t, k: c.k, v: -vn, a: c.a, x: B[c.a].p[0] + c.ra[0], z: B[c.a].p[2] + c.ra[2] }); }
        }
      }
      for (let it = 0; it < 10; it++) {
        for (const c of C) {
          let v = rel(c); const vn = v[0] * c.n[0] + v[1] * c.n[1] + v[2] * c.n[2];
          let dj = (c.bias - vn) / c.kn; const nj = Math.max(c.jn + dj, 0); dj = nj - c.jn; c.jn = nj;
          apply(c, [c.n[0] * dj, c.n[1] * dj, c.n[2] * dj]);
          v = rel(c); const vn2 = v[0] * c.n[0] + v[1] * c.n[1] + v[2] * c.n[2];
          const vt = [v[0] - c.n[0] * vn2, v[1] - c.n[1] * vn2, v[2] - c.n[2] * vn2]; const vtl = Math.hypot(vt[0], vt[1], vt[2]);
          if (vtl > 1e-7) {
            const t = [vt[0] / vtl, vt[1] / vtl, vt[2] / vtl]; const dm = -vtl / kOf(c, t);
            let nt = [c.jt[0] + t[0] * dm, c.jt[1] + t[1] * dm, c.jt[2] + t[2] * dm];
            const ntl = Math.hypot(nt[0], nt[1], nt[2]), mx = c.mu * c.jn;
            if (ntl > mx) nt = nt.map(x => x / ntl * mx);
            apply(c, [nt[0] - c.jt[0], nt[1] - c.jt[1], nt[2] - c.jt[2]]); c.jt = nt;
          }
        }
      }
      // integrate
      for (let i = 0; i < 3; i++) {
        const b = B[i];
        if (step * DT > 3) { b.v = b.v.map(x => x * 0.99); b.w = b.w.map(x => x * 0.99); }
        if (inContact[i]) {
          const f = 1 - 1.2 * DT; b.w[0] *= f; b.w[1] *= f; b.w[2] *= f;
          const sv = Math.hypot(...b.v), sw = Math.hypot(...b.w);
          if (sv < 0.5 && sw < 1.2) { const g = 0.985; b.v = b.v.map(x => x * g); b.w = b.w.map(x => x * g); }
        }
        b.p[0] += b.v[0] * DT; b.p[1] += b.v[1] * DT; b.p[2] += b.v[2] * DT;
        const w = b.w, q = b.q, h = DT * 0.5;
        const dq = qmul([0, w[0], w[1], w[2]], q);
        q[0] += dq[0] * h; q[1] += dq[1] * h; q[2] += dq[2] * h; q[3] += dq[3] * h; qnorm(q);
      }
      for (const c of C) {
        const corr = Math.max(c.d - 0.003, 0) * 0.45;
        if (c.b < 0) { const A = B[c.a]; A.p[0] += c.n[0] * corr; A.p[1] += c.n[1] * corr; A.p[2] += c.n[2] * corr; }
        else { const A = B[c.a], Bb = B[c.b]; for (let k = 0; k < 3; k++) { A.p[k] += c.n[k] * corr * .5; Bb.p[k] -= c.n[k] * corr * .5; } }
      }
      let allCalm = true;
      for (let i = 0; i < 3; i++) {
        const b = B[i];
        if (!b.pp) { b.pp = b.p.slice(); b.pq = b.q.slice(); }
        let moving = true;
        if ((step + 1) % 12 === 0) {
          const dp = Math.hypot(b.p[0] - b.pp[0], b.p[1] - b.pp[1], b.p[2] - b.pp[2]) / (12 * DT);
          const dq = Math.abs(Math.abs(b.p && (b.q[0] * b.pq[0] + b.q[1] * b.pq[1] + b.q[2] * b.pq[2] + b.q[3] * b.pq[3])) - 1) ;
          b.mv = dp > 0.1 || dq > 2e-6; b.pp = b.p.slice(); b.pq = b.q.slice();
        }
        moving = b.mv !== false;
        if (moving) { settle[i] = (step + 1) * DT; allCalm = false; }
      }
      calmAll = allCalm ? calmAll + 1 : 0;
      if ((step + 1) % every === 0) rec();
      if (calmAll > 0.3 / DT) break;
    }
    rec();
    const res = B.map(b => {
      let best = 0, by = -2;
      for (let k = 0; k < 6; k++) { const y = qrot(b.q, AX[k], [0, 0, 0])[1]; if (y > by) { by = y; best = k; } }
      const hd = Math.hypot(b.p[0], b.p[2]);
      return { axis: best, tilt: by, out: hd > RIMR - 0.05 || b.p[1] < -0.1, p: b.p.slice() };
    });
    return { frames: new Float32Array(frames), n: frames.length / 21, fps: FPS, events, settle, res, dur: frames.length / 21 / FPS };
  }

  // 24 cube rotations
  const ROTS = (() => {
    const out = [[1, 0, 0, 0]]; const s = Math.SQRT1_2;
    const gens = [[s, s, 0, 0], [s, 0, s, 0], [s, 0, 0, s]];
    const key = q => { const m = [AX[0], AX[2], AX[4]].map(a => qrot(q, a, [0, 0, 0]).map(x => Math.round(x)).join(',')).join(';'); return m; };
    const seen = new Set([key(out[0])]);
    for (let i = 0; i < out.length; i++) for (const g of gens) { const q = qnorm(qmul(g, out[i])); const k = key(q); if (!seen.has(k)) { seen.add(k); out.push(q); } }
    return out;
  })();
  function axisIndexOf(v) { let best = 0, bd = -2; for (let k = 0; k < 6; k++) { const d = v[0] * AX[k][0] + v[1] * AX[k][1] + v[2] * AX[k][2]; if (d > bd) { bd = d; best = k; } } return best; }
  // find offset rotation O such that O * AX[meshAxis] = AX[bodyAxis]
  function offsetFor(meshAxis, bodyAxis, r) {
    const c = ROTS.filter(q => axisIndexOf(qrot(q, AX[meshAxis], [0, 0, 0])) === bodyAxis);
    return c[Math.floor((r ? r() : Math.random()) * c.length)];
  }
  // value shown: mesh axis = O^-1 * bodyAxis
  function valueOf(O, bodyAxis, faces) {
    const oi = [O[0], -O[1], -O[2], -O[3]];
    return faces[axisIndexOf(qrot(oi, AX[bodyAxis], [0, 0, 0]))];
  }
  return { R, T, CY, RIM, TAB, FOOT, H, SR, RIMR, HAND, AX, ROTS, setup, simulate, offsetFor, valueOf, qrot, qmul, rng };
})();
if (typeof module !== 'undefined') module.exports = PHYS;
