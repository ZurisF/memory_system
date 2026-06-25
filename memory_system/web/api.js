"use strict";

// POST JSON 小工具
async function postJSON(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    return await r.json();
  } catch (e) { return { error: String(e) }; }
}

// 在途锁:防 LLM/写库等耗时操作被重复点击触发重复处理。
// 同 key 在途时,后续点击被吞掉(toast 提示);响应回来(finally)才解锁——按完成解锁,非定时器。
// 传 btn 则处理期间置灰显「处理中…」;若 btn 在重渲染后已离开 DOM(isConnected=false),跳过复原。
const INFLIGHT = new Set();
async function once(key, fn, btn) {
  if (INFLIGHT.has(key)) { toast("正在处理中,请稍候…", true); return; }
  INFLIGHT.add(key);
  let prev;
  if (btn) { prev = btn.textContent; btn.disabled = true; btn.textContent = "处理中…"; }
  try {
    return await fn();
  } finally {
    INFLIGHT.delete(key);
    if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = prev; }
  }
}

async function loadProviders() {
  const d = await (await fetch("/api/agent/providers")).json();
  PROVIDERS = d.providers || [];
  CHUNK_MODEL = d.chunk_model || "sonnet";
  const fill = (sel, role) => {
    if (!sel) return;
    const defProvider = role === "chunk" ? (d.chunk_provider || d.chunk_model) :
                        role === "triage" ? (d.extract_provider || d.extract_model) : "";
    sel.innerHTML = "";
    PROVIDERS.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = `${p.id}${p.available ? "" : " (不可用)"}${p.id === defProvider ? " ✓默认" : ""}`;
      o.disabled = !p.available;
      if (p.id === defProvider && p.available) o.selected = true;
      sel.appendChild(o);
    });
  };
  fill($("#provider"), "chunk");     // 切段 agent 选择器
  fill($("#tri-provider"), "triage"); // 提取 agent 选择器(待整理右栏)
  $("#model").placeholder = `模型(空=默认 ${CHUNK_MODEL})`;
  const tm = $("#tri-model");
  if (tm) tm.placeholder = `模型(空=默认 ${d.extract_model || "opus"})`;
}
