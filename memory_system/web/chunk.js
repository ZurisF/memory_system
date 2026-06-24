"use strict";

// ---- 载入一个 transcript ----
async function loadTranscript(path, el) {
  SEL.clear();
  SEG_PICK.clear();
  $("#hint").style.display = "none";
  const d = await (await fetch("/api/transcript?path=" + encodeURIComponent(path))).json();
  if (d.error) { toast(d.error, true); return; }
  CUR = d;
  await loadSegments();
  render();
  renderListItems();   // 刷新当前/锁定高亮 + 候选篮子
}

async function loadSegments() {
  if (!CUR) return;
  const d = await (await fetch("/api/segments?path=" + encodeURIComponent(CUR.path))).json();
  SEGS = (d.segments || []).map(normSeg);
  renderAlerts(d);
  const a = d.agent;
  $("#seg-status").textContent = a
    ? `上次 agent: ${a.provider}/${a.model} · ${a.attempts}次 · $${(a.cost_usd || 0).toFixed(4)}`
    : "尚未切块";
}

function normSeg(s) {
  return { seg_id: s.seg_id || "", start_turn: s.start_turn, end_turn: s.end_turn,
    tag: s.tag || "", cut_reason: s.cut_reason || "", short: !!s.short,
    deletions: s.deletions || [], origin: s.origin || "edited" };
}

function renderAlerts(d) {
  const box = $("#alerts");
  box.innerHTML = "";
  if (d.retry && d.retry.length) {
    const last = d.retry[d.retry.length - 1];
    const el = document.createElement("div");
    el.className = "alert err";
    el.innerHTML = `⚠ 切块失败 ${d.retry.length} 次,最近:${esc(last.error).slice(0, 160)}` +
      `<div class="row" style="margin-top:6px"><button id="retry-btn">重试切块</button></div>`;
    box.appendChild(el);
    $("#retry-btn").onclick = runChunk;
  }
}

// ---- 渲染回合(带段色带)----
function turnSegMap() {
  const map = {};            // turn idx -> seg index(后段覆盖前段)
  SEGS.forEach((s, i) => {
    for (let t = s.start_turn; t <= s.end_turn; t++) map[t] = i;
  });
  return map;
}
function delTurns() {
  const set = new Set();      // 被标删的回合
  SEGS.forEach((s) => (s.deletions || []).forEach((d) => {
    const nums = (String(d.range).match(/\d+/g) || []).map(Number);
    if (nums.length === 1) set.add(nums[0]);
    else if (nums.length >= 2) for (let t = nums[0]; t <= nums[1]; t++) set.add(t);
  }));
  return set;
}

function render() {
  const d = CUR;
  $("#main-title").textContent = d.session_id.slice(0, 8) +
    (d.maybe_writing ? "  ⚠ 可能正在写入" : "") + `  ·  ${d.turns.length}回合`;
  const segMap = turnSegMap();
  const dels = delTurns();
  const box = $("#turns");
  box.innerHTML = "";
  d.turns.forEach((t) => {
    const segIdx = segMap[t.idx];
    const color = segIdx != null ? PALETTE[segIdx % PALETTE.length] : "transparent";
    const el = document.createElement("div");
    el.className = "turn" + (t.processed ? " done" : "") + (SEL.has(t.idx) ? " sel" : "") +
                   (dels.has(t.idx) ? " del" : "");
    el.dataset.idx = t.idx;
    el.style.borderLeftColor = color;
    let html = "";
    if (t.processed) html += `<span class="done-tag">✓ 已处理</span>`;
    html += `<div class="tmeta">回合 ${t.idx} · ${t.msg_count} msg` +
      (segIdx != null ? `<span class="seg-chip" style="color:${color}">▌段 ${SEGS[segIdx].start_turn}–${SEGS[segIdx].end_turn}</span>` : "") +
      `</div>`;
    if (t.human_text)
      html += `<div class="bubble me"><span class="who me">[我]</span>${esc(t.human_text)}</div>`;
    if (t.assistant_text)
      html += `<div class="bubble claude"><span class="who claude">[Claude]</span>${esc(t.assistant_text)}</div>`;
    el.innerHTML = html;
    el.onclick = () => toggle(t.idx);
    box.appendChild(el);
  });
  renderSegs();
  updateBar();
}

