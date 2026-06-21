"use strict";

// ---- 状态 ----
let CUR = null;            // {path, session_id, turns, ...}
let SEGS = [];            // [{seg_id,start_turn,end_turn,tag,cut_reason,short,deletions,origin}]
let PROVIDERS = [];
let CHUNK_MODEL = "sonnet";
const SEL = new Set();     // 选中回合 idx(建段 / 标已处理共用)

const PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#f7768e",
                 "#7dcfff", "#e6a3c9", "#73daca", "#ff9e64", "#c0caf5"];

const $ = (s) => document.querySelector(s);

function toast(msg, bad) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show" + (bad ? " bad" : "");
  setTimeout(() => el.classList.remove("show"), 2200);
}
function esc(s) {
  return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function fmtSize(n) { return n < 1024 ? n + "B" : (n / 1024 | 0) + "KB"; }
function fmtTime(mtime) {
  const d = new Date(mtime * 1000);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit" });
}

// ---- transcript 列表 ----
async function loadList() {
  const d = await (await fetch("/api/transcripts")).json();
  const box = $("#t-items");
  box.innerHTML = "";
  if (d.hidden_empty) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `已隐藏 ${d.hidden_empty} 个空会话(/clear 空壳等)`;
    box.appendChild(note);
  }
  d.transcripts.forEach((t) => {
    const el = document.createElement("div");
    el.className = "t-item";
    el.dataset.path = t.path;
    el.innerHTML =
      `<div><span class="sid">${t.session_id.slice(0, 8)}</span>` +
      (t.maybe_writing ? `<span class="badge">正在写入</span>` : "") + `</div>` +
      `<div class="meta">${fmtTime(t.mtime)} · ${t.turn_count}回合 · ${fmtSize(t.size)}</div>` +
      `<div class="meta">${t.cwd || ""}</div>`;
    el.onclick = () => loadTranscript(t.path, el);
    box.appendChild(el);
  });
}

async function loadProviders() {
  const d = await (await fetch("/api/agent/providers")).json();
  PROVIDERS = d.providers || [];
  CHUNK_MODEL = d.chunk_model || "sonnet";
  const sel = $("#provider");
  sel.innerHTML = "";
  PROVIDERS.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = `${p.id}${p.available ? "" : " (不可用)"}${p.default ? " ✓默认" : ""}`;
    o.disabled = !p.available;
    if (p.default && p.available) o.selected = true;
    sel.appendChild(o);
  });
  $("#model").placeholder = `模型(空=默认 ${CHUNK_MODEL})`;
}

// ---- 载入一个 transcript ----
async function loadTranscript(path, el) {
  document.querySelectorAll(".t-item.active").forEach((e) => e.classList.remove("active"));
  if (el) el.classList.add("active");
  SEL.clear();
  $("#hint").style.display = "none";
  const d = await (await fetch("/api/transcript?path=" + encodeURIComponent(path))).json();
  if (d.error) { toast(d.error, true); return; }
  CUR = d;
  await loadSegments();
  render();
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
      html += `<div><span class="who me">[我]</span> <span class="txt">${esc(t.human_text)}</span></div>`;
    if (t.assistant_text)
      html += `<div><span class="who claude">[Claude]</span> <span class="txt">${esc(t.assistant_text)}</span></div>`;
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
    card.innerHTML =
      `<div class="hd"><span style="color:${color}">▌</span>` +
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
      (i < SEGS.length - 1 ? `<button class="sk-merge" data-i="${i}">并下段</button>` : "") +
      `<button class="sk-adddel" data-i="${i}">标删选中</button>` +
      `<button class="sk-del" data-i="${i}">删段</button>` +
      `</div>`;
    box.appendChild(card);
  });
  wireSegEvents();
}

function wireSegEvents() {
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
  $("#seg-list").querySelectorAll(".sk-del").forEach((e) => e.onclick = () => { SEGS.splice(+e.dataset.i, 1); render(); });
  $("#seg-list").querySelectorAll(".sk-adddel").forEach((e) => e.onclick = () => addDeletion(+e.dataset.i));
  $("#seg-list").querySelectorAll(".sk-deldel").forEach((e) => e.onclick = () => {
    SEGS[+e.dataset.i].deletions.splice(+e.dataset.di, 1); render();
  });
}

function markEdited(i) {
  if (SEGS[i].origin === "agent") SEGS[i].origin = "edited";
  $("#save-segs").disabled = false;
}

function splitSeg(i) {
  const s = SEGS[i];
  const at = parseInt(prompt(`在哪个回合后切开?(${s.start_turn} … ${s.end_turn - 1})`), 10);
  if (!at || at < s.start_turn || at >= s.end_turn) { toast("切点不在段内", true); return; }
  const right = { ...s, start_turn: at + 1, seg_id: "", tag: "", origin: "edited" };
  s.end_turn = at; s.seg_id = ""; s.origin = "edited";
  SEGS.splice(i + 1, 0, right);
  SEGS.sort((a, b) => a.start_turn - b.start_turn);
  $("#save-segs").disabled = false; render();
}
function mergeSeg(i) {
  const a = SEGS[i], b = SEGS[i + 1];
  if (!b) return;
  a.end_turn = Math.max(a.end_turn, b.end_turn);
  a.start_turn = Math.min(a.start_turn, b.start_turn);
  a.deletions = (a.deletions || []).concat(b.deletions || []);
  a.origin = "edited"; a.seg_id = a.seg_id || "";
  SEGS.splice(i + 1, 1);
  $("#save-segs").disabled = false; render();
}
function addDeletion(i) {
  const s = SEGS[i];
  const picked = [...SEL].filter((t) => t >= s.start_turn && t <= s.end_turn).sort((x, y) => x - y);
  if (!picked.length) { toast("先选中本段内的回合", true); return; }
  const range = picked.length === 1 ? `回合 ${picked[0]}` : `回合 ${picked[0]}-${picked[picked.length - 1]}`;
  const reason = prompt(`删除理由(${range})`, "噪声");
  if (reason === null) return;
  s.deletions.push({ range, reason: reason || "" });
  s.origin = "edited"; $("#save-segs").disabled = false; render();
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
  $("#sel-info").textContent = n === 0 ? (SEGS.length ? `${SEGS.length} 段` : "未选")
    : `已选 ${n} 回合(${[...SEL].sort((a, b) => a - b).join(",")})`;
}

// ---- 运行切块 agent ----
async function runChunk() {
  if (!CUR) return;
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
  $("#save-segs").disabled = false;
  render();
}

async function saveSegs() {
  if (!CUR) return;
  const payload = SEGS.map((s) => ({ seg_id: s.seg_id, start_turn: s.start_turn,
    end_turn: s.end_turn, tag: s.tag, cut_reason: s.cut_reason, short: s.short,
    deletions: s.deletions, origin: s.origin }));
  const r = await fetch("/api/segments", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: CUR.path, segments: payload }),
  });
  const d = await r.json();
  if (d.error) { toast("保存失败: " + d.error, true); return; }
  SEGS = (d.segments || []).map(normSeg);
  $("#save-segs").disabled = true;
  toast(`已保存 ${SEGS.length} 段`);
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
  await loadTranscript(CUR.path, document.querySelector(".t-item.active"));
}

$("#run-chunk").onclick = runChunk;
$("#make-seg").onclick = makeSegFromSel;
$("#save-segs").onclick = saveSegs;
$("#mark").onclick = markSelected;
loadProviders();
loadList();
