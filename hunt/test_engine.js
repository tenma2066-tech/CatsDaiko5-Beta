'use strict';
// node test_engine.js … ルールのテストと、オート（AI）で全ステージを遊ばせたバランス確認
const assert = require('assert');
const E = require('./engine.js');
let n = 0;
const t = (name, fn) => { fn(); n++; console.log('ok  ' + name); };
const R5 = E.R_KEYS.map((key) => ({ key, owner: 0 }));
const party = (keys) => keys.map((key) => ({ key, owner: 0 }));
function play(stage, team, seed, maxActs = 600) {
  const s = E.createBattle(stage, team, seed); E.start(s);
  let acts = 0;
  while (s.phase !== 'over' && acts < maxActs) { const r = E.aiTurn(s); assert.ok(r.ok, r.error); acts++; }
  return { s, acts };
}
// テスト用: いまの番をハンター uid にする
function turnTo(s, uid) { s.turn = uid; s.phase = 'input'; s.moved = false; }

t('属性のすくみ', () => {
  assert.strictEqual(E.elemMul('red', 'green'), 1.3); assert.strictEqual(E.elemMul('green', 'red'), 0.75);
  assert.strictEqual(E.elemMul('blue', 'red'), 1.3); assert.strictEqual(E.elemMul('yellow', 'purple'), 1.3);
  assert.strictEqual(E.elemMul('purple', 'yellow'), 1.3); assert.strictEqual(E.elemMul('red', 'yellow'), 1);
});
t('キャラは R5・SR11・UR8。どのキャラも 通常攻撃・アビリティ・必殺技・奥義 とセット・条件を持つ', () => {
  const by = { R: 0, SR: 0, UR: 0 };
  for (const [k, c] of Object.entries(E.CHARACTERS)) {
    by[c.rarity]++;
    assert.deepStrictEqual(c.skills.map((x) => x.kind), ['normal', 'ability', 'special', 'ultimate'], k);
    assert.ok(c.set.name && c.cond.name && c.cond.trig, k);
    for (const sk of c.skills) if (sk.target === 'enemy' || sk.target === 'enemyArea' || sk.target === 'ally') assert.ok(sk.range >= 1, k + sk.name);
  }
  assert.deepStrictEqual(by, { R: 5, SR: 11, UR: 8 });
  assert.deepStrictEqual(E.R_KEYS, ['kagari', 'minamo', 'touka', 'raiga', 'shion']);
});
t('ステージ: 草原5つ＋特別2つ。敵と岩は戦場の中で重ならない', () => {
  assert.deepStrictEqual(E.STAGE_ORDER, ['1-1', '1-2', '1-3', '1-4', '1-5', 'ex1', 'ex2']);
  for (const k of E.STAGE_ORDER) {
    const s = E.createBattle(k, R5, 1), seen = new Set(s.blocks.map(([x, z]) => x + ',' + z));
    for (const u of s.units) for (let i = 0; i < u.size; i++) for (let j = 0; j < u.size; j++) {
      const x = u.x + i, z = u.z + j;
      assert.ok(x >= 0 && z >= 0 && x < E.W && z < E.H, k + ' ' + u.id + ' outside');
      assert.ok(!seen.has(x + ',' + z), k + ' overlap at ' + x + ',' + z); seen.add(x + ',' + z);
    }
  }
  assert.strictEqual(E.prevStage('1-1'), null); assert.strictEqual(E.prevStage('1-3'), '1-2'); assert.strictEqual(E.prevStage('ex1'), '1-5');
});
t('動ける範囲: mov マスまで。岩・敵は通れず、味方の上には止まれない', () => {
  const s = E.createBattle('1-1', R5, 3); const u = E.unit(s, 'h0');
  const R = E.reach(s, u);
  for (const v of R.values()) assert.ok(Math.abs(v.x - u.x) + Math.abs(v.z - u.z) <= u.mov);
  for (const [x, z] of s.blocks) assert.ok(!R.has(x + ',' + z));
  for (const h of E.hunters(s)) if (h !== u) assert.ok(!R.has(h.x + ',' + h.z));
});
t('動く→技。同じターンに2回は動けない。射程の外は断る', () => {
  const s = E.createBattle('1-1', R5, 3); turnTo(s, 'h2');
  const u = E.unit(s, 'h2');
  assert.strictEqual(E.act(s, 'h2', 0, 'e0').ok, false);                 // 弓でも最初は遠すぎる
  const R = E.reach(s, u); const dest = [...R.values()].find((v) => v.d === 3);
  assert.ok(E.move(s, 'h2', dest.x, dest.z).ok);
  assert.strictEqual(E.move(s, 'h2', u.x, u.z + 1).ok, false);
  assert.strictEqual(E.unit(s, 'h2').x, dest.x);
});
t('射程: 近づけば当たる（大きい敵は近いマスまでの距離）', () => {
  const s = E.createBattle('1-5', R5, 3); const boss = E.unit(s, 'e0'), k = E.unit(s, 'h0');
  k.x = boss.x - 1; k.z = boss.z + 1; turnTo(s, 'h0');
  assert.strictEqual(E.dist(k, boss), 1);
  assert.ok(E.act(s, 'h0', 0, 'e0.p0').ok);
});
t('自分の番でない・奥義ゲージ不足は断る', () => {
  const s = E.createBattle('1-1', R5, 5); E.start(s);
  const other = E.hunters(s).find((u) => u.id !== s.turn);
  assert.strictEqual(E.act(s, other.id, 0, 'e0').ok, false);
  const u = E.unit(s, s.turn); u.x = 4; u.z = 6;
  assert.ok(/奥義/.test(E.targetError(s, u, 3, 'e0')));
});
t('鈍足でターンが回ってきにくくなる', () => {
  const s = E.createBattle('1-1', R5, 5); const m = E.unit(s, 'e0');
  const cnt = () => E.timeline(s, 40).filter((x) => x === 'e0').length;
  const before = cnt(); E.addStatus(s, m, m, { st: 'slow', turns: 99 }, [], true);
  assert.ok(cnt() < before);
});
t('毒は最大体力の割合ダメージ・停止はターンを失う', () => {
  const s = E.createBattle('1-1', R5, 5); const h = E.unit(s, 'h0');
  h.st = [{ id: 'poison', turns: 2 }]; h.gauge = 999; for (const u of s.units) if (u !== h) u.gauge = 0;
  const ev = E.start(s); assert.strictEqual(ev.find((e) => e.t === 'poison').v, Math.round(h.maxHp * 0.06));
  const s2 = E.createBattle('1-1', R5, 5); const m = E.unit(s2, 'e0'); m.st = [{ id: 'stop', turns: 1 }]; m.gauge = 999; for (const u of s2.units) if (u !== m) u.gauge = 0;
  assert.ok(E.start(s2).some((e) => e.t === 'skip' && e.u === 'e0'));
});
t('予告した大技は赤いマスに来る。マスから出れば当たらない', () => {
  let checked = 0;
  for (let seed = 1; seed < 80 && checked < 3; seed++) {
    const s = E.createBattle('1-5', R5, seed); let ev = E.start(s);
    for (let i = 0; i < 200 && s.phase !== 'over'; i++) {
      const tg = ev.find((e) => e.t === 'telegraph');
      if (tg && s.phase === 'input') {
        const danger = new Set(tg.tiles.map(([x, z]) => x + ',' + z));
        // 全員を危ないマスの外へ（テストなので直接動かす）
        const free = []; for (let x = 0; x < E.W; x++) for (let z = 0; z < 2; z++) if (!danger.has(x + ',' + z)) free.push([x, z]);
        E.hunters(s).forEach((h, k) => { [h.x, h.z] = free[k]; });
        let guard = 0;
        while (s.phase === 'input' && guard++ < 40) {
          const r = E.wait(s, s.turn);
          const fire = r.events.find((e) => e.t === 'skill' && e.u === tg.u && e.tiles);
          if (fire) { assert.deepStrictEqual(fire.targets, []); checked++; break; }
        }
        break;
      }
      ev = E.aiTurn(s).events;
    }
  }
  assert.ok(checked >= 1, 'telegraph not observed');
});
t('条件アビリティ: 不屈（体力1で耐える）', () => {
  const s = E.createBattle('1-1', R5, 5); const sh = E.unit(s, 'h4'), e = E.unit(s, 'e0');
  sh.hp = 3; sh.x = 5; sh.z = 6; e.x = 5; e.z = 7; sh.st = [{ id: 'taunt', turns: 9 }];
  e.gauge = 999; for (const u of s.units) if (u !== e) u.gauge = 0;
  const ev = E.start(s);
  const ci = ev.findIndex((x) => x.t === 'cond' && x.u === 'h4'), ki = ev.findIndex((x) => x.t === 'ko' && x.u === 'h4');
  assert.ok(ci >= 0, 'endure not fired'); assert.ok(ki < 0 || ki > ci, 'ko before endure');   // 耐えたあとに別の敵が追撃することはある
});
t('条件アビリティ: 戦いの始め（ソラ）で味方全員が妨害無効', () => {
  const s = E.createBattle('1-1', party(['sora', 'kagari', 'touka']), 2); const ev = E.start(s);
  assert.ok(ev.some((x) => x.t === 'cond'));
  for (const h of E.hunters(s)) assert.ok(E.has(h, 'immune'));
});
t('部位を壊すと停止して防御が下がる。トウカが追い撃ちする', () => {
  const s = E.createBattle('1-5', R5, 5); const b = E.unit(s, 'e0'), k = E.unit(s, 'h0');
  k.x = b.x - 1; k.z = b.z; b.parts[0].hp = 1; turnTo(s, 'h0');
  const r = E.act(s, 'h0', 0, 'e0.p0');
  assert.ok(r.events.some((e) => e.t === 'break')); assert.ok(E.has(b, 'defDown'));
  assert.ok(r.events.some((e) => e.t === 'cond' && e.u === 'h2'));
});
t('重ねるほど少し強くなる（1つにつき +4%）', () => {
  const s = E.createBattle('1-1', [{ key: 'kagari', dup: 4 }], 1);
  assert.strictEqual(E.unit(s, 'h0').maxHp, Math.round(970 * 1.16));
});
t('オートは技か待機を必ず選び、同じ種なら同じ結果', () => {
  const a = play('1-3', R5, 7), b = play('1-3', R5, 7);
  assert.deepStrictEqual([a.s.win, a.acts, a.s.turns], [b.s.win, b.acts, b.s.turns]);
});
t('ブラウザへ送る形に、内部の数値（種・攻撃力）が入らない', () => {
  const s = E.createBattle('1-5', R5, 9); E.start(s); const v = E.view(s);
  assert.strictEqual(v.seed, undefined); assert.strictEqual(v.units[0].atk, undefined);
  assert.ok(v.reach.length > 0 && v.order.length > 0);
});

console.log('\n--- バランス（オート同士・各60戦の勝率と平均操作回数） ---');
const TEAMS = {
  'R5人': E.R_KEYS,
  'SR中心': ['homura', 'yomogi', 'aoi', 'kohaku', 'yukari'],
  'UR5人': ['akatsuki', 'sora', 'midori', 'kirara', 'shigure'],
  '新SR　': ['tsubaki', 'hisui', 'kogane', 'sumire', 'ruri'],
  '新UR＋': ['enju', 'tokiwa', 'yoru', 'kagari', 'shion'],
};
for (const [label, keys] of Object.entries(TEAMS)) {
  const row = [];
  for (const st of E.STAGE_ORDER) {
    let w = 0, acts = 0;
    for (let seed = 1; seed <= 60; seed++) { const r = play(st, party(keys), seed * 97); if (r.s.win) w++; acts += r.acts; }
    row.push(`${st}:${String(Math.round(w / 60 * 100)).padStart(3)}%/${String(Math.round(acts / 60)).padStart(2)}`);
  }
  console.log(label.padEnd(5, '　'), row.join('  '));
}
console.log(`\n${n} tests passed`);