// ---- 渲染段面板 ----
function renderSegs() {
  const box = $("#seg-list");
  box.innerHTML = "";
  SEGS.forEach((s, i) => {
    const color = PALETTE[i % PALETTE.length];
    const card = document.createElement("div");
    card.className = "seg-card";
    card.style.borderLeftColor = color;
    const turns = s.end_turn - s.start_turn + 1;
    const pickable = !!s.seg_id;   // 没 seg_id(没存盘的新段)不能批量删,先存
    card.innerHTML =
      `<div class="hd">` +
      `<input type="checkbox" class="seg-pick" data-sid="${escAttr(s.seg_id)}"${SEG_PICK.has(s.seg_id) ? " checked" : ""}` +
      `${pickable ? "" : " disabled title='先确认分段存盘后才能批量删'"}>` +
      `<span style="color:${color}">▌</span>` +
      `<b>回合 ${s.start_turn}–${s.end_turn}</b> <span>(${turns}轮)</span>` +
      `<span class="origin">${s.origin}</span>` +
      `<label style="margin-left:auto"><input type="checkbox" ${s.short ? "checked" : ""} data-i="${i}" class="sk-short"> short</label></div>` +
      `<div class="lbl">tag</div><input type="text" class="sk-tag" data-i="${i}" value="${esc(s.tag)}">` +
      `<div class="lbl">cut_reason</div><textarea class="sk-reason" data-i="${i}">${esc(s.cut_reason)}</textarea>` +
      (s.deletions.length ? `<div class="lbl">deletions(${s.deletions.length})</div>` +
        s.deletions.map((d, di) =>
          `<div class="row"><span style="flex:1;font-size:12px;color:var(--muted)">${esc(d.range)} · ${esc(d.reason)}</span>` +
          `<button class="sk-deldel" data-i="${i}" data-di="${di}">✕</button></div>`).join("") : "") +
      `<div class="ops">` +
      `<button class="sk-bs-" data-i="${i}">首−</button><button class="sk-bs+" data-i="${i}">首+</button>` +
      `<button class="sk-be-" data-i="${i}">尾−</button><button class="sk-be+" data-i="${i}">尾+</button>` +
      `<button class="sk-split" data-i="${i}">拆分…</button>` +
      (i > 0 ? `<button class="sk-mergeup" data-i="${i}">并上段</button>` : "") +
      (i < SEGS.length - 1 ? `<button class="sk-merge" data-i="${i}">并下段</button>` : "") +
      `<button class="sk-addturns" data-i="${i}">加选中回合</button>` +
      `<button class="sk-adddel" data-i="${i}">标删选中</button>` +
      `<button class="sk-del" data-i="${i}">删段</button>` +
      `</div>`;
    box.appendChild(card);
  });
  wireSegEvents();
}

