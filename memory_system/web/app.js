"use strict";

// ---- 状态 ----
let CUR = null;            // {path, session_id, turns, ...}
let SEGS = [];            // [{seg_id,start_turn,end_turn,tag,cut_reason,short,deletions,origin}]
let PROVIDERS = [];
let CHUNK_MODEL = "sonnet";
const SEL = new Set();     // 选中回合 idx(建段 / 标已处理共用)

// 块 B:子阶段 + 候选区 + 处理锁
let STAGE = "chunk";       // chunk(切段) | triage(待整理)
let LOCK = null;           // 处理锁:正在切段的 path;非空时其他条目禁点(防误触)
let TLIST = [];            // 当前 transcript 列表(渲染用,避免勾选就重新拉网络)
let HIDDEN_EMPTY = 0;
const CAND = new Set();    // 候选篮子:勾选的 transcript path
const EDITED = new Set();  // 本次会话点过/编辑过的 path(刚编辑 flag)
const TMAP = new Map();    // path -> transcript info(候选/待整理显示 sid/cwd)

// 块 C:待整理(蒸馏)三栏 —— 左 session 列表 / 中 条目编辑 / 右 选项
let TRIS = [];             // /api/staging/all 的 sessions(扫磁盘,与候选区无关)
let TRI_CUR = null;        // 当前选中 session_id
const SELT = new Set();    // 待整理选中项 key(path,kind,id) kind∈seg|ep
const UNDO = {};           // 每条 episode 的本地撤销栈:{epKey:[fieldsSnapshot,...]}

const PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#f7768e",
                 "#7dcfff", "#e6a3c9", "#73daca", "#ff9e64", "#c0caf5"];

// ---- 本地游标持久化:刷新/换页不丢「在看哪几条」+ 刚编辑标记 + 当前子屏 ----
// 真正的数据(段/staging/碎片)都在磁盘上;这里只记浏览器侧的视图游标。
const LS_KEY = "memsys_ingest_v1";
function saveState() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(
      { cand: [...CAND], edited: [...EDITED], stage: STAGE, tri_cur: TRI_CUR }));
  } catch (e) { /* 隐私模式/超额:静默,内存态仍可用 */ }
}
function restoreState() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    (s.cand || []).forEach((p) => CAND.add(p));
    (s.edited || []).forEach((p) => EDITED.add(p));
    if (s.stage === "chunk" || s.stage === "triage") STAGE = s.stage;
    if (s.tri_cur) TRI_CUR = s.tri_cur;
  } catch (e) { /* 坏数据:忽略,从空起 */ }
}

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
  TLIST = d.transcripts || [];
  HIDDEN_EMPTY = d.hidden_empty || 0;
  TMAP.clear();
  TLIST.forEach((t) => TMAP.set(t.path, t));
  renderListItems();
}

// 从 TLIST 重渲染(勾选/锁/当前态变化时,不重新拉网络)
function renderListItems() {
  const box = $("#t-items");
  box.innerHTML = "";
  if (HIDDEN_EMPTY) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `已隐藏 ${HIDDEN_EMPTY} 个空会话(/clear 空壳等)`;
    box.appendChild(note);
  }
  TLIST.forEach((t) => {
    const el = document.createElement("div");
    el.className = "t-item" +
      (LOCK && t.path !== LOCK ? " locked" : "") +
      (CUR && CUR.path === t.path ? " cur" : "");
    el.dataset.path = t.path;
    const flags =
      (EDITED.has(t.path) ? `<span class="flag">刚编辑</span>` : "") +
      (CAND.has(t.path) ? `<span class="flag" style="color:var(--me)">候选</span>` : "");
    el.innerHTML =
      `<input type="checkbox" class="cb"${CAND.has(t.path) ? " checked" : ""}>` +
      `<div class="body"><div><span class="sid">${t.session_id.slice(0, 8)}</span>` +
      (t.maybe_writing ? `<span class="badge">正在写入</span>` : "") + flags + `</div>` +
      `<div class="meta">${fmtTime(t.mtime)} · ${t.turn_count}回合 · ${fmtSize(t.size)}</div>` +
      `<div class="meta">${esc(t.cwd || "")}</div></div>`;
    el.querySelector(".cb").onclick = (ev) => { ev.stopPropagation(); toggleCand(t.path); };
    el.querySelector(".body").onclick = () => openForChunk(t.path, el);
    box.appendChild(el);
  });
  renderBasket();
}

