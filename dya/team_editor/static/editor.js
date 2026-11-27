// Original Team Editor — pure client-side.
// Initial defaults are injected by proxy.js as window.__EDITOR_DEFAULTS.
// Persistence is JSON file download/upload + localStorage handoff to /offline?original=1.

const BATTER_FLAGS = [
  { code: 3,  name: '内野安打' },
  { code: 4,  name: '流し打ち' },
  { code: 5,  name: '引っ張り' },
  { code: 6,  name: '粘り強い' },
  { code: 7,  name: 'チャンス強い' },
  { code: 8,  name: 'チャンス弱い' },
  { code: 9,  name: '三振' },
  { code: 10, name: 'リードオフマン' },
  { code: 11, name: '初球狙い' },
  { code: 22, name: '反撃の狼煙' },
  { code: 23, name: '逆転弾' },
  { code: 24, name: 'マシンガン' },
];

const PITCHER_FLAGS = [
  { code: 12, name: 'カットボール (スライダー強化)' },
  { code: 13, name: 'スプリット (フォーク強化)' },
  { code: 14, name: 'ツーシーム (シュート強化)' },
  { code: 15, name: 'チェンジアップ (フォーク強化)' },
  { code: 16, name: 'サークルチェンジ (スクリュー強化)' },
  { code: 17, name: '縦スライダー (フォーク強化)' },
  { code: 18, name: '高速シンカー (スクリュー強化)' },
  { code: 19, name: 'スラーブ (カーブ強化)' },
  { code: 20, name: 'ノビ (ストレート補正)' },
  { code: 21, name: 'ホップ (ストレート補正)' },
];

const SECONDARY_ABILITIES = [
  { code: 0,  name: 'なし' },
  { code: 62, name: 'リード弱' },
  { code: 65, name: 'リード強' },
  { code: 66, name: 'チャンス強 (62?)' },
  { code: 67, name: 'チャンス強 (67?)' },
  { code: 68, name: '盗塁' },
  { code: 69, name: 'サヨナラ男' },
];

// メイン能力（ゲームプレイで実際に参照される値）と
// 未使用エリア（dya.js は格納するがゲームプレイで読まない）を分離。
const BATTER_STAT_LABELS = [
  { key: 'batting_hand', label: '打席', special: 'hand' },
  { key: 'power',        label: '長打 (パワー)', group: 'main' },
  { key: 'contact',      label: 'ミート', group: 'main' },
  { key: 'eye',          label: '選球眼', group: 'main' },
  { key: 'speed',        label: '走力', group: 'main' },
  // 以下は ゲームプレイで未使用 (dya.js が読まない)。round-trip 保持のためのみ編集可。
  { key: 'defense_unused_0', label: '未使用 0', group: 'unused' },
  { key: 'defense_unused_1', label: '未使用 1', group: 'unused' },
  { key: 'defense_unused_2', label: '未使用 2', group: 'unused' },
  { key: 'defense_unused_3', label: '未使用 3', group: 'unused' },
  { key: 'defense_unused_4', label: '未使用 4', group: 'unused' },
  { key: 'defense_unused_5', label: '未使用 5', group: 'unused' },
  { key: 'defense_unused_6', label: '未使用 6', group: 'unused' },
  { key: 'defense_unused_7', label: '未使用 7', group: 'unused' },
  { key: 'defense_unused_8', label: '未使用 8', group: 'unused' },
];

const PITCHER_STAT_LABELS = [
  { key: 'handedness',     label: '利き腕', special: 'hand_p' },
  { key: 'speed',          label: '球速', group: 'main' },
  { key: 'control',        label: 'コントロール', group: 'main' },
  { key: 'stamina_rank',   label: 'スタミナ (0-9 → 最大値 8-60)', group: 'main' },
  { key: 'break_slider',   label: 'スライダー変化量', group: 'main' },
  { key: 'break_curve',    label: 'カーブ変化量', group: 'main' },
  { key: 'break_fork',     label: 'フォーク変化量', group: 'main' },
  { key: 'break_screw',    label: 'スクリュー変化量', group: 'main' },
  { key: 'break_shoot',    label: 'シュート変化量', group: 'main' },
];

