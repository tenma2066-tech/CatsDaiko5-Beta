'use strict';
/*
 * 狩りバトルのルール（サーバーが正解を持つ。ブラウザは結果を見せるだけ）。
 *
 * - 速さで行動順が決まるターン制。各ユニットは「行動ゲージ」を速さぶんずつためて、1000 に届いた順に動く。
 * - 属性は 赤→緑→青→赤 の三すくみと、黄⇔紫 の相互有利。有利 ×1.3 / 不利 ×0.75。
 * - 技のタイプ: 物理(phys) / 特殊(spec) / 防御(guard) / 回復・治療(heal) / 妨害(debuff)
 * - 状態: 攻撃力上昇・低下 / 鈍足 / 停止 / 毒 / 妨害無効 / 守り / 引きつけ / 防御低下 / 停止耐性
 * - モンスターは部位を持つ。部位を壊すと 1 ターン停止し、防御が下がる。
 * 乱数は state.seed から作るので、同じ入力なら同じ結果になる（テストできる）。
 */

const ELEMENTS = ['red', 'green', 'blue', 'yellow', 'purple'];
const EL_NAME = { red: '赤', green: '緑', blue: '青', yellow: '黄', purple: '紫' };
const BEATS = { red: 'green', green: 'blue', blue: 'red', yellow: 'purple', purple: 'yellow' };
const ADV = 1.3, DIS = 0.75;

function elemMul(a, d) {
  if (BEATS[a] === d) return ADV;
  if (BEATS[d] === a) return DIS;
  return 1;
}

/* ---------- 状態（バフ・デバフ） ---------- */
const STATUS = {
  atkUp:      { name: '攻撃力上昇', kind: 'buff' },
  atkDown:    { name: '攻撃力低下', kind: 'debuff' },
  slow:       { name: '鈍足',       kind: 'debuff' },   // 速さ ×0.6（ターンが回ってきにくい）
  stop:       { name: '停止',       kind: 'debuff' },   // 次の自分のターンを失う
  poison:     { name: '毒',         kind: 'debuff' },   // ターン開始時に最大体力の割合ダメージ
  defDown:    { name: '防御低下',   kind: 'debuff' },
  immune:     { name: '妨害無効',   kind: 'buff' },
  guard:      { name: '守り',       kind: 'buff' },     // 受けるダメージ ×val
  taunt:      { name: '引きつけ',   kind: 'buff' },     // 単体攻撃を自分に向ける
  stopResist: { name: '停止耐性',   kind: 'buff' },
};
const ATK_UP = 1.3, ATK_DOWN = 0.7, SLOW = 0.6, DEF_DOWN = 0.7;

/* ---------- ハンター（5色・役割ちがい） ----------
 * 動作の種類:
 *   選べるもの … normal(通常攻撃) / ability(アビリティ) / special(必殺技) / ultimate(奥義)
 *   選べないもの … set(セットアビリティ＝いつも効いている) / cond(条件アビリティ＝条件を満たすと自動で発動)
 * 技: type / target(enemy|ally|self|allies) / power(攻撃力の倍率) / cd(使ったあと待つターン)
 */