function wireSegEvents() {
  $("#seg-list").querySelectorAll(".seg-pick").forEach((e) =>
    e.onclick = (ev) => {
      ev.stopPropagation();
      const sid = e.dataset.sid;
      if (e.checked) SEG_PICK.add(sid); else SEG_PICK.delete(sid);
      updateBar();
    });
  $("#seg-list").querySelectorAll(".sk-tag").forEach((e) =>
    e.oninput = () => { SEGS[+e.dataset.i].tag = e.value; markEdited(+e.dataset.i); });
  $("#seg-list").querySelectorAll(".sk-reason").forEach((e) =>
    e.oninput = () => { SEGS[+e.dataset.i].cut_reason = e.value; markEdited(+e.dataset.i); });
  $("#seg-list").querySelectorAll(".sk-short").forEach((e) =>
    e.onchange = () => { SEGS[+e.dataset.i].short = e.checked; markEdited(+e.dataset.i); });
  const move = (i, which, delta) => {
    const s = SEGS[i], n = CUR.turns.length;
    if (which === "s") s.start_turn = Math.min(Math.max(1, s.start_turn + delta), s.end_turn);
    else s.end_turn = Math.max(Math.min(n, s.end_turn + delta), s.start_turn);
    markEdited(i); render();
  };
  $("#seg-list").querySelectorAll(".sk-bs-").forEach((e) => e.onclick = () => move(+e.dataset.i, "s", -1));
  $("#seg-list").querySelectorAll(".sk-bs\\+").forEach((e) => e.onclick = () => move(+e.dataset.i, "s", 1));
  $("#seg-list").querySelectorAll(".sk-be-").forEach((e) => e.onclick = () => move(+e.dataset.i, "e", -1));
  $("#seg-list").querySelectorAll(".sk-be\\+").forEach((e) => e.onclick = () => move(+e.dataset.i, "e", 1));
  $("#seg-list").querySelectorAll(".sk-split").forEach((e) => e.onclick = () => splitSeg(+e.dataset.i));
  $("#seg-list").querySelectorAll(".sk-merge").forEach((e) => e.onclick = () => mergeSeg(+e.dataset.i));
  $("#seg-list").querySelectorAll(".sk-mergeup").forEach((e) => e.onclick = () => mergeSeg(+e.dataset.i - 1));
  $("#seg-list").querySelectorAll(".sk-addturns").forEach((e) => e.onclick = () => addTurnsToSeg(+e.dataset.i));
  $("#seg-list").querySelectorAll(".sk-del").forEach((e) => e.onclick = () => { SEGS.splice(+e.dataset.i, 1); markDirty(); render(); });
  $("#seg-list").querySelectorAll(".sk-adddel").forEach((e) => e.onclick = () => addDeletion(+e.dataset.i));
  $("#seg-list").querySelectorAll(".sk-deldel").forEach((e) => e.onclick = () => {
    SEGS[+e.dataset.i].deletions.splice(+e.dataset.di, 1); markDirty(); render();
  });
}

function markEdited(i) {
  if (SEGS[i].origin === "agent") SEGS[i].origin = "edited";
  markDirty();
}

function splitSeg(i) {
  const s = SEGS[i];
  const at = parseInt(prompt(`在哪个回合后切开?(${s.start_turn} … ${s.end_turn - 1})`), 10);
  if (!at || at < s.start_turn || at >= s.end_turn) { toast("切点不在段内", true); return; }
  const right = { ...s, start_turn: at + 1, seg_id: "", tag: "", origin: "edited" };
  s.end_turn = at; s.seg_id = ""; s.origin = "edited";
  SEGS.splice(i + 1, 0, right);
  SEGS.sort((a, b) => a.start_turn - b.start_turn);
  markDirty(); render();
}
function mergeSeg(i) {
  const a = SEGS[i], b = SEGS[i + 1];
  if (!b) return;
  a.end_turn = Math.max(a.end_turn, b.end_turn);
  a.start_turn = Math.min(a.start_turn, b.start_turn);
  a.deletions = (a.deletions || []).concat(b.deletions || []);
  a.origin = "edited"; a.seg_id = a.seg_id || "";
  SEGS.splice(i + 1, 1);
  markDirty(); render();
}
function groupRanges(nums) {
  // 把回合号压成连续区间 [[a,b],...](用于把空洞回合写成 deletions)。
  const sorted = [...new Set(nums)].sort((a, b) => a - b);
  const out = [];
  for (const n of sorted) {
    const last = out[out.length - 1];
    if (last && n === last[1] + 1) last[1] = n;
    else out.push([n, n]);
  }
  return out;
}
function segDelTurns(s) {
  const set = new Set();      // 本段已标删的回合(避免空洞重复标删)
  (s.deletions || []).forEach((d) => {
    const nums = (String(d.range).match(/\d+/g) || []).map(Number);
    if (nums.length === 1) set.add(nums[0]);
    else if (nums.length >= 2) for (let t = nums[0]; t <= nums[1]; t++) set.add(t);
  });
  return set;
}
function addTurnsToSeg(i) {
  // 把选中回合并入段:扩边界到并集;支持跳选——新跨度内既不在原段、也没被选中的
  // 空洞回合自动标删(deletions),保住「只要这些回合,中间当噪声」的语义。后端保存再校验越界/重叠。
  const s = SEGS[i];
  const picked = [...SEL];
  if (!picked.length) { toast("先选中要并入的回合", true); return; }
  const newStart = Math.min(s.start_turn, ...picked);
  const newEnd = Math.max(s.end_turn, ...picked);
  const wanted = new Set(picked);          // 想要的回合 = 原段范围 ∪ 选中回合
  for (let t = s.start_turn; t <= s.end_turn; t++) wanted.add(t);
  const dels = segDelTurns(s);
  const gaps = [];
  for (let t = newStart; t <= newEnd; t++) {
    if (!wanted.has(t) && !dels.has(t)) gaps.push(t);
  }
  s.start_turn = newStart;
  s.end_turn = newEnd;
  groupRanges(gaps).forEach(([a, b]) => {
    s.deletions.push({ range: a === b ? `回合 ${a}` : `回合 ${a}-${b}`, reason: "跨选空洞" });
  });
  SEL.clear();
  markEdited(i);
  if (gaps.length) toast(`并入 ${picked.length} 回合,${gaps.length} 个空洞回合已标删`);
  render();
}
function addDeletion(i) {
  const s = SEGS[i];
  const picked = [...SEL].filter((t) => t >= s.start_turn && t <= s.end_turn).sort((x, y) => x - y);
  if (!picked.length) { toast("先选中本段内的回合", true); return; }
  const range = picked.length === 1 ? `回合 ${picked[0]}` : `回合 ${picked[0]}-${picked[picked.length - 1]}`;
  const reason = prompt(`删除理由(${range})`, "噪声");
  if (reason === null) return;
  s.deletions.push({ range, reason: reason || "" });
  s.origin = "edited"; markDirty(); render();
}