const DEF_LABELS = ['投', '捕', '一', '二', '三', '遊', '左', '中', '右'];

const DISPLAY_KEYS = [
  { key: 'power',    label: 'パワー' },
  { key: 'contact',  label: 'ミート' },
  { key: 'speed',    label: '走力' },
  { key: 'defense',  label: '守備' },
  { key: 'overall',  label: '総合' },
  { key: 'pos_type', label: 'ポジ' },
];

let state = {
  data: null,
  teamIndex: 0,
  playerSlot: 0,
  faceRefList: [],
  dirty: false,
};

// ---------- Boot ----------

// エディタ内 autosave 用の sessionStorage キー（タブを閉じれば消える）。
// ゲームへの受け渡しは「💾 ダウンロード」 → /offline?original=1 でユーザがアップロード。
// 永続的な localStorage 保存は意図的にしない。
const SESSION_KEY = '__orig_teams_editor_session';

function getEmbeddedDefaults() {
  if (!window.__EDITOR_DEFAULTS) {
    throw new Error('window.__EDITOR_DEFAULTS が見つかりません。proxy 経由で /editor を開いてください。');
  }
  return JSON.parse(JSON.stringify(window.__EDITOR_DEFAULTS));
}

function loadInitialData() {
  // セッション復帰: 直前の編集中データがあれば優先
  try {
    const s = sessionStorage.getItem(SESSION_KEY);
    if (s) {
      const parsed = JSON.parse(s);
      if (parsed && Array.isArray(parsed.teams) && parsed.teams.length === 12) {
        console.log('[editor] restored editing session');
        return parsed;
      }
    }
  } catch (_) {}
  return getEmbeddedDefaults();
}

function bootWithData(data) {
  state.data = data;
  buildFaceRefList();
  renderTeams();
  selectTeam(0);
  setStatus('読み込み完了');
}

function boot() {
  state.data = loadInitialData();
  buildFaceRefList();
  renderTeams();
  selectTeam(0);
  bindGlobalActions();
  setStatus('準備完了');
}

function buildFaceRefList() {
  const seen = new Set();
  const list = [];
  for (const t of state.data.teams) {
    for (const p of t.players) {
      if (p.face_ref && !seen.has(p.face_ref)) {
        seen.add(p.face_ref);
        list.push({ id: p.face_ref, name: p.name });
      }
    }
  }
  list.sort((a, b) => a.id.localeCompare(b.id));
  state.faceRefList = list;
}

// ---------- Team list ----------

function renderTeams() {
  const ul = document.getElementById('teams-ul');
  ul.innerHTML = '';
  state.data.teams.forEach((t, idx) => {
    const li = document.createElement('li');
    li.dataset.teamIdx = idx;
    li.innerHTML = `<span class="team-color-dot" style="background:${t.color}"></span>
                    <span class="team-name">${escapeHtml(t.name)}</span>`;
    li.addEventListener('click', () => selectTeam(idx));
    ul.appendChild(li);
  });
  highlightActiveTeam();
}

function highlightActiveTeam() {
  document.querySelectorAll('#teams-ul li').forEach((li, idx) => {
    li.classList.toggle('active', idx === state.teamIndex);
  });
}

function selectTeam(idx) {
  state.teamIndex = idx;
  highlightActiveTeam();
  const team = state.data.teams[idx];

  // Team header inputs
  document.getElementById('team-name-input').value = team.name;
  document.getElementById('team-color-input').value = team.color;

  renderPlayerList();
  renderStartingOrder();
  // Auto-select first player
  selectPlayer(0);
}

document.getElementById('team-name-input').addEventListener('input', e => {
  state.data.teams[state.teamIndex].name = e.target.value;
  document.querySelector(`#teams-ul li[data-team-idx="${state.teamIndex}"] .team-name`).textContent = e.target.value;
  markDirty();
});
document.getElementById('team-color-input').addEventListener('input', e => {
  state.data.teams[state.teamIndex].color = e.target.value;
  document.querySelector(`#teams-ul li[data-team-idx="${state.teamIndex}"] .team-color-dot`).style.background = e.target.value;
  markDirty();
});

