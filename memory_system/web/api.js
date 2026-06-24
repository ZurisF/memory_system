"use strict";

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