const HUNTERS = {
  kagari: {
    name: 'カガリ', el: 'red', role: '物理アタッカー', hp: 1050, atk: 165, def: 55, res: 40, spd: 108,
    set: { name: '闘志', desc: '会心が出やすい（会心率 +15%）。' },
    cond: { name: '逆境', desc: '体力が半分以下になると、攻撃力上昇（3ターン）。1回の狩りで1度だけ。' },
    skills: [
      { kind: 'normal', name: '斬撃', type: 'phys', target: 'enemy', power: 1.0, cd: 0, desc: '敵に物理ダメージ。' },
      { kind: 'ability', name: '鼓舞の炎', type: 'guard', target: 'self', power: 0, cd: 3, fx: [{ st: 'atkUp', turns: 3 }], quick: true,
        desc: '自分の攻撃力を上げる（3ターン）。すぐもう一度動ける。' },
      { kind: 'special', name: '烈火斬', type: 'phys', target: 'enemy', power: 2.5, cd: 3, desc: '敵に大きな物理ダメージ。' },
      { kind: 'ultimate', name: '紅蓮乱舞', type: 'phys', target: 'enemy', power: 4.4, burst: true, desc: '敵に特大の物理ダメージ。' },
    ],
  },
  minamo: {
    name: 'ミナモ', el: 'blue', role: '回復・治療', hp: 950, atk: 120, def: 45, res: 70, spd: 112,
    set: { name: '水の加護', desc: '自分の回復の量が 20% 増える。' },
    cond: { name: '救いの手', desc: '味方の体力が 30% を切ると、すぐに回復する。1回の狩りで2度まで。' },
    skills: [
      { kind: 'normal', name: '水弾', type: 'spec', target: 'enemy', power: 0.9, cd: 0, desc: '敵に特殊ダメージ。' },
      { kind: 'ability', name: '癒しの雫', type: 'heal', target: 'ally', power: 0, cd: 1, heal: { atkPct: 1.8 }, desc: '味方1人を、自分の攻撃力の180%ぶん回復。' },
      { kind: 'special', name: '清めの水', type: 'heal', target: 'allies', power: 0, cd: 3, heal: { atkPct: 0.7 }, cleanse: true,
        desc: '味方全員の妨害を治し、攻撃力の70%ぶん回復。' },
      { kind: 'ultimate', name: '慈雨', type: 'heal', target: 'allies', power: 0, burst: true, heal: { atkPct: 1.5 }, cleanse: true, revive: 0.3,
        fx: [{ st: 'immune', turns: 2 }], desc: '味方全員を大きく回復し、倒れた味方を起こす。妨害無効（2ターン）。' },
    ],
  },
  touka: {
    name: 'トウカ', el: 'green', role: '妨害', hp: 980, atk: 135, def: 50, res: 55, spd: 122,
    set: { name: '狩人の目', desc: '妨害がかかっている敵へのダメージが 20% 増える。' },
    cond: { name: '追い撃ち', desc: 'だれかが部位を壊すと、すぐに追加で攻撃する。' },
    skills: [
      { kind: 'normal', name: '早撃ち', type: 'phys', target: 'enemy', power: 0.95, cd: 0, desc: '敵に物理ダメージ。' },
      { kind: 'ability', name: '絡み蔦', type: 'debuff', target: 'enemy', power: 0.8, cd: 2, fx: [{ st: 'slow', turns: 3 }], desc: '敵を鈍足にする（3ターン）。' },
      { kind: 'special', name: '毒牙の矢', type: 'debuff', target: 'enemy', power: 1.3, cd: 3, fx: [{ st: 'poison', turns: 3 }], desc: '敵にダメージを与え、毒にする（3ターン・最大体力の割合ダメージ）。' },
      { kind: 'ultimate', name: '茨の檻', type: 'debuff', target: 'enemy', power: 2.4, burst: true, fx: [{ st: 'stop', turns: 1 }], desc: '敵にダメージを与え、停止させる。' },
    ],
  },
  raiga: {
    name: 'ライガ', el: 'yellow', role: '特殊アタッカー', hp: 900, atk: 170, def: 40, res: 60, spd: 116,
    set: { name: '帯電', desc: '特殊攻撃のダメージが 12% 増える。' },
    cond: { name: '連鎖雷', desc: '会心が出ると、次の自分の番が早く回ってくる。' },
    skills: [
      { kind: 'normal', name: '雷撃', type: 'spec', target: 'enemy', power: 1.0, cd: 0, desc: '敵に特殊ダメージ。' },
      { kind: 'ability', name: '萎縮の雷', type: 'debuff', target: 'enemy', power: 0.9, cd: 3, fx: [{ st: 'atkDown', turns: 3 }], desc: '敵の攻撃力を下げる（3ターン）。' },
      { kind: 'special', name: '雷槍', type: 'spec', target: 'enemy', power: 2.4, cd: 3, desc: '敵に大きな特殊ダメージ。' },
      { kind: 'ultimate', name: '雷帝招来', type: 'spec', target: 'enemy', power: 4.2, burst: true, desc: '敵に特大の特殊ダメージ。' },
    ],
  },
  shion: {
    name: 'シオン', el: 'purple', role: '防御', hp: 1450, atk: 110, def: 95, res: 80, spd: 98,
    set: { name: '鉄壁', desc: '受けるダメージが 10% 減る。' },
    cond: { name: '不屈', desc: '倒れるダメージを受けても、体力1で耐える。1回の狩りで1度だけ。' },
    skills: [
      { kind: 'normal', name: '盾打ち', type: 'phys', target: 'enemy', power: 0.9, cd: 0, desc: '敵に物理ダメージ。' },
      { kind: 'ability', name: '守護の構え', type: 'guard', target: 'self', power: 0, cd: 2, fx: [{ st: 'taunt', turns: 2 }, { st: 'guard', turns: 2, val: 0.55 }],
        desc: '敵の単体攻撃を自分に引きつけ、受けるダメージを減らす（2ターン）。' },
      { kind: 'special', name: '生命の鼓動', type: 'heal', target: 'self', power: 0, cd: 3, heal: { maxHpPct: 0.3 }, desc: '自分の最大体力の30%を回復。' },
      { kind: 'ultimate', name: '不落の陣', type: 'guard', target: 'allies', power: 0, burst: true, fx: [{ st: 'guard', turns: 2, val: 0.6 }],
        desc: '味方全員の受けるダメージを減らす（2ターン）。' },
    ],
  },
};
const KIND_NAME = { normal: '通常攻撃', ability: 'アビリティ', special: '必殺技', ultimate: '奥義', set: 'セットアビリティ', cond: '条件アビリティ' };
const PARTY = ['kagari', 'minamo', 'touka', 'raiga', 'shion'];

