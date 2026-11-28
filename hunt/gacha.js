'use strict';
/*
 * ガチャ（彩石で引く）。抽選はすべてサーバーの中で、crypto の乱数で行う。
 * - 確率: R 78% / SR 19% / UR 3%
 * - 10連は10回目が SR 以上確定
 * - 天井: そのガチャで70回引くまでに UR が出なければ、70回目は UR（ピックアップがあればピックアップ）
 * - ピックアップ: そのレアが出たとき、半分の確率でピックアップのキャラになる
 * - 同じキャラが出たら「重ね」（最大4回・1回につき体力と攻撃力 +4%）。重ねきったあとは彩石に戻す
 */
const crypto = require('crypto');
const E = require('./engine.js');

const RATES = { R: 78, SR: 19, UR: 3 };
const PITY = 70;
const REFUND = { R: 10, SR: 50, UR: 200 };
const MAX_DUP = 4;

const BANNERS = [
  { id: 'standard', name: '彩の祈り', sub: '常設', desc: 'すべてのキャラが出るガチャ。', cost1: 150, cost10: 1500, color: '#d9bf86' },
  { id: 'akatsuki', name: '紅月の剣聖', sub: 'ピックアップ', desc: 'UR アカツキの出る確率アップ！ SR ホムラ・ユカリもアップ。',
    cost1: 150, cost10: 1500, upUR: ['akatsuki'], upSR: ['homura', 'yukari'], color: '#ff6f61' },
  { id: 'sky', name: '蒼天と翠嵐', sub: 'ピックアップ', desc: 'UR ソラ・ミドリの出る確率アップ！ SR ヨモギ・アオイもアップ。',
    cost1: 150, cost10: 1500, upUR: ['sora', 'midori'], upSR: ['yomogi', 'aoi'], color: '#5cb2ff' },
  { id: 'thunder', name: '雷と紫電', sub: 'ピックアップ', desc: 'UR キララ・シグレの出る確率アップ！ SR コハク・シズクもアップ。',
    cost1: 150, cost10: 1500, upUR: ['kirara', 'shigure'], upSR: ['kohaku', 'shizuku'], color: '#f6d256' },
  { id: 'beginner', name: 'はじめての10連', sub: '1回だけ', desc: '1回だけ引ける半額の10連。SR 以上が1体確定。',
    cost10: 750, once: true, color: '#6fd58a' },
];

const poolOf = (rarity) => Object.keys(E.CHARACTERS).filter((k) => E.CHARACTERS[k].rarity === rarity);
const pick = (arr) => arr[crypto.randomInt(0, arr.length)];

function rollRarity() {
  const r = crypto.randomInt(0, 100);
  return r < RATES.UR ? 'UR' : r < RATES.UR + RATES.SR ? 'SR' : 'R';
}
function keyFor(banner, rarity) {
  const up = rarity === 'UR' ? banner.upUR : rarity === 'SR' ? banner.upSR : null;
  if (up && up.length && crypto.randomInt(0, 2) === 0) return pick(up);
  return pick(poolOf(rarity));
}

/* 引く。pity は「前回 UR が出てから何回引いたか」。戻り値 { keys, pity } */
function roll(banner, count, pity) {
  const keys = [];
  let p = pity | 0, gotHigh = false;
  for (let i = 0; i < count; i++) {
    p++;
    let rarity = rollRarity();
    if (p >= PITY) rarity = 'UR';
    if (count === 10 && i === 9 && !gotHigh && rarity === 'R') rarity = crypto.randomInt(0, RATES.SR + RATES.UR) < RATES.UR ? 'UR' : 'SR';
    if (rarity !== 'R') gotHigh = true;
    let key = keyFor(banner, rarity);
    if (rarity === 'UR' && p >= PITY && banner.upUR) key = pick(banner.upUR);   // 天井はピックアップ
    if (rarity === 'UR') p = 0;
    keys.push(key);
  }
  return { keys, pity: p };
}

function info() {
  return {
    rates: RATES, pity: PITY, refund: REFUND, maxDup: MAX_DUP,
    banners: BANNERS.map((b) => ({ id: b.id, name: b.name, sub: b.sub, desc: b.desc, cost1: b.cost1 || null, cost10: b.cost10, once: !!b.once,
      upUR: b.upUR || [], upSR: b.upSR || [], color: b.color })),
  };
}

module.exports = { BANNERS, RATES, PITY, REFUND, MAX_DUP, roll, info, poolOf };