// ---------- Player list ----------

function renderPlayerList() {
  const ol = document.getElementById('players-ul');
  ol.innerHTML = '';
  const team = state.data.teams[state.teamIndex];
  team.players.forEach(p => {
    const li = document.createElement('li');
    li.dataset.slot = p.slot;
    const badge = p.is_pitcher ? `<span class="pitcher-badge">P</span>` : '';
    li.innerHTML = `${badge}<span class="player-name">${escapeHtml(p.name)}</span>`;
    li.addEventListener('click', () => selectPlayer(p.slot));
    ol.appendChild(li);
  });
  highlightActivePlayer();
}

function highlightActivePlayer() {
  document.querySelectorAll('#players-ul li').forEach(li => {
    li.classList.toggle('active', Number(li.dataset.slot) === state.playerSlot);
  });
}

function renderStartingOrder() {
  const root = document.getElementById('starting-order');
  root.innerHTML = '';
  const order = state.data.teams[state.teamIndex].starting_order;
  order.forEach((slotIdx, i) => {
    const div = document.createElement('div');
    div.className = 'slot';
    div.title = `${i+1}番: 選手 #${slotIdx}`;
    div.textContent = slotIdx;
    root.appendChild(div);
  });
}

// ---------- Player edit ----------

function selectPlayer(slot) {
  state.playerSlot = slot;
  highlightActivePlayer();
  const team = state.data.teams[state.teamIndex];
  const p = team.players.find(x => x.slot === slot);
  if (!p) return;
  renderEditForm(p);
}

function renderEditForm(p) {
  document.getElementById('edit-empty').hidden = true;
  document.getElementById('edit-form').hidden = false;
  document.getElementById('edit-player-title').textContent =
    `編集: ${p.name} (#${p.slot})`;

  // Basic fields
  setVal('f-name', p.name);
  setVal('f-skin', p.skin_color);
  setVal('f-is-pitcher', String(!!p.is_pitcher));

  // Face ref dropdown
  const fr = document.getElementById('f-face-ref');
  fr.innerHTML = '';
  for (const item of state.faceRefList) {
    const opt = document.createElement('option');
    opt.value = item.id;
    opt.textContent = `${item.id} - ${item.name}`;
    if (item.id === p.face_ref) opt.selected = true;
    fr.appendChild(opt);
  }

  // Secondary ability
  const sec = document.getElementById('f-secondary');
  sec.innerHTML = '';
  for (const a of SECONDARY_ABILITIES) {
    const opt = document.createElement('option');
    opt.value = a.code;
    opt.textContent = a.name;
    if (a.code === p.secondary_ability) opt.selected = true;
    sec.appendChild(opt);
  }

  // Batter stats
  renderStatGroup('batter-stats', BATTER_STAT_LABELS, p.batter_stats, (key, val) => {
    p.batter_stats[key] = val;
    markDirty();
  });

  // Pitcher stats
  const pf = document.getElementById('pitcher-fieldset');
  pf.hidden = !p.is_pitcher;
  document.getElementById('pitcher-flags-fieldset').hidden = !p.is_pitcher;
  if (p.is_pitcher) {
    if (!p.pitcher_stats) p.pitcher_stats = defaultPitcherStats();
    renderStatGroup('pitcher-stats', PITCHER_STAT_LABELS, p.pitcher_stats, (key, val) => {
      p.pitcher_stats[key] = val;
      markDirty();
    });
  }

  // Defensive eligibility (batter only; pitcher uses 9-digit slot for pitcher_stats)
  const defField = document.querySelector('fieldset:has(#defensive-elig)');
  if (defField) defField.hidden = p.is_pitcher;
  if (!p.is_pitcher) {
    if (!p.defensive_eligibility) p.defensive_eligibility = [1,1,1,1,1,1,1,1,1];
    renderDefensive(p);
  }

  // Special flags
  renderFlagGrid('batter-flags', BATTER_FLAGS, p.special_flags, (codes) => {
    p.special_flags = mergeFlagSet(p.special_flags, BATTER_FLAGS.map(f => f.code), codes);
    markDirty();
  });
  renderFlagGrid('pitcher-flags', PITCHER_FLAGS, p.special_flags, (codes) => {
    p.special_flags = mergeFlagSet(p.special_flags, PITCHER_FLAGS.map(f => f.code), codes);
    markDirty();
  });

  // Display hint
  renderDisplay(p);
}