// ---- 选择 ----
function toggle(idx) {
  if (SEL.has(idx)) SEL.delete(idx); else SEL.add(idx);
  render();
}
function updateBar() {
  const n = SEL.size;
  $("#mark").disabled = n === 0;
  $("#make-seg").disabled = n === 0;
  $("#run-chunk").disabled = !CUR;
  $("#confirm-segs").disabled = !(CUR && SEGS.length);
  const savedSegs = SEGS.filter((s) => s.seg_id).length;
  $("#seg-all").disabled = savedSegs === 0;
  $("#seg-del").disabled = SEG_PICK.size === 0;
  $("#seg-del").textContent = SEG_PICK.size ? `删除勾选段 (${SEG_PICK.size})` : "删除勾选段";
  $("#sel-info").textContent = n === 0 ? (SEGS.length ? `${SEGS.length} 段` : "未选")
    : `已选 ${n} 回合(${[...SEL].sort((a, b) => a - b).join(",")})`;
}

// ---- 运行切块 agent ----
async function runChunk() {
  if (!CUR) return;
  beginEdit();   // 跑切块 agent = 开始编辑,上锁
  const btn = $("#run-chunk");
  btn.disabled = true;
  $("#seg-status").innerHTML = `<span class="spin">◴</span> 切块中…(${$("#provider").value})`;
  let d;
  try {
    const r = await fetch("/api/chunk", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: CUR.path, provider: $("#provider").value,
        model: $("#model").value.trim() || undefined }),
    });
    d = await r.json();
  } catch (e) { toast("请求失败: " + e, true); btn.disabled = false; return; }

  if (d.kind === "oversized") {
    $("#alerts").innerHTML = `<div class="alert warn">⚠ 对话过大(${d.chars} 字符 > ${d.limit})。` +
      `请用"选中回合 → 建段"先人工粗分成几块,再逐块切。绝不静默截断。</div>`;
    $("#seg-status").textContent = "超大,需人工粗分";
  } else if (d.kind === "failed" || d.kind === "unavailable") {
    SEGS = (d.segments || []).map(normSeg);
    renderAlerts(d);
    $("#seg-status").textContent = d.kind === "unavailable" ? d.error : "切块失败(见上方告警)";
    if (d.kind === "unavailable") toast(d.error, true);
  } else if (d.error) {
    toast(d.error, true); $("#seg-status").textContent = d.error;
  } else {
    SEGS = (d.segments || []).map(normSeg);
    renderAlerts(d);
    const a = d.agent || {};
    $("#seg-status").textContent =
      `切出 ${SEGS.length} 段 · ${a.provider}/${a.model} · ${a.attempts}次 · $${(a.cost_usd || 0).toFixed(4)}`;
    toast(`切出 ${SEGS.length} 段`);
  }
  btn.disabled = false;
  render();
}

