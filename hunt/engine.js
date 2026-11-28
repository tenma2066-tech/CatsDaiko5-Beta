'use strict';
/*
 * 彩狩のルール（サーバーだけが持つ。ブラウザは結果を見せるだけ）。
 *
 * 戦場はマス目（W×H）。ハンターは自分の番に「動く（任意・1回）→ 行動する（技か待機）」。
 *   - 動ける範囲 … mov マス（上下左右。岩や敵は通れない。味方は通れるが、そこには止まれない）
 *   - 技の射程 … range マス（自分の足元から相手の足元までの上下左右の距離）
 * 行動順は速さで決まる（行動ゲージ方式）。
 * 属性: 赤→緑→青→赤 の三すくみ、黄⇔紫 の相互有利（有利 ×1.3 / 不利 ×0.75）。
 * 動作の種類: 選べる … 通常攻撃 / アビリティ / 必殺技 / 奥義
 *             選べない … セットアビリティ（いつも効く）/ 条件アビリティ（条件で自動発動）
 * 乱数は state.seed から作る（同じ入力なら同じ結果になる＝テストできる）。
 */

const W = 12, H = 10;
const EL_NAME = { red: '赤', green: '緑', blue: '青', yellow: '黄', purple: '紫' };
const BEATS = { red: 'green', green: 'blue', blue: 'red', yellow: 'purple', purple: 'yellow' };
const ADV = 1.3, DIS = 0.75;
function elemMul(a, d) { if (BEATS[a] === d) return ADV; if (BEATS[d] === a) return DIS; return 1; }

const STATUS = {
  atkUp: { name: '攻撃力上昇', kind: 'buff' }, atkDown: { name: '攻撃力低下', kind: 'debuff' },
  slow: { name: '鈍足', kind: 'debuff' }, stop: { name: '停止', kind: 'debuff' },
  poison: { name: '毒', kind: 'debuff' }, defDown: { name: '防御低下', kind: 'debuff' },
  immune: { name: '妨害無効', kind: 'buff' }, guard: { name: '守り', kind: 'buff' },
  taunt: { name: '引きつけ', kind: 'buff' }, stopResist: { name: '停止耐性', kind: 'buff' },
};
const ATK_UP = 1.3, ATK_DOWN = 0.7, SLOW = 0.6, DEF_DOWN = 0.7;
const KIND_NAME = { normal: '通常攻撃', ability: 'アビリティ', special: '必殺技', ultimate: '奥義', set: 'セットアビリティ', cond: '条件アビリティ' };
const RARITY = { R: { name: 'R', order: 0 }, SR: { name: 'SR', order: 1 }, UR: { name: 'UR', order: 2 } };

/* ---------------------------------------------------------------------------
 * キャラ
 * 技: kind / type(phys|spec|guard|heal|debuff) / target / range / area / power / hits / cd / fx / heal / cleanse / revive / quick
 *   target: enemy（敵1体）・enemyArea（敵1体を中心に area マス以内の敵全員）・ally（味方1人）
 *           self（自分）・allies（自分から area マス以内の味方全員。99 なら全員）
 * set（セット）: critUp / physUp / specUp / healUp / dmgCut / vsDebuffed
 * cond（条件）: trig = halfHp / allyLow / partBreak / crit / lethal / kill / battleStart
 * look: 3D の見た目（weapon / hair）。
 * ------------------------------------------------------------------------- */