function toggleCand(path) {
  if (CAND.has(path)) CAND.delete(path); else CAND.add(path);
  saveState();
  renderListItems();
}

// 候选篮子
function renderBasket() {
  const box = $("#cand-basket");
  $("#cand-info").textContent = `候选 ${CAND.size}`;
  box.innerHTML = "";
  if (!CAND.size) return;
  const h = document.createElement("div");
  h.className = "cb-h";
  h.textContent = `候选区 (${CAND.size})`;
  box.appendChild(h);
  CAND.forEach((p) => {
    const t = TMAP.get(p);
    const it = document.createElement("div");
    it.className = "cb-item" + (CUR && CUR.path === p ? " cur" : "");
    it.innerHTML =
      `<span class="cb-sid">${t ? t.session_id.slice(0, 8) : esc(p.split("/").pop())}</span>` +
      `<span style="color:var(--muted)">${t ? t.turn_count + "回合" : ""}</span>`;
    it.onclick = () => openForChunk(p, null);
    box.appendChild(it);
  });
}

// 打开一条(切段屏)。点击=预览,不上锁、不标记;真正动手编辑时才由 beginEdit 上锁。
function openForChunk(path, el) {
  if (LOCK && path !== LOCK) { toast("有正在编辑的条目,请先「确认分段」或「退出处理」", true); return; }
  setStage("chunk");
  loadTranscript(path, el);
}

// 开始编辑(切块/建段/改段任意一处触发):上锁,其他条目禁点。预览不触发。
function beginEdit() {
  if (!CUR || LOCK === CUR.path) return;
  LOCK = CUR.path;
  $("#exit-proc").style.display = "";
  renderListItems();
}
// 标脏:上锁。所有就地编辑动作统一走这里。存盘由「确认分段→待整理」一键完成。
function markDirty() {
  beginEdit();
}

// 子阶段切换(视图冻结:只显隐,不销毁各自状态)
function setStage(stage) {
  STAGE = stage;
  saveState();
  $("#screen-chunk").style.display = stage === "chunk" ? "flex" : "none";
  $("#screen-triage").style.display = stage === "triage" ? "flex" : "none";
  document.querySelectorAll("#substage button").forEach((b) =>
    b.classList.toggle("active", b.dataset.stage === stage));
  if (stage === "triage") loadTriageAll();
}

// ====== 块 C:待整理树 + 五件套编辑器 + 批量归档 ======

const tkey = (path, kind, id) => path + "\u0000" + kind + "\u0000" + id;

