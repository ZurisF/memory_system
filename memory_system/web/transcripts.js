"use strict";

// ---- transcript 列表 ----
// q 给定则更新关键词(空串=清空搜索);省略则沿用当前 ST.tquery 重拉。
async function loadList(q) {
  if (q !== undefined) ST.tquery = q;
  const url = "/api/transcripts" + (ST.tquery ? "?q=" + encodeURIComponent(ST.tquery) : "");
  let d;
  try {
    d = await (await fetch(url)).json();
  } catch (e) { toast("加载列表失败: " + e, true); return; }
  if (d.error) { toast("加载列表失败: " + d.error, true); return; }
  ST.tlist = d.transcripts || [];
  ST.hiddenEmpty = d.hidden_empty || 0;
  ST.tmap.clear();
  ST.touched.clear();
  ST.tlist.forEach((t) => {
    ST.tmap.set(t.path, t);
    if (t.touched) ST.touched.add(t.path);
  });
  renderListItems();
}

// 按当前模式+方向返回渲染顺序。排序在前端按 mtime 做,方向 desc(新→旧)/asc(旧→新);
// touched 模式额外把动过的稳定下沉到底部。
function sortedList() {
  const arr = ST.tlist.slice().sort((a, b) =>
    ST.sortDir === "desc" ? b.mtime - a.mtime : a.mtime - b.mtime);
  if (ST.sortMode !== "touched") return arr;
  const fresh = [], done = [];
  arr.forEach((t) => (t.touched ? done : fresh).push(t));
  return fresh.concat(done);
}

// 选择本地 jsonl → 读内容上传(浏览器只给内容不给真实路径)→ 落到 imports/ → 刷新列表。
async function importFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  let ok = 0, fail = 0, lastErr = "";
  for (const f of files) {
    try {
      const content = await f.text();
      const r = await postJSON("/api/import", { filename: f.name, content });
      if (r && r.ok) ok++; else { fail++; lastErr = (r && r.error) || ""; }
    } catch (e) { fail++; lastErr = String(e); }
  }
  if (ok) {
    await loadList();
    toast(`已导入 ${ok} 个 jsonl` + (fail ? `,${fail} 个失败:${lastErr}` : ""), fail > 0);
  } else {
    toast(`导入失败:${lastErr || fail + " 个"}`, true);
  }
}

// 从 ST.tlist 重渲染(勾选/锁/当前态变化时,不重新拉网络)
function renderListItems() {
  const box = $("#t-items");
  box.innerHTML = "";
  const list = sortedList();
  if (ST.tquery) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `搜索「${ST.tquery}」:${list.length} 条命中` +
      (list.length ? "" : "(原始 jsonl 无此串)");
    box.appendChild(note);
  }
  if (ST.hiddenEmpty) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `已隐藏 ${ST.hiddenEmpty} 个空会话(/clear 空壳等)`;
    box.appendChild(note);
  }
  let groupedDone = false;
  list.forEach((t) => {
    // touched 模式下,第一条动过的会话前插一个分隔标签
    if (ST.sortMode === "touched" && t.touched && !groupedDone) {
      groupedDone = true;
      const lab = document.createElement("div");
      lab.className = "list-group-label";
      lab.textContent = "── 已处理 / 动过 ──";
      box.appendChild(lab);
    }
    const el = document.createElement("div");
    el.className = "t-item" +
      (ST.lock && t.path !== ST.lock ? " locked" : "") +
      (ST.cur && ST.cur.path === t.path ? " cur" : "");
    el.dataset.path = t.path;
    const flags =
      (ST.edited.has(t.path) ? `<span class="flag">刚编辑</span>` : "") +
      (ST.cand.has(t.path) ? `<span class="flag" style="color:var(--me)">候选</span>` : "");
    el.innerHTML =
      `<input type="checkbox" class="cb"${ST.cand.has(t.path) ? " checked" : ""}>` +
      `<div class="body"><div><span class="sid">${esc((t.session_id || "").slice(0, 8))}</span>` +
      (t.maybe_writing ? `<span class="badge">正在写入</span>` : "") +
      (t.imported ? `<span class="badge imported">导入</span>` : "") + flags + `</div>` +
      `<div class="meta">${fmtTime(t.mtime)} · ${t.turn_count}回合 · ${fmtSize(t.size)}</div>` +
      `<div class="meta">${esc(t.cwd || "")}</div></div>`;
    el.querySelector(".cb").onclick = (ev) => { ev.stopPropagation(); toggleCand(t.path); };
    el.querySelector(".body").onclick = () => openForChunk(t.path, el);
    box.appendChild(el);
  });
  renderBasket();
}

function toggleCand(path) {
  if (ST.cand.has(path)) ST.cand.delete(path); else ST.cand.add(path);
  saveState();
  renderListItems();
}

// 候选篮子
function renderBasket() {
  const box = $("#cand-basket");
  $("#cand-info").textContent = `候选 ${ST.cand.size}`;
  box.innerHTML = "";
  if (!ST.cand.size) return;
  const h = document.createElement("div");
  h.className = "cb-h";
  h.textContent = `候选区 (${ST.cand.size})`;
  box.appendChild(h);
  ST.cand.forEach((p) => {
    const t = ST.tmap.get(p);
    const it = document.createElement("div");
    it.className = "cb-item" + (ST.cur && ST.cur.path === p ? " cur" : "");
    it.innerHTML =
      `<span class="cb-sid">${t ? esc((t.session_id || "").slice(0, 8)) : esc(p.split("/").pop())}</span>` +
      `<span style="color:var(--muted)">${t ? t.turn_count + "回合" : ""}</span>`;
    it.onclick = () => openForChunk(p, null);
    box.appendChild(it);
  });
}

// 打开一条(切段屏)。点击=预览,不上锁、不标记;真正动手编辑时才由 beginEdit 上锁。
function openForChunk(path, el) {
  if (ST.lock && path !== ST.lock) { toast("有正在编辑的条目,请先「确认分段」或「退出处理」", true); return; }
  if (ST.segDirty && ST.cur && ST.cur.path !== path &&
      !window.confirm("当前分段还没保存,切换会丢失本地修改。确认丢弃并打开另一条?")) {
    return;
  }
  setStage("chunk");
  loadTranscript(path, el);
}

// 开始编辑(切块/建段/改段任意一处触发):上锁,其他条目禁点。预览不触发。
function beginEdit() {
  if (!ST.cur || ST.lock === ST.cur.path) return;
  ST.lock = ST.cur.path;
  $("#exit-proc").style.display = "";
  renderListItems();
}
// 标脏:上锁。所有就地编辑动作统一走这里。存盘由「确认分段→待整理」一键完成。
function markDirty() {
  beginEdit();
  ST.segDirty = true;
}

// 子阶段切换(视图冻结:只显隐,不销毁各自状态)
function setStage(stage) {
  if (stage === "triage" && ST.segDirty) {
    toast("当前分段还没保存,请先「确认分段→待整理」或「退出处理」", true);
    return;
  }
  ST.stage = stage;
  saveState();
  $("#screen-chunk").style.display = stage === "chunk" ? "flex" : "none";
  $("#screen-triage").style.display = stage === "triage" ? "flex" : "none";
  document.querySelectorAll("#substage button").forEach((b) =>
    b.classList.toggle("active", b.dataset.stage === stage));
  if (stage === "triage") loadTriageAll();
}
