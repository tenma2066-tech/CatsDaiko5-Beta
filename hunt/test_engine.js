'use strict';
// node test_engine.js … ルールのテストと、自動対戦でのバランス確認
const assert = require('assert');
const E = require('./engine.js');
let n = 0;
const t = (name, fn) => { fn(); n++; console.log('ok  ' + name); };

// かしこめの自動操作（バランス確認用）
function smart(s) {
  const u = E.unit(s, s.turn), sks = E.skillsOf(u), m = E.monster(s);
  const hs = E.hunters(s), live = hs.filter((x) => x.hp > 0);
  const can = (i, tg) => !E.canUse(s, u, i, tg);
  const part = m.parts.find((p) => !p.broken);
  const tgt = part ? part.id : 'm';
  const low = live.slice().sort((a, b) => a.hp / a.maxHp - b.hp / b.maxHp)[0];
  if (u.key === 'minamo') {
    if (can(3) && (hs.some((x) => x.hp <= 0) || live.filter((x) => x.hp < x.maxHp * 0.6).length >= 3)) return [3, null];
    if (can(2) && live.some((x) => x.st.some((y) => E.STATUS[y.id].kind === 'debuff'))) return [2, null];
    if (can(1, low.id) && low.hp < low.maxHp * 0.65) return [1, low.id];
    return [0, tgt];
  }
  if (u.key === 'shion') {
    if (can(3) && m.charging >= 0) return [3, null];
    if (can(2) && u.hp < u.maxHp * 0.55) return [2, null];
    if (can(1) && !E.has(u, 'taunt')) return [1, null];
    return [0, tgt];
  }
  if (can(3, tgt)) return [3, tgt];
  if (u.key === 'kagari' && can(2) && !E.has(u, 'atkUp')) return [2, null];
  if (can(1, tgt) && !(u.key === 'touka' && E.has(m, 'slow'))) return [1, tgt];
  if (can(2, tgt) && !(u.key === 'touka' && E.has(m, 'poison')) && !(u.key === 'raiga' && E.has(m, 'atkDown'))) return [2, tgt];
  return [0, tgt];
}
function play(quest, seed, policy) {
  const s = E.createBattle(quest, 1, seed); E.start(s);
  let acts = 0;
  while (s.phase !== 'over' && acts < 2000) {
    const [si, tg] = policy ? policy(s) : [0, 'm'];
    const r = E.act(s, s.turn, si, tg); assert.ok(r.ok, r.error); acts++;
  }
  return { win: s.win, acts, turns: s.turns, s };
}