const S = (kind, name, type, target, o) => Object.assign({ kind, name, type, target, range: 1, area: 0, power: 0, cd: 0 }, o);
const CHARACTERS = {
  // ======== R（はじめから持っている5人） ========
  kagari: { name: 'カガリ', rarity: 'R', el: 'red', role: '物理アタッカー', look: { weapon: 'sword', hair: 'spiky' },
    hp: 970, atk: 152, def: 50, res: 37, spd: 106, mov: 3,
    set: { name: '闘志', desc: '会心が出やすい（会心率 +15%）。', critUp: 0.15 },
    cond: { name: '逆境', desc: '体力が半分以下になると、攻撃力上昇（3ターン）。1回だけ。', trig: 'halfHp', fx: [{ st: 'atkUp', turns: 3 }], uses: 1 },
    skills: [
      S('normal', '斬撃', 'phys', 'enemy', { power: 1.0, desc: '隣の敵に物理ダメージ。' }),
      S('ability', '鼓舞の炎', 'guard', 'self', { cd: 3, fx: [{ st: 'atkUp', turns: 3 }], quick: true, desc: '自分の攻撃力を上げる（3ターン）。すぐもう一度動ける。' }),
      S('special', '烈火斬', 'phys', 'enemy', { power: 2.3, cd: 3, desc: '隣の敵に大きな物理ダメージ。' }),
      S('ultimate', '紅蓮乱舞', 'phys', 'enemyArea', { area: 1, power: 3.6, desc: '敵とそのまわり1マスに特大の物理ダメージ。' }),
    ] },
  minamo: { name: 'ミナモ', rarity: 'R', el: 'blue', role: '回復・治療', look: { weapon: 'staff', hair: 'twin' },
    hp: 875, atk: 110, def: 41, res: 64, spd: 110, mov: 3,
    set: { name: '水の加護', desc: '回復の量が 20% 増える。', healUp: 0.2 },
    cond: { name: '救いの手', desc: '射程の中の味方の体力が 30% を切ると、すぐに回復する。2回まで。', trig: 'allyLow', heal: 1.2, uses: 2 },
    skills: [
      S('normal', '水弾', 'spec', 'enemy', { range: 3, power: 0.9, desc: '3マス先までの敵に特殊ダメージ。' }),
      S('ability', '癒しの雫', 'heal', 'ally', { range: 3, cd: 1, heal: { atkPct: 1.8 }, desc: '3マス以内の味方1人を、攻撃力の180%ぶん回復。' }),
      S('special', '清めの水', 'heal', 'allies', { area: 2, cd: 3, heal: { atkPct: 0.7 }, cleanse: true, desc: '自分から2マス以内の味方の妨害を治し、攻撃力の70%ぶん回復。' }),
      S('ultimate', '慈雨', 'heal', 'allies', { area: 99, heal: { atkPct: 1.4 }, cleanse: true, revive: 0.3, fx: [{ st: 'immune', turns: 2 }], desc: '味方全員を回復し、倒れた味方を起こす。妨害無効（2ターン）。' }),
    ] },
  touka: { name: 'トウカ', rarity: 'R', el: 'green', role: '妨害', look: { weapon: 'bow', hair: 'pony' },
    hp: 900, atk: 124, def: 46, res: 50, spd: 118, mov: 3,
    set: { name: '狩人の目', desc: '妨害がかかっている敵へのダメージが 20% 増える。', vsDebuffed: 0.2 },
    cond: { name: '追い撃ち', desc: 'だれかが部位を壊すと、すぐに追加で矢を放つ。', trig: 'partBreak', power: 1.4 },
    skills: [
      S('normal', '早撃ち', 'phys', 'enemy', { range: 4, power: 0.9, desc: '4マス先までの敵に物理ダメージ。' }),
      S('ability', '絡み蔦', 'debuff', 'enemy', { range: 4, power: 0.7, cd: 2, fx: [{ st: 'slow', turns: 3 }], desc: '敵を鈍足にする（3ターン）。' }),
      S('special', '毒牙の矢', 'debuff', 'enemy', { range: 4, power: 1.2, cd: 3, fx: [{ st: 'poison', turns: 3 }], desc: '敵にダメージを与え、毒にする（3ターン）。' }),
      S('ultimate', '茨の檻', 'debuff', 'enemyArea', { range: 4, area: 1, power: 2.0, fx: [{ st: 'stop', turns: 1 }], desc: '敵とそのまわり1マスにダメージを与え、停止させる。' }),
    ] },
  raiga: { name: 'ライガ', rarity: 'R', el: 'yellow', role: '特殊アタッカー', look: { weapon: 'rod', hair: 'messy' },
    hp: 830, atk: 156, def: 37, res: 55, spd: 112, mov: 3,
    set: { name: '帯電', desc: '特殊攻撃のダメージが 12% 増える。', specUp: 0.12 },
    cond: { name: '連鎖雷', desc: '会心が出ると、次の自分の番が早く回ってくる。', trig: 'crit', haste: 400 },
    skills: [
      S('normal', '雷撃', 'spec', 'enemy', { range: 3, power: 1.0, desc: '3マス先までの敵に特殊ダメージ。' }),
      S('ability', '萎縮の雷', 'debuff', 'enemy', { range: 3, power: 0.8, cd: 3, fx: [{ st: 'atkDown', turns: 3 }], desc: '敵の攻撃力を下げる（3ターン）。' }),
      S('special', '雷槍', 'spec', 'enemy', { range: 3, power: 2.2, cd: 3, desc: '敵に大きな特殊ダメージ。' }),
      S('ultimate', '雷帝招来', 'spec', 'enemyArea', { range: 3, area: 1, power: 3.4, desc: '敵とそのまわり1マスに特大の特殊ダメージ。' }),
    ] },
  shion: { name: 'シオン', rarity: 'R', el: 'purple', role: '防御', look: { weapon: 'shield', hair: 'helm' },
    hp: 1330, atk: 101, def: 87, res: 74, spd: 96, mov: 2,
    set: { name: '鉄壁', desc: '受けるダメージが 10% 減る。', dmgCut: 0.1 },
    cond: { name: '不屈', desc: '倒れるダメージを受けても、体力1で耐える。1回だけ。', trig: 'lethal', uses: 1 },
    skills: [
      S('normal', '盾打ち', 'phys', 'enemy', { power: 0.9, desc: '隣の敵に物理ダメージ。' }),
      S('ability', '守護の構え', 'guard', 'self', { cd: 2, fx: [{ st: 'taunt', turns: 2 }, { st: 'guard', turns: 2, val: 0.55 }], desc: '敵の単体攻撃を自分に引きつけ、受けるダメージを減らす（2ターン）。' }),
      S('special', '生命の鼓動', 'heal', 'self', { cd: 3, heal: { maxHpPct: 0.3 }, desc: '自分の最大体力の30%を回復。' }),
      S('ultimate', '不落の陣', 'guard', 'allies', { area: 3, fx: [{ st: 'guard', turns: 2, val: 0.6 }], desc: '自分から3マス以内の味方の受けるダメージを減らす（2ターン）。' }),
    ] },

  // ======== SR ========
  homura: { name: 'ホムラ', rarity: 'SR', el: 'red', role: '物理アタッカー', look: { weapon: 'dual', hair: 'short' },
    hp: 1080, atk: 172, def: 52, res: 42, spd: 120, mov: 4,
    set: { name: '二刀の心得', desc: '会心が出やすい（会心率 +10%）。', critUp: 0.1 },
    cond: { name: '勢い', desc: '敵を倒すと、次の自分の番が早く回ってくる。', trig: 'kill', haste: 500 },
    skills: [
      S('normal', '連撃', 'phys', 'enemy', { power: 0.6, hits: 2, desc: '隣の敵に2回の物理ダメージ。' }),
      S('ability', '疾風', 'guard', 'self', { cd: 3, fx: [{ st: 'atkUp', turns: 2 }], quick: true, desc: '自分の攻撃力を上げる（2ターン）。すぐもう一度動ける。' }),
      S('special', '焔舞', 'phys', 'enemy', { power: 0.95, hits: 3, cd: 3, desc: '隣の敵に3回の物理ダメージ。' }),
      S('ultimate', '百花繚乱', 'phys', 'enemyArea', { area: 1, power: 3.4, desc: '敵とそのまわり1マスに特大の物理ダメージ。' }),
    ] },
  shizuku: { name: 'シズク', rarity: 'SR', el: 'blue', role: '特殊アタッカー', look: { weapon: 'gun', hair: 'bob' },
    hp: 900, atk: 165, def: 40, res: 58, spd: 114, mov: 3,
    set: { name: '水の理', desc: '特殊攻撃のダメージが 10% 増える。', specUp: 0.1 },
    cond: { name: '冷静', desc: '体力が半分以下になると、妨害無効（2ターン）。1回だけ。', trig: 'halfHp', fx: [{ st: 'immune', turns: 2 }], uses: 1 },
    skills: [
      S('normal', '水銃', 'spec', 'enemy', { range: 5, power: 0.95, desc: '5マス先までの敵に特殊ダメージ。' }),
      S('ability', '狙い撃ち', 'guard', 'self', { cd: 3, fx: [{ st: 'atkUp', turns: 2 }], quick: true, desc: '自分の攻撃力を上げる（2ターン）。すぐもう一度動ける。' }),
      S('special', '貫通弾', 'spec', 'enemyArea', { range: 5, area: 1, power: 1.7, cd: 3, desc: '敵とそのまわり1マスに特殊ダメージ。' }),
      S('ultimate', '大海嘯', 'spec', 'enemyArea', { range: 5, area: 2, power: 2.9, desc: '敵とそのまわり2マスに特大の特殊ダメージ。' }),
    ] },
  yomogi: { name: 'ヨモギ', rarity: 'SR', el: 'green', role: '回復・治療', look: { weapon: 'leafstaff', hair: 'bun' },
    hp: 1000, atk: 128, def: 50, res: 74, spd: 112, mov: 3,
    set: { name: '森の恵み', desc: '回復の量が 15% 増える。', healUp: 0.15 },
    cond: { name: '芽吹き', desc: '射程の中の味方の体力が 30% を切ると、すぐに回復する。2回まで。', trig: 'allyLow', heal: 1.3, uses: 2 },
    skills: [
      S('normal', '葉刃', 'spec', 'enemy', { range: 3, power: 0.85, desc: '3マス先までの敵に特殊ダメージ。' }),
      S('ability', '若葉の癒し', 'heal', 'ally', { range: 3, cd: 1, heal: { atkPct: 2.0 }, desc: '3マス以内の味方1人を、攻撃力の200%ぶん回復。' }),
      S('special', '森の息吹', 'heal', 'allies', { area: 2, cd: 3, heal: { atkPct: 0.9 }, cleanse: true, desc: '2マス以内の味方の妨害を治し、攻撃力の90%ぶん回復。' }),
      S('ultimate', '大樹の加護', 'heal', 'allies', { area: 99, heal: { atkPct: 1.4 }, revive: 0.4, fx: [{ st: 'guard', turns: 2, val: 0.75 }], desc: '味方全員を回復し、倒れた味方を起こす。受けるダメージを減らす（2ターン）。' }),
    ] },
  kohaku: { name: 'コハク', rarity: 'SR', el: 'yellow', role: '妨害', look: { weapon: 'hammer', hair: 'short' },
    hp: 1250, atk: 168, def: 70, res: 45, spd: 98, mov: 2,
    set: { name: '剛腕', desc: '物理攻撃のダメージが 10% 増える。', physUp: 0.1 },
    cond: { name: '踏ん張り', desc: '体力が半分以下になると、守り（2ターン）。1回だけ。', trig: 'halfHp', fx: [{ st: 'guard', turns: 2, val: 0.6 }], uses: 1 },
    skills: [
      S('normal', '打ち据え', 'phys', 'enemy', { power: 1.1, desc: '隣の敵に物理ダメージ。' }),
      S('ability', '地鳴らし', 'debuff', 'enemyArea', { area: 1, power: 0.7, cd: 2, fx: [{ st: 'slow', turns: 2 }], desc: '敵とそのまわり1マスにダメージを与え、鈍足にする（2ターン）。' }),
      S('special', '雷鎚', 'phys', 'enemy', { power: 2.1, cd: 4, fx: [{ st: 'stop', turns: 1 }], desc: '隣の敵に大きな物理ダメージを与え、停止させる。' }),
      S('ultimate', '天崩し', 'phys', 'enemyArea', { area: 2, power: 2.9, fx: [{ st: 'defDown', turns: 3 }], desc: '敵とそのまわり2マスに特大の物理ダメージ。防御低下（3ターン）。' }),
    ] },
  yukari: { name: 'ユカリ', rarity: 'SR', el: 'purple', role: '妨害', look: { weapon: 'tome', hair: 'long' },
    hp: 880, atk: 158, def: 38, res: 66, spd: 116, mov: 3,
    set: { name: '呪術', desc: '妨害がかかっている敵へのダメージが 15% 増える。', vsDebuffed: 0.15 },
    cond: { name: '魂喰らい', desc: '敵を倒すと、次の自分の番が早く回ってくる。', trig: 'kill', haste: 500 },
    skills: [
      S('normal', '呪文', 'spec', 'enemy', { range: 3, power: 0.95, desc: '3マス先までの敵に特殊ダメージ。' }),
      S('ability', '蝕みの呪', 'debuff', 'enemy', { range: 3, power: 0.6, cd: 2, fx: [{ st: 'poison', turns: 3 }], desc: '敵を毒にする（3ターン）。' }),
      S('special', '闇の帳', 'debuff', 'enemyArea', { range: 3, area: 1, power: 1.2, cd: 3, fx: [{ st: 'atkDown', turns: 3 }], desc: '敵とまわり1マスにダメージ。攻撃力低下（3ターン）。' }),
      S('ultimate', '深淵', 'spec', 'enemyArea', { range: 3, area: 2, power: 2.7, fx: [{ st: 'poison', turns: 3 }, { st: 'defDown', turns: 2 }], desc: '敵とまわり2マスに特大の特殊ダメージ。毒と防御低下。' }),
    ] },
  aoi: { name: 'アオイ', rarity: 'SR', el: 'blue', role: '防御', look: { weapon: 'spear', hair: 'pony' },
    hp: 1180, atk: 150, def: 72, res: 60, spd: 104, mov: 3,
    set: { name: '槍衾', desc: '受けるダメージが 8% 減る。', dmgCut: 0.08 },
    cond: { name: '不退転', desc: '倒れるダメージを受けても、体力1で耐える。1回だけ。', trig: 'lethal', uses: 1 },
    skills: [
      S('normal', '突き', 'phys', 'enemy', { range: 2, power: 1.0, desc: '2マス先までの敵に物理ダメージ。' }),
      S('ability', '守りの槍', 'guard', 'self', { cd: 2, fx: [{ st: 'taunt', turns: 2 }, { st: 'guard', turns: 2, val: 0.6 }], desc: '敵の単体攻撃を自分に引きつけ、受けるダメージを減らす（2ターン）。' }),
      S('special', '流星突き', 'phys', 'enemy', { range: 2, power: 2.0, cd: 3, fx: [{ st: 'defDown', turns: 2 }], desc: '敵に大きな物理ダメージ。防御低下（2ターン）。' }),
      S('ultimate', '蒼槍乱舞', 'phys', 'enemyArea', { range: 2, area: 1, power: 3.0, desc: '敵とそのまわり1マスに特大の物理ダメージ。' }),
    ] },

  // ======== UR ========
  akatsuki: { name: 'アカツキ', rarity: 'UR', el: 'red', role: '物理アタッカー', look: { weapon: 'katana', hair: 'long', crown: true },
    hp: 1300, atk: 205, def: 62, res: 50, spd: 122, mov: 4,
    set: { name: '剣聖', desc: '会心が出やすい（会心率 +20%）。', critUp: 0.2 },
    cond: { name: '残心', desc: '敵を倒すと、次の自分の番がすぐ回ってくる。', trig: 'kill', haste: 650 },
    skills: [
      S('normal', '一閃', 'phys', 'enemy', { power: 1.1, desc: '隣の敵に物理ダメージ。' }),
      S('ability', '心眼', 'guard', 'self', { cd: 3, fx: [{ st: 'atkUp', turns: 3 }, { st: 'immune', turns: 2 }], quick: true, desc: '攻撃力上昇（3ターン）と妨害無効（2ターン）。すぐもう一度動ける。' }),
      S('special', '紅月斬', 'phys', 'enemy', { power: 2.8, cd: 3, desc: '隣の敵に特大の物理ダメージ。' }),
      S('ultimate', '朧月夜', 'phys', 'enemyArea', { area: 2, power: 4.2, desc: '敵とそのまわり2マスに極大の物理ダメージ。' }),
    ] },
  sora: { name: 'ソラ', rarity: 'UR', el: 'blue', role: '回復・治療', look: { weapon: 'greatstaff', hair: 'long', crown: true },
    hp: 1150, atk: 150, def: 58, res: 90, spd: 118, mov: 3,
    set: { name: '天の恵み', desc: '回復の量が 25% 増える。', healUp: 0.25 },
    cond: { name: '天の加護', desc: '戦いの始めに、味方全員を妨害無効にする（2ターン）。', trig: 'battleStart', fx: [{ st: 'immune', turns: 2 }] },
    skills: [
      S('normal', '天光', 'spec', 'enemy', { range: 3, power: 0.95, desc: '3マス先までの敵に特殊ダメージ。' }),
      S('ability', '慈光', 'heal', 'ally', { range: 4, cd: 1, heal: { atkPct: 2.3 }, desc: '4マス以内の味方1人を、攻撃力の230%ぶん回復。' }),
      S('special', '祝福', 'heal', 'allies', { area: 3, cd: 3, heal: { atkPct: 1.0 }, cleanse: true, fx: [{ st: 'atkUp', turns: 2 }], desc: '3マス以内の味方を回復・治療し、攻撃力を上げる（2ターン）。' }),
      S('ultimate', '天上の歌', 'heal', 'allies', { area: 99, heal: { atkPct: 1.8 }, cleanse: true, revive: 0.5, fx: [{ st: 'immune', turns: 2 }], desc: '味方全員を大きく回復し、倒れた味方を起こす。妨害無効（2ターン）。' }),
    ] },
  midori: { name: 'ミドリ', rarity: 'UR', el: 'green', role: '妨害', look: { weapon: 'greatbow', hair: 'pony', crown: true },
    hp: 1050, atk: 180, def: 50, res: 60, spd: 126, mov: 3,
    set: { name: '風読み', desc: '妨害がかかっている敵へのダメージが 20% 増える。', vsDebuffed: 0.2 },
    cond: { name: '急所撃ち', desc: 'だれかが部位を壊すと、すぐに追加で矢を放つ。', trig: 'partBreak', power: 2.0 },
    skills: [
      S('normal', '翠矢', 'phys', 'enemy', { range: 5, power: 1.0, desc: '5マス先までの敵に物理ダメージ。' }),
      S('ability', '縫い止め', 'debuff', 'enemy', { range: 5, power: 0.9, cd: 2, fx: [{ st: 'slow', turns: 3 }], desc: '敵を鈍足にする（3ターン）。' }),
      S('special', '嵐の矢雨', 'debuff', 'enemyArea', { range: 5, area: 1, power: 1.6, cd: 3, fx: [{ st: 'poison', turns: 2 }], desc: '敵とまわり1マスにダメージを与え、毒にする（2ターン）。' }),
      S('ultimate', '翠嵐', 'debuff', 'enemyArea', { range: 6, area: 3, power: 3.0, fx: [{ st: 'slow', turns: 2 }], desc: '敵とまわり3マスに特大のダメージ。鈍足（2ターン）。' }),
    ] },
  kirara: { name: 'キララ', rarity: 'UR', el: 'yellow', role: '特殊アタッカー', look: { weapon: 'lance', hair: 'twin', crown: true },
    hp: 1100, atk: 200, def: 52, res: 62, spd: 130, mov: 4,
    set: { name: '雷神', desc: '特殊攻撃のダメージが 18% 増える。', specUp: 0.18 },
    cond: { name: '迅雷', desc: '会心が出ると、次の自分の番がすぐ回ってくる。', trig: 'crit', haste: 600 },
    skills: [
      S('normal', '迅雷', 'spec', 'enemy', { range: 2, power: 1.0, desc: '2マス先までの敵に特殊ダメージ。' }),
      S('ability', '帯電', 'guard', 'self', { cd: 2, fx: [{ st: 'atkUp', turns: 2 }], quick: true, desc: '自分の攻撃力を上げる（2ターン）。すぐもう一度動ける。' }),
      S('special', '雷光一閃', 'spec', 'enemy', { range: 3, power: 2.6, cd: 3, desc: '3マス先までの敵に特大の特殊ダメージ。' }),
      S('ultimate', '万雷', 'spec', 'enemyArea', { range: 3, area: 2, power: 3.8, desc: '敵とまわり2マスに極大の特殊ダメージ。' }),
    ] },
  shigure: { name: 'シグレ', rarity: 'UR', el: 'purple', role: '防御', look: { weapon: 'shieldsword', hair: 'helm', crown: true },
    hp: 1650, atk: 150, def: 105, res: 92, spd: 108, mov: 3,
    set: { name: '紫電の守り', desc: '受けるダメージが 15% 減る。', dmgCut: 0.15 },
    cond: { name: '不壊', desc: '倒れるダメージを受けても、体力1で耐える。2回まで。', trig: 'lethal', uses: 2 },
    skills: [
      S('normal', '盾剣撃', 'phys', 'enemy', { power: 1.0, desc: '隣の敵に物理ダメージ。' }),
      S('ability', '紫電の構え', 'guard', 'self', { cd: 2, fx: [{ st: 'taunt', turns: 3 }, { st: 'guard', turns: 3, val: 0.5 }], desc: '敵の単体攻撃を自分に引きつけ、受けるダメージを大きく減らす（3ターン）。' }),
      S('special', '雷盾', 'guard', 'allies', { area: 2, cd: 3, fx: [{ st: 'guard', turns: 2, val: 0.65 }], desc: '2マス以内の味方の受けるダメージを減らす（2ターン）。' }),
      S('ultimate', '不壊の城塞', 'guard', 'allies', { area: 99, fx: [{ st: 'guard', turns: 2, val: 0.5 }, { st: 'immune', turns: 2 }], desc: '味方全員の受けるダメージを大きく減らし、妨害無効（2ターン）。' }),
    ] },
};
const R_KEYS = Object.keys(CHARACTERS).filter((k) => CHARACTERS[k].rarity === 'R');

