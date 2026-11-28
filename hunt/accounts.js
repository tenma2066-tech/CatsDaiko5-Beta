'use strict';
/*
 * アカウント（ユーザー名＋パスワード）と、個人のデータ。
 *   持っているキャラ（重ねた数）・編成・ステージの進み・戦績・実績・彩石・ガチャの天井・ログインボーナス。
 * - パスワードは scrypt のハッシュだけを保存する（元のパスワードは保存しない・戻せない）。
 * - ログイン中のしるし（セッション）も、ハッシュにして保存する。サーバーを入れ直してもログインは続く。
 * - 保存先は HUNT_DATA_DIR/accounts.json（書き込みは一時ファイル → 置き換え）。
 * - 彩石・キャラ・実績が増える処理は、すべてサーバーの中だけで行う。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const E = require('./engine.js');
const G = require('./gacha.js');

const NAME_RE = /^[A-Za-z0-9_぀-ヿ㐀-鿿ｦ-ﾟ]{3,12}$/;   // 英数字・ひらがな・カタカナ・漢字 3〜12文字
const PASS_MIN = 6, PASS_MAX = 64;
const MAX_SESSIONS = 5;
const SCRYPT = { N: 16384, r: 8, p: 1, len: 32 };
const START_STONES = 3000;   // はじめての人への贈り物（10連2回ぶん）
const DAILY_STONES = 300;    // ログインボーナス（日本時間で1日1回）

const owned = (u) => Object.keys(u.chars || {});
const countBy = (u, r) => owned(u).filter((k) => E.CHARACTERS[k] && E.CHARACTERS[k].rarity === r).length;
const stageCleared = (u, k) => !!(u.stages && u.stages[k] && u.stages[k].clears > 0);
// 実績。check(u) が true になったら達成。reward は受け取れる彩石
const ACHIEVEMENTS = [
  { id: 'first_hunt', name: 'はじめての狩り', desc: 'ステージを1つクリアする', reward: 100, check: (u) => u.stats.wins >= 1 },
  { id: 'meadow', name: '草原の覇者', desc: '「草原の主」（1-5）をクリアする', reward: 300, check: (u) => stageCleared(u, '1-5') },
  { id: 'ex1', name: '泥沼を越えて', desc: '特別討伐 EX1 をクリアする', reward: 400, check: (u) => stageCleared(u, 'ex1') },
  { id: 'ex2', name: '雷角を折る者', desc: '特別討伐 EX2 をクリアする', reward: 600, check: (u) => stageCleared(u, 'ex2') },
  { id: 'breaker', name: '部位破壊の達人', desc: '部位を合計10回壊す', reward: 200, check: (u) => u.stats.breaks >= 10 },
  { id: 'coop', name: '仲間とともに', desc: '2人以上でステージをクリアする', reward: 200, check: (u) => u.stats.coopWins >= 1 },
  { id: 'wins10', name: '熟練ハンター', desc: 'ステージを10回クリアする', reward: 500, check: (u) => u.stats.wins >= 10 },
  { id: 'gacha', name: '彩の祈り', desc: 'ガチャを1回引く', reward: 50, check: (u) => (u.stats.pulls || 0) >= 1 },
  { id: 'sr3', name: '頼もしい仲間', desc: 'SR のキャラを3人集める', reward: 200, check: (u) => countBy(u, 'SR') >= 3 },
  { id: 'ur1', name: '伝説との出会い', desc: 'UR のキャラを手に入れる', reward: 300, check: (u) => countBy(u, 'UR') >= 1 },
  { id: 'collect10', name: 'コレクター', desc: 'キャラを10人集める', reward: 400, check: (u) => owned(u).length >= 10 },
];

function jstDay(t) { return new Date((t || Date.now()) + 9 * 3600 * 1000).toISOString().slice(0, 10); }

function create(dataDir) {
  const file = path.join(dataDir, 'accounts.json');
  let db = { users: {} };
  try { db = JSON.parse(fs.readFileSync(file, 'utf8')); if (!db || typeof db.users !== 'object') db = { users: {} }; } catch (e) { db = { users: {} }; }
  let dirty = false, saving = false;

  function save() {
    dirty = true;
    if (saving) return;
    saving = true;
    setTimeout(() => {
      saving = false;
      if (!dirty) return;
      dirty = false;
      try {
        fs.mkdirSync(dataDir, { recursive: true });
        const tmp = file + '.tmp';
        fs.writeFileSync(tmp, JSON.stringify(db), { mode: 0o600 });
        fs.renameSync(tmp, file);
      } catch (e) { console.error('accounts save failed:', e.message); dirty = true; }
    }, 300);
  }
  function flush() {
    if (!dirty) return;
    try { fs.mkdirSync(dataDir, { recursive: true }); fs.writeFileSync(file + '.tmp', JSON.stringify(db), { mode: 0o600 }); fs.renameSync(file + '.tmp', file); dirty = false; } catch (e) { /* 次の保存でやり直す */ }
  }

  const keyOf = (name) => String(name).normalize('NFKC').toLowerCase();
  const sha = (t) => crypto.createHash('sha256').update(String(t)).digest('hex');
  const hashPw = (pw, salt) => new Promise((res, rej) => crypto.scrypt(String(pw), salt, SCRYPT.len, { N: SCRYPT.N, r: SCRYPT.r, p: SCRYPT.p }, (e, k) => (e ? rej(e) : res(k.toString('hex')))));
  const blankStats = () => ({ hunts: 0, wins: 0, coopWins: 0, breaks: 0, pulls: 0, byStage: {} });

  /* 古い形のデータを、いまの形にそろえる（キャラ・編成・はじめての彩石など） */
  function ensure(u) {
    if (!u.stats) u.stats = blankStats();
    if (!u.stats.byStage) u.stats.byStage = {};
    if (!u.chars) { u.chars = {}; for (const k of E.R_KEYS) u.chars[k] = { dup: 0, got: Date.now() }; }
    for (const k of Object.keys(u.chars)) if (!E.CHARACTERS[k]) delete u.chars[k];
    if (!Array.isArray(u.deck) || !u.deck.length) u.deck = E.R_KEYS.slice();
    u.deck = u.deck.filter((k) => u.chars[k]).slice(0, 5);
    if (!u.stages) u.stages = {};
    if (!u.pity) u.pity = {};
    if (!u.claimed) u.claimed = {};
    if (!u.starter) { u.starter = Date.now(); u.stones = (u.stones || 0) + START_STONES; }
    if (!u.log) u.log = [];
    return u;
  }
  const get = (k) => (db.users[k] ? ensure(db.users[k]) : null);

  function checkInput(name, pw) {
    if (typeof name !== 'string' || !NAME_RE.test(name.normalize('NFKC'))) return 'ユーザー名は 3〜12文字（英数字・ひらがな・カタカナ・漢字）で入力してください。';
    if (typeof pw !== 'string' || pw.length < PASS_MIN || pw.length > PASS_MAX) return `パスワードは ${PASS_MIN}〜${PASS_MAX}文字で入力してください。`;
    return '';
  }
  function newSession(u) {
    const token = crypto.randomBytes(24).toString('hex');
    u.sessions = (u.sessions || []).concat(sha(token)).slice(-MAX_SESSIONS);
    save();
    return token;
  }

  async function register(name, pw) {
    const err = checkInput(name, pw);
    if (err) return { error: err };
    name = name.normalize('NFKC');
    const k = keyOf(name);
    if (db.users[k]) return { error: 'そのユーザー名は、もう使われています。' };
    const salt = crypto.randomBytes(16).toString('hex');
    const hash = await hashPw(pw, salt);
    if (db.users[k]) return { error: 'そのユーザー名は、もう使われています。' };
    const u = db.users[k] = ensure({ name, salt, hash, created: Date.now(), stones: 0, sessions: [] });
    return { user: k, session: newSession(u) };
  }

  async function login(name, pw) {
    const bad = { error: 'ユーザー名かパスワードが違います。' };
    if (typeof name !== 'string' || typeof pw !== 'string' || pw.length > PASS_MAX || name.length > 40) return bad;
    const k = keyOf(name), u = db.users[k];
    // いないユーザーでも同じだけ計算して、いるかどうかを時間から読めないようにする
    const hash = await hashPw(pw, u ? u.salt : '00000000000000000000000000000000');
    if (!u || !crypto.timingSafeEqual(Buffer.from(hash, 'hex'), Buffer.from(u.hash, 'hex'))) return bad;
    ensure(u);
    return { user: k, session: newSession(u) };
  }

  function resume(token) {
    if (typeof token !== 'string' || token.length !== 48) return null;
    const h = sha(token);
    for (const k of Object.keys(db.users)) if ((db.users[k].sessions || []).includes(h)) return k;
    return null;
  }
  function logout(k, token) {
    const u = db.users[k]; if (!u) return;
    const h = sha(token); u.sessions = (u.sessions || []).filter((x) => x !== h); save();
  }

  /* 画面に出す自分のデータ */
  function profile(k) {
    const u = get(k); if (!u) return null;
    const s = u.stats;
    return {
      name: u.name, stones: u.stones, hunts: s.hunts, wins: s.wins, breaks: s.breaks, pulls: s.pulls || 0,
      chars: u.chars, deck: u.deck, stages: u.stages, pity: u.pity, beginnerUsed: !!u.beginnerUsed,
      daily: u.daily !== jstDay(), log: u.log.slice(-20),
      achievements: ACHIEVEMENTS.map((a) => ({ id: a.id, name: a.name, desc: a.desc, reward: a.reward,
        state: u.claimed[a.id] ? 'claimed' : a.check(u) ? 'ready' : 'locked' })),
    };
  }
  const readyIds = (u) => new Set(ACHIEVEMENTS.filter((a) => a.check(u)).map((a) => a.id));
  const newly = (u, before) => ACHIEVEMENTS.filter((a) => a.check(u) && !before.has(a.id)).map((a) => a.name);

  /* ステージの結果を記録する。戻り値 { stones, first, unlocked } */
  function record(k, r) {
    const u = get(k); if (!u) return null;
    const before = readyIds(u), s = u.stats;
    s.hunts++; s.breaks += Math.max(0, Math.min(10, r.breaks | 0));
    const bs = s.byStage[r.stage] || (s.byStage[r.stage] = { hunts: 0, wins: 0, best: 0 });
    bs.hunts++;
    let stones = 0, first = false;
    if (r.win) {
      s.wins++; bs.wins++; if (r.players > 1) s.coopWins++;
      if (!bs.best || r.turns < bs.best) bs.best = r.turns;
      const st = E.STAGES[r.stage];
      const prog = u.stages[r.stage] || (u.stages[r.stage] = { clears: 0, best: 0 });
      first = prog.clears === 0;
      prog.clears++; if (!prog.best || r.turns < prog.best) prog.best = r.turns;
      stones = st ? (first ? st.first : st.again) : 0;
      u.stones += stones;
    }
    save();
    return { stones, first, unlocked: newly(u, before) };
  }

  /* 実績の報酬を受け取る */
  function claim(k, id) {
    const u = get(k); if (!u) return { error: 'ログインしてください。' };
    const a = ACHIEVEMENTS.find((x) => x.id === id);
    if (!a) return { error: 'その実績はありません。' };
    if (u.claimed[id]) return { error: 'もう受け取っています。' };
    if (!a.check(u)) return { error: 'まだ達成していません。' };
    u.claimed[id] = Date.now(); u.stones += a.reward; save();
    return { reward: a.reward, stones: u.stones };
  }

  /* ログインボーナス（日本時間で1日1回） */
  function daily(k) {
    const u = get(k); if (!u) return { error: 'ログインしてください。' };
    const today = jstDay();
    if (u.daily === today) return { error: '今日のログインボーナスは受け取り済みです。' };
    u.daily = today; u.stones += DAILY_STONES; save();
    return { reward: DAILY_STONES, stones: u.stones };
  }

  /* 編成を変える（持っているキャラ・重なりなし・1〜5人） */
  function setDeck(k, keys) {
    const u = get(k); if (!u) return { error: 'ログインしてください。' };
    if (!Array.isArray(keys)) return { error: '編成が正しくありません。' };
    const d = [...new Set(keys.map(String))].filter((x) => u.chars[x]).slice(0, 5);
    if (!d.length) return { error: '1人以上えらんでください。' };
    u.deck = d; save();
    return { deck: d };
  }

  /* ガチャを引く。戻り値 { results:[{key, rarity, isNew, dup, refund}], stones, pity } */
  function pull(k, bannerId, count) {
    const u = get(k); if (!u) return { error: 'ガチャはログインすると引けます。' };
    const b = G.BANNERS.find((x) => x.id === bannerId);
    if (!b) return { error: 'そのガチャはありません。' };
    count = count === 10 ? 10 : 1;
    if (count === 1 && !b.cost1) return { error: 'このガチャは10連だけです。' };
    if (b.once && u.beginnerUsed) return { error: 'このガチャはもう引きました。' };
    const cost = count === 10 ? b.cost10 : b.cost1;
    if (u.stones < cost) return { error: `彩石が足りません（必要 ${cost.toLocaleString()} ／ 所持 ${u.stones.toLocaleString()}）。` };
    const before = readyIds(u);
    u.stones -= cost;
    const r = G.roll(b, count, u.pity[b.id] || 0);
    u.pity[b.id] = r.pity;
    if (b.once) u.beginnerUsed = true;
    const results = r.keys.map((key) => {
      const c = E.CHARACTERS[key], have = u.chars[key];
      if (!have) { u.chars[key] = { dup: 0, got: Date.now() }; return { key, rarity: c.rarity, isNew: true, dup: 0, refund: 0 }; }
      if (have.dup < G.MAX_DUP) { have.dup++; return { key, rarity: c.rarity, isNew: false, dup: have.dup, refund: 0 }; }
      const refund = G.REFUND[c.rarity]; u.stones += refund;
      return { key, rarity: c.rarity, isNew: false, dup: have.dup, refund };
    });
    u.stats.pulls = (u.stats.pulls || 0) + count;
    u.log.push({ t: Date.now(), b: b.id, k: r.keys });
    u.log = u.log.slice(-30);
    save();
    return { results, stones: u.stones, pity: r.pity, unlocked: newly(u, before) };
  }

  /* 戦いに連れていくキャラ（{ key, dup } の並び）。ゲストは R だけ */
  function partyOf(k) {
    const u = get(k); if (!u) return null;
    return u.deck.map((key) => ({ key, dup: u.chars[key] ? u.chars[key].dup : 0 }));
  }
  function stageOpen(k, stage) {
    const prev = E.prevStage(stage);
    if (!prev) return true;
    const u = get(k);
    return !!(u && stageCleared(u, prev));
  }

  const nameOf = (k) => (db.users[k] ? db.users[k].name : '');
  return { register, login, resume, logout, profile, record, claim, daily, setDeck, pull, partyOf, stageOpen, nameOf, flush,
           count: () => Object.keys(db.users).length };
}

module.exports = { create, ACHIEVEMENTS, NAME_RE, START_STONES, DAILY_STONES, jstDay };
