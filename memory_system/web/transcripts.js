"use strict";

// ---- transcript 列表 ----
// q 给定则更新关键词(空串=清空搜索);省略则沿用当前 TQUERY 重拉。
async function loadList(q) {
  if (q !== undefined) TQUERY = q;
  const url = "/api/transcripts" + (TQUERY ? "?q=" + encodeURIComponent(TQUERY) : "");
  const d = await (await fetch(url)).json();
  TLIST = d.transcripts || [];
  HIDDEN_EMPTY = d.hidden_empty || 0;
  TMAP.clear();
  TOUCHED.clear();
  TLIST.forEach((t) => {
    TMAP.set(t.path, t);
    if (t.touched) TOUCHED.add(t.path);
  });
  renderListItems();
}

// 按当前模式+方向返回渲染顺序。排序在前端按 mtime 做,方向 desc(新→旧)/asc(旧→新);
// touched 模式额外把动过的稳定下沉到底部。
function sortedList() {
  const arr = TLIST.slice().sort((a, b) =>
    SORT_DIR === "desc" ? b.mtime - a.mtime : a.mtime - b.mtime);
  if (SORT_MODE !== "touched") return arr;
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

// 从 TLIST 重渲染(勾选/锁/当前态变化时,不重新拉网络)
function renderListItems() {
  const box = $("#t-items");
  box.innerHTML = "";
  const list = sortedList();
  if (TQUERY) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `搜索「${TQUERY}」:${list.length} 条命中` +
      (list.length ? "" : "(原始 jsonl 无此串)");
    box.appendChild(note);
  }
  if (HIDDEN_EMPTY) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `已隐藏 ${HIDDEN_EMPTY} 个空会话(/clear 空壳等)`;
    box.appendChild(note);
  }
  let groupedDone = false;
  list.forEach((t) => {
    // touched 模式下,第一条动过的会话前插一个分隔标签
    if (SORT_MODE === "touched" && t.touched && !groupedDone) {
      groupedDone = true;
      const lab = document.createElement("div");
      lab.className = "list-group-label";
      lab.textContent = "── 已处理 / 动过 ──";
      box.appendChild(lab);
    }
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
  if (SEG_DIRTY && CUR && CUR.path !== path &&
      !window.confirm("当前分段还没保存,切换会丢失本地修改。确认丢弃并打开另一条?")) {
    return;
  }
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
  SEG_DIRTY = true;
}

// 子阶段切换(视图冻结:只显隐,不销毁各自状态)
function setStage(stage) {
  if (stage === "triage" && SEG_DIRTY) {
    toast("当前分段还没保存,请先「确认分段→待整理」或「退出处理」", true);
    return;
  }
  STAGE = stage;
  saveState();
  $("#screen-chunk").style.display = stage === "chunk" ? "flex" : "none";
  $("#screen-triage").style.display = stage === "triage" ? "flex" : "none";
  document.querySelectorAll("#substage button").forEach((b) =>
    b.classList.toggle("active", b.dataset.stage === stage));
  if (stage === "triage") loadTriageAll();
}