/* ---------- モンスター ---------- */
// 技: target(one|all) / w(選ばれやすさ) / charge(予告してから次のターンに撃つ大技)
const QUESTS = {
  hibashiri: {
    name: 'ヒバシリ', title: '炎狼の狩猟', star: 1, el: 'red', shape: 'wolf',
    hp: 6800, atk: 165, def: 55, res: 45, spd: 110, poisonPct: 0.025,
    parts: [{ name: 'たてがみ', hp: 1300 }, { name: '前足の爪', hp: 1100 }],
    skills: [
      { name: '噛みつき', type: 'phys', target: 'one', power: 1.1, w: 4 },
      { name: '連続突進', type: 'phys', target: 'one', power: 0.75, hits: 2, w: 3, cd: 1 },
      { name: '火炎の息', type: 'spec', target: 'all', power: 0.7, w: 2, cd: 2 },
      { name: '業火の咆哮', type: 'spec', target: 'all', power: 1.35, w: 2, cd: 4, charge: true, tell: '大きく息を吸い込んでいる…（次のターンに全体へ大技）' },
    ],
  },
  drogarm: {
    name: 'ドロガルム', title: '泥沼の大蜥蜴', star: 2, el: 'green', shape: 'lizard',
    hp: 9200, atk: 195, def: 70, res: 55, spd: 102, poisonPct: 0.02,
    parts: [{ name: '背中のこぶ', hp: 1700 }, { name: '尻尾', hp: 1500 }],
    skills: [
      { name: '尻尾なぎ払い', type: 'phys', target: 'all', power: 0.65, w: 3, cd: 1 },
      { name: '踏みつけ', type: 'phys', target: 'one', power: 1.25, w: 4 },
      { name: '毒泥', type: 'spec', target: 'one', power: 0.7, w: 3, cd: 2, fx: [{ st: 'poison', turns: 3 }] },
      { name: '泥しぶき', type: 'spec', target: 'all', power: 0.45, w: 2, cd: 3, fx: [{ st: 'slow', turns: 2 }] },
      { name: '大地沈め', type: 'phys', target: 'all', power: 1.4, w: 2, cd: 4, charge: true, tell: '泥の中へ深く沈みこんだ…（次のターンに全体へ大技）' },
    ],
  },
  raizen: {
    name: 'ライゼン', title: '雷角の覇獣', star: 3, el: 'yellow', shape: 'beast',
    hp: 12500, atk: 225, def: 75, res: 70, spd: 114, poisonPct: 0.015,
    parts: [{ name: '雷角', hp: 2200 }, { name: '蓄電器官', hp: 2000 }],
    skills: [
      { name: '角突き', type: 'phys', target: 'one', power: 1.3, w: 4 },
      { name: '放電', type: 'spec', target: 'all', power: 0.7, w: 3, cd: 1 },
      { name: '麻痺の雷', type: 'spec', target: 'one', power: 0.8, w: 2, cd: 3, fx: [{ st: 'stop', turns: 1 }] },
      { name: '威圧の咆哮', type: 'debuff', target: 'all', power: 0, w: 2, cd: 4, fx: [{ st: 'atkDown', turns: 2 }] },
      { name: '天雷', type: 'spec', target: 'all', power: 1.5, w: 2, cd: 4, charge: true, tell: '角に雷が集まっていく…（次のターンに全体へ大技）' },
    ],
  },
};