// ---- 待整理(蒸馏)三栏:扫磁盘 /api/staging/all 列会话,点 session 开其条目 ----
function fmtIso(s) {
  try {
    return new Date(s).toLocaleString("zh-CN",
      { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

// 一个会话里"还没提取成 episode 的段"(按 seg_id 判)
function unextractedSegs(s) {
  const done = new Set((s.episodes || []).map((e) => e.seg_id));
  return (s.segments || []).filter((seg) => !done.has(seg.seg_id));
}
function curSession() { return TRIS.find((s) => s.session_id === TRI_CUR) || null; }

// 扫磁盘拉全部在处理会话(改服务端状态后调它重拉);与候选区无关,不丢
async function loadTriageAll() {
  const box = $("#tri-sessions");
  box.innerHTML = `<div class="list-note" style="padding:10px 14px">加载中…</div>`;
  try {
    const d = await (await fetch("/api/staging/all")).json();
    TRIS = d.sessions || [];
  } catch (e) { TRIS = []; }
  if (TRI_CUR && !TRIS.some((s) => s.session_id === TRI_CUR)) TRI_CUR = null;
  // 清掉已不存在的选中项
  const live = new Set();
  TRIS.forEach((s) => {
    unextractedSegs(s).forEach((seg) => live.add(tkey(s.source_path, "seg", seg.seg_id)));
    (s.episodes || []).forEach((e) => live.add(tkey(s.source_path, "ep", e.stage_id)));
  });
  [...SELT].forEach((k) => { if (!live.has(k)) SELT.delete(k); });
  renderTriSessions();
  renderTriMain();
}

// 左栏:会话列表(仿切段 jsonl 列表风格,点条目本身=选中)
function renderTriSessions() {
  const box = $("#tri-sessions");
  box.innerHTML = "";
  if (!TRIS.length) {
    box.innerHTML = `<div class="list-note" style="padding:10px 14px">磁盘上暂无在处理的会话。` +
      `去「切段」切一条,「确认分段→待整理」后即来此。</div>`;
    return;
  }
  TRIS.forEach((s) => {
    const t = TMAP.get(s.source_path);
    const nEp = (s.episodes || []).length;
    const nSeg = unextractedSegs(s).length;
    const el = document.createElement("div");
    el.className = "s-item" + (TRI_CUR === s.session_id ? " active" : "") +
      (s.source_exists ? "" : " gone");
    const stagedB = nEp ? `<span class="s-badge staged">${nEp} 已提取</span>` : "";
    const segB = nSeg ? `<span class="s-badge">${nSeg} 段待提取</span>` : "";
    const candB = CAND.has(s.source_path) ? `<span class="s-badge cand">候选</span>` : "";
    const cwd = t ? (t.cwd || "") : (s.source_exists ? "" : "源 jsonl 已清");
    el.innerHTML =
      `<div><span class="sid">${s.session_id.slice(0, 8)}</span>${stagedB}${segB}${candB}</div>` +
      `<div class="cwd">${esc(cwd)}</div>` +
      `<div class="meta">${s.updated_at ? fmtIso(s.updated_at) : ""}</div>`;
    el.onclick = () => {
      TRI_CUR = s.session_id; saveState();
      renderTriSessions(); renderTriMain();
    };
    box.appendChild(el);
  });
}

// 中栏:选中会话的条目(段→提取卡;episode→五件套编辑器)
function renderTriMain() {
  const box = $("#tri-eps");
  const hint = $("#triage-hint");
  const s = curSession();
  if (!s) {
    hint.style.display = ""; box.innerHTML = "";
    $("#tri-main-title").textContent = "蒸馏 / 审核";
    updateTriageBar();
    return;
  }
  hint.style.display = "none";
  const segs = unextractedSegs(s);
  const eps = s.episodes || [];
  const retry = s.retry || [];
  $("#tri-main-title").textContent =
    `${s.session_id.slice(0, 8)} · ${eps.length} 条目 / ${segs.length} 段待提取`;
  box.innerHTML = "";
  const r = { path: s.source_path };
  segs.forEach((seg) => box.appendChild(triSegCard(r, seg)));
  eps.forEach((e) => box.appendChild(triEpCard(r, e)));
  retry.forEach((rt) => box.appendChild(triRetryCard(r, rt)));
  if (!segs.length && !eps.length && !retry.length) {
    box.innerHTML = `<div class="list-note" style="padding:14px">该会话无段无条目。</div>`;
  }
  updateTriageBar();
}

// 未提取的段:小卡 + 提取按钮 + 勾选
function triSegCard(r, s) {
  const k = tkey(r.path, "seg", s.seg_id);
  const card = document.createElement("div");
  card.className = "tri-seg-card";
  card.innerHTML =
    `<input type="checkbox" class="r-cb"${SELT.has(k) ? " checked" : ""}>` +
    `<span class="seg-meta">段 ${s.start_turn}–${s.end_turn} · ${esc(s.tag || "")} ` +
    `<i>[${esc(s.origin || "")}] 未提取</i></span>`;
  const btn = document.createElement("button");
  btn.className = "seg-extract"; btn.textContent = "提取总结";
  btn.onclick = () => extractPaths({ [r.path]: [s.seg_id] });
  card.appendChild(btn);
  card.querySelector(".r-cb").onclick = (ev) => { toggleSel(k, ev.target.checked); };
  return card;
}

// 已提取条目:勾选头 + 五件套编辑器(复用 epEditor)
function triEpCard(r, e) {
  const k = tkey(r.path, "ep", e.stage_id);
  const card = document.createElement("div");
  card.className = "tri-ep-card";
  const tier = e.salience_tier || 1;
  const head = document.createElement("div");
  head.className = "tri-ep-head";
  head.innerHTML =
    `<input type="checkbox" class="r-cb"${SELT.has(k) ? " checked" : ""}>` +
    `<b>${e.stage_id}</b> · 段 ${e.start_turn}–${e.end_turn}` +
    `<span class="tier t${tier}">显著 ${tier}</span>`;
  head.querySelector(".r-cb").onclick = (ev) => { toggleSel(k, ev.target.checked); };
  card.appendChild(head);
  card.appendChild(epEditor(r, e));
  return card;
}

function triRetryCard(r, rt) {
  const card = document.createElement("div");
  card.className = "tri-seg-card";
  card.innerHTML = `<span class="seg-meta rretry">段 ${rt.start_turn}–${rt.end_turn} 提取失败:` +
    `${esc((rt.errors && rt.errors[rt.errors.length - 1]) || "")}</span>`;
  const btn = document.createElement("button");
  btn.className = "seg-extract"; btn.textContent = "重试提取";
  btn.onclick = () => extractPaths({ [r.path]: [rt.seg_id] });
  card.appendChild(btn);
  return card;
}

function toggleSel(k, on) {
  if (on) SELT.add(k); else SELT.delete(k);
  updateTriageBar();
}

function updateTriageBar() {
  let segN = 0, epN = 0;
  SELT.forEach((k) => (k.split("\u0000")[1] === "seg" ? segN++ : epN++));
  $("#t-extract").disabled = segN === 0;
  $("#t-confirm").disabled = epN === 0;
  $("#t-reject").disabled = epN === 0;
  $("#t-selinfo").textContent = SELT.size ? `已选 ${segN} 段 / ${epN} 条目` : "未选";
}

// ---- 五件套就地编辑器 ----
function epEditor(r, e) {
  const k = tkey(r.path, "ep", e.stage_id);
  const box = document.createElement("div");
  box.className = "ep-editor";
  const dels = (e.deletions || []).map((d) => `${esc(d.range)} · ${esc(d.reason || "")}`).join("<br>");
  box.innerHTML =
    `<div class="lbl">overview(检索向量来源)</div><textarea class="ed-ov">${esc(e.overview || "")}</textarea>` +
    `<div class="lbl">summary</div><textarea class="ed-sum">${esc(e.summary || "")}</textarea>` +
    `<div class="row"><div style="flex:1"><div class="lbl">salience_tier</div>` +
    `<select class="ed-tier"><option value="1">1 低</option><option value="2">2 中</option><option value="3">3 高</option></select></div>` +
    `<div style="flex:2"><div class="lbl">salience_reason</div><input type="text" class="ed-sr" value="${esc(e.salience_reason || "")}"></div></div>` +
    `<div class="lbl">nodes(概念,逗号分隔 label)</div><input type="text" class="ed-nodes" value="${esc((e.nodes || []).map((n) => n.label).join(", "))}">` +
    `<div class="lbl">highlights(${(e.highlights || []).length})</div><div class="hl-chips"></div>` +
    `<div class="lbl">source_text(去噪:手动删噪声;选中下方预览可加高光)</div>` +
    (dels ? `<div class="del-hint">建议删除:<br>${dels}</div>` : "") +
    `<textarea class="src ed-src">${esc(e.source_text || "")}</textarea>` +
    `<div class="row"><span class="hint">高光预览(黄块=命中 highlights):</span>` +
    `<input type="text" class="hl-tag" placeholder="新高光 tag(可空)" style="width:160px"><button class="hl-add expand">＋选中文字加高光</button></div>` +
    `<div class="hl-prev"></div>` +
    `<div class="acts">` +
    `<button class="ed-save primary">保存编辑</button>` +
    `<button class="ed-undo">↶ 撤销(ctrl-z)</button>` +
    `<button class="ed-confirm primary" title="写入正本+DB,不可逆(只能事后 archive)">确认入库</button>` +
    `<button class="ed-reject danger">拒绝</button>` +
    `</div>`;
  box.querySelector(".ed-tier").value = String(e.salience_tier || 1);

  // 渲染 highlights chips
  const renderChips = () => {
    const wrap = box.querySelector(".hl-chips");
    wrap.innerHTML = "";
    (e.highlights || []).forEach((h, hi) => {
      const c = document.createElement("span");
      c.className = "chip";
      c.innerHTML = `${esc(h.text.slice(0, 40))}${h.text.length > 40 ? "…" : ""}` +
        (h.tag ? ` <i style="color:var(--muted)">#${esc(h.tag)}</i>` : "");
      const x = document.createElement("button");
      x.textContent = "✕";
      x.onclick = () => { e.highlights.splice(hi, 1); renderChips(); renderPrev(); };
      c.appendChild(x);
      wrap.appendChild(c);
    });
  };
  // 渲染高光预览(把 source_text 内命中的 highlight 文本包成 mark)
  const renderPrev = () => {
    const prev = box.querySelector(".hl-prev");
    const src = box.querySelector(".ed-src").value;
    prev.innerHTML = markHighlights(src, e.highlights || []);
  };
  renderChips(); renderPrev();
  box.querySelector(".ed-src").oninput = renderPrev;

  // 手动拉高光:选中预览里的文字 → 加进 highlights
  box.querySelector(".hl-add").onclick = () => {
    const sel = (window.getSelection() ? window.getSelection().toString() : "").trim();
    if (!sel) { toast("先在下方预览里选中一段文字", true); return; }
    const src = box.querySelector(".ed-src").value;
    if (!src.includes(sel)) { toast("选中文字不在 source_text 内", true); return; }
    const tag = box.querySelector(".hl-tag").value.trim();
    e.highlights = e.highlights || [];
    e.highlights.push({ text: sel, tag });
    box.querySelector(".hl-tag").value = "";
    renderChips(); renderPrev();
    toast("已加高光(记得保存编辑)");
  };

  // 收集编辑器当前字段(白名单 _EDITABLE)
  const collect = () => ({
    overview: box.querySelector(".ed-ov").value,
    summary: box.querySelector(".ed-sum").value,
    salience_tier: parseInt(box.querySelector(".ed-tier").value, 10) || 1,
    salience_reason: box.querySelector(".ed-sr").value,
    nodes: box.querySelector(".ed-nodes").value.split(",").map((x) => x.trim()).filter(Boolean)
      .map((label) => ({ label, action: "match_existing" })),
    source_text: box.querySelector(".ed-src").value,
    highlights: e.highlights || [],
    deletions: e.deletions || [],
  });

  box.querySelector(".ed-save").onclick = () => saveEpEdit(r, e, k, collect());
  box.querySelector(".ed-undo").onclick = () => undoEpEdit(r, e, k);
  box.querySelector(".ed-confirm").onclick = () => confirmEps([{ path: r.path, stage_id: e.stage_id }]);
  box.querySelector(".ed-reject").onclick = () => rejectEps([{ path: r.path, stage_id: e.stage_id }]);

  // ctrl-z 绑定(编辑器内)
  box.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "z") { ev.preventDefault(); undoEpEdit(r, e, k); }
  });
  return box;
}

// source_text 内命中 highlight 文本 → <mark>(转义后再插标签,防 XSS)
function markHighlights(src, highlights) {
  const safe = esc(src);
  let out = safe;
  const texts = [...new Set((highlights || []).map((h) => h.text).filter(Boolean))]
    .sort((a, b) => b.length - a.length);  // 长的先替,避免子串先命中
  texts.forEach((t) => {
    const et = esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(et, "g"), (m) => `\x01${m}\x02`);
  });
  return out.replace(/\x01/g, "<mark>").replace(/\x02/g, "</mark>");
}

