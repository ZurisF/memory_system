"use strict";

// ====== 块 C:待整理树 + 五件套编辑器 + 批量归档 ======

const tkey = (sessionId, kind, id) => sessionId + "\u0000" + kind + "\u0000" + id;
function triBySession(sessionId) { return TRIS.find((s) => s.session_id === sessionId) || null; }

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

// 一个会话在蒸馏区还有没有可显示的东西(段/条目/重试)。全空 = 不显示该 jsonl,保列表整洁。
function triNonEmpty(s) {
  return unextractedSegs(s).length > 0 ||
    (s.episodes || []).length > 0 || (s.retry || []).length > 0;
}

// 扫磁盘拉全部在处理会话(改服务端状态后调它重拉);与候选区无关,不丢
async function loadTriageAll() {
  const box = $("#tri-sessions");
  box.innerHTML = `<div class="list-note" style="padding:10px 14px">加载中…</div>`;
  try {
    const d = await (await fetch("/api/staging/all")).json();
    TRIS = (d.sessions || []).filter(triNonEmpty);   // 删空的 jsonl 自动离开列表
  } catch (e) { TRIS = []; }
  if (TRI_CUR && !TRIS.some((s) => s.session_id === TRI_CUR)) TRI_CUR = null;
  // 清掉已不存在的选中项
  const live = new Set();
  TRIS.forEach((s) => {
    unextractedSegs(s).forEach((seg) => live.add(tkey(s.session_id, "seg", seg.seg_id)));
    (s.episodes || []).forEach((e) => live.add(tkey(s.session_id, "ep", e.stage_id)));
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
  const r = { session_id: s.session_id, path: s.source_path, source_exists: s.source_exists };
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
  const k = tkey(r.session_id, "seg", s.seg_id);
  const card = document.createElement("div");
  card.className = "tri-seg-card";
  card.innerHTML =
    `<input type="checkbox" class="r-cb"${SELT.has(k) ? " checked" : ""}>` +
    `<span class="seg-meta">段 ${s.start_turn}–${s.end_turn} · ${esc(s.tag || "")} ` +
    `<i>[${esc(s.origin || "")}] 未提取</i></span>`;
  const btn = document.createElement("button");
  btn.className = "seg-extract"; btn.textContent = "提取总结";
  btn.disabled = !r.source_exists;
  if (!r.source_exists) btn.title = "源 jsonl 已清,不能再提取新段";
  btn.onclick = () => extractPaths({ [r.path]: [s.seg_id] });
  card.appendChild(btn);
  card.querySelector(".r-cb").onclick = (ev) => { toggleSel(k, ev.target.checked); };
  return card;
}

// 已提取条目:勾选头 + 五件套编辑器(复用 epEditor)
function triEpCard(r, e) {
  const k = tkey(r.session_id, "ep", e.stage_id);
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
  btn.disabled = !r.source_exists;
  if (!r.source_exists) btn.title = "源 jsonl 已清,不能再重试提取";
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
  let extractable = 0;
  SELT.forEach((k) => {
    const [sessionId, kind] = k.split("\u0000");
    if (kind === "seg") {
      segN++;
      const s = triBySession(sessionId);
      if (s && s.source_exists && s.source_path) extractable++;
    } else {
      epN++;
    }
  });
  $("#t-extract").disabled = extractable === 0;
  $("#t-confirm").disabled = epN === 0;
  $("#t-reject").disabled = epN === 0;
  $("#t-delete").disabled = SELT.size === 0;          // 段或条目都能删
  $("#t-delete").textContent = SELT.size ? `删除选中 (${SELT.size})` : "删除选中";
  $("#t-all").disabled = !curSession();
  $("#t-selinfo").textContent = SELT.size ? `已选 ${segN} 段 / ${epN} 条目` : "未选";
}

// ---- 五件套就地编辑器 ----
function epEditor(r, e) {
  const k = tkey(r.session_id, "ep", e.stage_id);
  const box = document.createElement("div");
  box.className = "ep-editor";
  const dels = (e.deletions || []).map((d) => `${esc(d.range)} · ${esc(d.reason || "")}`).join("<br>");
  const nodeJson = JSON.stringify(e.nodes || [], null, 2);
  box.innerHTML =
    `<div class="lbl">overview(检索向量来源)</div><textarea class="ed-ov">${esc(e.overview || "")}</textarea>` +
    `<div class="lbl">summary</div><textarea class="ed-sum">${esc(e.summary || "")}</textarea>` +
    `<div class="row"><div style="flex:1"><div class="lbl">salience_tier</div>` +
    `<select class="ed-tier"><option value="1">1 低</option><option value="2">2 中</option><option value="3">3 高</option></select></div>` +
    `<div style="flex:2"><div class="lbl">salience_reason</div><input type="text" class="ed-sr" value="${escAttr(e.salience_reason || "")}"></div></div>` +
    `<div class="lbl">nodes(JSON;保留 action/new_alias)</div><textarea class="ed-nodes">${esc(nodeJson)}</textarea>` +
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
  const collect = () => {
    let nodes;
    try {
      nodes = JSON.parse(box.querySelector(".ed-nodes").value || "[]");
      if (!Array.isArray(nodes)) throw new Error("nodes 必须是数组");
    } catch (err) {
      toast("nodes JSON 无效: " + err.message, true);
      return null;
    }
    return {
      overview: box.querySelector(".ed-ov").value,
      summary: box.querySelector(".ed-sum").value,
      salience_tier: parseInt(box.querySelector(".ed-tier").value, 10) || 1,
      salience_reason: box.querySelector(".ed-sr").value,
      nodes,
      source_text: box.querySelector(".ed-src").value,
      highlights: e.highlights || [],
      deletions: e.deletions || [],
    };
  };

  box.querySelector(".ed-save").onclick = () => {
    const fields = collect();
    if (fields) saveEpEdit(r, e, k, fields);
  };
  box.querySelector(".ed-undo").onclick = () => undoEpEdit(r, e, k);
  box.querySelector(".ed-confirm").onclick = () => confirmEps([{ session_id: r.session_id, stage_id: e.stage_id }]);
  box.querySelector(".ed-reject").onclick = () => rejectEps([{ session_id: r.session_id, stage_id: e.stage_id }]);

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
  const d = await postJSON("/api/staging/edit", { session_id: r.session_id, stage_id: e.stage_id, fields });
  if (!d || d.error) { toast("保存失败: " + ((d && d.error) || "网络"), true); return; }
  patchLocalEpisode(r, e, d);
  toast("已保存编辑");
}

function undoEpEdit(r, e, k) {
  const stack = UNDO[k];
  if (!stack || !stack.length) { toast("无可撤销", true); return; }
  const prev = stack.pop();
  postJSON("/api/staging/edit", { session_id: r.session_id, stage_id: e.stage_id, fields: prev }).then((d) => {
    if (!d || d.error) { toast("撤销失败", true); return; }
    patchLocalEpisode(r, e, d);
    renderTriMain();   // 撤销要让中栏编辑器显示回退后的值
    toast("已撤销");
  });
}

function snapshotFields(e) {
  return {
    overview: e.overview, summary: e.summary, salience_tier: e.salience_tier,
    salience_reason: e.salience_reason, nodes: clone(e.nodes), source_text: e.source_text,
    highlights: clone(e.highlights), deletions: clone(e.deletions),
  };
}

// 用 staging/edit 等回的最新 staging 更新内存会话,并只刷左栏计数(不重渲染中栏,免打断编辑)
function patchEpisode(sessionId, d) {
  const s = triBySession(sessionId);
  if (s && d.episodes) { s.episodes = d.episodes; s.retry = d.retry || []; }
  renderTriSessions();
}
function patchLocalEpisode(r, e, d) {
  patchEpisode(r.session_id, d);
  const s = triBySession(r.session_id);
  const fresh = s && (s.episodes || []).find((x) => x.stage_id === e.stage_id);
  if (fresh) Object.assign(e, clone(fresh));
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
    const [sessionId, kind, id] = k.split("\u0000");
    if (kind !== "seg") return;
    const s = triBySession(sessionId);
    if (!s || !s.source_exists || !s.source_path) return;
    (byPath[s.source_path] = byPath[s.source_path] || []).push(id);
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
    const d = await postJSON("/api/confirm", { session_id: it.session_id, stage_id: it.stage_id });
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
    const [sessionId, kind, id] = k.split("\u0000");
    if (kind === "ep") items.push({ session_id: sessionId, stage_id: id });
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
    const d = await postJSON("/api/reject", { session_id: it.session_id, stage_id: it.stage_id, reason });
    if (!d || d.error) { toast(`拒绝失败(${it.stage_id})`, true); break; }
    okN++;
  }
  if (okN) { toast(`已拒绝 ${okN} 条`); SELT.clear(); await loadTriageAll(); }
}

function doRejectSel() {
  const items = [];
  SELT.forEach((k) => {
    const [sessionId, kind, id] = k.split("\u0000");
    if (kind === "ep") items.push({ session_id: sessionId, stage_id: id });
  });
  rejectEps(items);
}

// ---- 干净删除(条目 remove_episode / 段 segments/delete)----
// 条目删除:撤掉 staging episode,不留痕,源段回「未提取」。段删除:从 chunks 真删。
async function deleteEps(items) {           // [{session_id, stage_id}]
  let okN = 0;
  for (const it of items) {
    const d = await postJSON("/api/staging/delete",
      { session_id: it.session_id, stage_id: it.stage_id });
    if (!d || d.error) { toast(`删除条目失败(${it.stage_id})`, true); break; }
    okN++;
  }
  return okN;
}
async function deleteSegsTri(bySession) {   // {session_id: [seg_id,...]}
  let okN = 0;
  for (const [sid, ids] of Object.entries(bySession)) {
    let d = await postJSON("/api/segments/delete", { session_id: sid, seg_ids: ids });
    // 蒸馏区里能选的段都是「未提取段」,理论不触发 needs_confirm;真碰上就直接 force。
    if (d && d.needs_confirm) {
      d = await postJSON("/api/segments/delete", { session_id: sid, seg_ids: ids, force: true });
    }
    if (!d || d.error) { toast("删段失败", true); break; }
    okN += d.deleted || 0;
  }
  return okN;
}

// 单条条目删除(卡上「删除」按钮)
async function deleteOneEp(sessionId, stageId) {
  if (!window.confirm("删除这条未入库条目?\n干净撤掉、不留痕;源段回到「未提取」(可重新提取或删段)。")) return;
  if (await deleteEps([{ session_id: sessionId, stage_id: stageId }])) {
    toast("已删除条目");
    await loadTriageAll();
  }
}

// 批量删除选中(段 + 条目)
async function doDeleteSel() {
  const eps = [], segBy = {};
  SELT.forEach((k) => {
    const [sessionId, kind, id] = k.split("\u0000");
    if (kind === "ep") eps.push({ session_id: sessionId, stage_id: id });
    else (segBy[sessionId] = segBy[sessionId] || []).push(id);
  });
  const nSeg = Object.values(segBy).reduce((a, b) => a + b.length, 0);
  if (!eps.length && !nSeg) { toast("先勾选要删的段/条目", true); return; }
  let msg = `确认删除选中的 ${nSeg} 段 + ${eps.length} 条目?`;
  if (eps.length) msg += `\n条目为干净撤除(不留痕、不入库),源段回到「未提取」。`;
  if (!window.confirm(msg)) return;
  let okEp = 0, okSeg = 0;
  if (eps.length) okEp = await deleteEps(eps);
  if (nSeg) okSeg = await deleteSegsTri(segBy);
  SELT.clear();
  await loadTriageAll();
  toast(`已删 ${okSeg} 段 / ${okEp} 条目`);
}

// 全选 / 取消全选当前会话的段 + 条目
function toggleAllTri() {
  const s = curSession();
  if (!s) return;
  const keys = [];
  unextractedSegs(s).forEach((seg) => keys.push(tkey(s.session_id, "seg", seg.seg_id)));
  (s.episodes || []).forEach((e) => keys.push(tkey(s.session_id, "ep", e.stage_id)));
  const allOn = keys.length && keys.every((k) => SELT.has(k));
  if (allOn) keys.forEach((k) => SELT.delete(k));
  else keys.forEach((k) => SELT.add(k));
  renderTriMain();
}