/* ---------- 乱数（mulberry32） ---------- */
function rnd(state) {
  let t = (state.seed = (state.seed + 0x6D2B79F5) | 0);
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}

/* ---------- 戦いを作る ---------- */
function createBattle(questKey, nPlayers, seed) {
  const q = QUESTS[questKey];
  if (!q) throw new Error('unknown quest');
  const n = Math.max(1, Math.min(PARTY.length, nPlayers | 0 || 1));
  const units = PARTY.map((key, i) => {
    const h = HUNTERS[key];
    return { id: 'h' + i, side: 'h', key, name: h.name, el: h.el, role: h.role, maxHp: h.hp, hp: h.hp,
             atk: h.atk, def: h.def, res: h.res, spd: h.spd, gauge: 0, burst: 0, cds: [0, 0, 0, 0], st: [], owner: i % n,
             condLeft: key === 'minamo' ? 2 : 1, haste: 0 };
  });
  units.push({ id: 'm', side: 'm', key: questKey, name: q.name, el: q.el, shape: q.shape, maxHp: q.hp, hp: q.hp,
               atk: q.atk, def: q.def, res: q.res, spd: q.spd, gauge: 0, cds: q.skills.map(() => 0), st: [],
               parts: q.parts.map((p, i) => ({ id: 'p' + i, name: p.name, hp: p.hp, maxHp: p.hp, broken: false })),
               rage: false, charging: -1 });
  const state = { quest: questKey, seed: (seed | 0) || 1, units, turn: null, phase: 'input', win: null, turns: 0, log: 0 };
  // 最初の並びが毎回同じにならないよう、ゲージを少しばらす
  for (const u of units) u.gauge = Math.floor(rnd(state) * 250);
  return state;
}

const unit = (s, id) => s.units.find((u) => u.id === id);
const alive = (u) => u.hp > 0;
const hunters = (s) => s.units.filter((u) => u.side === 'h');
const monster = (s) => unit(s, 'm');
const has = (u, st) => u.st.find((x) => x.id === st);
const skillsOf = (u) => (u.side === 'h' ? HUNTERS[u.key].skills : QUESTS[u.key].skills);

function effSpd(u) { return u.spd * (has(u, 'slow') ? SLOW : 1) * (u.rage ? 1.12 : 1); }
function effAtk(u) { return u.atk * (has(u, 'atkUp') ? ATK_UP : 1) * (has(u, 'atkDown') ? ATK_DOWN : 1) * (u.rage ? 1.2 : 1); }
function effDef(u, type) { return (type === 'spec' ? u.res : u.def) * (has(u, 'defDown') ? DEF_DOWN : 1); }

/* 次に動くユニットを決めて、全員のゲージを進める */
function advance(s) {
  let best = null, bt = Infinity;
  for (const u of s.units) {
    if (!alive(u)) continue;
    const t = (1000 - u.gauge) / effSpd(u);
    if (t < bt - 1e-9) { bt = t; best = u; }
  }
  if (!best) return null;
  for (const u of s.units) if (alive(u)) u.gauge += effSpd(u) * bt;
  best.gauge = 1000;
  return best;
}