async function saveEpEdit(r, e, k, fields) {
  // 入栈当前快照供撤销(只存可编辑字段)
  (UNDO[k] = UNDO[k] || []).push(snapshotFields(e));
  if (UNDO[k].length > 20) UNDO[k].shift();
  const d = await postJSON("/api/staging/edit", { path: r.path, stage_id: e.stage_id, fields });
  if (!d || d.error) { toast("保存失败: " + ((d && d.error) || "网络"), true); return; }
  patchEpisode(r.path, d);
  toast("已保存编辑");
}

function undoEpEdit(r, e, k) {
  const stack = UNDO[k];
  if (!stack || !stack.length) { toast("无可撤销", true); return; }
  const prev = stack.pop();
  postJSON("/api/staging/edit", { path: r.path, stage_id: e.stage_id, fields: prev }).then((d) => {
    if (!d || d.error) { toast("撤销失败", true); return; }
    patchEpisode(r.path, d);
    renderTriMain();   // 撤销要让中栏编辑器显示回退后的值
    toast("已撤销");
  });
}

function snapshotFields(e) {
  return {
    overview: e.overview, summary: e.summary, salience_tier: e.salience_tier,
    salience_reason: e.salience_reason, nodes: e.nodes, source_text: e.source_text,
    highlights: e.highlights, deletions: e.deletions,
  };
}