// ---- 手动建段 / 保存 / 标已处理 ----
function makeSegFromSel() {
  if (!SEL.size) return;
  const arr = [...SEL].sort((a, b) => a - b);
  const start = arr[0], end = arr[arr.length - 1];
  SEGS.push({ seg_id: "", start_turn: start, end_turn: end, tag: "", cut_reason: "手动切块",
    short: (end - start + 1) < 15, deletions: [], origin: "manual" });
  SEGS.sort((a, b) => a.start_turn - b.start_turn);
  SEL.clear();
  markDirty();
  render();
}

async function saveSegs() {
  if (!CUR) return false;
  const payload = SEGS.map((s) => ({ seg_id: s.seg_id, start_turn: s.start_turn,
    end_turn: s.end_turn, tag: s.tag, cut_reason: s.cut_reason, short: s.short,
    deletions: s.deletions, origin: s.origin }));
  const r = await fetch("/api/segments", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: CUR.path, segments: payload }),
  });
  const d = await r.json();
  if (d.error) {
    const ov = d.overlaps ? " 冲突:" + d.overlaps.map((o) => (o.range || []).join("-")).join(",") : "";
    toast("保存失败: " + d.error + ov, true);
    return false;
  }
  SEGS = (d.segments || []).map(normSeg);
  // 提交语义:标记编辑过、纳入候选(待整理区据此显示)、解锁
  EDITED.add(CUR.path);
  CAND.add(CUR.path);
  saveState();
  SEG_DIRTY = false;
  LOCK = null;
  $("#exit-proc").style.display = "none";
  renderListItems();
  const gaps = d.gaps || [];
  toast(`已保存 ${SEGS.length} 段` + (gaps.length ? `,${gaps.length} 处空洞回合未覆盖` : ""));
  render();
  return true;
}

// 全选 / 取消全选已存盘的段(只挑有 seg_id 的)
function toggleAllSegs() {
  const saved = SEGS.filter((s) => s.seg_id).map((s) => s.seg_id);
  if (SEG_PICK.size >= saved.length && saved.length) SEG_PICK.clear();
  else { SEG_PICK.clear(); saved.forEach((id) => SEG_PICK.add(id)); }
  render();
}

// 批量删段:走 /api/segments/delete。后端检出已蒸馏段会回 409 needs_confirm,二次确认后 force。
async function deletePickedSegs() {
  if (!CUR || !SEG_PICK.size) return;
  const ids = [...SEG_PICK];
  let d = await postJSON("/api/segments/delete",
    { session_id: CUR.session_id, seg_ids: ids });
  if (d && d.needs_confirm) {
    if (!window.confirm(
        `${d.staged.length} 个待删段已在蒸馏区有提取。\n` +
        `删分段不影响已蒸馏的 episode(它们仍可在蒸馏区审核/删除)。\n确认删除这 ${ids.length} 段?`)) {
      return;
    }
    d = await postJSON("/api/segments/delete",
      { session_id: CUR.session_id, seg_ids: ids, force: true });
  }
  if (!d || d.error) { toast("删段失败: " + ((d && d.error) || "网络"), true); return; }
  SEG_PICK.clear();
  SEGS = (d.segments || []).map(normSeg);
  toast(`已删 ${d.deleted} 段` + (SEGS.length ? "" : ",本条已无段"));
  // 段全删光 = 该会话退出处理,同步解锁 + 移出候选/动过标记
  if (!SEGS.length) {
    CAND.delete(CUR.path); EDITED.delete(CUR.path);
    SEG_DIRTY = false; LOCK = null;
    $("#exit-proc").style.display = "none";
    saveState();
    await loadList();   // 刷新沉底/候选状态
  }
  render();
}

async function markSelected() {
  if (!CUR || SEL.size === 0) return;
  const r = await fetch("/api/select", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: CUR.path, session_id: CUR.session_id, turn_idxs: [...SEL] }),
  });
  const d = await r.json();
  if (d.error) { toast("失败: " + d.error, true); return; }
  toast(`已登记 ${d.turns.length} 回合 / ${d.covered} 条消息`);
  SEL.clear();
  await loadTranscript(CUR.path, null);
}