/* これから動く順番（画面の行動順の表示用）。状態は変えない */
function timeline(s, n) {
  const g = s.units.filter(alive).map((u) => ({ id: u.id, gauge: u.gauge, spd: effSpd(u) }));
  const out = [];
  if (s.turn && s.phase === 'input') { out.push(s.turn); const c = g.find((x) => x.id === s.turn); if (c) c.gauge = 0; }
  while (out.length < n && g.length) {
    let best = null, bt = Infinity;
    for (const u of g) { const t = (1000 - u.gauge) / u.spd; if (t < bt - 1e-9) { bt = t; best = u; } }
    for (const u of g) u.gauge += u.spd * bt;
    best.gauge = 0; out.push(best.id);
  }
  return out;
}

/* ---------- 状態を付ける・治す ---------- */
function addStatus(s, src, tgt, fx, ev, force) {
  const def = STATUS[fx.st];
  if (!alive(tgt)) return false;
  if (def.kind === 'debuff' && !force) {
    if (has(tgt, 'immune')) { ev.push({ t: 'resist', u: tgt.id, st: fx.st, why: 'immune' }); return false; }
    if (fx.st === 'stop' && has(tgt, 'stopResist')) { ev.push({ t: 'resist', u: tgt.id, st: fx.st, why: 'resist' }); return false; }
  }
  const cur = has(tgt, fx.st);
  const turns = fx.turns | 0;
  if (cur) { cur.turns = Math.max(cur.turns, turns); if (fx.val != null) cur.val = Math.min(cur.val == null ? 1 : cur.val, fx.val); }
  else tgt.st.push({ id: fx.st, turns, val: fx.val == null ? null : fx.val });
  ev.push({ t: 'st', u: tgt.id, st: fx.st, turns });
  // 大型モンスターは、止められたあとしばらく止まらない（ずっと止め続けられないように）
  if (fx.st === 'stop' && tgt.side === 'm') addStatus(s, src, tgt, { st: 'stopResist', turns: 4 }, ev, true);
  return true;
}

function cleanse(tgt, ev) {
  const before = tgt.st.length;
  tgt.st = tgt.st.filter((x) => STATUS[x.id].kind !== 'debuff');
  if (tgt.st.length !== before) ev.push({ t: 'cleanse', u: tgt.id });
}