// 用 staging/edit 等回的最新 staging 更新内存会话,并只刷左栏计数(不重渲染中栏,免打断编辑)
function patchEpisode(path, d) {
  const s = TRIS.find((x) => x.source_path === path);
  if (s && d.episodes) { s.episodes = d.episodes; s.retry = d.retry || []; }
  renderTriSessions();
}

// ---- 提取(可批量) ----
async function extractPaths(byPath) {
  const provider = ($("#tri-provider") || {}).value || undefined;
  const model = (($("#tri-model") || {}).value || "").trim() || undefined;
  let staged = 0, failed = 0;
  for (const [path, segIds] of Object.entries(byPath)) {
    const d = await postJSON("/api/extract", { path, seg_ids: segIds, provider, model });
    if (!d || d.error || d.kind === "unavailable") {
      toast("提取失败: " + ((d && d.error) || "provider 不可用"), true);
      continue;
    }
    staged += d.staged || 0; failed += d.failed || 0;
  }
  toast(`提取完成:成功 ${staged} 段` + (failed ? `,失败 ${failed}` : ""));
  await loadTriageAll();   // 重扫磁盘
}

function doExtractSel() {
  const byPath = {};
  SELT.forEach((k) => {
    const [path, kind, id] = k.split("\u0000");
    if (kind !== "seg") return;
    (byPath[path] = byPath[path] || []).push(id);
  });
  if (!Object.keys(byPath).length) { toast("先勾选未提取的段", true); return; }
  extractPaths(byPath);
}