/* ---------------------------------------------------------------------------
 * 敵（size は占めるマスの一辺。ボスは 2×2）
 * 技: target = one（射程内の1人）・area（射程内の1人を中心に a マス）・around（自分から a マス）
 *     charge … 予告してから次の自分の番に撃つ（予告したマスから逃げればよけられる）
 * ------------------------------------------------------------------------- */
const E_S = (name, type, target, o) => Object.assign({ name, type, target, range: 1, a: 0, power: 1, w: 1, cd: 0 }, o);
const ENEMIES = {
  tsunousa: { name: 'ツノウサ', el: 'green', shape: 'rabbit', size: 1, hp: 1100, atk: 128, def: 40, res: 35, spd: 112, mov: 3,
    skills: [E_S('角突き', 'phys', 'one', { power: 1.0, w: 4 }), E_S('跳び蹴り', 'phys', 'one', { power: 1.35, w: 2, cd: 2 })] },
  hanedori: { name: 'ハネドリ', el: 'blue', shape: 'bird', size: 1, hp: 920, atk: 130, def: 32, res: 45, spd: 124, mov: 4,
    skills: [E_S('風切り', 'spec', 'one', { range: 2, power: 0.95, w: 4 }), E_S('突風', 'spec', 'area', { range: 3, a: 1, power: 0.6, w: 2, cd: 3, fx: [{ st: 'slow', turns: 2 }] })] },
  hanamodoki: { name: 'ハナモドキ', el: 'purple', shape: 'plant', size: 1, hp: 1050, atk: 122, def: 45, res: 55, spd: 98, mov: 1,
    skills: [E_S('蔓打ち', 'phys', 'one', { range: 2, power: 1.0, w: 3 }), E_S('毒花粉', 'spec', 'area', { range: 3, a: 1, power: 0.5, w: 3, cd: 2, fx: [{ st: 'poison', turns: 2 }] })] },
  iwakoro: { name: 'イワコロ', el: 'yellow', shape: 'golem', size: 1, hp: 1900, atk: 152, def: 95, res: 50, spd: 88, mov: 2,
    skills: [E_S('岩拳', 'phys', 'one', { power: 1.2, w: 4 }), E_S('地響き', 'phys', 'around', { a: 2, power: 0.7, w: 2, cd: 3 })] },
  hibashiri: { name: 'ヒバシリ', el: 'red', shape: 'wolf', size: 2, boss: true, hp: 7000, atk: 165, def: 55, res: 45, spd: 110, mov: 3, poisonPct: 0.025,
    parts: [{ name: 'たてがみ', hp: 1300 }, { name: '前足の爪', hp: 1100 }],
    skills: [E_S('噛みつき', 'phys', 'one', { power: 1.15, w: 4 }), E_S('連続突進', 'phys', 'one', { power: 0.75, hits: 2, w: 3, cd: 1 }),
      E_S('火炎の息', 'spec', 'area', { range: 3, a: 1, power: 0.8, w: 2, cd: 2 }),
      E_S('業火の咆哮', 'spec', 'area', { range: 3, a: 2, power: 1.35, w: 2, cd: 4, charge: true, tell: '大きく息を吸い込んでいる…赤いマスから離れろ！' })] },
  drogarm: { name: 'ドロガルム', el: 'green', shape: 'lizard', size: 2, boss: true, hp: 9200, atk: 190, def: 70, res: 55, spd: 102, mov: 2, poisonPct: 0.02,
    parts: [{ name: '背中のこぶ', hp: 1700 }, { name: '尻尾', hp: 1500 }],
    skills: [E_S('尻尾なぎ払い', 'phys', 'around', { a: 2, power: 0.7, w: 3, cd: 1 }), E_S('踏みつけ', 'phys', 'one', { power: 1.25, w: 4 }),
      E_S('毒泥', 'spec', 'one', { range: 3, power: 0.7, w: 3, cd: 2, fx: [{ st: 'poison', turns: 3 }] }),
      E_S('泥しぶき', 'spec', 'area', { range: 3, a: 1, power: 0.5, w: 2, cd: 3, fx: [{ st: 'slow', turns: 2 }] }),
      E_S('大地沈め', 'phys', 'around', { a: 3, power: 1.4, w: 2, cd: 4, charge: true, tell: '泥の中へ深く沈みこんだ…赤いマスから離れろ！' })] },
  raizen: { name: 'ライゼン', el: 'yellow', shape: 'beast', size: 2, boss: true, hp: 12500, atk: 215, def: 75, res: 70, spd: 114, mov: 3, poisonPct: 0.015,
    parts: [{ name: '雷角', hp: 2200 }, { name: '蓄電器官', hp: 2000 }],
    skills: [E_S('角突き', 'phys', 'one', { power: 1.3, w: 4 }), E_S('放電', 'spec', 'around', { a: 2, power: 0.7, w: 3, cd: 1 }),
      E_S('麻痺の雷', 'spec', 'one', { range: 3, power: 0.8, w: 2, cd: 3, fx: [{ st: 'stop', turns: 1 }] }),
      E_S('威圧の咆哮', 'debuff', 'around', { a: 4, power: 0, w: 2, cd: 4, fx: [{ st: 'atkDown', turns: 2 }] }),
      E_S('天雷', 'spec', 'area', { range: 4, a: 2, power: 1.5, w: 2, cd: 4, charge: true, tell: '角に雷が集まっていく…赤いマスから離れろ！' })] },
};