/* ---------- ダメージ・回復 ---------- */
function dealDamage(s, src, tgt, sk, ev, partId) {
  if (!alive(tgt)) return 0;
  const crit = rnd(s) < (src.key === 'kagari' ? 0.25 : 0.1);                       // セット: 闘志
  const em = elemMul(src.el, tgt.el);
  let v = effAtk(src) * sk.power * (100 / (100 + effDef(tgt, sk.type))) * em * (0.95 + rnd(s) * 0.1) * (crit ? 1.5 : 1);
  if (src.key === 'raiga' && sk.type === 'spec') v *= 1.12;                         // セット: 帯電
  if (src.key === 'touka' && tgt.st.some((x) => STATUS[x.id].kind === 'debuff')) v *= 1.2;   // セット: 狩人の目
  if (tgt.key === 'shion') v *= 0.9;                                                // セット: 鉄壁
  const g = has(tgt, 'guard');
  if (g) v *= g.val == null ? 0.6 : g.val;
  v = Math.max(1, Math.round(v));
  const before = tgt.hp;
  tgt.hp = Math.max(0, tgt.hp - v);
  let endure = false;
  if (tgt.key === 'shion' && tgt.hp === 0 && tgt.condLeft > 0) { tgt.condLeft--; tgt.hp = 1; endure = true; }   // 条件: 不屈
  const e = { t: 'dmg', u: tgt.id, src: src.id, v: before - tgt.hp, crit, em: em > 1 ? 'adv' : em < 1 ? 'dis' : null, ty: sk.type };
  if (g) e.guard = true;
  if (partId) e.part = partId;
  ev.push(e);
  if (endure) ev.push({ t: 'cond', u: tgt.id, name: HUNTERS.shion.cond.name });
  if (crit && src.key === 'raiga') { src.haste = 400; ev.push({ t: 'cond', u: src.id, name: HUNTERS.raiga.cond.name }); }   // 条件: 連鎖雷
  if (tgt.side === 'h') {
    tgt.burst = Math.min(100, tgt.burst + 10);
    if (alive(tgt)) {
      if (tgt.key === 'kagari' && tgt.condLeft > 0 && tgt.hp <= tgt.maxHp * 0.5) {   // 条件: 逆境
        tgt.condLeft--; ev.push({ t: 'cond', u: tgt.id, name: HUNTERS.kagari.cond.name });
        addStatus(s, tgt, tgt, { st: 'atkUp', turns: 3 }, ev);
      }
      const mi = hunters(s).find((x) => x.key === 'minamo');
      if (mi && alive(mi) && mi.condLeft > 0 && tgt.hp < tgt.maxHp * 0.3) {           // 条件: 救いの手
        mi.condLeft--; ev.push({ t: 'cond', u: mi.id, name: HUNTERS.minamo.cond.name });
        heal(tgt, effAtk(mi) * 1.2 * 1.2, ev);
      }
    }
  }
  if (tgt.side === 'm') {
    if (partId) {
      const p = tgt.parts.find((x) => x.id === partId);
      if (p && !p.broken) {
        p.hp = Math.max(0, p.hp - v);
        if (p.hp === 0) {
          p.broken = true;
          ev.push({ t: 'break', part: p.id, name: p.name, by: src.id });
          if (alive(tgt)) {
            addStatus(s, src, tgt, { st: 'defDown', turns: 99 }, ev, true);
            tgt.st = tgt.st.filter((x) => x.id !== 'stopResist');
            addStatus(s, src, tgt, { st: 'stop', turns: 1 }, ev, true);
            tgt.charging = -1;   // 予告していた大技も止まる
            const to = hunters(s).find((x) => x.key === 'touka');
            if (to && alive(to) && !s.chasing) {                                      // 条件: 追い撃ち
              s.chasing = true;
              ev.push({ t: 'cond', u: to.id, name: HUNTERS.touka.cond.name });
              dealDamage(s, to, tgt, { power: 1.5, type: 'phys' }, ev, null);
              s.chasing = false;
            }
          }
        }
      }
    }
    if (!tgt.rage && alive(tgt) && tgt.hp <= tgt.maxHp * 0.5) { tgt.rage = true; ev.push({ t: 'rage', u: tgt.id }); }
  }
  if (!alive(tgt) && !ev.some((x) => x.t === 'ko' && x.u === tgt.id && x._n === s.turns)) { tgt.st = []; tgt.gauge = 0; ev.push({ t: 'ko', u: tgt.id, _n: s.turns }); }
  return v;
}

function heal(tgt, v, ev) {
  if (!alive(tgt)) return;
  v = Math.max(1, Math.round(v));
  const real = Math.min(v, tgt.maxHp - tgt.hp);
  tgt.hp += real;
  ev.push({ t: 'heal', u: tgt.id, v: real });
}