function renderStatGroup(rootId, labels, stats, onChange) {
  const root = document.getElementById(rootId);
  root.innerHTML = '';

  // 「特殊 (打席/利き腕/初期調子)」「メイン能力」「未使用」を区別。
  const isSpecial = l => l.special != null;
  const isUnused  = l => l.group === 'unused';
  const isMain    = l => !isSpecial(l) && !isUnused(l);

  function makeRow(parent, { key, label, special }) {
    const row = document.createElement('div');
    row.className = 'stat-row';
    if (special === 'hand') {
      row.innerHTML = `
        <label>${label}</label>
        <select data-stat="${key}">
          <option value="1">右打</option>
          <option value="2">左打</option>
          <option value="3">switch</option>
        </select>
        <span class="stat-val"></span>`;
      const sel = row.querySelector('select');
      sel.value = String(stats[key]);
      sel.addEventListener('change', e => onChange(key, Number(e.target.value)));
    } else if (special === 'hand_p') {
      row.innerHTML = `
        <label>${label}</label>
        <select data-stat="${key}">
          <option value="1">右投</option>
          <option value="2">左投</option>
        </select>
        <span class="stat-val"></span>`;
      const sel = row.querySelector('select');
      sel.value = String(stats[key]);
      sel.addEventListener('change', e => onChange(key, Number(e.target.value)));
    } else if (special === 'cond') {
      row.innerHTML = `
        <label>${label}</label>
        <select data-stat="${key}">
          <option value="0">絶好調</option>
          <option value="1">好調</option>
          <option value="2">普通</option>
          <option value="3">不調</option>
          <option value="4">絶不調</option>
        </select>
        <span class="stat-val"></span>`;
      const sel = row.querySelector('select');
      sel.value = String(stats[key]);
      sel.addEventListener('change', e => onChange(key, Number(e.target.value)));
    } else {
      // Default: range 1-10 slider with color bar background
      row.innerHTML = `
        <label>${label}</label>
        <input type="range" min="1" max="10" step="1" data-stat="${key}">
        <span class="stat-val">--</span>`;
      const inp = row.querySelector('input');
      const out = row.querySelector('.stat-val');
      inp.value = stats[key] || 1;
      out.textContent = inp.value;
      updateStatColor(out, inp.value);
      inp.addEventListener('input', e => {
        const v = Number(e.target.value);
        out.textContent = v;
        updateStatColor(out, v);
        onChange(key, v);
      });
    }
    parent.appendChild(row);
  }

  // 1) special と main を root に直接配置
  for (const l of labels.filter(isSpecial)) makeRow(root, l);
  for (const l of labels.filter(isMain)) makeRow(root, l);

  // 2) 未使用は折り畳み可能なセクションへ
  const unusedLabels = labels.filter(isUnused);
  if (unusedLabels.length > 0) {
    const details = document.createElement('details');
    details.className = 'unused-stats';
    const summary = document.createElement('summary');
    summary.textContent = `▸ 未使用ステータス (${unusedLabels.length} 項目) — round-trip 保持のみ`;
    details.appendChild(summary);
    const inner = document.createElement('div');
    details.appendChild(inner);
    for (const l of unusedLabels) makeRow(inner, l);
    root.appendChild(details);
  }
}

function updateStatColor(el, v) {
  // 1-10 を色グラデで表示: 低=赤, 中=黄, 高=緑
  v = Number(v);
  let color = '#4ad28f';
  if (v <= 3) color = '#e25d5d';
  else if (v <= 5) color = '#e0a04a';
  else if (v <= 7) color = '#e0d24a';
  el.style.color = color;
}