// ---- 确认入库(可批量,二次确认,逐条调以便单条失败定位)----
async function confirmEps(items) {
  if (!items.length) return;
  if (!window.confirm(`确认把 ${items.length} 条写入记忆正本 + DB?\n此操作不可逆(只能事后 archive)。`)) return;
  let okN = 0;
  for (const it of items) {
    const d = await postJSON("/api/confirm", { path: it.path, stage_id: it.stage_id });
    if (!d || d.error) {
      toast(`确认失败(${it.stage_id}): ${(d && d.error) || "网络"},已停止`, true);
      break;
    }
    okN++;
    toast(`已入库 ${d.public_id}`);
  }
  if (okN) { SELT.clear(); await loadTriageAll(); }
}

function doConfirmSel() {
  const items = [];
  SELT.forEach((k) => {
    const [path, kind, id] = k.split("\u0000");
    if (kind === "ep") items.push({ path, stage_id: id });
  });
  confirmEps(items);
}

// ---- 拒绝(可批量)----
async function rejectEps(items) {
  if (!items.length) return;
  const reason = prompt(`拒绝 ${items.length} 条的理由(可空):`, "");
  if (reason === null) return;
  let okN = 0;
  for (const it of items) {
    const d = await postJSON("/api/reject", { path: it.path, stage_id: it.stage_id, reason });
    if (!d || d.error) { toast(`拒绝失败(${it.stage_id})`, true); break; }
    okN++;
  }
  if (okN) { toast(`已拒绝 ${okN} 条`); SELT.clear(); await loadTriageAll(); }
}