/* ---------- 技を使う ---------- */
function useSkill(s, u, si, targetId, ev) {
  const sk = skillsOf(u)[si];
  const m = monster(s);
  let targets = [], partId = null;
  if (u.side === 'h') {
    if (sk.target === 'enemy') {
      targets = [m];
      const p = m.parts.find((x) => x.id === targetId);
      if (p && !p.broken) partId = p.id;
    } else if (sk.target === 'self') targets = [u];
    else if (sk.target === 'ally') targets = [unit(s, targetId)];
    else if (sk.target === 'allies') targets = hunters(s);
  } else {
    const liveH = hunters(s).filter(alive);
    if (sk.target === 'all') targets = liveH;
    else {
      const t = liveH.find((x) => has(x, 'taunt')) || liveH[Math.floor(rnd(s) * liveH.length)];
      targets = t ? [t] : [];
    }
  }
  ev.push({ t: 'skill', u: u.id, si, name: sk.name, type: sk.type, kind: sk.kind || 'normal', burst: !!sk.burst,
            targets: targets.map((x) => x.id), part: partId });
  if (sk.revive) for (const t of targets) if (!alive(t)) { t.hp = Math.round(t.maxHp * sk.revive); t.gauge = 0; ev.push({ t: 'revive', u: t.id, v: t.hp }); }
  for (const t of targets) {
    if (sk.power > 0) for (let h = 0; h < (sk.hits || 1); h++) dealDamage(s, u, t, sk, ev, partId);
    if (sk.cleanse) cleanse(t, ev);
    if (sk.heal) heal(t, ((sk.heal.atkPct ? effAtk(u) * sk.heal.atkPct : 0) + (sk.heal.maxHpPct ? t.maxHp * sk.heal.maxHpPct : 0)) * (u.key === 'minamo' ? 1.2 : 1), ev);   // セット: 水の加護
    if (sk.fx) for (const fx of sk.fx) addStatus(s, u, t, fx, ev);
  }
  if (u.side === 'h') {
    if (sk.burst) u.burst = 0; else u.burst = Math.min(100, u.burst + 25);
  }
  u.cds[si] = sk.cd ? sk.cd + 1 : 0;   // このターンの終わりに 1 減るので +1
  return sk;
}

/* ターンの終わり: 待ちターンと状態の残りを減らす */
function endTurn(s, u, quick) {
  for (let i = 0; i < u.cds.length; i++) if (u.cds[i] > 0) u.cds[i]--;
  if (alive(u)) {
    for (const x of u.st) x.turns--;
    u.st = u.st.filter((x) => x.turns > 0);
    u.gauge = quick ? 1000 : (u.haste || 0);   // quick の技は、すぐもう一度動ける。連鎖雷は次の番が早まる
    u.haste = 0;
  }
  s.turns++;
}

function checkEnd(s, ev) {
  if (s.phase === 'over') return true;
  const m = monster(s);
  if (!alive(m)) { s.phase = 'over'; s.win = true; s.turn = null; ev.push({ t: 'end', win: true }); return true; }
  if (!hunters(s).some(alive)) { s.phase = 'over'; s.win = false; s.turn = null; ev.push({ t: 'end', win: false }); return true; }
  return false;
}

/* モンスターの行動を選ぶ */
function monsterAct(s, m, ev) {
  const sks = skillsOf(m);
  if (m.charging >= 0) { const si = m.charging; m.charging = -1; useSkill(s, m, si, null, ev); return; }
  const pool = [];
  sks.forEach((sk, i) => { if (!m.cds[i]) for (let k = 0; k < sk.w; k++) pool.push(i); });
  const si = pool.length ? pool[Math.floor(rnd(s) * pool.length)] : 0;
  const sk = sks[si];
  if (sk.charge) {
    m.charging = si;
    m.cds[si] = sk.cd + 1;
    ev.push({ t: 'telegraph', u: m.id, name: sk.name, text: sk.tell });
    return;
  }
  useSkill(s, m, si, null, ev);
}

/*
 * 次に人が操作するところまで進める。
 * 毒・停止・モンスターの行動はここで自動で処理し、起きたことを ev に並べる。
 */
