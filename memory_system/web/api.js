"use strict";

// POST JSON 小工具
async function postJSON(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    return await r.json();
  } catch (e) { return { error: String(e) }; }
}

// DELETE JSON 小工具(删 episode / node 走 query 参数,无请求体)
async function delJSON(url) {
  try {
    const r = await fetch(url, { method: "DELETE" });
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
  let d;
  try {
    d = await (await fetch("/api/agent/providers")).json();
  } catch (e) { toast("加载 provider 列表失败: " + e, true); return; }
  ST.providers = d.providers || [];
  ST.chunkModel = d.chunk_model || "sonnet";
  const fill = (sel, role) => {
    if (!sel) return;
    const defProvider = role === "chunk" ? d.chunk_provider :
                        role === "triage" ? d.extract_provider : "";
    sel.innerHTML = "";
    const def = ST.providers.find((p) => p.id === defProvider);
    if (defProvider && (!def || !def.available)) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = `后端默认: ${defProvider}${def ? " (不可用)" : " (未列出)"}`;
      o.selected = true;
      sel.appendChild(o);
    }
    ST.providers.filter((p) => p.available || p.id === defProvider || p.builtin === false).forEach((p) => {
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
  $("#model").placeholder = `模型(空=默认 ${ST.chunkModel})`;
  const tm = $("#tri-model");
  if (tm) tm.placeholder = `模型(空=默认 ${d.extract_model || "opus"})`;
}