function renderDefensive(p) {
  const root = document.getElementById('defensive-elig');
  root.innerHTML = '';
  p.defensive_eligibility.forEach((v, i) => {
    const row = document.createElement('div');
    row.className = 'stat-row';
    row.innerHTML = `
      <label>${DEF_LABELS[i]}</label>
      <input type="range" min="0" max="9" step="1">
      <span class="stat-val">${v}</span>`;
    const inp = row.querySelector('input');
    const out = row.querySelector('.stat-val');
    inp.value = v;
    inp.addEventListener('input', e => {
      const val = Number(e.target.value);
      out.textContent = val;
      p.defensive_eligibility[i] = val;
      markDirty();
    });
    root.appendChild(row);
  });
}

function renderFlagGrid(rootId, defs, currentFlags, onChange) {
  const root = document.getElementById(rootId);
  root.innerHTML = '';
  const set = new Set(currentFlags);
  for (const f of defs) {
    const label = document.createElement('label');
    label.innerHTML = `
      <input type="checkbox" value="${f.code}" ${set.has(f.code) ? 'checked' : ''}>
      <span class="ability-id">[${f.code}]</span>
      <span>${escapeHtml(f.name)}</span>`;
    const cb = label.querySelector('input');
    cb.addEventListener('change', () => {
      const codes = Array.from(root.querySelectorAll('input:checked')).map(x => Number(x.value));
      onChange(codes);
    });
    root.appendChild(label);
  }
}

function mergeFlagSet(existing, domainCodes, newSelectedInDomain) {
  // Keep flags outside the current domain, replace flags in the domain
  const outside = existing.filter(c => !domainCodes.includes(c));
  return [...outside, ...newSelectedInDomain].sort((a, b) => a - b);
}

function renderDisplay(p) {
  const root = document.getElementById('display-hint');
  root.innerHTML = '';
  const hint = p.display_hint || {};
  for (const { key, label } of DISPLAY_KEYS) {
    const row = document.createElement('div');
    row.className = 'dh-row';
    row.innerHTML = `
      <label>${label}</label>
      <input type="number" value="${hint[key] ?? 0}">`;
    const inp = row.querySelector('input');
    inp.addEventListener('input', e => {
      if (!p.display_hint) p.display_hint = {};
      p.display_hint[key] = Number(e.target.value);
      markDirty();
    });
    root.appendChild(row);
  }
}

// ---------- Form bindings for top-level player fields ----------

document.getElementById('f-name').addEventListener('input', e => {
  getCurrentPlayer().name = e.target.value;
  document.querySelector(`#players-ul li[data-slot="${state.playerSlot}"] .player-name`)
    .textContent = e.target.value;
  document.getElementById('edit-player-title').textContent =
    `編集: ${e.target.value} (#${state.playerSlot})`;
  markDirty();
});
document.getElementById('f-skin').addEventListener('input', e => {
  getCurrentPlayer().skin_color = e.target.value;
  markDirty();
});
document.getElementById('f-face-ref').addEventListener('change', e => {
  getCurrentPlayer().face_ref = e.target.value;
  markDirty();
});
document.getElementById('f-is-pitcher').addEventListener('change', e => {
  const p = getCurrentPlayer();
  p.is_pitcher = e.target.value === 'true';
  if (p.is_pitcher && !p.pitcher_stats) p.pitcher_stats = defaultPitcherStats();
  // Re-render to toggle pitcher fields
  renderEditForm(p);
  // Update list badge
  renderPlayerList();
  markDirty();
});
document.getElementById('f-secondary').addEventListener('change', e => {
  getCurrentPlayer().secondary_ability = Number(e.target.value);
  markDirty();
});

// ---------- Toolbar actions: New / Open / Download / Apply ----------