function run(s, ev) {
  let guard = 0;
  while (s.phase !== 'over' && guard++ < 200) {
    if (checkEnd(s, ev)) break;
    const u = advance(s);
    if (!u) break;
    ev.push({ t: 'turn', u: u.id });
    // ターンの始まり: 毒
    const po = has(u, 'poison');
    if (po) {
      const pct = u.side === 'm' ? QUESTS[u.key].poisonPct : 0.06;
      const v = Math.max(1, Math.round(u.maxHp * pct));
      u.hp = Math.max(0, u.hp - v);
      ev.push({ t: 'poison', u: u.id, v });
      if (u.side === 'm' && !u.rage && alive(u) && u.hp <= u.maxHp * 0.5) { u.rage = true; ev.push({ t: 'rage', u: u.id }); }
      if (!alive(u)) { u.st = []; u.gauge = 0; ev.push({ t: 'ko', u: u.id }); s.turns++; continue; }
    }
    // 停止: このターンを失う
    if (has(u, 'stop')) { ev.push({ t: 'skip', u: u.id }); endTurn(s, u); continue; }
    if (u.side === 'm') { monsterAct(s, u, ev); endTurn(s, u); continue; }
    s.turn = u.id; s.phase = 'input';
    return;
  }
  checkEnd(s, ev);
}

function start(s) { const ev = []; run(s, ev); return ev; }

/* その技がいま使えるか。使えないなら理由を返す */
function canUse(s, u, si, targetId) {
  const sk = skillsOf(u)[si];
  if (!sk) return '技がありません';
  if (u.cds[si] > 0) return 'まだ使えません';
  if (sk.burst && u.burst < 100) return '奥義ゲージがたまっていません';
  if (sk.target === 'ally') {
    const t = unit(s, targetId);
    if (!t || t.side !== 'h' || !alive(t)) return '対象を選んでください';
  }
  return '';
}

/* ハンターの行動（人が選んだもの）。戻り値 { ok, error, events } */
function act(s, uid, si, targetId) {
  if (s.phase !== 'input' || s.turn !== uid) return { ok: false, error: 'いまは操作できません' };
  const u = unit(s, uid);
  si = si | 0;
  const err = canUse(s, u, si, targetId);
  if (err) return { ok: false, error: err };
  const ev = [];
  const sk = useSkill(s, u, si, targetId, ev);
  endTurn(s, u, !!sk.quick);
  s.turn = null;
  run(s, ev);
  return { ok: true, events: ev };
}

/* 時間切れ・不在のときの自動行動（通常攻撃） */
function autoAct(s) {
  if (s.phase !== 'input' || !s.turn) return { ok: false, error: 'no turn' };
  return act(s, s.turn, 0, 'm');
}

/* ブラウザへ送る形（技の説明などの決まった情報は defs で1回だけ送る） */
function view(s) {
  return {
    quest: s.quest, phase: s.phase, win: s.win, turn: s.turn, turns: s.turns,
    order: timeline(s, 8),
    units: s.units.map((u) => ({
      id: u.id, side: u.side, key: u.key, name: u.name, el: u.el, role: u.role, shape: u.shape,
      hp: u.hp, maxHp: u.maxHp, burst: u.burst, cds: u.cds, st: u.st.map((x) => ({ id: x.id, turns: x.turns })),
      owner: u.owner, parts: u.parts, rage: u.rage, charging: u.charging >= 0,
    })),
  };
}

function defs() {
  const pick = (sk) => ({ kind: sk.kind, name: sk.name, type: sk.type, target: sk.target, cd: sk.cd || 0, burst: !!sk.burst, desc: sk.desc || '' });
  const h = {};
  for (const k of PARTY) h[k] = { name: HUNTERS[k].name, el: HUNTERS[k].el, role: HUNTERS[k].role, set: HUNTERS[k].set, cond: HUNTERS[k].cond, skills: HUNTERS[k].skills.map(pick) };
  const q = {};
  for (const k of Object.keys(QUESTS)) q[k] = { name: QUESTS[k].name, title: QUESTS[k].title, star: QUESTS[k].star, el: QUESTS[k].el, shape: QUESTS[k].shape };
  const st = {};
  for (const k of Object.keys(STATUS)) st[k] = STATUS[k];
  return { hunters: h, quests: q, status: st, elName: EL_NAME, party: PARTY, kindName: KIND_NAME };
}

module.exports = { KIND_NAME, ELEMENTS, EL_NAME, HUNTERS, QUESTS, PARTY, STATUS, elemMul, createBattle, start, act, autoAct, canUse,
                   view, defs, timeline, unit, monster, hunters, has, effSpd, effAtk, skillsOf, addStatus, rnd };