/* ---------------------------------------------------------------------------
 * ステージ（敵の配置 [種類, x, z]・岩のマス・敵の強さ scale=[体力, 攻撃力, 速さ] の倍率・報酬）
 * scale は、オート同士の対戦で R の5人がおよそ 1-1〜1-4: 9割以上 / 1-5: 7〜8割 / EX1: 4割 / EX2: 2割 勝つように合わせた
 * ------------------------------------------------------------------------- */
const STAGES = {
  '1-1': { world: 'meadow', no: '1-1', name: 'はじまりの草原', star: 1, enemies: [['tsunousa', 4, 7], ['tsunousa', 7, 8]],
    blocks: [[2, 5], [9, 4], [6, 4]], scale: [1.3, 2.6, 1.3], first: 200, again: 30 },
  '1-2': { world: 'meadow', no: '1-2', name: 'そよ風の丘', star: 1, enemies: [['tsunousa', 3, 7], ['tsunousa', 8, 7], ['hanedori', 6, 9]],
    blocks: [[5, 5], [6, 5], [1, 4], [10, 6]], scale: [1.35, 2.7, 1.28], first: 200, again: 30 },
  '1-3': { world: 'meadow', no: '1-3', name: '花咲く小径', star: 2, enemies: [['hanamodoki', 3, 8], ['hanamodoki', 8, 8], ['tsunousa', 6, 6]],
    blocks: [[4, 4], [7, 4], [2, 6], [9, 6], [5, 8]], scale: [1.8, 3.6, 1.38], first: 250, again: 40 },
  '1-4': { world: 'meadow', no: '1-4', name: '苔むす石塚', star: 2, enemies: [['iwakoro', 5, 7], ['hanedori', 2, 8], ['hanedori', 9, 8]],
    blocks: [[3, 5], [4, 5], [7, 5], [8, 5], [5, 9], [6, 9]], scale: [1.8, 3.5, 1.4], first: 250, again: 40 },
  '1-5': { world: 'meadow', no: '1-5', name: '草原の主', star: 3, boss: true, enemies: [['hibashiri', 5, 6], ['tsunousa', 2, 7], ['tsunousa', 9, 7]],
    blocks: [[1, 4], [10, 4], [3, 8], [8, 8]], scale: [1.7, 3.3, 1.35], first: 400, again: 60 },
  'ex1': { world: 'ex', no: 'EX1', name: '泥沼の大蜥蜴', star: 4, boss: true, need: '1-5', enemies: [['drogarm', 5, 6]],
    blocks: [[2, 5], [9, 5], [4, 9], [7, 9]], scale: [2.0, 3.8, 1.52], first: 500, again: 80 },
  'ex2': { world: 'ex', no: 'EX2', name: '雷角の覇獣', star: 5, boss: true, need: 'ex1', enemies: [['raizen', 5, 6]],
    blocks: [[3, 4], [8, 4], [1, 7], [10, 7]], scale: [1.85, 3.7, 1.5], first: 800, again: 120 },
};
if (process.env.HUNT_SCALE) { const o = JSON.parse(process.env.HUNT_SCALE); for (const k of Object.keys(o)) STAGES[k].scale = o[k]; }   // 調整用
const STAGE_ORDER = Object.keys(STAGES);
const prevStage = (key) => { const i = STAGE_ORDER.indexOf(key); return i > 0 ? (STAGES[key].need || STAGE_ORDER[i - 1]) : null; };
const HUNTER_SPAWN = [[2, 1], [4, 0], [6, 1], [8, 0], [10, 1]];

/* ---------- 乱数（mulberry32） ---------- */
function rnd(s) {
  let t = (s.seed = (s.seed + 0x6D2B79F5) | 0);
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}

