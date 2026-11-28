{
// ================= basics =================
const $ = s => document.querySelector(s);
const fmt = n => Math.round(n).toLocaleString('ja-JP');
const rnd = (a, b) => a + Math.random() * (b - a);
const pick = a => a[Math.floor(Math.random() * a.length)];
const shuffle = a => { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
const LS = 'chinchiro.v1';
const DEF = { hifumi2: true, pin5: true, shonLose: true, fast: false, fx: 'high', sound: true, chips: 10000, laps: 2 };
let CFG = { ...DEF }, ST = { games: {}, wins: {}, roles: {}, ach: {}, best: 0, throws: 0 };
try { const d = JSON.parse(localStorage.getItem(LS) || '{}'); if (d.cfg) CFG = { ...DEF, ...d.cfg }; if (d.st) ST = { ...ST, ...d.st }; } catch (e) { }
const save = () => { try { localStorage.setItem(LS, JSON.stringify({ cfg: CFG, st: ST })); } catch (e) { } };
SND.on = CFG.sound;

let TOK = 0;
const guard = p => { const t = TOK; return p.then(v => t === TOK ? v : new Promise(() => { })); };
let cpuTurn = false;
const sleep = ms => guard(new Promise(r => setTimeout(r, ms * (CFG.fast && cpuTurn ? .45 : 1) / (window.__spd || 1))));

const ACH = {
  first: ['初めての一投', 'サイを初めて振った'], pin: ['ピンゾロ', '1・1・1を出した'], zoro: ['アラシを呼ぶ者', 'ゾロ目を出した'],
  s456: ['シゴロ使い', '4・5・6を出した'], h123: ['ヒフミの悲劇', '1・2・3を出してしまった'], shon: ['茶碗の外へ', 'ションベンをした'],
  tut: ['卒業', 'チュートリアルを終えた'], quiz: ['役の達人', '役当てクイズで満点'], hwin: ['賭場の主', '本格モードで1位'],
  rich: ['大旦那', '本格モードで5万チップ到達'], dh: ['デッドヒート制覇', '宿敵に勝った'], comeback: ['大逆転', '2000以上の差から逆転勝ち'],
  sudden: ['サドンデスの鬼', 'サドンデスを制した'], eye: ['見破り', 'イカサマを見抜いた'], eye3: ['完全看破', 'イカサマを全て見抜き、誤りなし'],
  lab: ['確率の探究者', '確率ラボで物理試行を実行']
};
function unlock(k) { if (ST.ach[k]) return; ST.ach[k] = Date.now(); save(); toast('実績解除「' + ACH[k][0] + '」'); SND.win(); }
function toast(t) { const d = document.createElement('div'); d.className = 'tst'; d.textContent = t; $('#toast').appendChild(d); setTimeout(() => d.remove(), 3300); }

// ================= rules =================
const NAMES = { pin: 'ピンゾロ', '456': 'シゴロ', '123': 'ヒフミ', none: '目なし', shon: 'ションベン' };
function judge(v) {
  const [a, b, c] = v.slice().sort((x, y) => x - y);
  if (a === 1 && b === 1 && c === 1) return { k: 'pin', name: 'ピンゾロ', rank: 100, v };
  if (a === b && b === c) return { k: 'zoro', name: a + 'のゾロ目', rank: 90 + a, v };
  if (a === 4 && b === 5 && c === 6) return { k: '456', name: 'シゴロ', rank: 80, v };
  if (a === 1 && b === 2 && c === 3) return { k: '123', name: 'ヒフミ', rank: -10, v };
  if (a === b) return { k: 'me', name: c + 'の目', rank: c, v };
  if (b === c) return { k: 'me', name: a + 'の目', rank: a, v };
  return { k: 'none', name: '目なし', rank: 0, v };
}
const SHON = v => ({ k: 'shon', name: 'ションベン', rank: -5, v });
const pinM = () => CFG.pin5 ? 5 : 3, hifM = () => CFG.hifumi2 ? 2 : 1;
const instant = pr => pr.k !== 'me';
function childMult(pr, cr) {
  if (pr.k === 'pin') return -pinM(); if (pr.k === 'zoro') return -3; if (pr.k === '456') return -2;
  if (pr.k === '123') return hifM(); if (pr.k === 'none' || pr.k === 'shon') return 1;
  if (cr.k === 'pin') return pinM(); if (cr.k === 'zoro') return 3; if (cr.k === '456') return 2;
  if (cr.k === '123') return -hifM(); if (cr.k === 'none' || cr.k === 'shon') return -1;
  return Math.sign(cr.rank - pr.rank);
}
const CLS = { pin: 'gold', zoro: 'gold', '456': 'shu', me: '', '123': 'blue', none: 'gray', shon: 'blue' };

// ================= scene =================
const SC = { dice: [null, null, null], mode: 'idle', setup: null, pb: null, jig: 0, loaded: false, view: null, swipe: null, rattleT: 0, attract: false };
function setView(v) { SC.view = v || null; GFX.setView(SC.view); }
function placeHand(loaded) {
  SC.pb = null; SC.loaded = !!loaded;
  SC.setup = PHYS.setup((Math.random() * 1e9) | 0);
  SC.dice = SC.setup.bodies.map(b => ({ p: b.p.slice(), q: b.q.slice(), o: [1, 0, 0, 0], loaded: SC.loaded }));
  SC.mode = 'hand'; setView({ hand: true });
}
function clearDice() { SC.dice = [null, null, null]; SC.pb = null; SC.mode = 'idle'; }
function simFor(power, aim, force, wantShon) {
  let sim, tries = 0, pw = power, am = aim;
  for (; ;) {
    sim = PHYS.simulate(SC.setup, pw, am);
    const out = sim.res.some(r => r.out);
    if (wantShon) { if (out || tries > 40) break; pw = 1; am = rnd(-1, 1); }
    else if (force) { if (!out || tries > 40) break; pw = Math.max(.15, pw * .85); am = rnd(-.5, .5); }
    else break;
    tries++;
  }
  return sim;
}
// returns {vals, out, outs, sim}
function throwDice({ power, aim = 0, force = null, wantShon = false, slow = false, rate = 1, mute = false }) {
  return guard(new Promise(res => {
    setView(null);
    const sim = simFor(power, aim, force, wantShon);
    const faces = SC.loaded ? FACES_L : FACES_N;
    const offs = sim.res.map((r, i) => {
      if (force && !r.out && !SC.loaded) { const m = faces.indexOf(force[i]); return PHYS.offsetFor(m, r.axis); }
      return [1, 0, 0, 0];
    });
    const vals = sim.res.map((r, i) => PHYS.valueOf(offs[i], r.axis, faces));
    SC.dice.forEach((d, i) => d.o = offs[i]);
    const pb = { sim, t: 0, ev: 0, rate, mute, offs, res, vals, out: sim.res.some(r => r.out), outs: sim.res.map(r => r.out) };
    if (slow && CFG.fx === 'high') {
      const ord = [0, 1, 2].sort((a, b) => sim.settle[a] - sim.settle[b]);
      const s1 = sim.settle[ord[1]], s2 = sim.settle[ord[2]];
      if (s2 - s1 > .12 && !sim.res[ord[2]].out) { pb.slow = [Math.max(.15, s1 - .05, s2 - .42), s2 + .05]; pb.slowDie = ord[2]; }
      else if (s2 > .5) { pb.slow = [s2 - .35, s2 + .05]; pb.slowDie = ord[2]; }
    }
    SC.mode = 'play'; SC.pb = pb;
  }));
}
function advance(dt) {
  const pb = SC.pb, sim = pb.sim;
  let rate = pb.rate;
  if (pb.slow) {
    if (pb.t >= pb.slow[0] && pb.t < pb.slow[1]) {
      rate *= .26;
      if (!pb.slowOn) { pb.slowOn = true; const p = sim.res[pb.slowDie].p; setView({ focus: [p[0], .4, p[2]], k: .68 }); $('#vig').classList.add('red'); SND.beat(); }
    } else if (pb.slowOn && pb.t >= pb.slow[1]) { pb.slowOn = false; $('#vig').classList.remove('red'); }
  }
  pb.t += dt * rate;
  const ev = sim.events;
  while (pb.ev < ev.length && ev[pb.ev].t <= pb.t) {
    const e = ev[pb.ev++]; if (pb.mute) continue; SND.hit(e.k, e.v * (pb.slowOn ? .7 : 1), e.x);
    if (e.v > 28) { try { navigator.vibrate && navigator.vibrate(6); } catch (x) { } }
  }
  const f = Math.min(sim.n - 1, pb.t * sim.fps), i0 = Math.floor(f), i1 = Math.min(sim.n - 1, i0 + 1), a = f - i0, F = sim.frames;
  for (let d = 0; d < 3; d++) {
    const o0 = i0 * 21 + d * 7, o1 = i1 * 21 + d * 7, D = SC.dice[d];
    for (let k = 0; k < 3; k++) D.p[k] = F[o0 + k] + (F[o1 + k] - F[o0 + k]) * a;
    let q1 = [F[o1 + 3], F[o1 + 4], F[o1 + 5], F[o1 + 6]]; const q0 = [F[o0 + 3], F[o0 + 4], F[o0 + 5], F[o0 + 6]];
    if (q0[0] * q1[0] + q0[1] * q1[1] + q0[2] * q1[2] + q0[3] * q1[3] < 0) q1 = q1.map(x => -x);
    const q = q0.map((x, k) => x + (q1[k] - x) * a); const l = Math.hypot(...q); D.q = q.map(x => x / l);
  }
  if (pb.t >= sim.dur) {
    SC.pb = null; SC.mode = 'rest'; $('#vig').classList.remove('red');
    if (pb.slow) setTimeout(() => { if (SC.mode === 'rest') setView(null); }, 450);
    pb.res({ vals: pb.vals, out: pb.out, outs: pb.outs, sim });
  }
}
let last = performance.now(), attractT = 0;
function loop(now) {
  const dt = Math.min(.05, (now - last) / 1000) * (window.__spd || 1); last = now;
  if (SC.pb) advance(dt);
  else if (SC.mode === 'hand' && SC.setup) {
    const j = SC.jig, t = now / 1000;
    SC.dice.forEach((d, i) => {
      const b = SC.setup.bodies[i];
      d.p = [b.p[0] + Math.sin(t * 31 + i * 2) * .12 * j, b.p[1] + Math.sin(t * 7 + i) * .05 + Math.sin(t * 37 + i) * .1 * j, b.p[2] + Math.cos(t * 29 + i * 3) * .1 * j];
      if (j) { const ax = [Math.sin(t * 9 + i), Math.cos(t * 11 + i), Math.sin(t * 13)]; const an = .5 * j * Math.sin(t * 25 + i); const s = Math.sin(an / 2); d.q = PHYS.qmul([Math.cos(an / 2), ax[0] * s * .6, ax[1] * s * .6, ax[2] * s * .6], b.q); const l = Math.hypot(...d.q); d.q = d.q.map(x => x / l); }
    });
    if (j && now - SC.rattleT > 75) { SC.rattleT = now; SND.rattle(); }
  }
  if (SC.attract && !SC.pb) {
    attractT += dt;
    if (attractT > 3.4) { attractT = 0; const v = SC.view; placeHand(false); throwDice({ power: rnd(.25, .7), aim: rnd(-.6, .6), mute: true }); setView(v); }
  }
  GFX.frame(SC.dice, dt, now / 1000);
  requestAnimationFrame(loop);
}

// ================= UI helpers =================
const G = { mode: null, players: [], parent: 0, turn: -1, round: 0, maxRounds: 0 };
const shortName = n => String(n).replace(/^[^ァ-ヶ]{1,5}?(?:な|の|れ)(?=[ァ-ヶ])/, '');   // 「慎重なタケ」→「タケ」
function renderSeats() {
  $('#seats').innerHTML = G.players.map((p, i) => `<div class="seat ${G.turn === i ? 'turn' : ''} ${p.out ? 'out' : ''}" id="seat${i}">
    <div class="sn">${G.parent === i && G.mode !== 'tut' ? '<i class="oya">親</i>' : ''}${innerWidth < 520 ? shortName(p.name) : p.name}</div>
    <div class="sc">${p.label || (p.bank ? '胴元' : fmt(p.chips))}</div>
    <div class="sb">${p.bet ? '賭 ' + fmt(p.bet) + ' ' : ''}${p.res ? `<span class="rr">${p.res.name}</span>` : ''}</div></div>`).join('');
}
function delta(i, v) {
  const s = $('#seat' + i); if (!s || !v) return;
  const d = document.createElement('div'); d.className = 'delta ' + (v > 0 ? 'p' : 'm'); d.textContent = (v > 0 ? '+' : '−') + fmt(Math.abs(v)); s.appendChild(d); setTimeout(() => d.remove(), 1700);
}
function banner(main, sub = '', cls = '', ms = 1300) {
  const b = $('#banner'); b.className = 'show ' + cls; b.innerHTML = `<div class="bm">${main}</div>${sub ? `<div class="bs">${sub}</div>` : ''}`;
  return sleep(ms).then(() => { b.className = cls; });
}
function ctrl(html) { $('#ctrl').innerHTML = html ? `<div class="panel">${html}</div>` : ''; }
function setTag(t) { $('#modeTag').textContent = t; }
function screen(html, solid) { const s = $('#screen'); s.className = 'on' + (solid ? ' solid' : ''); s.innerHTML = `<div class="wrap">${html}</div>`; s.scrollTop = 0; }
function closeScreen() { $('#screen').className = ''; $('#screen').innerHTML = ''; }
function modal(html) { $('#mbox').innerHTML = html; $('#modal').classList.add('on'); }
function closeModal() { $('#modal').classList.remove('on'); }
function dieSVG(v, s = 26, cheat) {
  const P = { 1: [[0, 0]], 2: [[-1, -1], [1, 1]], 3: [[-1, -1], [0, 0], [1, 1]], 4: [[-1, -1], [1, -1], [-1, 1], [1, 1]], 5: [[-1, -1], [1, -1], [0, 0], [-1, 1], [1, 1]], 6: [[-1, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [1, 1]] }[v];
  return `<svg width="${s}" height="${s}" viewBox="-12 -12 24 24" aria-label="${v}"><rect x="-11" y="-11" width="22" height="22" rx="5" fill="#f4eddd" stroke="#0004"/>${P.map(([x, y]) => `<circle cx="${x * 5.5}" cy="${y * 5.5}" r="${v === 1 ? 4.2 : 2.3}" fill="${v === 1 ? '#c4231b' : '#1d1a18'}"/>`).join('')}</svg>`;
}
const dset = (vals, s) => `<span class="dset">${vals.map(v => dieSVG(v, s)).join('')}</span>`;
function say(text, opts = {}) {
  const g = $('#guide'); g.hidden = false; $('#gtext').innerHTML = text;
  const btns = opts.buttons || (opts.wait === false ? [] : [['next', '次へ']]);
  $('#grow').innerHTML = btns.map(([k, l], i) => `<button class="btn sm ${i === btns.length - 1 ? 'shu' : 'ghost'}" data-k="${k}">${l}</button>`).join('');
  if (!btns.length) return Promise.resolve();
  return guard(new Promise(r => { $('#grow').onclick = e => { const b = e.target.closest('button'); if (b) { SND.click(); r(b.dataset.k); } }; }));
}
const hideGuide = () => { $('#guide').hidden = true; };
function choose(cap, opts) {
  ctrl(`${cap ? `<p class="cap">${cap}</p>` : ''}<div class="choices">${opts.map(([k, l, c, dis]) => `<button class="btn ${c || ''}" data-k="${k}" ${dis ? 'disabled' : ''}>${l}</button>`).join('')}</div>`);
  return guard(new Promise(r => { $('#ctrl').onclick = e => { const b = e.target.closest('button[data-k]'); if (b) { SND.click(); $('#ctrl').onclick = null; r(b.dataset.k); } }; }));
}
function waitBet(min, max, def, cap) {
  let v = Math.max(min, Math.min(max, def));
  ctrl(`<div class="bet"><div class="amt"><span>${cap || '賭け金'}<br><small class="legend">上限 ${fmt(max)}</small></span><b id="betv">${fmt(v)}</b></div>
   <div class="chips">${[100, 500, 1000, 5000].map(x => `<button class="chip" data-a="${x}">+${fmt(x)}</button>`).join('')}
   <button class="chip" data-a="half">半分</button><button class="chip" data-a="max">上限</button><button class="chip" data-a="min">最小</button><button class="chip" data-a="m100">−100</button></div>
   <button class="btn shu" id="betOk" style="width:100%">この額で勝負</button></div>`);
  return guard(new Promise(r => {
    $('#ctrl').onclick = e => {
      const b = e.target.closest('button'); if (!b) return;
      if (b.id === 'betOk') { SND.chip(4); $('#ctrl').onclick = null; r(v); return; }
      const a = b.dataset.a; SND.chip(1);
      v = a === 'half' ? Math.floor(v / 2 / 100) * 100 : a === 'max' ? max : a === 'min' ? min : a === 'm100' ? v - 100 : v + (+a);
      v = Math.max(min, Math.min(max, v)); $('#betv').textContent = fmt(v);
    };
  }));
}
function waitThrow(tryIdx, hint) {
  ctrl(`<div class="throw"><div class="tries">${[0, 1, 2].map(i => `<i class="${i < tryIdx ? 'used' : i === tryIdx ? 'now' : ''}"></i>`).join('')}<span>${tryIdx + 1}投目（最大3投）</span></div>
   <div class="gauge"><b id="gfill"></b><em></em><span class="gl">弱</span><span class="gr">強すぎ</span></div>
   <button class="btn shu" id="throwBtn"><span class="tb1">振る</span><span class="tb2">押しているあいだ力がたまる → 離すと投げる</span></button>
   <p class="hint">${hint || '赤い所で離すと丼の外に出やすい。丼を上へ払っても投げられる'}</p></div>`);
  return guard(new Promise(res => {
    const btn = $('#throwBtn'), fill = $('#gfill'); let t0 = 0, raf = 0, hold = false, p = 0, fin = false;
    const tick = () => { const t = (performance.now() - t0) / 1000; p = (1 - Math.cos(t * Math.PI / .9)) / 2; fill.style.width = (p * 100).toFixed(1) + '%'; fill.classList.toggle('hot', p > .82); SC.jig = .4 + p * .8; raf = requestAnimationFrame(tick); };
    const down = e => { e.preventDefault(); SND.init(); if (hold) return; hold = true; t0 = performance.now(); try { e.pointerId !== undefined && btn.setPointerCapture(e.pointerId); } catch (x) { } tick(); };
    const up = () => { if (!hold) return; hold = false; cancelAnimationFrame(raf); try { navigator.vibrate && navigator.vibrate(12); } catch (x) { } done({ power: p, aim: rnd(-.35, .35) }); };
    const kd = e => { if (e.code === 'Space' && !e.repeat) down(e); }, ku = e => { if (e.code === 'Space') up(); };
    btn.addEventListener('pointerdown', down); btn.addEventListener('pointerup', up); btn.addEventListener('pointercancel', up);
    btn.addEventListener('contextmenu', e => e.preventDefault());   // スマホの長押しメニューを出さない
    addEventListener('keydown', kd); addEventListener('keyup', ku);
    SC.swipe = (power, aim) => done({ power, aim });
    function done(v) { if (fin) return; fin = true; SC.swipe = null; SC.jig = 0; removeEventListener('keydown', kd); removeEventListener('keyup', ku); ctrl(''); res(v); }
  }));
}

// ================= turns =================
const STY = {
  safe: { bet: [.04, .08], pw: [.3, .6] }, bold: { bet: [.12, .3], pw: [.55, .9] }, whim: { bet: [.02, .35], pw: [.15, .96] }, kage: { bet: [.1, .2], pw: [.45, .82] }, bank: { pw: [.4, .75] }
};
async function announce(j, pl, sub) {
  const big = ['pin', 'zoro', '456'].includes(j.k);
  if (big) { SND.drum(true); if (CFG.fx === 'high') GFX.shake(.5); } else if (j.k === 'me') SND.drum(false); else SND.bad();
  if (pl.human && G.mode !== 'tut') {
    ST.roles[j.k] = (ST.roles[j.k] || 0) + 1; save();
    ({ pin: 'pin', zoro: 'zoro', '456': 's456', '123': 'h123', shon: 'shon' })[j.k] && unlock({ pin: 'pin', zoro: 'zoro', '456': 's456', '123': 'h123', shon: 'shon' }[j.k]);
  }
  await banner(j.name, sub || pl.name, CLS[j.k], big ? 1700 : 1150);
}
async function rollTurn(pi, o = {}) {
  const pl = G.players[pi]; G.turn = pi; pl.res = null; renderSeats();
  cpuTurn = !pl.human;
  for (let i = 0; i < 3; i++) {
    placeHand(o.loaded);
    let pw, aim;
    if (pl.human) {
      ({ power: pw, aim } = await waitThrow(i, o.hint));
      ST.throws++; save(); unlock('first');
    } else {
      ctrl(`<p class="cap" style="margin:0;text-align:center">${pl.name}が振る…</p>`);
      await sleep(350); SC.jig = .9; await sleep(rnd(550, 900)); SC.jig = 0;
      const s = STY[pl.style] || STY.safe; pw = rnd(s.pw[0], s.pw[1]); aim = rnd(-.6, .6); ctrl('');
    }
    const r = await throwDice({ power: pw, aim, force: o.force ? o.force(i) : null, wantShon: o.wantShon, slow: o.slow, rate: !pl.human && CFG.fast ? 1.6 : 1 });
    let j = r.out ? SHON(r.vals) : judge(r.vals);
    pl.res = j; renderSeats();
    if (j.k === 'shon') {
      if (CFG.shonLose || i === 2) { await announce(j, pl, pl.name + ' — 茶碗の外、即負け'); return j; }
      await announce(j, pl, `${pl.name} — 1投消費（残り${2 - i}投）`); continue;
    }
    if (j.k !== 'none') { await announce(j, pl, `${pl.name}  ${r.vals.join('・')}`); return j; }
    await announce(j, pl, i < 2 ? `${pl.name} — 振り直し（残り${2 - i}投）` : `${pl.name} — 3投とも目なし`);
  }
  return pl.res;
}
function pay(from, to, amt) {
  const A = G.players[from], B = G.players[to];
  const a = A.bank ? amt : Math.min(amt, A.chips);
  if (!A.bank) A.chips -= a; if (!B.bank) B.chips += a;
  delta(from, -a); delta(to, a); if (a) SND.chip(Math.min(8, 2 + Math.round(a / 1000)));
  if (!A.bank && A.chips < 100) A.out = true;
  return a;
}
function settleOne(pi, ci, pr, cr, bonus = (x, w) => x) {
  const c = G.players[ci]; const m = childMult(pr, cr);
  if (m > 0) { const amt = bonus(Math.round(m * c.bet), ci); return pay(pi, ci, amt); }
  if (m < 0) { const amt = bonus(Math.round(-m * c.bet), pi); return -pay(ci, pi, amt); }
  return 0;
}
const mtxt = (pr, cr) => { const m = childMult(pr, cr); return m > 0 ? `子の勝ち ×${m}` : m < 0 ? `親の勝ち ×${-m}` : '分け'; };

// ================= modes: common =================
function enter(mode, tag) {
  TOK++; G.mode = mode; SC.attract = false; SC.pb = null; SC.jig = 0; SC.swipe = null; cpuTurn = false;
  closeScreen(); closeModal(); hideGuide(); ctrl(''); setTag(tag); $('#banner').className = ''; $('#vig').classList.remove('red');
  SND.heart(0); clearDice(); setView(null); $('#hud').style.display = '';
  ST.games[mode] = (ST.games[mode] || 0) + 1; save();
}
function title() {
  TOK++; G.mode = 'title'; G.players = []; renderSeats(); ctrl(''); hideGuide(); setTag(''); SND.heart(0); closeModal();
  $('#hud').style.display = 'none'; SC.attract = true; attractT = 2.6; setView({ title: true });
  screen(`<div class="title"><h1>チン<span>チ</span>ロ</h1><p>藍の丼に三つのサイ。物理演算で転がる本格賭場</p></div>
  <div class="modes">
   <button class="mode hero" data-m="honkaku"><b>本格モード</b><small>CPU3人と親を回して勝負。チップを最も増やした者が勝ち</small></button>
   <button class="mode" data-m="tut"><b>チュートリアル</b><small>投げ方・役・配当を実際に振りながら覚える</small></button>
   <button class="mode" data-m="dead"><b>超デッドヒート</b><small>宿敵と1対1。レート倍増、最終局は全賭け</small></button>
   <button class="mode" data-m="ika"><b>イカサマ見破り</b><small>胴元が4・5・6賽を混ぜてくる。見抜けるか</small></button>
   <button class="mode" data-m="lab"><b>確率ラボ</b><small>役の理論確率と、物理での実測を比べる</small></button>
  </div>
  <div class="subs"><button class="btn ghost" data-m="roles">役一覧</button><button class="btn ghost" data-m="stats">戦績・実績</button><button class="btn ghost" data-m="set">設定</button></div>`);
}
$('#screen').addEventListener('click', e => {
  const b = e.target.closest('[data-m]'); if (!b) return; SND.init(); SND.click();
  const m = b.dataset.m;
  ({ honkaku: startHonkaku, tut: startTutorial, dead: startDead, ika: startIka, lab: showLab, roles: () => showRoles(), stats: showStats, set: () => showSettings(), title })[m]?.();
});

// ================= 本格 =================
async function startHonkaku() {
  enter('honkaku', '本格モード');
  G.players = [
    { name: 'あなた', human: true, chips: CFG.chips },
    { name: '慎重なタケ', style: 'safe', chips: CFG.chips },
    { name: '強気のリュウ', style: 'bold', chips: CFG.chips },
    { name: '気まぐれミヨ', style: 'whim', chips: CFG.chips }
  ];
  G.maxRounds = CFG.laps * 4; G.parent = 0;
  for (G.round = 1; G.round <= G.maxRounds; G.round++) {
    const alive = G.players.filter(p => !p.out);
    if (G.players[0].out || alive.length < 2) break;
    while (G.players[G.parent].out) G.parent = (G.parent + 1) % 4;
    const P = G.parent, par = G.players[P];
    setTag(`本格  第${G.round}/${G.maxRounds}局`);
    G.players.forEach(p => { p.bet = 0; p.res = null; }); G.turn = -1; renderSeats();
    await banner(`第${G.round}局`, `親は ${par.name}`, '', 1100);
    const kids = [1, 2, 3].map(k => (P + k) % 4).filter(i => !G.players[i].out);
    const maxB = i => Math.max(100, Math.min(G.players[i].chips, Math.floor(par.chips / kids.length / 2 / 100) * 100));
    for (const ci of kids) {
      const c = G.players[ci];
      if (c.human) c.bet = await waitBet(100, maxB(ci), Math.min(1000, maxB(ci)), `親 ${par.name} に賭ける`);
      else { const s = STY[c.style]; c.bet = Math.max(100, Math.min(maxB(ci), Math.round(c.chips * rnd(s.bet[0], s.bet[1]) / 100) * 100)); }
      c.bet = Math.min(c.bet, c.chips); renderSeats(); SND.chip(2); await sleep(220);
    }
    const pr = await rollTurn(P, { slow: CFG.fx === 'high' && par.human });
    if (instant(pr)) {
      await sleep(300);
      for (const ci of kids) settleOne(P, ci, pr, null);
      renderSeats(); await banner(childMult(pr, null) > 0 ? '親の総払い' : '親の総取り', pr.name, childMult(pr, null) > 0 ? 'blue' : 'shu', 1300);
    } else {
      for (const ci of kids) {
        if (G.players[ci].out || par.out) continue;
        const cr = await rollTurn(ci, { slow: CFG.fx === 'high' && (G.players[ci].human || par.human) });
        const g = settleOne(P, ci, pr, cr); renderSeats();
        await banner(mtxt(pr, cr), `${G.players[ci].name} ${g > 0 ? '+' : g < 0 ? '−' : '±'}${fmt(Math.abs(g))}`, g > 0 && G.players[ci].human ? 'gold' : '', 1000);
      }
    }
    G.turn = -1; renderSeats(); if (G.players[0].chips >= 50000) unlock('rich');
    G.parent = (G.parent + 1) % 4;
  }
  const rank = G.players.map((p, i) => ({ ...p, i })).sort((a, b) => b.chips - a.chips);
  const won = rank[0].human;
  ST.best = Math.max(ST.best, G.players[0].chips); if (won) { ST.wins.honkaku = (ST.wins.honkaku || 0) + 1; unlock('hwin'); } save();
  (won ? SND.win : SND.bad)();
  result(won ? '勝ち頭' : (G.players[0].out ? '素寒貧' : '負け越し'), `<table><thead><tr><th>順位</th><th>名前</th><th class="n">チップ</th><th class="n">増減</th></tr></thead><tbody>${rank.map((p, k) => `<tr><td>${k + 1}</td><td>${p.name}</td><td class="n">${fmt(p.chips)}</td><td class="n">${p.chips >= CFG.chips ? '+' : '−'}${fmt(Math.abs(p.chips - CFG.chips))}</td></tr>`).join('')}</tbody></table>`, 'honkaku');
}
function result(head, body, again, label) {
  hideGuide(); ctrl(''); SND.heart(0);
  screen(`<p class="big">${head}</p>${body}<div class="row2"><button class="btn shu" data-m="${again}">${label || 'もう一度'}</button><button class="btn ghost" data-m="title">タイトルへ</button></div>`, true);
}

// ================= デッドヒート =================
async function startDead() {
  enter('dead', '超デッドヒート');
  const S0 = 5000;
  G.players = [{ name: 'あなた', human: true, chips: S0 }, { name: '宿敵カゲトラ', style: 'kage', chips: S0 }];
  const RATES = [300, 400, 500, 700, 900, 1200, 1500, 2000, 2500, 0];
  G.maxRounds = RATES.length; const hist = [0]; let worst = 0;
  const bonus = (amt, winner) => { const w = G.players[winner], l = G.players[1 - winner]; return l.chips - w.chips >= 1000 ? Math.round(amt * 1.5) : amt; };
  const bpm = () => { const d = Math.abs(G.players[0].chips - G.players[1].chips); return Math.round(80 + 70 * Math.max(0, 1 - d / 5000) * (.4 + .6 * G.round / G.maxRounds)); };
  await say('宿敵カゲトラとの1対1。親は1局ごとに交代し、賭け金（レート）は局が進むたびに上がる。<br>負けている側が1000以上離されていれば、勝った時の受け取りが<b>1.5倍</b>になる「逆転レート」が付く。最終局は互いの持ち金の少ない方まで全賭け。', { buttons: [['go', '勝負']] });
  hideGuide();
  const duel = async (r, final) => {
    const P = (r + 1) % 2, C = 1 - P; G.parent = P;
    const rate = final ? Math.min(G.players[0].chips, G.players[1].chips) : RATES[r - 1];
    G.players.forEach(p => { p.res = null; p.bet = 0; }); G.players[C].bet = Math.min(rate, G.players[C].chips, G.players[P].chips);
    setTag(final === 'sd' ? '超デッドヒート  サドンデス' : `超デッドヒート  第${r}/${G.maxRounds}局  レート${fmt(G.players[C].bet)}`); renderSeats();
    await banner(final ? (final === 'sd' ? 'サドンデス' : '最終決戦') : `第${r}局`, `親 ${G.players[P].name}　賭け金 ${fmt(G.players[C].bet)}`, final ? 'shu' : '', 1300);
    SND.heart(CFG.sound ? bpm() : 0);
    const pr = await rollTurn(P, { slow: true });
    let g;
    if (instant(pr)) g = settleOne(P, C, pr, null, bonus);
    else { const cr = await rollTurn(C, { slow: true }); g = settleOne(P, C, pr, cr, bonus); }
    SND.heart(0); renderSeats();
    const youG = C === 0 ? g : -g;
    await banner(youG > 0 ? '勝ち' : youG < 0 ? '負け' : '分け', `あなた ${youG >= 0 ? '+' : '−'}${fmt(Math.abs(youG))}`, youG > 0 ? 'gold' : youG < 0 ? 'blue' : 'gray', 1300);
    hist.push(G.players[0].chips - G.players[1].chips); worst = Math.min(worst, hist[hist.length - 1]);
  };
  for (G.round = 1; G.round <= G.maxRounds; G.round++) {
    if (G.players.some(p => p.chips <= 0)) break;
    await duel(G.round, G.round === G.maxRounds ? 'final' : false);
  }
  let sd = false, sdw = -1;
  while (G.players[0].chips === G.players[1].chips && G.players[0].chips > 0) {
    sd = true; G.players.forEach(p => { p.res = null; p.bet = 0; }); renderSeats(); setTag('超デッドヒート  サドンデス');
    await banner('サドンデス', '一投勝負。役の強い方が勝ち', 'shu', 1500);
    const a = await rollTurn(0, { slow: true }), b = await rollTurn(1, { slow: true });
    if (a.rank !== b.rank) { sdw = a.rank > b.rank ? 0 : 1; break; }
  }
  const won = sdw >= 0 ? sdw === 0 : G.players[0].chips > G.players[1].chips;
  if (won) { unlock('dh'); ST.wins.dead = (ST.wins.dead || 0) + 1; if (worst <= -2000) unlock('comeback'); if (sd) unlock('sudden'); } save();
  (won ? SND.win : SND.bad)();
  const W = 600, H = 160, mx = Math.max(1000, ...hist.map(Math.abs));
  const pts = hist.map((d, i) => `${(i / (hist.length - 1 || 1) * (W - 20) + 10).toFixed(1)},${(H / 2 - d / mx * (H / 2 - 12)).toFixed(1)}`).join(' ');
  result(won ? '宿敵を下した' : '宿敵に敗れた', `<p style="text-align:center">あなた ${fmt(G.players[0].chips)} ／ カゲトラ ${fmt(G.players[1].chips)}</p>
   <h3>チップ差の推移</h3><svg class="svgline" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="#fff3" stroke-dasharray="4 4"/><polyline points="${pts}" fill="none" stroke="#f2c14e" stroke-width="3" vector-effect="non-scaling-stroke"/></svg>
   <p class="legend">中央線より上があなたの優勢。${sd ? 'サドンデスで決着。' : ''}</p>`, 'dead');
}

// ================= イカサマ見破り =================
function cubeHTML(faces) {
  const T = ['rotateY(90deg)', 'rotateY(-90deg)', 'rotateX(90deg)', 'rotateX(-90deg)', 'rotateY(0deg)', 'rotateY(180deg)'];
  return `<div class="cube">${faces.map((v, i) => `<div style="transform:${T[i]} translateZ(32px)">${dieSVG(v, 64)}</div>`).join('')}</div>`;
}
async function startIka() {
  enter('ika', 'イカサマ見破り');
  G.players = [{ name: 'あなた', human: true, chips: 5000 }, { name: '胴元クロダ', style: 'bank', bank: true, chips: 0 }];
  const N = 6; const plan = shuffle([1, 1, 0, 0, 0, Math.random() < .5 ? 1 : 0]);
  let insp = 2, hit = 0, miss = 0, wrong = 0;
  await say('胴元クロダが親を務める。こいつは時々、4・5・6しか無い<b>細工サイ</b>を紛れ込ませる。<br>胴元が振った後、画面をドラッグして視点を回せばサイの側面が見える。「サイを改める」で手に取って確かめることも（2回まで）。<br>見抜いて指摘すれば賭け金の5倍を取れるが、いいがかりなら3倍払いだ。', { buttons: [['go', '始める']] });
  hideGuide(); G.parent = 1;
  for (G.round = 1; G.round <= N; G.round++) {
    const me = G.players[0]; if (me.chips <= 0) break;
    setTag(`イカサマ見破り  第${G.round}/${N}局`); G.players.forEach(p => { p.res = null; p.bet = 0; }); renderSeats();
    me.bet = await waitBet(200, Math.min(me.chips, 3000), Math.min(1000, me.chips), '胴元に賭ける'); renderSeats();
    const loaded = !!plan[G.round - 1];
    const pr = await rollTurn(1, { loaded });
    let done = false;
    for (; ;) {
      const k = await choose('胴元の出目をどうする？ ドラッグで視点を回して側面を確かめられる', [['look', `サイを改める（残り${insp}回）`, 'ghost', insp <= 0], ['acc', 'イカサマだ！', 'shu'], ['go', instant(pr) ? 'このまま受け入れる' : '勝負を続ける']]);
      if (k === 'look') {
        insp--; modal(`<h3 style="margin-top:0">胴元のサイ</h3><div class="cubes">${[0, 1, 2].map(() => cubeHTML(loaded ? FACES_L : FACES_N)).join('')}</div><p class="legend">3個とも同じ作り。全ての面を見比べよう。</p><button class="btn" style="width:100%" id="mok">閉じる</button>`);
        await guard(new Promise(r => $('#mok').onclick = r)); closeModal(); continue;
      }
      if (k === 'acc') {
        if (loaded) { hit++; SND.drum(true); GFX.shake(.6); const a = pay(1, 0, me.bet * 5); renderSeats(); await banner('イカサマ発覚', `細工サイだった！ +${fmt(a)}`, 'gold', 1800); unlock('eye'); }
        else { wrong++; SND.bad(); const a = pay(0, 1, me.bet * 3); renderSeats(); await banner('いいがかり', `まともなサイだった… −${fmt(a)}`, 'blue', 1700); }
        done = true;
      }
      break;
    }
    if (!done) {
      if (loaded) miss++;
      if (instant(pr)) settleOne(1, 0, pr, null);
      else { const cr = await rollTurn(0, {}); const g = settleOne(1, 0, pr, cr); await banner(mtxt(pr, cr), `${g >= 0 ? '+' : '−'}${fmt(Math.abs(g))}`, g > 0 ? 'gold' : '', 1000); }
      renderSeats();
      if (loaded) await banner('見逃した', 'さっきのは細工サイだった…', 'blue', 1500);
    }
  }
  const tot = plan.filter(Boolean).length;
  if (hit === tot && !wrong && tot) unlock('eye3');
  ST.wins.ika = Math.max(ST.wins.ika || 0, hit); save();
  result(hit === tot && !wrong ? '完全看破' : hit ? '眼力あり' : 'カモにされた', `<table><tbody><tr><td>最終チップ</td><td class="n">${fmt(G.players[0].chips)}</td></tr><tr><td>細工サイの局</td><td class="n">${tot}</td></tr><tr><td>見破った</td><td class="n">${hit}</td></tr><tr><td>見逃した</td><td class="n">${miss}</td></tr><tr><td>いいがかり</td><td class="n">${wrong}</td></tr></tbody></table>
   <p class="legend">見分け方：細工サイは向かい合う面が同じ数。側面に1・2・3が見えたら普通のサイ。</p>`, 'ika');
}

// ================= チュートリアル =================
function genSet(kind) {
  if (kind === 'pin') return [1, 1, 1];
  if (kind === 'zoro') { const n = 2 + Math.floor(Math.random() * 5); return [n, n, n]; }
  if (kind === '456') return shuffle([4, 5, 6]); if (kind === '123') return shuffle([1, 2, 3]);
  if (kind === 'me') { const p = 1 + Math.floor(Math.random() * 6); let c; do c = 1 + Math.floor(Math.random() * 6); while (c === p); return shuffle([p, p, c]); }
  let v; do v = [0, 0, 0].map(() => 1 + Math.floor(Math.random() * 6)); while (judge(v).k !== 'none'); return v;
}
async function startTutorial() {
  enter('tut', 'チュートリアル');
  G.players = [{ name: 'あなた', human: true, chips: 0, label: '練習中' }]; G.parent = 0; renderSeats();
  const T = async (text, force, extra = {}) => {
    await say(text, { wait: false });
    G.turn = 0; placeHand(false); const { power, aim } = await waitThrow(0, extra.hint);
    ST.throws++; unlock('first');
    const r = await throwDice({ power, aim, force, wantShon: extra.shon, slow: extra.slow });
    const j = r.out ? SHON(r.vals) : judge(r.vals); G.players[0].res = j; renderSeats(); await announce(j, G.players[0], r.vals.join('・')); return { r, j };
  };
  await say('ようこそ、チンチロの卓へ。使うのは<b>丼（茶碗）ひとつとサイコロ3個</b>だけ。3個を一度に投げ入れて、出た目の組み合わせ＝<b>役</b>の強さで勝負する。');
  await say('卓は指でドラッグすると視点を回せる。ピンチやホイールで寄ることもできる。右上の「視点」で真上や低めのアングルに切り替え。');
  let { r } = await T('まず一度振ってみよう。<b>「振る」ボタンを長押し</b>するとゲージが往復する。好きなところで離すと、その強さで投げる。', null, { hint: 'まずは真ん中くらいの力で' });
  await say(`出目は ${dset(r.vals, 24)}。この組み合わせで役が決まる。では代表的な役を順番に出してみよう。`);
  const demos = [
    [[2, 2, 5], '同じ数が2つ揃うと、<b>残り1つの数がそのまま「目」</b>になる。いま狙うのは「5の目」。', '2・2・5で<b>5の目</b>。目は1〜6の6段階で、数が大きいほど強い。いちばんよく出る普通の勝ち筋。'],
    [[4, 5, 6], '次は強い役。4・5・6が揃う<b>シゴロ</b>だ。', 'シゴロは目より強く、<b>賭け金の2倍</b>を受け取れる。親が出せば子は振る前に負けが決まる。'],
    [[3, 3, 3], '3つとも同じ数が揃うと<b>ゾロ目（アラシ）</b>。', 'ゾロ目は<b>3倍</b>付け。シゴロより強い。'],
    [[1, 1, 1], '最強の役。1が3つ並ぶ<b>ピンゾロ</b>。', 'ピンゾロは<b>5倍</b>付け（設定で3倍にもできる）。一晩に何度も見られるものじゃない。'],
    [[1, 2, 3], '逆に、出したくない役もある。1・2・3の<b>ヒフミ</b>。', 'ヒフミは即負け、しかも<b>2倍払い</b>。強く祈っても出るときは出る。'],
    [[1, 3, 6], 'どれにも当てはまらないと<b>目なし</b>。', '目なしは振り直し。<b>最大3回</b>まで振れて、3回とも目なしなら負け（1倍払い）。']
  ];
  for (const [f, a, b] of demos) { await T(a, f); await say(b); }
  await T('最後に、わざとゲージの<b>赤いところで離して</b>、思い切り強く投げてみよう。', null, { shon: true, hint: '赤まで溜めて離す！' });
  await say('サイが丼の外に飛び出すと<b>ションベン</b>。即負けで1倍払い（設定で「1投消費」に変更可）。力の加減も腕のうち。');
  await say('勝負は<b>親</b>1人と<b>子</b>たちで行う。子が先に賭け金を置き、親が振る。親がシゴロ以上なら子の総負け、ヒフミ・目なし・ションベンなら親の総払い。');
  await say('親が「目」を出したら、子が1人ずつ振って<b>親の目と比べる</b>。上なら子の勝ち、同じなら分け、下なら負け。子のシゴロ・ゾロ目・ピンゾロは倍付けで受け取れる。親は1局ごとに右隣へ回る。');
  await say('では役当てクイズ。5問、出目を見て役を選ぼう。', { buttons: [['q', 'クイズへ']] });
  hideGuide(); clearDice();
  let score = 0; const kinds = shuffle(['me', '456', 'zoro', '123', 'none', 'pin', 'me']).slice(0, 5);
  for (let q = 0; q < 5; q++) {
    const v = genSet(kinds[q]), j = judge(v);
    const pool = shuffle(['ピンゾロ', 'シゴロ', 'ヒフミ', '目なし', ...[1, 2, 3, 4, 5, 6].map(n => n + 'の目'), ...[2, 3, 4, 5, 6].map(n => n + 'のゾロ目')].filter(x => x !== j.name)).slice(0, 3);
    const opts = shuffle([j.name, ...pool]);
    ctrl(`<p class="cap">第${q + 1}問　この出目の役は？</p><div style="text-align:center;margin-bottom:10px">${dset(v, 54)}</div><div class="qopt">${opts.map(o => `<button class="btn ghost" data-k="${o}">${o}</button>`).join('')}</div>`);
    const a = await guard(new Promise(r => { $('#ctrl').onclick = e => { const b = e.target.closest('button[data-k]'); if (b) { $('#ctrl').onclick = null; r(b.dataset.k); } }; }));
    const ok = a === j.name; if (ok) { score++; SND.win(); } else SND.bad();
    await banner(ok ? '正解' : '不正解', ok ? j.name : `正しくは ${j.name}`, ok ? 'gold' : 'blue', 1000);
  }
  ctrl(''); unlock('tut'); if (score === 5) unlock('quiz');
  result(`${score} / 5 問正解`, `<p style="text-align:center">これで卓に着く準備はできた。本格モードでCPU相手に腕を試そう。</p>`, 'honkaku', '本格モードへ');
}

// ================= ラボ =================
function theory() {
  const c = { pin: 0, zoro: 0, '456': 0, '123': 0, none: 0 }, me = [0, 0, 0, 0, 0, 0, 0];
  for (let a = 1; a <= 6; a++) for (let b = 1; b <= 6; b++) for (let d = 1; d <= 6; d++) { const j = judge([a, b, d]); if (j.k === 'me') me[j.rank]++; else c[j.k]++; }
  return { c, me };
}
function evChild() {
  const { c, me } = theory(), q = c.none / 216, k = 1 + q + q * q;
  const dist = [['pin', 0, c.pin / 216 * k], ['zoro', 0, c.zoro / 216 * k], ['456', 0, c['456'] / 216 * k], ['123', 0, c['123'] / 216 * k], ['none', 0, q ** 3]];
  for (let m = 1; m <= 6; m++) dist.push(['me', m, me[m] / 216 * k]);
  let ev = 0;
  for (const [pk, pm, pp] of dist) {
    const pr = { k: pk, rank: pm };
    if (instant(pr)) { ev += pp * childMult(pr, null); continue; }
    for (const [ck, cm, cp] of dist) ev += pp * cp * childMult(pr, { k: ck, rank: ck === 'me' ? cm : ck === '123' ? -10 : 0 });
  }
  return ev;
}
function showLab() {
  TOK++; SC.attract = true; $('#hud').style.display = 'none';
  const { c, me } = theory(), q = c.none / 216, k = 1 + q + q * q;
  const rows = [['ピンゾロ', c.pin, [1, 1, 1]], ['ゾロ目（2〜6）', c.zoro, [3, 3, 3]], ['シゴロ', c['456'], [4, 5, 6]], ['目（1〜6）', me.reduce((a, b) => a + b), [2, 2, 5]], ['ヒフミ', c['123'], [1, 2, 3]], ['目なし', c.none, [1, 3, 6]]];
  const ev = evChild();
  screen(`<h2>確率ラボ</h2>
   <h3>理論確率</h3><table><thead><tr><th>役</th><th class="n">1投</th><th class="n">1手番（3投まで）</th></tr></thead><tbody>
   ${rows.map(([n, x, d], i) => `<tr><td>${dset(d, 18)} ${n}</td><td class="n">${(x / 216 * 100).toFixed(2)}%</td><td class="n">${(i === 5 ? q ** 3 : x / 216 * k) * 100 > 0 ? ((i === 5 ? q ** 3 : x / 216 * k) * 100).toFixed(2) + '%' : '-'}</td></tr>`).join('')}</tbody></table>
   <p class="legend">3個のサイの出方は全216通り。目なしが50%なので、振り直しの有無で手番の確率は1.75倍になる。</p>
   <h3>今のハウスルールでの子の期待値</h3><p style="font-size:28px;font-weight:900;margin:0">${ev >= 0 ? '+' : '−'}${Math.abs(ev * 100).toFixed(2)}% <small class="legend">賭け金あたり（ションベン除く）</small></p>
   <p class="legend">マイナスなら親が有利。ピンゾロ倍率・ヒフミ倍払いの設定で変わる。</p>
   <h3>物理シミュレーションで実測</h3>
   <div class="set"><div>投げる強さ<small>強いほどションベンが増える</small></div><div class="seg" id="pw">${[['.3', '弱'], ['.6', '中'], ['.9', '強'], ['1', '最大']].map(([v, l], i) => `<button data-v="${v}" aria-pressed="${i === 1}">${l}</button>`).join('')}</div></div>
   <div class="row2"><button class="btn shu" id="labGo">200回振る</button></div>
   <div id="labOut"></div>
   <div class="row2"><button class="btn ghost" data-m="title">タイトルへ</button></div>`, true);
  let pw = .6;
  $('#pw').onclick = e => { const b = e.target.closest('button'); if (!b) return; pw = +b.dataset.v; [...$('#pw').children].forEach(x => x.setAttribute('aria-pressed', x === b)); };
  $('#labGo').onclick = () => {
    const N = 200, cnt = { pin: 0, zoro: 0, '456': 0, me: 0, '123': 0, none: 0, shon: 0 }, face = [0, 0, 0, 0, 0, 0, 0]; let i = 0; const t = TOK;
    $('#labGo').disabled = true; $('#labOut').innerHTML = `<div class="prog" style="margin-top:12px"><b id="lp"></b></div>`;
    const run = () => {
      if (t !== TOK) return;
      for (let s = 0; s < 4 && i < N; s++, i++) {
        const su = PHYS.setup((Math.random() * 1e9) | 0), sim = PHYS.simulate(su, pw * rnd(.92, 1), rnd(-.6, .6));
        const v = sim.res.map(r => FACES_N[r.axis]);
        sim.res.forEach((r, k) => { if (!r.out) face[v[k]]++; });
        cnt[sim.res.some(r => r.out) ? 'shon' : judge(v).k]++;
      }
      $('#lp').style.width = (i / N * 100) + '%';
      if (i < N) return setTimeout(run, 0);
      unlock('lab'); $('#labGo').disabled = false;
      const th = { pin: c.pin, zoro: c.zoro, '456': c['456'], me: me.reduce((a, b) => a + b), '123': c['123'], none: c.none, shon: 0 };
      const fm = Math.max(...face.slice(1));
      $('#labOut').innerHTML = `<h3>出た面（1〜6）</h3><table><tbody>${[1, 2, 3, 4, 5, 6].map(n => `<tr><td style="width:40px">${dieSVG(n, 22)}</td><td><div class="bar" style="width:${face[n] / fm * 100}%"></div></td><td class="n">${face[n]}</td></tr>`).join('')}</tbody></table>
       <p class="legend">偏りが小さければ公平なサイ。理論上はそれぞれ約1/6。</p>
       <h3>役の出現（1投あたり）</h3><table><thead><tr><th>役</th><th>実測（太）と理論（細）</th><th class="n">実測</th></tr></thead><tbody>${Object.keys(cnt).map(kk => `<tr><td>${kk === 'me' ? '目' : kk === 'zoro' ? 'ゾロ目' : NAMES[kk]}</td><td><div class="bar" style="width:${cnt[kk] / N * 180}%"></div><div class="bar t" style="width:${th[kk] / 216 * 180}%"></div></td><td class="n">${(cnt[kk] / N * 100).toFixed(1)}%</td></tr>`).join('')}</tbody></table>`;
    };
    run();
  };
}

// ================= 役一覧・戦績・設定 =================
function rolesHTML() {
  return `<table><thead><tr><th>役</th><th>出目</th><th class="n">配当</th></tr></thead><tbody>
  <tr><td><b>ピンゾロ</b></td><td>${dset([1, 1, 1], 20)}</td><td class="n">${pinM()}倍</td></tr>
  <tr><td><b>ゾロ目（アラシ）</b></td><td>${dset([5, 5, 5], 20)}</td><td class="n">3倍</td></tr>
  <tr><td><b>シゴロ</b></td><td>${dset([4, 5, 6], 20)}</td><td class="n">2倍</td></tr>
  <tr><td><b>目（1〜6）</b></td><td>${dset([3, 3, 6], 20)}</td><td class="n">1倍</td></tr>
  <tr><td>目なし</td><td>${dset([2, 4, 5], 20)}</td><td class="n">振り直し</td></tr>
  <tr><td>ヒフミ</td><td>${dset([1, 2, 3], 20)}</td><td class="n">${hifM()}倍払い</td></tr>
  <tr><td>ションベン</td><td>丼の外</td><td class="n">${CFG.shonLose ? '即負け' : '1投消費'}</td></tr></tbody></table>
  <p class="legend">強さ：ピンゾロ ＞ ゾロ目 ＞ シゴロ ＞ 6の目 … 1の目 ＞ 目なし・ションベン ＞ ヒフミ。目なしは3投まで振り直せる。親がシゴロ以上・ヒフミ・目なしを出した時点で、子は振らずに決着する。</p>`;
}
function showRoles(back) {
  if (back) { modal(`<h2 style="margin-top:0">役一覧</h2>${rolesHTML()}<button class="btn" style="width:100%;margin-top:12px" id="mok">閉じる</button>`); $('#mok').onclick = closeModal; return; }
  screen(`<h2>役一覧</h2>${rolesHTML()}<div class="row2"><button class="btn ghost" data-m="title">タイトルへ</button></div>`, true);
}
function showStats() {
  const r = ST.roles, n = Object.keys(ACH).filter(k => ST.ach[k]).length;
  screen(`<h2>戦績・実績</h2><table><tbody>
   <tr><td>投げた回数</td><td class="n">${fmt(ST.throws)}</td></tr>
   <tr><td>本格モード 対局／1位</td><td class="n">${ST.games.honkaku || 0} ／ ${ST.wins.honkaku || 0}</td></tr>
   <tr><td>本格モード 最高チップ</td><td class="n">${fmt(ST.best)}</td></tr>
   <tr><td>デッドヒート 挑戦／勝利</td><td class="n">${ST.games.dead || 0} ／ ${ST.wins.dead || 0}</td></tr>
   <tr><td>イカサマ 最多看破</td><td class="n">${ST.wins.ika || 0}</td></tr>
   <tr><td>あなたの役</td><td class="n">ピンゾロ ${r.pin || 0}・ゾロ目 ${r.zoro || 0}・シゴロ ${r['456'] || 0}・ヒフミ ${r['123'] || 0}・ションベン ${r.shon || 0}</td></tr></tbody></table>
   <h3>実績 ${n} / ${Object.keys(ACH).length}</h3><div class="ach">${Object.entries(ACH).map(([k, [t, d]]) => `<div class="${ST.ach[k] ? 'got' : ''}"><b>${ST.ach[k] ? t : '？？？'}</b>${d}</div>`).join('')}</div>
   <div class="row2"><button class="btn ghost" id="rst">記録を消す</button><button class="btn" data-m="title">タイトルへ</button></div>`, true);
  $('#rst').onclick = () => {
    modal(`<p>戦績と実績をすべて消します。元に戻せません。</p><div class="row2"><button class="btn ghost" id="mno">やめる</button><button class="btn shu" id="myes">消す</button></div>`);
    $('#mno').onclick = closeModal; $('#myes').onclick = () => { ST = { games: {}, wins: {}, roles: {}, ach: {}, best: 0, throws: 0 }; save(); closeModal(); showStats(); };
  };
}
function settingsHTML() {
  const tg = (k, t, d) => `<div class="set"><div>${t}<small>${d}</small></div><button class="tg" data-t="${k}" aria-pressed="${!!CFG[k]}" aria-label="${t}"></button></div>`;
  const sg = (k, t, d, opts) => `<div class="set"><div>${t}<small>${d}</small></div><div class="seg" data-s="${k}">${opts.map(([v, l]) => `<button data-v="${v}" aria-pressed="${String(CFG[k]) === String(v)}">${l}</button>`).join('')}</div></div>`;
  return `<h3>ハウスルール</h3>${tg('pin5', 'ピンゾロ5倍', 'オフで3倍')}${tg('hifumi2', 'ヒフミ倍払い', 'オフで1倍払い')}${tg('shonLose', 'ションベン即負け', 'オフで1投消費して振り直し')}
  <h3>本格モード</h3>${sg('chips', '持ち金', '開始時のチップ', [[5000, '5千'], [10000, '1万'], [30000, '3万']])}${sg('laps', '局数', '親が何周するか', [[1, '1周'], [2, '2周'], [3, '3周']])}
  <h3>演出・操作</h3>${tg('sound', '効果音', '茶碗の音、心音など')}${sg('fx', '演出', 'スローモーションと画面の揺れ', [['high', '派手'], ['low', '控えめ']])}${tg('fast', 'CPUを早送り', 'CPUの手番を速く進める')}`;
}
function bindSettings(root) {
  root.onclick = e => {
    const t = e.target.closest('[data-t]'); if (t) { const k = t.dataset.t; CFG[k] = !CFG[k]; t.setAttribute('aria-pressed', CFG[k]); if (k === 'sound') { SND.on = CFG.sound; sndLabel(); } save(); SND.click(); return; }
    const b = e.target.closest('.seg button'); if (b) { const k = b.parentNode.dataset.s; const v = b.dataset.v; CFG[k] = isNaN(+v) ? v : +v; [...b.parentNode.children].forEach(x => x.setAttribute('aria-pressed', x === b)); save(); SND.click(); }
  };
}
function showSettings() {
  screen(`<h2>設定</h2><div id="setw">${settingsHTML()}</div><div class="row2"><button class="btn" data-m="title">タイトルへ</button></div>`, true);
  bindSettings($('#setw'));
}

// ================= top bar / input =================
const sndLabel = () => { $('#sndBtn').textContent = CFG.sound ? '音 ON' : '音 OFF'; };
sndLabel();
$('#sndBtn').onclick = () => { SND.init(); CFG.sound = !CFG.sound; SND.on = CFG.sound; sndLabel(); save(); };
const VIEWS = [['標準', .95], ['真上', 1.5], ['低め', .5]]; let vi = 0;
$('#viewBtn').onclick = () => { vi = (vi + 1) % 3; GFX.cam.user.el = VIEWS[vi][1]; GFX.cam.user.az = 0; GFX.cam.user.zoom = 1; GFX.setView(SC.view); $('#viewBtn').textContent = VIEWS[vi][0]; };
$('#menuBtn').onclick = () => {
  SND.click();
  modal(`<h2 style="margin-top:0">メニュー</h2><div class="choices"><button class="btn" id="m1">続ける</button><button class="btn ghost" id="m2">役一覧</button><button class="btn ghost" id="m3">設定</button><button class="btn shu" id="m4">タイトルへ戻る</button></div>`);
  $('#m1').onclick = closeModal; $('#m2').onclick = () => showRoles(true);
  $('#m3').onclick = () => { modal(`<h2 style="margin-top:0">設定</h2><div id="setm">${settingsHTML()}</div><button class="btn" style="width:100%;margin-top:12px" id="mok">閉じる</button>`); bindSettings($('#setm')); $('#mok').onclick = closeModal; };
  $('#m4').onclick = () => { closeModal(); title(); };
};
const cv = $('#cv'); const ptr = new Map(); let drag = null, pinch = 0;
cv.addEventListener('pointerdown', e => {
  SND.init(); ptr.set(e.pointerId, [e.clientX, e.clientY]); cv.setPointerCapture(e.pointerId);
  if (ptr.size === 2) { const [a, b] = [...ptr.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); drag = null; return; }
  drag = { x: e.clientX, y: e.clientY, t: performance.now(), az: GFX.cam.user.az, el: GFX.cam.user.el };
  if (SC.swipe) SC.jig = .8;
});
cv.addEventListener('pointermove', e => {
  if (!ptr.has(e.pointerId)) return; ptr.set(e.pointerId, [e.clientX, e.clientY]);
  if (ptr.size === 2) { const [a, b] = [...ptr.values()]; const d = Math.hypot(a[0] - b[0], a[1] - b[1]); if (pinch) { GFX.cam.user.zoom = Math.max(.45, Math.min(1.6, GFX.cam.user.zoom * pinch / d)); GFX.setView(SC.view); } pinch = d; return; }
  if (!drag || SC.swipe) return;
  GFX.cam.user.az = drag.az - (e.clientX - drag.x) * .006; GFX.cam.user.el = Math.max(.3, Math.min(1.53, drag.el + (e.clientY - drag.y) * .005)); GFX.setView(SC.view);
});
const pend = e => {
  ptr.delete(e.pointerId); if (ptr.size < 2) pinch = 0;
  if (drag && SC.swipe) {
    const dy = drag.y - e.clientY, dt = Math.max(16, performance.now() - drag.t), v = dy / dt;
    SC.jig = 0;
    if (dy > 40 && v > .25) SC.swipe(Math.max(0, Math.min(1, (v - .25) / 2.2)), Math.max(-1, Math.min(1, (e.clientX - drag.x) / 220)));
  }
  drag = null;
};
cv.addEventListener('pointerup', pend); cv.addEventListener('pointercancel', pend);
cv.addEventListener('wheel', e => { e.preventDefault(); GFX.cam.user.zoom = Math.max(.45, Math.min(1.6, GFX.cam.user.zoom * (1 + e.deltaY * .001))); GFX.setView(SC.view); }, { passive: false });
addEventListener('resize', () => { GFX.resize(); GFX.setView(SC.view); });

// ================= boot =================
let okGL = false; try { okGL = GFX.init(cv); } catch (e) { console.error(e); }
if (!okGL) { document.body.insertAdjacentHTML('beforeend', '<p style="position:fixed;top:40%;width:100%;text-align:center">このブラウザではWebGLが使えないため、3D表示ができません。</p>'); }
setView(null); requestAnimationFrame(loop); title();
window.__chin = { G, SC, judge, childMult, evChild, CFG }; window.GFX = GFX;
}