function doRejectSel() {
  const items = [];
  SELT.forEach((k) => {
    const [path, kind, id] = k.split("\u0000");
    if (kind === "ep") items.push({ path, stage_id: id });
  });
  rejectEps(items);
}

// POST JSON 小工具
async function postJSON(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    return await r.json();
  } catch (e) { return { error: String(e) }; }
}

async function loadProviders() {
  const d = await (await fetch("/api/agent/providers")).json();
  PROVIDERS = d.providers || [];
  CHUNK_MODEL = d.chunk_model || "sonnet";
  const fill = (sel) => {
    if (!sel) return;
    sel.innerHTML = "";
    PROVIDERS.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = `${p.id}${p.available ? "" : " (不可用)"}${p.default ? " ✓默认" : ""}`;
      o.disabled = !p.available;
      if (p.default && p.available) o.selected = true;
      sel.appendChild(o);
    });
  };
  fill($("#provider"));    // 切段 agent 选择器
  fill($("#tri-provider")); // 提取 agent 选择器(待整理右栏)
  $("#model").placeholder = `模型(空=默认 ${CHUNK_MODEL})`;
  const tm = $("#tri-model");
  if (tm) tm.placeholder = `模型(空=默认 ${d.extract_model || "opus"})`;
}

// ---- 载入一个 transcript ----
async function loadTranscript(path, el) {
  SEL.clear();
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
  LOCK = null;
  $("#exit-proc").style.display = "none";
  renderListItems();
  const gaps = d.gaps || [];
  toast(`已保存 ${SEGS.length} 段` + (gaps.length ? `,${gaps.length} 处空洞回合未覆盖` : ""));
  render();
  return true;
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

$("#run-chunk").onclick = runChunk;
$("#make-seg").onclick = makeSegFromSel;
$("#mark").onclick = markSelected;

// 块 B:确认分段(存段→待整理)、退出处理(解锁)、子阶段切换
$("#confirm-segs").onclick = async () => {
  if (await saveSegs()) {       // saveSegs 已解锁/标记/纳入候选
    setStage("triage");
    toast("已确认分段,进入待整理区");
  }
};
$("#exit-proc").onclick = () => {
  LOCK = null;
  $("#exit-proc").style.display = "none";
  renderListItems();
  toast("已退出处理");
};
document.querySelectorAll("#substage button").forEach((b) =>
  b.onclick = () => setStage(b.dataset.stage));

// 块 C:待整理批量工具条(此前定义了 doExtractSel/doConfirmSel/doRejectSel 却漏绑)
$("#t-extract").onclick = doExtractSel;
$("#t-confirm").onclick = doConfirmSel;
$("#t-reject").onclick = doRejectSel;
$("#t-refresh").onclick = () => loadTriageAll();

// 启动:先恢复本地游标(候选区/刚编辑/子屏),再拉列表;列表到位后把视图切回上次所在子屏。
restoreState();
loadProviders();
loadList().then(() => { if (STAGE === "triage") setStage("triage"); });
