'use strict';
/*
 * アカウント（ユーザー名＋パスワード）と、個人のデータ（戦績・実績・彩石）。
 * - パスワードは scrypt のハッシュだけを保存する（元のパスワードは保存しない・戻せない）。
 * - ログイン中のしるし（セッション）も、ハッシュにして保存する。サーバーを入れ直してもログインは続く。
 * - 保存先は HUNT_DATA_DIR/accounts.json（書き込みは一時ファイル → 置き換え）。
 * - 彩石や実績が増える処理は、すべてサーバーの中だけで行う（ブラウザからは「受け取る」としか言えない）。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const NAME_RE = /^[A-Za-z0-9_぀-ヿ㐀-鿿ｦ-ﾟ]{3,12}$/;   // 英数字・ひらがな・カタカナ・漢字 3〜12文字
const PASS_MIN = 6, PASS_MAX = 64;
const MAX_SESSIONS = 5;
const SCRYPT = { N: 16384, r: 8, p: 1, len: 32 };

// 実績。check(stats) が true になったら達成。reward は受け取れる彩石
const ACHIEVEMENTS = [
  { id: 'first_hunt', name: 'はじめての狩り', desc: '狩りに1回成功する', reward: 100, check: (s) => s.wins >= 1 },
  { id: 'q_hibashiri', name: '炎狼を狩る者', desc: 'ヒバシリを狩る', reward: 150, check: (s) => wins(s, 'hibashiri') >= 1 },
  { id: 'q_drogarm', name: '泥沼を越えて', desc: 'ドロガルムを狩る', reward: 250, check: (s) => wins(s, 'drogarm') >= 1 },
  { id: 'q_raizen', name: '雷角を折る者', desc: 'ライゼンを狩る', reward: 400, check: (s) => wins(s, 'raizen') >= 1 },
  { id: 'breaker', name: '部位破壊の達人', desc: '部位を合計10回壊す', reward: 200, check: (s) => s.breaks >= 10 },
  { id: 'coop', name: '仲間とともに', desc: '2人以上の狩りに成功する', reward: 200, check: (s) => s.coopWins >= 1 },
  { id: 'wins10', name: '熟練ハンター', desc: '狩りに10回成功する', reward: 500, check: (s) => s.wins >= 10 },
  { id: 'swift', name: '電光石火', desc: 'ヒバシリを50ターン以内に狩る', reward: 300, check: (s) => best(s, 'hibashiri') > 0 && best(s, 'hibashiri') <= 50 },
];
function wins(s, q) { return (s.byQuest[q] || {}).wins || 0; }
function best(s, q) { return (s.byQuest[q] || {}).best || 0; }
const WIN_STONES = { hibashiri: 20, drogarm: 35, raizen: 60 };   // 狩りに成功するたびにもらえる彩石

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
  const blankStats = () => ({ hunts: 0, wins: 0, coopWins: 0, breaks: 0, byQuest: {} });

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
    const u = db.users[k] = { name, salt, hash, created: Date.now(), stones: 0, stats: blankStats(), claimed: {}, sessions: [] };
    return { user: k, session: newSession(u) };
  }

  async function login(name, pw) {
    const bad = { error: 'ユーザー名かパスワードが違います。' };
    if (typeof name !== 'string' || typeof pw !== 'string' || pw.length > PASS_MAX || name.length > 40) return bad;
    const k = keyOf(name), u = db.users[k];
    // いないユーザーでも同じだけ計算して、いるかどうかを時間から読めないようにする
    const hash = await hashPw(pw, u ? u.salt : '00000000000000000000000000000000');
    if (!u || !crypto.timingSafeEqual(Buffer.from(hash, 'hex'), Buffer.from(u.hash, 'hex'))) return bad;
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
    const u = db.users[k]; if (!u) return null;
    const s = u.stats;
    return {
      name: u.name, stones: u.stones, hunts: s.hunts, wins: s.wins, breaks: s.breaks, byQuest: s.byQuest,
      achievements: ACHIEVEMENTS.map((a) => ({ id: a.id, name: a.name, desc: a.desc, reward: a.reward,
        state: u.claimed[a.id] ? 'claimed' : a.check(s) ? 'ready' : 'locked' })),
    };
  }

  /* 狩りの結果を記録する。戻り値は { stones（今回もらった彩石）, unlocked（新しく達成した実績の名前） } */
  function record(k, r) {
    const u = db.users[k]; if (!u) return null;
    const s = u.stats;
    const before = new Set(ACHIEVEMENTS.filter((a) => a.check(s)).map((a) => a.id));
    s.hunts++; s.breaks += Math.max(0, Math.min(8, r.breaks | 0));
    const q = s.byQuest[r.quest] || (s.byQuest[r.quest] = { hunts: 0, wins: 0, best: 0 });
    q.hunts++;
    let stones = 0;
    if (r.win) {
      s.wins++; q.wins++; if (r.players > 1) s.coopWins++;
      if (!q.best || r.turns < q.best) q.best = r.turns;
      stones = WIN_STONES[r.quest] || 0; u.stones += stones;
    }
    save();
    return { stones, unlocked: ACHIEVEMENTS.filter((a) => a.check(s) && !before.has(a.id)).map((a) => a.name) };
  }

  /* 実績の報酬を受け取る */
  function claim(k, id) {
    const u = db.users[k]; if (!u) return { error: 'ログインしてください。' };
    const a = ACHIEVEMENTS.find((x) => x.id === id);
    if (!a) return { error: 'その実績はありません。' };
    if (u.claimed[id]) return { error: 'もう受け取っています。' };
    if (!a.check(u.stats)) return { error: 'まだ達成していません。' };
    u.claimed[id] = Date.now(); u.stones += a.reward; save();
    return { reward: a.reward, stones: u.stones };
  }

  const nameOf = (k) => (db.users[k] ? db.users[k].name : '');
  return { register, login, resume, logout, profile, record, claim, nameOf, flush, count: () => Object.keys(db.users).length };
}

module.exports = { create, ACHIEVEMENTS, NAME_RE };