function bindGlobalActions() {
  document.getElementById('btn-new').addEventListener('click', newFromDefaults);
  document.getElementById('btn-open').addEventListener('click', () => {
    document.getElementById('file-input').click();
  });
  document.getElementById('file-input').addEventListener('change', handleFileOpen);
  document.getElementById('btn-download').addEventListener('click', downloadJson);

  document.getElementById('player-search').addEventListener('input', filterPlayers);
  document.getElementById('btn-clone').addEventListener('click', clonePlayerPrompt);
  document.getElementById('preset-select').addEventListener('change', applyPreset);
  window.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault();
      downloadJson();
    }
  });
  // 保存忘れ防止: 離脱時に未保存があれば確認
  window.addEventListener('beforeunload', e => {
    if (state.dirty) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

function newFromDefaults() {
  if (state.dirty && !confirm('編集中のデータを破棄して初期データに戻します。よろしいですか？')) return;
  state.data = getEmbeddedDefaults();
  buildFaceRefList();
  renderTeams();
  selectTeam(0);
  state.dirty = false;
  sessionStorage.removeItem(SESSION_KEY);
  setStatus('✓ 初期データを読み込みました');
}

function handleFileOpen(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = '';   // 同じファイルを連続選択できるようにリセット
  if (!file) return;
  if (state.dirty && !confirm('編集中のデータを破棄して読み込みますか？')) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(reader.result);
      if (!parsed || !Array.isArray(parsed.teams) || parsed.teams.length !== 12) {
        throw new Error('不正な JSON 形式（teams 配列が 12 要素ではありません）');
      }
      state.data = parsed;
      buildFaceRefList();
      renderTeams();
      selectTeam(0);
      state.dirty = false;
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(state.data));
      setStatus('✓ 「' + file.name + '」を読み込みました');
    } catch (err) {
      alert('読み込み失敗: ' + err.message);
      setStatus('✗ 読み込み失敗');
    }
  };
  reader.onerror = () => {
    alert('ファイル読込エラー');
    setStatus('✗ ファイル読込エラー');
  };
  reader.readAsText(file, 'utf-8');
}

function downloadJson() {
  state.data.modified = new Date().toISOString();
  const json = JSON.stringify(state.data, null, 2);
  const blob = new Blob([json], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  a.href = url;
  a.download = `original_teams_${ts}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  state.dirty = false;
  setStatus('✓ ダウンロード: ' + a.download);
}


// ---------- C: Search filter ----------

function filterPlayers() {
  const q = document.getElementById('player-search').value.trim().toLowerCase();
  document.querySelectorAll('#players-ul li').forEach(li => {
    const nm = li.querySelector('.player-name').textContent.toLowerCase();
    li.classList.toggle('hidden', !!q && nm.indexOf(q) < 0);
  });
}

// ---------- C: Clone player ----------

function clonePlayerPrompt() {
  const src = getCurrentPlayer();
  if (!src) return;
  const teams = state.data.teams;
  const opts = [];
  for (let t = 0; t < teams.length; t++) {
    for (const p of teams[t].players) {
      if (t === state.teamIndex && p.slot === state.playerSlot) continue;
      opts.push(`${t}/${p.slot}: ${teams[t].name} #${p.slot} ${p.name}`);
    }
  }
  const choice = prompt(
    `「${src.name}」を上書きする宛先を指定してください。\n\n` +
    `形式: <team>/<slot>  (例: 0/5 = チーム 0 のスロット 5)\n` +
    `現在のチーム=${state.teamIndex}\n\n` +
    `※ slot 0-20, team 0-11。スロット先の選手データは完全に上書きされます。`
  );
  if (!choice) return;
  const m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(choice);
  if (!m) { alert('形式が正しくありません'); return; }
  const dt = Number(m[1]), ds = Number(m[2]);
  if (dt < 0 || dt > 11 || ds < 0 || ds > 20) { alert('範囲外'); return; }
  const dst = teams[dt].players.find(x => x.slot === ds);
  if (!dst) { alert('宛先 slot が見つかりません'); return; }
  if (!confirm(`チーム ${dt} スロット ${ds} の「${dst.name}」を「${src.name}」のデータで上書きします。よろしいですか？`)) return;

  // Deep clone source, preserve destination's slot field
  const cloned = JSON.parse(JSON.stringify(src));
  cloned.slot = ds;
  Object.assign(dst, cloned);

  // Update UI
  if (dt === state.teamIndex) renderPlayerList();
  markDirty();
  alert(`複製完了: チーム ${dt} スロット ${ds} → ${src.name}`);
}