/* ---------- 戦いを作る ---------- */
// party: [{ key, dup, owner }]（最大5人）。dup は重ねた数（1つにつき 体力と攻撃力 +4%）
function createBattle(stageKey, party, seed) {
  const st = STAGES[stageKey];
  if (!st) throw new Error('unknown stage');
  const units = [];
  (party || []).slice(0, 5).forEach((p, i) => {
    const c = CHARACTERS[p.key];
    if (!c) return;
    const k = 1 + 0.04 * Math.max(0, Math.min(4, p.dup | 0));
    const [x, z] = HUNTER_SPAWN[i];
    units.push({ id: 'h' + i, side: 'h', key: p.key, name: c.name, el: c.el, rarity: c.rarity, role: c.role, x, z, size: 1,
      maxHp: Math.round(c.hp * k), hp: Math.round(c.hp * k), atk: Math.round(c.atk * k), def: c.def, res: c.res, spd: c.spd, mov: c.mov,
      gauge: 0, burst: 0, cds: [0, 0, 0, 0], st: [], owner: p.owner | 0, dup: p.dup | 0, condLeft: c.cond.uses || 99, haste: 0 });
  });
  const sc = st.scale || [1, 1, 1];   // ステージごとの敵の強さ（体力・攻撃力・速さの倍率）
  st.enemies.forEach(([key, x, z], i) => {
    const e = ENEMIES[key];
    const hp = Math.round(e.hp * sc[0]);
    units.push({ id: 'e' + i, side: 'm', key, name: e.name, el: e.el, shape: e.shape, size: e.size, boss: !!e.boss, x, z,
      maxHp: hp, hp, atk: Math.round(e.atk * sc[1]), def: e.def, res: e.res, spd: Math.round(e.spd * (sc[2] || 1)), mov: e.mov, gauge: 0, cds: e.skills.map(() => 0), st: [],
      parts: (e.parts || []).map((p, j) => ({ id: 'e' + i + '.p' + j, name: p.name, hp: Math.round(p.hp * sc[0]), maxHp: Math.round(p.hp * sc[0]), broken: false })),
      rage: false, charge: null });
  });
  const s = { stage: stageKey, seed: (seed | 0) || 1, w: W, h: H, blocks: st.blocks.map((b) => b.slice()), units,
              turn: null, phase: 'input', moved: false, win: null, turns: 0 };
  for (const u of units) u.gauge = Math.floor(rnd(s) * 250);
  return s;
}

const unit = (s, id) => s.units.find((u) => u.id === id);
const alive = (u) => u && u.hp > 0;
const hunters = (s) => s.units.filter((u) => u.side === 'h');
const enemies = (s) => s.units.filter((u) => u.side === 'm');
const has = (u, st) => u.st.find((x) => x.id === st);
const charOf = (u) => CHARACTERS[u.key];
const skillsOf = (u) => (u.side === 'h' ? CHARACTERS[u.key].skills : ENEMIES[u.key].skills);
const setOf = (u) => (u.side === 'h' ? CHARACTERS[u.key].set : {});
const condOf = (u) => (u.side === 'h' ? CHARACTERS[u.key].cond : {});
const isDebuffed = (u) => u.st.some((x) => STATUS[x.id].kind === 'debuff');

function effSpd(u) { return u.spd * (has(u, 'slow') ? SLOW : 1) * (u.rage ? 1.12 : 1); }
function effAtk(u) { return u.atk * (has(u, 'atkUp') ? ATK_UP : 1) * (has(u, 'atkDown') ? ATK_DOWN : 1) * (u.rage ? 1.2 : 1); }
function effDef(u, type) { return (type === 'spec' ? u.res : u.def) * (has(u, 'defDown') ? DEF_DOWN : 1); }

/* ---------- マス目 ---------- */
const tkey = (x, z) => x + ',' + z;
function tilesOf(u, x, z) { const out = []; const X = x == null ? u.x : x, Z = z == null ? u.z : z; for (let i = 0; i < u.size; i++) for (let j = 0; j < u.size; j++) out.push([X + i, Z + j]); return out; }
// 足元どうしの上下左右の距離（大きい敵は、いちばん近いマスまで）
function spanDist(ax, az, as, bx, bz, bs) {
  const dx = Math.max(0, bx - (ax + as - 1), ax - (bx + bs - 1)), dz = Math.max(0, bz - (az + as - 1), az - (bz + bs - 1));
  return dx + dz;
}
const dist = (a, b) => spanDist(a.x, a.z, a.size, b.x, b.z, b.size);
const distFrom = (a, x, z, b) => spanDist(x, z, a.size, b.x, b.z, b.size);
function blockSet(s) { const b = new Set(); for (const [x, z] of s.blocks) b.add(tkey(x, z)); return b; }
function occupancy(s, ignore) { const m = new Map(); for (const u of s.units) if (alive(u) && u !== ignore) for (const [x, z] of tilesOf(u)) m.set(tkey(x, z), u); return m; }

/* その場所から動ける範囲（Map: "x,z" -> { x, z, d, prev }）。自分の足元も入る */
function reach(s, u, maxSteps) {
  const steps = maxSteps == null ? u.mov : maxSteps;
  const blocks = blockSet(s), occ = occupancy(s, u);
  const fits = (x, z, final) => {
    if (x < 0 || z < 0 || x + u.size > W || z + u.size > H) return false;
    for (const [tx, tz] of tilesOf(u, x, z)) {
      const k = tkey(tx, tz);
      if (blocks.has(k)) return false;
      const o = occ.get(k);
      if (o && (final || o.side !== u.side)) return false;   // 味方の上は通れるが止まれない。敵は通れない
    }
    return true;
  };
  const out = new Map([[tkey(u.x, u.z), { x: u.x, z: u.z, d: 0, prev: null }]]);
  const q = [[u.x, u.z, 0]];
  while (q.length) {
    const [x, z, d] = q.shift();
    if (d >= steps) continue;
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = x + dx, nz = z + dz, k = tkey(nx, nz);
      if (out.has(k) || !fits(nx, nz, false)) continue;
      out.set(k, { x: nx, z: nz, d: d + 1, prev: tkey(x, z), pass: !fits(nx, nz, true) });
      q.push([nx, nz, d + 1]);
    }
  }
  for (const [k, v] of out) if (v.pass) out.delete(k);   // 通るだけのマスには止まれない
  return out;
}
function pathTo(R, x, z) {
  const path = []; let k = tkey(x, z);
  while (k) { const v = R.get(k) || null; if (!v) break; path.unshift([v.x, v.z]); k = v.prev; }
  return path;
}
// 予告されている危ないマス
function dangerSet(s) { const d = new Set(); for (const e of enemies(s)) if (alive(e) && e.charge) for (const [x, z] of e.charge.tiles) d.add(tkey(x, z)); return d; }
function tilesWithin(cx, cz, cs, r) {
  const out = [];
  for (let x = 0; x < W; x++) for (let z = 0; z < H; z++) if (spanDist(cx, cz, cs, x, z, 1) <= r) out.push([x, z]);
  return out;
}

/* ---------- 状態 ---------- */
function addStatus(s, src, tgt, fx, ev, force) {
  const def = STATUS[fx.st];
  if (!alive(tgt)) return false;
  if (def.kind === 'debuff' && !force) {
    if (has(tgt, 'immune')) { ev.push({ t: 'resist', u: tgt.id, st: fx.st, why: 'immune' }); return false; }
    if (fx.st === 'stop' && has(tgt, 'stopResist')) { ev.push({ t: 'resist', u: tgt.id, st: fx.st, why: 'resist' }); return false; }
  }
  const cur = has(tgt, fx.st), turns = fx.turns | 0;
  if (cur) { cur.turns = Math.max(cur.turns, turns); if (fx.val != null) cur.val = Math.min(cur.val == null ? 1 : cur.val, fx.val); }
  else tgt.st.push({ id: fx.st, turns, val: fx.val == null ? null : fx.val });
  ev.push({ t: 'st', u: tgt.id, st: fx.st, turns });
  // 敵は止められたあとしばらく止まらない（ずっと止め続けられないように）
  if (fx.st === 'stop' && tgt.side === 'm') addStatus(s, src, tgt, { st: 'stopResist', turns: tgt.boss ? 4 : 3 }, ev, true);
  if (fx.st === 'stop' && tgt.charge) { tgt.charge = null; ev.push({ t: 'uncharge', u: tgt.id }); }
  return true;
}
function cleanse(tgt, ev) {
  const n = tgt.st.length;
  tgt.st = tgt.st.filter((x) => STATUS[x.id].kind !== 'debuff');
  if (tgt.st.length !== n) ev.push({ t: 'cleanse', u: tgt.id });
}
function heal(tgt, v, ev) {
  if (!alive(tgt)) return;
  const real = Math.min(Math.max(1, Math.round(v)), tgt.maxHp - tgt.hp);
  tgt.hp += real;
  ev.push({ t: 'heal', u: tgt.id, v: real });
}

