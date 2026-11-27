/* UltraOSERO engine — rules + AI.
   One file shared by the page (window.UltraEngine), the CPU Web Worker and server.js (require). */
(function (root) {
  'use strict';
  const HOLE = -2, WALL = -1, EMPTY = 0, MAXP = 6;
  // opposite directions sit next to each other: 0/1, 2/3, 4/5, 6/7
  const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, -1], [1, -1], [-1, 1]];
  const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  function rng(seed) {
    let s = (seed >>> 0) || 0x9e3779b9;
    return () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
  }

  const SHAPES = {
    square: () => true,
    cross: (w, h, x, y) => { const c = Math.max(1, Math.floor(Math.min(w, h) / 4)); return !((x < c || x >= w - c) && (y < c || y >= h - c)); },
    octagon: (w, h, x, y) => { const c = Math.max(1, Math.floor(Math.min(w, h) / 4)); return Math.min(x, w - 1 - x) + Math.min(y, h - 1 - y) >= c; },
    diamond: (w, h, x, y) => Math.abs(x - (w - 1) / 2) / (w / 2) + Math.abs(y - (h - 1) / 2) / (h / 2) <= 1.05,
    islands: (w, h, x, y) => { const a = Math.floor(w / 4), b = Math.floor(h / 4); return !((x === a || x === w - 1 - a) && (y === b || y === h - 1 - b)); }
  };

  function sanitize(c) {
    c = c || {};
    const players = clamp(c.players | 0 || 2, 2, MAXP);
    const w = Math.max(clamp(c.w | 0 || 8, 4, 20), players + 4);
    const h = Math.max(clamp(c.h | 0 || w, 4, 20), players + 4);
    return {
      w, h, players,
      shape: SHAPES[c.shape] ? c.shape : 'square',
      walls: clamp(c.walls | 0, 0, 16),
      chain: !!c.chain,
      seed: (c.seed >>> 0) || ((Math.random() * 4294967296) >>> 0)
    };
  }

  function createGame(cfg) {
    const c = sanitize(cfg), w = c.w, h = c.h, n = c.players, shape = SHAPES[c.shape];
    const cells = new Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) cells[y * w + x] = shape(w, h, x, y) ? EMPTY : HOLE;
    const ox = (w - n) >> 1, oy = (h - n) >> 1;
    const inBlock = (x, y, m) => x >= ox - m && x < ox + n + m && y >= oy - m && y < oy + n + m;
    // the start block and the ring around it are always playable
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (inBlock(x, y, 1) && cells[y * w + x] === HOLE) cells[y * w + x] = EMPTY;
    // Latin-square start: every player gets n stones, every row/column holds each colour once
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) cells[(oy + y) * w + ox + x] = 1 + (x + y) % n;
    // point-symmetric wall blocks
    const r = rng(c.seed); let placed = 0, tries = 0;
    while (placed < c.walls && tries++ < 4000) {
      const x = Math.floor(r() * w), y = Math.floor(r() * h);
      if (inBlock(x, y, 1)) continue;
      const i = y * w + x, j = (h - 1 - y) * w + (w - 1 - x);
      if (cells[i] !== EMPTY || cells[j] !== EMPTY) continue;
      cells[i] = WALL; cells[j] = WALL; placed += i === j ? 1 : 2;
    }
    const s = build({ w, h, n, cells, turn: 1, chain: c.chain, moveNo: 0, over: false, cfg: c });
    if (!hasMove(s, 1)) advance(s, null);
    return s;
  }

  function build(d) {
    const w = d.w, h = d.h, n = d.n, N = w * h, cells = Int8Array.from(d.cells);
    const s = { w, h, n, cells, turn: d.turn || 1, chain: !!d.chain, moveNo: d.moveNo | 0, over: !!d.over, cfg: d.cfg || null };
    const rays = new Array(N);
    for (let i = 0; i < N; i++) {
      if (cells[i] === HOLE) { rays[i] = null; continue; }
      const x = i % w, y = (i / w) | 0, rs = [];
      for (const [dx, dy] of DIRS) {
        const r = []; let cx = x + dx, cy = y + dy;
        while (cx >= 0 && cy >= 0 && cx < w && cy < h && cells[cy * w + cx] !== HOLE) { r.push(cy * w + cx); cx += dx; cy += dy; }
        rs.push(Int16Array.from(r));
      }
      rays[i] = rs;
    }
    s.rays = rays;
    // positional weights from geometry: a cell whose 4 lines are all blocked can never be flipped (a corner)
    const blk = new Int8Array(N), wt = new Float32Array(N), isCorner = new Uint8Array(N), xOf = new Int16Array(N).fill(-1);
    let playable = 0;
    for (let i = 0; i < N; i++) {
      if (cells[i] === HOLE || cells[i] === WALL) continue;
      playable++;
      let b = 0;
      for (let a = 0; a < 4; a++) {
        const r1 = rays[i][2 * a], r2 = rays[i][2 * a + 1];
        if (!r1.length || !r2.length || cells[r1[0]] === WALL || cells[r2[0]] === WALL) b++;
      }
      blk[i] = b; wt[i] = [0, 1, 2, 5, 36][b];
      if (b === 4) isCorner[i] = 1;
    }
    for (let c = 0; c < N; c++) {
      if (!isCorner[c]) continue;
      for (let k = 0; k < 8; k++) {
        const r = rays[c][k]; if (!r.length) continue;
        const j = r[0]; if (cells[j] === WALL || isCorner[j]) continue;
        if (k >= 4 && blk[j] < 3) { wt[j] = Math.min(wt[j], -16); xOf[j] = c; }
        else if (k < 4) { wt[j] = Math.min(wt[j], blk[j] >= 3 ? -5 : -3); xOf[j] = c; }
      }
    }
    Object.assign(s, { wt, isCorner, xOf, playable });
    // zobrist hashing (two 32-bit halves)
    const R = rng(0x5eed), Z = N * (n + 1);
    s.z1 = new Int32Array(Z); s.z2 = new Int32Array(Z); s.t1 = new Int32Array(n + 1); s.t2 = new Int32Array(n + 1);
    for (let k = 0; k < Z; k++) { s.z1[k] = (R() * 4294967296) | 0; s.z2[k] = (R() * 4294967296) | 0; }
    for (let k = 0; k <= n; k++) { s.t1[k] = (R() * 4294967296) | 0; s.t2[k] = (R() * 4294967296) | 0; }
    s.h1 = 0; s.h2 = 0;
    for (let i = 0; i < N; i++) if (cells[i] > 0) tg(s, i, cells[i]);
    s.h1 ^= s.t1[s.turn]; s.h2 ^= s.t2[s.turn];
    return s;
  }

  function tg(s, i, v) { const k = i * (s.n + 1) + v; s.h1 ^= s.z1[k]; s.h2 ^= s.z2[k]; }
  function xt(s, p) { s.h1 ^= s.t1[p]; s.h2 ^= s.t2[p]; }

  function lineCount(c, r, p) {
    const L = r.length;
    for (let k = 0; k < L; k++) { const v = c[r[k]]; if (v <= 0) return 0; if (v === p) return k; }
    return 0;
  }

  function isLegal(s, i, p) {
    if (i < 0 || i >= s.cells.length || s.cells[i] !== EMPTY) return false;
    const rs = s.rays[i], c = s.cells;
    for (let d = 0; d < 8; d++) if (lineCount(c, rs[d], p) > 0) return true;
    return false;
  }
  function moves(s, p) {
    const out = [], c = s.cells;
    for (let i = 0; i < c.length; i++) if (c[i] === EMPTY && isLegal(s, i, p)) out.push(i);
    return out;
  }
  function countMoves(s, p) {
    let k = 0; const c = s.cells;
    for (let i = 0; i < c.length; i++) if (c[i] === EMPTY && isLegal(s, i, p)) k++;
    return k;
  }
  function hasMove(s, p) {
    const c = s.cells;
    for (let i = 0; i < c.length; i++) if (c[i] === EMPTY && isLegal(s, i, p)) return true;
    return false;
  }

  // Plays the current player's stone at i (assumed legal). Returns a record for animation and undo.
  // flips is a flat array of quads: [cell, previousOwner, generation, sourceCell, ...]
  function play(s, i) {
    const p = s.turn, c = s.cells;
    const rec = { i, p, prevTurn: p, prevOver: s.over, flips: [], dirs: 0, gens: 0, passed: [] };
    c[i] = p; tg(s, i, p);
    let frontier = [i], gen = 0;
    while (frontier.length) {
      const next = [];
      for (const src of frontier) {
        const rs = s.rays[src];
        for (let d = 0; d < 8; d++) {
          const k = lineCount(c, rs[d], p);
          if (k <= 0) continue;
          if (src === i) rec.dirs++;
          for (let m = 0; m < k; m++) {
            const j = rs[d][m], from = c[j];
            tg(s, j, from); c[j] = p; tg(s, j, p);
            rec.flips.push(j, from, gen, src); next.push(j);
          }
        }
      }
      if (next.length) rec.gens = gen + 1;
      gen++;
      if (!s.chain) break;
      frontier = next;
    }
    s.moveNo++;
    advance(s, rec);
    return rec;
  }

  function advance(s, rec) {
    const n = s.n, start = s.turn;
    xt(s, start);
    for (let k = 1; k <= n; k++) {
      const q = ((start - 1 + k) % n) + 1;
      if (hasMove(s, q)) { s.turn = q; xt(s, q); return; }
      if (rec && q !== start) rec.passed.push(q);
    }
    s.over = true; s.turn = start; xt(s, start);
    if (rec) rec.passed.length = 0;
  }

  function undo(s, rec) {
    const c = s.cells, f = rec.flips;
    for (let k = f.length - 4; k >= 0; k -= 4) { const j = f[k]; tg(s, j, c[j]); c[j] = f[k + 1]; tg(s, j, c[j]); }
    tg(s, rec.i, rec.p); c[rec.i] = EMPTY;
    xt(s, s.turn); s.turn = rec.prevTurn; xt(s, s.turn);
    s.over = rec.prevOver; s.moveNo--;
  }

  function counts(s) {
    const a = new Array(s.n + 1).fill(0);
    for (const v of s.cells) if (v >= 0) a[v]++;
    return a; // a[0] = empty cells, a[p] = stones of player p
  }
  function ranking(s) {
    const a = counts(s), list = [];
    for (let p = 1; p <= s.n; p++) list.push({ p, stones: a[p] });
    return list.sort((x, y) => y.stones - x.stones);
  }
  function toData(s) {
    return { w: s.w, h: s.h, n: s.n, cells: Array.from(s.cells), turn: s.turn, chain: s.chain, moveNo: s.moveNo, over: s.over, cfg: s.cfg };
  }
  function fromData(d) { return build(d); }

  /* ---------------- AI ----------------
     Paranoid minimax: the CPU maximises its own score, every other player minimises it.
     With 2 players this is plain alpha-beta. Iterative deepening + transposition table + exact endgame. */
  const TIMEOUT = { timeout: true };
  const S = { deadline: 0, nodes: 0, tt: null };
  const P = new Float64Array(MAXP + 1), D = new Float64Array(MAXP + 1);

  function evaluate(s, r) {
    const c = s.cells, n = s.n;
    if (s.over) {
      const a = counts(s); let best = 0;
      for (let q = 1; q <= n; q++) if (q !== r && a[q] > best) best = a[q];
      const diff = a[r] - best;
      return (diff > 0 ? 1e6 : diff < 0 ? -1e6 : 0) + diff * 100;
    }
    P.fill(0); D.fill(0);
    let empties = 0;
    for (let i = 0; i < c.length; i++) {
      const v = c[i];
      if (v > 0) {
        let w = s.wt[i];
        if (w < 0 && s.xOf[i] >= 0 && c[s.xOf[i]] > 0) w = 2; // corner already taken: no danger
        P[v] += w; D[v]++;
      } else if (v === EMPTY) empties++;
    }
    if (D[r] === 0) return -5e5;
    let oppP = 0, oppD = 0;
    for (let q = 1; q <= n; q++) if (q !== r) { oppP += P[q]; oppD += D[q]; }
    const no = n - 1;
    const phase = 1 - empties / s.playable;
    let score = P[r] - oppP / no;
    score += (D[r] - oppD / no) * phase * phase * 2.5;
    const my = countMoves(s, r);
    if (n === 2) { const op = countMoves(s, 3 - r); score += (my - op) * 3.2; if (op === 0) score += 6; }
    else score += my * 2;
    return score;
  }

  function hkey(s) { return (s.h1 >>> 0) + (s.h2 & 0xfffff) * 4294967296; }

  function ab(s, r, depth, alpha, beta) {
    if ((++S.nodes & 1023) === 0 && now() > S.deadline) throw TIMEOUT;
    if (s.over || depth === 0) return evaluate(s, r);
    const key = hkey(s), e = S.tt.get(key);
    let ttMove = -1;
    if (e) {
      if (e.d >= depth) {
        if (e.f === 0) return e.v;
        if (e.f === 1 && e.v > alpha) alpha = e.v;
        else if (e.f === 2 && e.v < beta) beta = e.v;
        if (alpha >= beta) return e.v;
      }
      ttMove = e.m;
    }
    const p = s.turn, max = p === r, wt = s.wt;
    const ms = moves(s, p);
    ms.sort((a, b) => (b === ttMove) - (a === ttMove) || wt[b] - wt[a]);
    const a0 = alpha, b0 = beta;
    let best = max ? -Infinity : Infinity, bm = -1;
    for (let k = 0; k < ms.length; k++) {
      const rec = play(s, ms[k]);
      const v = ab(s, r, depth - 1, alpha, beta);
      undo(s, rec);
      if (max) { if (v > best) { best = v; bm = ms[k]; } if (best > alpha) alpha = best; }
      else { if (v < best) { best = v; bm = ms[k]; } if (best < beta) beta = best; }
      if (alpha >= beta) break;
    }
    const f = best <= a0 ? 2 : best >= b0 ? 1 : 0;
    if (S.tt.size > 1500000) S.tt.clear();
    S.tt.set(key, { d: depth, v: best, f, m: bm });
    return best;
  }

  function searchRoot(s, r, depth, deadline, order) {
    S.deadline = deadline; S.nodes = 0;
    const ms = order ? order.slice() : moves(s, r).sort((a, b) => s.wt[b] - s.wt[a]);
    let alpha = -Infinity, best = ms[0], bv = -Infinity; const scored = [];
    try {
      for (const m of ms) {
        const rec = play(s, m);
        const v = ab(s, r, depth - 1, alpha, Infinity);
        undo(s, rec);
        scored.push([m, v]);
        if (v > bv) { bv = v; best = m; }
        if (v > alpha) alpha = v;
      }
    } catch (err) { if (err === TIMEOUT) return null; throw err; }
    scored.sort((a, b) => b[1] - a[1]);
    return { move: best, score: bv, order: scored.map((x) => x[0]) };
  }

  const LV = { 2: { depth: 2, time: 500, end: 0 }, 3: { depth: 6, time: 900, end: 10 }, 4: { depth: 64, time: 2600, end: 14 } };

  // level: 1 よわい, 2 ふつう, 3 つよい, 4 超つよい. Returns a cell index or -1.
  function think(s, level, opt) {
    opt = opt || {};
    if (s.over) return -1;
    const r = s.turn, ms = moves(s, r);
    if (!ms.length) return -1;
    if (ms.length === 1) return ms[0];
    if (level <= 1) {
      let best = ms[0], bv = -1e9;
      for (const m of ms) {
        const rec = play(s, m), f = rec.flips.length / 4; undo(s, rec);
        const v = f + s.wt[m] * 0.15 + Math.random() * 3;
        if (v > bv) { bv = v; best = m; }
      }
      return best;
    }
    const L = LV[level] || LV[4], time = opt.time || L.time, t0 = now();
    S.tt = new Map();
    let empties = 0; for (const v of s.cells) if (v === EMPTY) empties++;
    const base = toData(s);
    const endLimit = s.n === 2 ? L.end - (s.chain ? 3 : 0) : L.end - 6;
    if (L.end && empties <= endLimit) {
      const res = searchRoot(fromData(base), r, empties, t0 + time * 2);
      if (res) return res.move;
      S.tt = new Map();
    }
    const t = fromData(base), deadline = now() + time;
    let best = ms[0], order = null;
    for (let d = 1; d <= L.depth && d <= empties; d++) {
      const res = searchRoot(t, r, d, deadline, order);
      if (!res) break;
      best = res.move; order = res.order;
      if (Math.abs(res.score) >= 9e5) break;
      if (now() - t0 > time * 0.55) break; // next depth would not finish
    }
    return best;
  }

  const API = { HOLE, WALL, EMPTY, MAXP, SHAPES: Object.keys(SHAPES), sanitize, createGame, fromData, toData, isLegal, moves, hasMove, play, undo, counts, ranking, think };

  // running as the CPU Web Worker
  if (typeof importScripts === 'function' && typeof window === 'undefined' && typeof self !== 'undefined') {
    self.onmessage = (e) => {
      const q = e.data || {}; let move = -1;
      try { move = think(fromData(q.data), q.level, { time: q.time }); } catch (err) { move = -1; }
      self.postMessage({ id: q.id, move });
    };
  }
  if (typeof module === 'object' && module.exports) module.exports = API;
  else root.UltraEngine = API;
})(typeof self !== 'undefined' ? self : globalThis);