// ---------- C: Presets ----------

const PRESETS = {
  batter_power:    { batting_hand: 1, power: 10, contact: 5,  eye: 6,  speed: 4  },
  batter_avg:      { batting_hand: 1, power: 5,  contact: 10, eye: 8,  speed: 6  },
  batter_speed:    { batting_hand: 1, power: 4,  contact: 7,  eye: 6,  speed: 10 },
  batter_balanced: { batting_hand: 1, power: 7,  contact: 7,  eye: 7,  speed: 7  },
  batter_eye:      { batting_hand: 1, power: 5,  contact: 8,  eye: 10, speed: 6  },
  pitcher_power:   { handedness: 1, speed: 10, control: 5, stamina_rank: 6, break_slider: 3, break_curve: 3, break_fork: 3, break_screw: 2, break_shoot: 2 },
  pitcher_finesse: { handedness: 1, speed: 5,  control: 10, stamina_rank: 7, break_slider: 8, break_curve: 8, break_fork: 8, break_screw: 7, break_shoot: 7 },
  pitcher_stamina: { handedness: 1, speed: 6,  control: 6,  stamina_rank: 10, break_slider: 5, break_curve: 5, break_fork: 5, break_screw: 5, break_shoot: 5 },
  pitcher_balanced:{ handedness: 1, speed: 7,  control: 7,  stamina_rank: 7,  break_slider: 7, break_curve: 7, break_fork: 7, break_screw: 7, break_shoot: 7 },
};

function applyPreset(e) {
  const key = e.target.value;
  if (!key) return;
  const preset = PRESETS[key];
  if (!preset) return;
  const p = getCurrentPlayer();
  if (!p) return;
  const isPitcherPreset = key.startsWith('pitcher_');
  if (isPitcherPreset && !p.is_pitcher) {
    if (!confirm('投手用プリセットですが、この選手は野手です。投手に変更して適用しますか？')) {
      e.target.value = '';
      return;
    }
    p.is_pitcher = true;
    if (!p.pitcher_stats) p.pitcher_stats = defaultPitcherStats();
  }
  if (!isPitcherPreset && p.is_pitcher) {
    if (!confirm('野手用プリセットですが、この選手は投手です。野手に変更して適用しますか？')) {
      e.target.value = '';
      return;
    }
    p.is_pitcher = false;
  }
  // Apply preset values
  const target = isPitcherPreset ? p.pitcher_stats : p.batter_stats;
  for (const [k, v] of Object.entries(preset)) {
    target[k] = v;
  }
  // Reset dropdown
  e.target.value = '';
  // Re-render to show new values
  renderEditForm(p);
  renderPlayerList();
  markDirty();
}

// 編集中状態は sessionStorage に随時 autosave（リロードしても消えない）
let autosaveTimer = null;
function markDirty() {
  state.dirty = true;
  setStatus('● 未保存（自動退避中）');
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(state.data));
    } catch (_) { /* QuotaExceeded 等は無視 */ }
  }, 500);
}
function setStatus(s) {
  document.getElementById('save-status').textContent = s;
}

// ---------- helpers ----------

function getCurrentPlayer() {
  return state.data.teams[state.teamIndex].players.find(p => p.slot === state.playerSlot);
}

function defaultPitcherStats() {
  return {
    handedness: 1, speed: 5, control: 5, stamina_rank: 5,
    break_slider: 5, break_curve: 5, break_fork: 5, break_screw: 5, break_shoot: 5,
  };
}

function setVal(id, v) {
  document.getElementById(id).value = v;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

try {
  boot();
} catch (err) {
  alert('起動エラー: ' + err.message);
  console.error(err);
}