/* ---------- ダメージ ---------- */
function dealDamage(s, src, tgt, sk, ev, partId) {
  if (!alive(tgt)) return 0;
  const set = setOf(src), tset = setOf(tgt);
  const crit = rnd(s) < 0.1 + (set.critUp || 0);
  const em = elemMul(src.el, tgt.el);
  let v = effAtk(src) * sk.power * (100 / (100 + effDef(tgt, sk.type))) * em * (0.95 + rnd(s) * 0.1) * (crit ? 1.5 : 1);
  if (sk.type === 'spec' && set.specUp) v *= 1 + set.specUp;
  if (sk.type === 'phys' && set.physUp) v *= 1 + set.physUp;
  if (set.vsDebuffed && isDebuffed(tgt)) v *= 1 + set.vsDebuffed;
  if (tset.dmgCut) v *= 1 - tset.dmgCut;
  const g = has(tgt, 'guard');
  if (g) v *= g.val == null ? 0.6 : g.val;
  v = Math.max(1, Math.round(v));
  const before = tgt.hp;
  tgt.hp = Math.max(0, tgt.hp - v);
  let endure = false;
  const tc = condOf(tgt);
  if (tc.trig === 'lethal' && tgt.hp === 0 && tgt.condLeft > 0) { tgt.condLeft--; tgt.hp = 1; endure = true; }
  const e = { t: 'dmg', u: tgt.id, src: src.id, v: before - tgt.hp, crit, em: em > 1 ? 'adv' : em < 1 ? 'dis' : null, ty: sk.type };
  if (g) e.guard = true;
  if (partId) e.part = partId;
  ev.push(e);
  if (endure) ev.push({ t: 'cond', u: tgt.id, name: tc.name });
  const sc = condOf(src);
  if (crit && sc.trig === 'crit') { src.haste = sc.haste; ev.push({ t: 'cond', u: src.id, name: sc.name }); }
  if (tgt.side === 'h') {
    tgt.burst = Math.min(100, tgt.burst + 10);
    if (alive(tgt)) {
      if (tc.trig === 'halfHp' && tgt.condLeft > 0 && tgt.hp <= tgt.maxHp * 0.5) {
        tgt.condLeft--; ev.push({ t: 'cond', u: tgt.id, name: tc.name });
        for (const fx of tc.fx) addStatus(s, tgt, tgt, fx, ev);
      }
      if (tgt.hp < tgt.maxHp * 0.3) {
        for (const h of hunters(s)) {
          const hc = condOf(h);
          if (hc.trig !== 'allyLow' || !alive(h) || h.condLeft <= 0) continue;
          const r = Math.max(...charOf(h).skills.filter((x) => x.target === 'ally').map((x) => x.range), 3);
          if (dist(h, tgt) > r) continue;
          h.condLeft--; ev.push({ t: 'cond', u: h.id, name: hc.name });
          heal(tgt, effAtk(h) * hc.heal * (1 + (setOf(h).healUp || 0)), ev);
          break;
        }
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
          ev.push({ t: 'break', u: tgt.id, part: p.id, name: p.name, by: src.id });
          if (alive(tgt)) {
            addStatus(s, src, tgt, { st: 'defDown', turns: 99 }, ev, true);
            tgt.st = tgt.st.filter((x) => x.id !== 'stopResist');
            addStatus(s, src, tgt, { st: 'stop', turns: 1 }, ev, true);
            if (!s.chasing) {
              for (const h of hunters(s)) {
                const hc = condOf(h);
                if (hc.trig !== 'partBreak' || !alive(h) || !alive(tgt)) continue;
                s.chasing = true;
                ev.push({ t: 'cond', u: h.id, name: hc.name });
                ev.push({ t: 'skill', u: h.id, si: -1, name: hc.name, type: 'phys', kind: 'cond', targets: [tgt.id] });
                dealDamage(s, h, tgt, { power: hc.power, type: 'phys' }, ev, null);
                s.chasing = false;
              }
            }
          }
        }
      }
    }
    if (tgt.boss && !tgt.rage && alive(tgt) && tgt.hp <= tgt.maxHp * 0.5) { tgt.rage = true; ev.push({ t: 'rage', u: tgt.id }); }
  }
  if (!alive(tgt) && !tgt._koSent) {
    tgt._koSent = true; tgt.st = []; tgt.gauge = 0; tgt.charge = null;
    ev.push({ t: 'ko', u: tgt.id });
    if (src.side === 'h' && tgt.side === 'm' && alive(src) && sc.trig === 'kill') { src.haste = sc.haste; ev.push({ t: 'cond', u: src.id, name: sc.name }); }
  }
  return v;
}

/* ---------- 技の対象（ハンター） ---------- */
// 技 si を、いまの場所（または x,z）から target に使えるか。使えないときは理由
function targetError(s, u, si, target, x, z) {
  const sk = skillsOf(u)[si];
  if (!sk) return '技がありません';
  if (u.cds[si] > 0) return 'まだ使えません';
  if (sk.kind === 'ultimate' && u.burst < 100) return '奥義ゲージがたまっていません';
  const px = x == null ? u.x : x, pz = z == null ? u.z : z;
  if (sk.target === 'enemy' || sk.target === 'enemyArea') {
    const t = enemyOfTarget(s, target);
    if (!t || !alive(t)) return '敵をえらんでください';
    if (spanDist(px, pz, 1, t.x, t.z, t.size) > sk.range) return '射程の外です';
  } else if (sk.target === 'ally') {
    const t = unit(s, target);
    if (!t || t.side !== 'h' || !alive(t)) return '味方をえらんでください';
    if (spanDist(px, pz, 1, t.x, t.z, 1) > sk.range) return '射程の外です';
  }
  return '';
}
function enemyOfTarget(s, target) {
  if (typeof target !== 'string') return null;
  const id = target.split('.')[0];
  const t = unit(s, id);
  return t && t.side === 'm' ? t : null;
}
function targetsOf(s, u, sk, target) {
  if (sk.target === 'enemy') return [enemyOfTarget(s, target)];
  if (sk.target === 'enemyArea') { const c = enemyOfTarget(s, target); return enemies(s).filter((e) => alive(e) && (e === c || dist(c, e) <= sk.area)); }
  if (sk.target === 'self') return [u];
  if (sk.target === 'ally') return [unit(s, target)];
  if (sk.target === 'allies') return hunters(s).filter((h) => (alive(h) || sk.revive) && dist(u, h) <= sk.area);
  return [];
}

function useSkill(s, u, si, target, ev) {
  const sk = skillsOf(u)[si];
  const targets = targetsOf(s, u, sk, target);
  let partId = null;
  const main = enemyOfTarget(s, target);
  if (main && typeof target === 'string' && target.includes('.')) { const p = main.parts.find((x) => x.id === target && !x.broken); if (p) partId = p.id; }
  ev.push({ t: 'skill', u: u.id, si, name: sk.name, type: sk.type, kind: sk.kind, targets: targets.map((x) => x.id), part: partId, center: main ? main.id : null });
  if (sk.revive) for (const t of targets) if (!alive(t)) { t.hp = Math.round(t.maxHp * sk.revive); t.gauge = 0; t._koSent = false; ev.push({ t: 'revive', u: t.id, v: t.hp }); }
  for (const t of targets) {
    if (!alive(t)) continue;
    if (sk.power > 0) for (let h = 0; h < (sk.hits || 1); h++) dealDamage(s, u, t, sk, ev, t === main ? partId : null);
    if (sk.cleanse) cleanse(t, ev);
    if (sk.heal) heal(t, ((sk.heal.atkPct ? effAtk(u) * sk.heal.atkPct : 0) + (sk.heal.maxHpPct ? t.maxHp * sk.heal.maxHpPct : 0)) * (1 + (setOf(u).healUp || 0)), ev);
    if (sk.fx) for (const fx of sk.fx) addStatus(s, u, t, fx, ev);
  }
  if (sk.kind === 'ultimate') u.burst = 0; else u.burst = Math.min(100, u.burst + 25);
  u.cds[si] = sk.cd ? sk.cd + 1 : 0;
  return sk;
}

/* ---------- ターン ---------- */
function effSpdOf(u) { return effSpd(u); }
function advance(s) {
  let best = null, bt = Infinity;
  for (const u of s.units) { if (!alive(u)) continue; const t = (1000 - u.gauge) / effSpd(u); if (t < bt - 1e-9) { bt = t; best = u; } }
  if (!best) return null;
  for (const u of s.units) if (alive(u)) u.gauge += effSpd(u) * bt;
  best.gauge = 1000;
  return best;
}
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
function endTurn(s, u, quick) {
  for (let i = 0; i < u.cds.length; i++) if (u.cds[i] > 0) u.cds[i]--;
  if (alive(u)) {
    for (const x of u.st) x.turns--;
    u.st = u.st.filter((x) => x.turns > 0);
    u.gauge = quick ? 1000 : (u.haste || 0);
    u.haste = 0;
  }
  s.turns++;
}
function checkEnd(s, ev) {
  if (s.phase === 'over') return true;
  if (!enemies(s).some(alive)) { s.phase = 'over'; s.win = true; s.turn = null; ev.push({ t: 'end', win: true }); return true; }
  if (!hunters(s).some(alive)) { s.phase = 'over'; s.win = false; s.turn = null; ev.push({ t: 'end', win: false }); return true; }
  return false;
}