t('属性のすくみ', () => {
  assert.strictEqual(E.elemMul('red', 'green'), 1.3); assert.strictEqual(E.elemMul('green', 'red'), 0.75);
  assert.strictEqual(E.elemMul('green', 'blue'), 1.3); assert.strictEqual(E.elemMul('blue', 'red'), 1.3);
  assert.strictEqual(E.elemMul('yellow', 'purple'), 1.3); assert.strictEqual(E.elemMul('purple', 'yellow'), 1.3);
  assert.strictEqual(E.elemMul('red', 'yellow'), 1); assert.strictEqual(E.elemMul('red', 'red'), 1);
});
t('同じ種なら同じ結果になる', () => {
  const a = play('hibashiri', 7, smart), b = play('hibashiri', 7, smart);
  assert.deepStrictEqual([a.win, a.acts, a.turns], [b.win, b.acts, b.turns]);
});
t('人数ぶんに操作を割りふる（1〜5人）', () => {
  for (let p = 1; p <= 5; p++) {
    const s = E.createBattle('hibashiri', p, 3);
    const owners = E.hunters(s).map((u) => u.owner);
    assert.strictEqual(new Set(owners).size, p);
  }
});
t('自分の番でない・待ち中・奥義ゲージ不足は断る', () => {
  const s = E.createBattle('hibashiri', 1, 5); E.start(s);
  const other = E.hunters(s).find((u) => u.id !== s.turn);
  assert.strictEqual(E.act(s, other.id, 0, 'm').ok, false);
  assert.strictEqual(E.act(s, s.turn, 3, 'm').ok, false);
  const u = E.unit(s, s.turn); u.cds[1] = 2;
  assert.strictEqual(E.act(s, u.id, 1, 'm').ok, false);
});
t('鈍足でターンが回ってきにくくなる', () => {
  const s = E.createBattle('hibashiri', 1, 5); const m = E.monster(s);
  const count = () => E.timeline(s, 40).filter((x) => x === 'm').length;
  const before = count(); E.addStatus(s, m, m, { st: 'slow', turns: 99 }, [], true);
  assert.ok(count() < before, `${count()} < ${before}`);
});
t('停止はターンを失い、モンスターには続けて効かない', () => {
  const s = E.createBattle('hibashiri', 1, 5); const m = E.monster(s); const ev = [];
  assert.ok(E.addStatus(s, m, m, { st: 'stop', turns: 1 }, ev));
  assert.ok(E.has(m, 'stopResist'));
  m.st = m.st.filter((x) => x.id !== 'stop');
  assert.strictEqual(E.addStatus(s, m, m, { st: 'stop', turns: 1 }, ev), false);
  m.st = [{ id: 'stop', turns: 1 }]; m.gauge = 999; for (const h of E.hunters(s)) h.gauge = 0;
  const e2 = E.start(s); assert.ok(e2.some((e) => e.t === 'skip' && e.u === 'm'));
});
t('毒は最大体力の割合ダメージ', () => {
  const s = E.createBattle('hibashiri', 1, 5); const h = E.hunters(s)[0];
  h.st = [{ id: 'poison', turns: 2 }]; h.gauge = 999; for (const u of s.units) if (u !== h) u.gauge = 0;
  const ev = E.start(s); const p = ev.find((e) => e.t === 'poison');
  assert.strictEqual(p.v, Math.round(h.maxHp * 0.06)); assert.strictEqual(s.turn, h.id);
});
t('妨害無効は妨害をはじき、治療は妨害だけ消す', () => {
  const s = E.createBattle('hibashiri', 1, 5); const h = E.hunters(s)[0]; const ev = [];
  E.addStatus(s, h, h, { st: 'immune', turns: 2 }, ev);
  assert.strictEqual(E.addStatus(s, h, h, { st: 'slow', turns: 2 }, ev), false);
  assert.ok(ev.some((e) => e.t === 'resist'));
});
t('自分の最大体力ぶん回復・攻撃力ぶん味方回復', () => {
  const s = E.createBattle('hibashiri', 1, 5); E.start(s);
  const sh = E.unit(s, 'h4'), mi = E.unit(s, 'h1'), ka = E.unit(s, 'h0');
  sh.hp = 100; s.turn = 'h4'; s.phase = 'input';
  let r = E.act(s, 'h4', 2, null); assert.strictEqual(r.events.find((e) => e.t === 'heal').v, Math.round(sh.maxHp * 0.3));
  ka.hp = 100; s.turn = 'h1'; s.phase = 'input'; mi.st = [];
  r = E.act(s, 'h1', 1, 'h0'); assert.strictEqual(r.events.find((e) => e.t === 'heal' && e.u === 'h0').v, Math.round(mi.atk * 2.0));
});
t('部位を壊すと停止して防御が下がる', () => {
  const s = E.createBattle('hibashiri', 1, 5); E.start(s); const m = E.monster(s);
  m.parts[0].hp = 1; m.st = [];
  const r = E.act(s, s.turn, 0, 'p0');
  assert.ok(r.events.some((e) => e.t === 'break')); assert.ok(m.parts[0].broken); assert.ok(E.has(m, 'defDown'));
});
t('引きつけ中は単体攻撃がその人に向く', () => {
  const s = E.createBattle('hibashiri', 1, 11); E.start(s); const sh = E.unit(s, 'h4');
  s.turn = 'h4'; s.phase = 'input'; const r = E.act(s, 'h4', 1, null); assert.ok(r.ok);
  for (let i = 0; i < 12 && s.phase !== 'over'; i++) {
    const rr = E.act(s, s.turn, 0, 'm');
    for (const e of rr.events) if (e.t === 'skill' && e.u === 'm' && e.targets.length === 1 && E.has(sh, 'taunt')) assert.strictEqual(e.targets[0], 'h4');
  }
});
t('予告した大技は次のターンに来る', () => {
  let seen = 0;
  for (let seed = 1; seed < 30 && !seen; seed++) {
    const s = E.createBattle('hibashiri', 1, seed); let ev = E.start(s);
    for (let i = 0; i < 80 && s.phase !== 'over'; i++) {
      const k = ev.findIndex((e) => e.t === 'telegraph');
      if (k >= 0) { seen++; assert.ok(E.monster(s).charging >= 0 || ev.slice(k).some((e) => e.t === 'skill' && e.u === 'm')); break; }
      ev = E.act(s, s.turn, 0, 'm').events;
    }
  }
  assert.ok(seen);
});
t('ブラウザへ送る形に、内部の数値（種など）が入らない', () => {
  const s = E.createBattle('raizen', 2, 9); E.start(s); const v = E.view(s);
  assert.strictEqual(v.seed, undefined); assert.strictEqual(v.units[0].atk, undefined);
  assert.ok(v.order.length > 0); assert.ok(E.defs().hunters.kagari.skills.length === 4);
});

console.log('\n--- バランス（自動対戦 各200戦） ---');
for (const q of Object.keys(E.QUESTS)) {
  for (const [label, pol] of [['通常攻撃だけ', null], ['かしこく', smart]]) {
    let w = 0, acts = 0, mn = 1e9, mx = 0;
    for (let seed = 1; seed <= 200; seed++) { const r = play(q, seed * 31, pol); if (r.win) w++; acts += r.acts; mn = Math.min(mn, r.acts); mx = Math.max(mx, r.acts); }
    console.log(`${E.QUESTS[q].name.padEnd(6, '　')} ${label.padEnd(6, '　')} 勝率 ${String(w / 2).padStart(5)}%  操作回数 平均 ${Math.round(acts / 200)} (${mn}〜${mx})`);
  }
}
console.log(`\n${n} tests passed`);