/* ---------- 敵の行動 ---------- */
function enemyHitTiles(s, e, sk, tgt, fromX, fromZ) {
  if (sk.target === 'around') return tilesWithin(fromX, fromZ, e.size, sk.a);
  if (sk.target === 'area') return tilesWithin(tgt.x, tgt.z, 1, sk.a);
  return tilesOf(tgt);
}
function huntersOnTiles(s, tiles) {
  const set = new Set(tiles.map(([x, z]) => tkey(x, z)));
  return hunters(s).filter((h) => alive(h) && set.has(tkey(h.x, h.z)));
}
function enemyFire(s, e, si, tiles, ev, mainTarget) {
  const sk = skillsOf(e)[si];
  const hit = sk.target === 'one' ? (mainTarget && alive(mainTarget) ? [mainTarget] : []) : huntersOnTiles(s, tiles);
  ev.push({ t: 'skill', u: e.id, si, name: sk.name, type: sk.type, kind: sk.charge ? 'special' : 'normal', targets: hit.map((h) => h.id), tiles: sk.target === 'one' ? undefined : tiles });
  for (const h of hit) {
    if (!alive(h)) continue;
    if (sk.power > 0) for (let k = 0; k < (sk.hits || 1); k++) dealDamage(s, e, h, sk, ev, null);
    if (sk.fx) for (const fx of sk.fx) addStatus(s, e, h, fx, ev);
  }
  e.cds[si] = sk.cd ? sk.cd + 1 : 0;
}
function enemyAct(s, e, ev) {
  const sks = skillsOf(e);
  if (e.charge) { const c = e.charge; e.charge = null; enemyFire(s, e, c.si, c.tiles, ev, null); return; }
  const pool = [];
  sks.forEach((sk, i) => { if (!e.cds[i]) for (let k = 0; k < sk.w; k++) pool.push(i); });
  const order = [...new Set(pool.sort(() => rnd(s) - 0.5))];
  const R = reach(s, e);
  const live = hunters(s).filter(alive);
  if (!live.length) return;
  const taunter = live.find((h) => has(h, 'taunt'));
  // 技ごとに、届く相手と立ち位置を探す（引きつけ中のハンターがいれば、そちらを優先）
  for (const si of order) {
    const sk = sks[si];
    const reachOf = sk.target === 'around' ? sk.a : sk.range;
    let best = null;
    for (const h of (taunter && sk.target !== 'around' ? [taunter, ...live.filter((x) => x !== taunter)] : live)) {
      for (const v of R.values()) {
        const d = spanDist(v.x, v.z, e.size, h.x, h.z, 1);
        if (d > reachOf) continue;
        let score = -v.d * 0.1 + (1 - h.hp / h.maxHp) * 2 + (h === taunter ? 50 : 0) + rnd(s) * 0.5;
        if (sk.target === 'around') score += tilesWithin(v.x, v.z, e.size, sk.a).filter(([x, z]) => live.some((q) => q.x === x && q.z === z)).length * 3;
        if (!best || score > best.score) best = { score, v, h };
      }
      if (best && h === taunter) break;
    }
    if (!best) continue;
    if (best.v.x !== e.x || best.v.z !== e.z) { const path = pathTo(R, best.v.x, best.v.z); e.x = best.v.x; e.z = best.v.z; ev.push({ t: 'move', u: e.id, path }); }
    const tiles = enemyHitTiles(s, e, sk, best.h, e.x, e.z);
    if (sk.charge) {
      e.charge = { si, tiles };
      e.cds[si] = sk.cd + 1;
      ev.push({ t: 'telegraph', u: e.id, name: sk.name, text: sk.tell, tiles });
      return;
    }
    enemyFire(s, e, si, tiles, ev, best.h);
    return;
  }
  // どの技も届かない: いちばん近いハンターへ寄る
  let best = null;
  for (const v of R.values()) { const d = Math.min(...live.map((h) => spanDist(v.x, v.z, e.size, h.x, h.z, 1))); if (!best || d < best.d) best = { d, v }; }
  if (best && (best.v.x !== e.x || best.v.z !== e.z)) { const path = pathTo(R, best.v.x, best.v.z); e.x = best.v.x; e.z = best.v.z; ev.push({ t: 'move', u: e.id, path }); }
  ev.push({ t: 'wait', u: e.id });
}

/* ---------- 進行 ---------- */
function run(s, ev) {
  let guard = 0;
  while (s.phase !== 'over' && guard++ < 400) {
    if (checkEnd(s, ev)) break;
    const u = advance(s);
    if (!u) break;
    ev.push({ t: 'turn', u: u.id });
    const po = has(u, 'poison');
    if (po) {
      const pct = u.side === 'm' ? (ENEMIES[u.key].poisonPct || 0.05) : 0.06;
      const v = Math.max(1, Math.round(u.maxHp * pct));
      u.hp = Math.max(0, u.hp - v);
      ev.push({ t: 'poison', u: u.id, v });
      if (u.boss && !u.rage && alive(u) && u.hp <= u.maxHp * 0.5) { u.rage = true; ev.push({ t: 'rage', u: u.id }); }
      if (!alive(u)) { u._koSent = true; u.st = []; u.gauge = 0; u.charge = null; ev.push({ t: 'ko', u: u.id }); s.turns++; continue; }
    }
    if (has(u, 'stop')) { ev.push({ t: 'skip', u: u.id }); endTurn(s, u); continue; }
    if (u.side === 'm') { enemyAct(s, u, ev); endTurn(s, u); continue; }
    s.turn = u.id; s.phase = 'input'; s.moved = false;
    return;
  }
  checkEnd(s, ev);
}
function start(s) {
  const ev = [];
  // 条件アビリティ「戦いの始め」
  for (const h of hunters(s)) {
    const c = condOf(h);
    if (c.trig !== 'battleStart') continue;
    ev.push({ t: 'cond', u: h.id, name: c.name });
    for (const t of hunters(s)) for (const fx of c.fx) addStatus(s, h, t, fx, ev);
  }
  run(s, ev);
  return ev;
}

/* ---------- ハンターの操作 ---------- */
const turnErr = (s, uid) => (s.phase !== 'input' || s.turn !== uid ? 'いまは操作できません' : '');
function move(s, uid, x, z) {
  const e = turnErr(s, uid); if (e) return { ok: false, error: e };
  if (s.moved) return { ok: false, error: 'このターンはもう動きました' };
  const u = unit(s, uid);
  x |= 0; z |= 0;
  if (x === u.x && z === u.z) return { ok: false, error: '同じ場所です' };
  const R = reach(s, u);
  if (!R.has(tkey(x, z))) return { ok: false, error: 'そこへは動けません' };
  const path = pathTo(R, x, z);
  u.x = x; u.z = z; s.moved = true;
  return { ok: true, events: [{ t: 'move', u: uid, path }] };
}
function act(s, uid, si, target) {
  const e = turnErr(s, uid); if (e) return { ok: false, error: e };
  const u = unit(s, uid);
  si |= 0;
  const err = targetError(s, u, si, target);
  if (err) return { ok: false, error: err };
  const ev = [];
  const sk = useSkill(s, u, si, target, ev);
  endTurn(s, u, !!sk.quick);
  s.turn = null;
  run(s, ev);
  return { ok: true, events: ev };
}
function wait(s, uid) {
  const e = turnErr(s, uid); if (e) return { ok: false, error: e };
  const u = unit(s, uid);
  const ev = [{ t: 'wait', u: uid }];
  endTurn(s, u, false);
  s.turn = null;
  run(s, ev);
  return { ok: true, events: ev };
}

/* ---------- オート（ハンターのAI。時間切れ・オートで使う） ---------- */
function estDamage(s, u, sk, t) {
  if (!sk.power) return 0;
  const set = setOf(u);
  let v = effAtk(u) * sk.power * (sk.hits || 1) * (100 / (100 + effDef(t, sk.type))) * elemMul(u.el, t.el);
  if (sk.type === 'spec' && set.specUp) v *= 1 + set.specUp;
  if (sk.type === 'phys' && set.physUp) v *= 1 + set.physUp;
  if (set.vsDebuffed && isDebuffed(t)) v *= 1 + set.vsDebuffed;
  return v;
}
function scoreAction(s, u, si, target, px, pz) {
  const sk = skillsOf(u)[si];
  const hs = hunters(s);
  const healAmt = (t) => ((sk.heal.atkPct ? effAtk(u) * sk.heal.atkPct : 0) + (sk.heal.maxHpPct ? t.maxHp * sk.heal.maxHpPct : 0)) * (1 + (setOf(u).healUp || 0));
  if (sk.target === 'enemy' || sk.target === 'enemyArea') {
    const c = enemyOfTarget(s, target);
    const ts = sk.target === 'enemy' ? [c] : enemies(s).filter((e) => alive(e) && (e === c || dist(c, e) <= sk.area));
    let sc = 0;
    for (const t of ts) {
      const d = estDamage(s, u, sk, t);
      sc += Math.min(d, t.hp) / t.maxHp * 100 + (d >= t.hp ? 35 : 0) + (t.boss ? d / t.maxHp * 60 : 0);
      if (sk.fx) for (const fx of sk.fx) if (!has(t, fx.st) && !(fx.st === 'stop' && has(t, 'stopResist'))) sc += fx.st === 'stop' ? 30 : 14;
    }
    if (sk.kind === 'ultimate') sc += 15;
    return sc;
  }
  if (sk.target === 'ally') {
    const t = unit(s, target);
    const miss = t.maxHp - t.hp;
    if (miss < t.maxHp * 0.3) return -1;
    return Math.min(healAmt(t), miss) / t.maxHp * 140;
  }
  if (sk.target === 'allies') {
    let sc = 0;
    for (const t of hs) {
      if (dist({ x: px, z: pz, size: 1 }, t) > sk.area) continue;
      if (!alive(t)) { if (sk.revive) sc += 120; continue; }
      if (sk.heal) { const miss = t.maxHp - t.hp; if (miss > t.maxHp * 0.15) sc += Math.min(healAmt(t), miss) / t.maxHp * 70; }
      if (sk.cleanse && isDebuffed(t)) sc += 22;
      if (sk.fx) for (const fx of sk.fx) if (!has(t, fx.st)) sc += fx.st === 'guard' ? (dangerSet(s).has(tkey(t.x, t.z)) ? 30 : 9) : 8;
    }
    return sc - (sk.kind === 'ultimate' ? 30 : 6);
  }
  if (sk.target === 'self') {
    let sc = 0;
    if (sk.heal) { const miss = u.maxHp - u.hp; if (miss > u.maxHp * 0.35) sc += Math.min(healAmt(u), miss) / u.maxHp * 110; }
    if (sk.fx) for (const fx of sk.fx) {
      if (has(u, fx.st)) continue;
      if (fx.st === 'atkUp') sc += sk.quick ? 34 : 18;
      else if (fx.st === 'taunt') sc += enemies(s).some((e) => alive(e) && dist({ x: px, z: pz, size: 1 }, e) <= 5) ? 30 : 4;
      else sc += 10;
    }
    return sc;
  }
  return 0;
}
// いまの番のハンターがどう動くか。{ move:[x,z]|null, si, target } または { move, wait:true }
function aiPlan(s, uid) {
  const u = unit(s, uid);
  const R = s.moved ? new Map([[tkey(u.x, u.z), { x: u.x, z: u.z, d: 0 }]]) : reach(s, u);
  const danger = dangerSet(s);
  const sks = skillsOf(u);
  const es = enemies(s).filter(alive), hs = hunters(s);
  let best = null;
  const consider = (score, tile, si, target) => {
    if (score <= 0) return;
    let sc = score - (danger.has(tkey(tile.x, tile.z)) ? 80 : 0) - tile.d * 0.2;
    if (!best || sc > best.sc) best = { sc, tile, si, target };
  };
  for (const tile of R.values()) {
    for (let si = 0; si < sks.length; si++) {
      const sk = sks[si];
      if (u.cds[si] > 0 || (sk.kind === 'ultimate' && u.burst < 100)) continue;
      if (sk.target === 'enemy' || sk.target === 'enemyArea') {
        for (const e of es) {
          if (spanDist(tile.x, tile.z, 1, e.x, e.z, e.size) > sk.range) continue;
          const part = e.parts.filter((p) => !p.broken).sort((a, b) => a.hp - b.hp)[0];
          const tgt = part ? part.id : e.id;
          // 遠くから撃てる技は、射程ぎりぎりから撃つほうが安全
          const safe = sk.range > 1 ? Math.min(spanDist(tile.x, tile.z, 1, e.x, e.z, e.size), sk.range) * 1.5 : 0;
          consider(scoreAction(s, u, si, tgt, tile.x, tile.z) + safe, tile, si, tgt);
        }
      } else if (sk.target === 'ally') {
        for (const h of hs) if (alive(h) && spanDist(tile.x, tile.z, 1, h.x, h.z, 1) <= sk.range) consider(scoreAction(s, u, si, h.id, tile.x, tile.z), tile, si, h.id);
      } else consider(scoreAction(s, u, si, null, tile.x, tile.z), tile, si, null);
    }
  }
  if (best) return { move: best.tile.x !== u.x || best.tile.z !== u.z ? [best.tile.x, best.tile.z] : null, si: best.si, target: best.target };
  // 何も届かない: いちばん近い敵へ寄る（危ないマスはさける）。回復役は味方から離れすぎない
  let go = null;
  for (const tile of R.values()) {
    if (danger.has(tkey(tile.x, tile.z))) continue;
    const d = es.length ? Math.min(...es.map((e) => spanDist(tile.x, tile.z, 1, e.x, e.z, e.size))) : 0;
    if (!go || d < go.d) go = { d, tile };
  }
  return { move: go && (go.tile.x !== u.x || go.tile.z !== u.z) ? [go.tile.x, go.tile.z] : null, wait: true };
}
function aiTurn(s) {
  if (s.phase !== 'input' || !s.turn) return { ok: false, error: 'no turn' };
  const uid = s.turn, plan = aiPlan(s, uid), ev = [];
  if (plan.move && !s.moved) { const r = move(s, uid, plan.move[0], plan.move[1]); if (r.ok) ev.push(...r.events); }
  const r = plan.wait ? wait(s, uid) : act(s, uid, plan.si, plan.target);
  if (!r.ok) { const w = wait(s, uid); return { ok: w.ok, events: ev.concat(w.events || []) }; }
  return { ok: true, events: ev.concat(r.events) };
}

/* ---------- ブラウザへ送る形 ---------- */
function view(s) {
  const cur = s.turn ? unit(s, s.turn) : null;
  return {
    stage: s.stage, phase: s.phase, win: s.win, turn: s.turn, turns: s.turns, moved: s.moved, w: W, h: H, blocks: s.blocks,
    order: timeline(s, 8),
    reach: cur && s.phase === 'input' && !s.moved ? [...reach(s, cur).values()].map((v) => [v.x, v.z]) : [],
    danger: [...dangerSet(s)].map((k) => k.split(',').map(Number)),
    units: s.units.map((u) => ({
      id: u.id, side: u.side, key: u.key, name: u.name, el: u.el, rarity: u.rarity, role: u.role, shape: u.shape, boss: u.boss,
      x: u.x, z: u.z, size: u.size, mov: u.mov, hp: u.hp, maxHp: u.maxHp, burst: u.burst, cds: u.cds,
      st: u.st.map((x) => ({ id: x.id, turns: x.turns })), owner: u.owner, parts: u.parts, rage: u.rage, charging: !!u.charge,
    })),
  };
}
function defs() {
  const pick = (sk) => ({ kind: sk.kind, name: sk.name, type: sk.type, target: sk.target, range: sk.range, area: sk.area, cd: sk.cd || 0, desc: sk.desc || '' });
  const chars = {};
  for (const k of Object.keys(CHARACTERS)) {
    const c = CHARACTERS[k];
    chars[k] = { name: c.name, rarity: c.rarity, el: c.el, role: c.role, look: c.look, mov: c.mov, hp: c.hp, atk: c.atk, def: c.def, res: c.res, spd: c.spd,
                 set: { name: c.set.name, desc: c.set.desc }, cond: { name: c.cond.name, desc: c.cond.desc }, skills: c.skills.map(pick) };
  }
  const en = {};
  for (const k of Object.keys(ENEMIES)) { const e = ENEMIES[k]; en[k] = { name: e.name, el: e.el, shape: e.shape, size: e.size, boss: !!e.boss }; }
  const stages = {};
  for (const k of STAGE_ORDER) { const t = STAGES[k]; stages[k] = { world: t.world, no: t.no, name: t.name, star: t.star, boss: !!t.boss, need: prevStage(k), enemies: t.enemies.map((e) => e[0]), first: t.first, again: t.again }; }
  const status = {};
  for (const k of Object.keys(STATUS)) status[k] = STATUS[k];
  return { chars, enemies: en, stages, stageOrder: STAGE_ORDER, status, elName: EL_NAME, kindName: KIND_NAME, rKeys: R_KEYS, w: W, h: H };
}

module.exports = {
  W, H, EL_NAME, KIND_NAME, RARITY, STATUS, CHARACTERS, ENEMIES, STAGES, STAGE_ORDER, R_KEYS, prevStage,
  elemMul, createBattle, start, move, act, wait, aiPlan, aiTurn, targetError, reach, dangerSet, view, defs, timeline,
  unit, hunters, enemies, has, effSpd, effAtk, skillsOf, addStatus, rnd, dist, spanDist, tkey,
};
