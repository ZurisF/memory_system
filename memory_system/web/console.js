"use strict";

/* console.js —— 控制台:agent 配置 / 模型切换 / 自定义 provider / key 状态(密文掩码) / 连接测试。
 *
 * API:
 *   GET  /api/agent/config       — 全部配置(agent + embedding + key 掩码)
 *   POST /api/agent/config       — 保存 model/provider 切换 → .env
 *   POST /api/agent/providers    — 添加自定义 OpenAI 兼容 provider
 *   PUT  /api/agent/providers    — 修改自定义 provider
 *   DELETE /api/agent/providers?id= — 删除自定义 provider
 *   POST /api/agent/test         — 连接测试(chat provider)
 *   POST /api/embedding/test     — 连接测试(embedding 端点)
 * key 永不经前端,只显掩码。
 */

var Console = (function () {

  var loaded = false;
  var CONFIG = null;

  /* ═══════════ 渲染入口 ═══════════ */

  function onShow() {
    if (loaded) return;
    fetchConfig();
  }

  async function fetchConfig() {
    try {
      var r = await fetch("/api/agent/config");
      CONFIG = await r.json();
    } catch (e) {
      CONFIG = { _error: String(e) };
    }
    loaded = true;
    render();
  }

  function render() {
    if (!CONFIG) return;
    var agentsEl = $("#cfg-agents");
    var embEl = $("#cfg-embedding");
    if (!agentsEl || !embEl) return;

    if (CONFIG._error) {
      agentsEl.innerHTML = '<div class="alert err" style="margin:0">加载配置失败: ' + esc(CONFIG._error) + '</div>';
      embEl.innerHTML = "";
      return;
    }

    agentsEl.innerHTML = renderAgentCards() + renderCustomProviders() + renderAddForm();
    embEl.innerHTML = renderEmbedding();
    updateNote();
    bindAll();
  }

  /* ═══════════ Agent 卡片 ═══════════ */

  var ROLE_META = {
    chunk:   { name: "切块",     role: "S3 · 分段 agent" },
    extract: { name: "提取总结", role: "S4 · 五件套 agent" },
  };

  function renderAgentCards() {
    var agents = CONFIG.agents || {};
    var keys = CONFIG.agent_keys || [];
    var html = "";

    ["chunk", "extract"].forEach(function (role) {
      var a = agents[role];
      if (!a) return;
      var meta = ROLE_META[role] || { name: role, role: "" };
      var providers = a.providers || [];
      var curProviderId = a.provider || "";
      var curProv = providers.find(function (p) { return p.id === curProviderId; }) || providers[0] || {};
      var providerOk = curProv.available;
      var keyInfo = findKeyInfo(keys, curProviderId);

      html += '<div class="cfg" data-role="' + escAttr(role) + '">';
      html += cardHead(meta.name, meta.role, providerOk, curProv.builtin === false ? "自定义" : null);

      html += '<div class="cfg-body">';

      // Provider 下拉(含自定义)
      html += '<div class="cfg-row">';
      html += '<span class="cfg-k">Provider</span>';
      html += '<select class="inp cfg-provider" style="width:auto;min-width:200px">';
      visibleProviders(providers, curProviderId).forEach(function (p) {
        var sel = p.id === curProviderId ? ' selected' : '';
        var dis = p.available ? '' : ' disabled';
        var label = p.id + (p.name ? ' (' + esc(p.name) + ')' : '') + (p.available ? '' : ' 不可用');
        html += '<option value="' + escAttr(p.id) + '"' + sel + dis + '>' + esc(label) + '</option>';
      });
      html += '</select>';
      html += '</div>';

      // 模型输入
      html += '<div class="cfg-row">';
      html += '<span class="cfg-k">模型</span>';
      html += '<input type="text" class="inp cfg-model" style="width:auto;min-width:200px" value="' +
        escAttr(a.model || "") + '" placeholder="如 sonnet / opus / haiku">';
      html += '</div>';

      // Key 状态（动态更新）
      html += '<div class="cfg-row" data-key-row="' + escAttr(role) + '">';
      html += '<span class="cfg-k">Key 状态</span>';
      html += renderKeyStatus(keyInfo);
      html += '</div>';

      // 超时
      if (CONFIG.timeout_s) {
        html += '<div class="cfg-row">';
        html += '<span class="cfg-k">超时</span>';
        html += '<span class="cfg-v" style="font-size:12px">' +
          esc(String(CONFIG.timeout_s)) + 's · 最多重试 ' + (CONFIG.max_retries || 0) + ' 次</span>';
        html += '</div>';
      }

      html += '</div>'; // cfg-body

      html += '<div class="cfg-foot">';
      html += '<button class="btn btn-s test-btn" data-provider="' + escAttr(curProviderId) + '">连接测试</button>';
      html += '<span class="test-status" style="font-size:11px;padding:0"></span>';
      html += '<button class="btn btn-s btn-ok save-btn" data-role="' + escAttr(role) + '" style="margin-left:auto">保存</button>';
      html += '<span class="save-status" style="font-size:11px;padding:0"></span>';
      html += '</div>';

      html += '</div>'; // cfg
    });

    // 精简 agent(Phase 2 占位)
    html += '<div class="cfg" style="opacity:0.4">';
    html += cardHead("精简", "Phase 2 · 压缩 agent", false, "未实装");
    html += '<div class="cfg-body">';
    html += '<div class="cfg-row"><span class="cfg-k">Provider</span><span class="cfg-v" style="color:var(--muted)">未配置</span></div>';
    html += '<div class="cfg-row"><span class="cfg-k">模型</span><span class="cfg-v" style="color:var(--muted)">—</span></div>';
    html += '</div>';
    html += '<div class="cfg-foot"><button class="btn btn-s" disabled>连接测试</button></div>';
    html += '</div>';

    return html;
  }

  function visibleProviders(providers, curProviderId) {
    // 角色下拉只放可直接使用的 provider + 当前 provider(哪怕不可用,用于显示问题)。
    // 大量不可用内置 provider 不再挤占下拉空间。
    return (providers || []).filter(function (p) {
      return p.available || p.id === curProviderId || p.builtin === false;
    });
  }

  function allProviders() {
    var seen = {};
    var out = [];
    ["chunk", "extract"].forEach(function (role) {
      var ag = (CONFIG.agents || {})[role];
      (ag && ag.providers || []).forEach(function (p) {
        if (!seen[p.id]) { seen[p.id] = true; out.push(p); }
      });
    });
    return out;
  }

  function renderCustomProviders() {
    var custom = allProviders().filter(function (p) { return p.builtin === false; });
    if (!custom.length) return "";
    var html = '<div class="cfg" id="cfg-custom-providers">';
    html += cardHead("自定义 Provider", "编辑 / 删除 OpenAI 兼容端点", true, String(custom.length));
    html += '<div class="cfg-body">';
    custom.forEach(function (p) {
      html += '<div class="cfg-provider-row" data-provider-id="' + escAttr(p.id) + '">';
      html += '<div class="cfg-provider-top"><span class="cfg-v">' + esc(p.id) + '</span>' +
        '<span class="badge ' + (p.available ? 'ok' : 'no') + '">' + (p.available ? '可用' : '不可用') + '</span></div>';
      html += '<div class="cfg-row"><span class="cfg-k">名称</span>' +
        '<input type="text" class="inp prov-name" value="' + escAttr(p.name || p.id) + '"></div>';
      html += '<div class="cfg-row"><span class="cfg-k">Base URL</span>' +
        '<input type="text" class="inp prov-url" value="' + escAttr(p.base_url || "") + '"></div>';
      html += '<div class="cfg-row"><span class="cfg-k">默认模型</span>' +
        '<input type="text" class="inp prov-model" value="' + escAttr(p.default_model || "") + '"></div>';
      html += '<div class="cfg-provider-actions">' +
        '<button class="btn btn-s test-btn" data-provider="' + escAttr(p.id) + '">连接测试</button>' +
        '<span class="test-status" style="font-size:11px;padding:0"></span>' +
        '<button class="btn btn-s btn-ok edit-prov-btn">保存修改</button>' +
        '<button class="btn btn-s btn-x del-prov-btn">删除</button>' +
        '</div>';
      html += '</div>';
    });
    html += '</div></div>';
    return html;
  }

  /* ═══════════ 添加 Provider 表单 ═══════════ */

  function renderAddForm() {
    return '<div class="cfg cfg-add" id="cfg-add-wrap">' +
      '<div class="cfg-head" id="cfg-add-toggle" style="cursor:pointer">' +
        '<span class="cfg-name">+ 添加 Provider</span>' +
        '<span class="cfg-role">OpenAI 兼容端点（如 MiniMax / qwen / 本地模型）</span>' +
      '</div>' +
      '<div class="cfg-body" id="cfg-add-body" style="display:none">' +
        '<div class="cfg-row">' +
          '<span class="cfg-k">名称</span>' +
          '<input type="text" class="inp" id="add-name" placeholder="如 MiniMax" style="width:auto;min-width:200px">' +
        '</div>' +
        '<div class="cfg-row">' +
          '<span class="cfg-k">Base URL</span>' +
          '<input type="text" class="inp" id="add-url" placeholder="https://api.example.com/v1" style="width:auto;min-width:280px">' +
        '</div>' +
        '<div class="cfg-row">' +
          '<span class="cfg-k">默认模型</span>' +
          '<input type="text" class="inp" id="add-model" placeholder="如 gpt-4o-mini（可选）" style="width:auto;min-width:200px">' +
        '</div>' +
        '<div class="cfg-row" style="margin-top:4px">' +
          '<span class="cfg-k"></span>' +
          '<span style="font-size:11px;color:var(--muted)">会自动生成环境变量名并在 .env 写入占位 key，你需要手动替换为真实 key</span>' +
        '</div>' +
        '<div style="margin-top:12px;display:flex;gap:8px">' +
          '<button class="btn btn-ok" id="add-submit">添加</button>' +
          '<span id="add-status" style="font-size:11px;color:var(--muted);align-self:center"></span>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function bindAddForm() {
    var toggle = document.getElementById("cfg-add-toggle");
    var body = document.getElementById("cfg-add-body");
    var submit = document.getElementById("add-submit");
    if (!toggle || !body || !submit) return;
    if (toggle._boundAdd) return;
    toggle._boundAdd = true;

    toggle.addEventListener("click", function () {
      body.style.display = body.style.display === "none" ? "block" : "none";
    });

    submit.addEventListener("click", function () {
      var name = (document.getElementById("add-name") || {}).value || "";
      var url = (document.getElementById("add-url") || {}).value || "";
      var model = (document.getElementById("add-model") || {}).value || "";
      var statusEl = document.getElementById("add-status");
      addProvider(name.trim(), url.trim(), model.trim(), submit, statusEl, body);
    });
  }

  async function addProvider(name, url, model, btn, statusEl, bodyEl) {
    if (!name || !url) {
      if (statusEl) { statusEl.textContent = "名称和 URL 必填"; statusEl.style.color = "var(--err)"; }
      return;
    }
    await once("add-provider", async function () {
      if (statusEl) { statusEl.textContent = "添加中…"; statusEl.style.color = "var(--muted)"; }
      try {
        var r = await postJSON("/api/agent/providers", { name: name, base_url: url, model: model });
        if (!r || r.error) {
          if (statusEl) { statusEl.textContent = "✗ " + (r ? r.error : "无响应"); statusEl.style.color = "var(--err)"; }
          toast("添加失败: " + (r ? r.error : "无响应"), true);
          return;
        }
        if (r.ok) {
          var prov = r.provider || {};
          var msg = "已添加 " + (prov.name || prov.id) + "。请到 ~/.memory_system/.env 把 " + (prov.api_key_env || "") + " 的占位值替换为真实 key";
          toast(msg);
          // 有警告则追加 toast
          if (r.warnings && r.warnings.length) {
            r.warnings.forEach(function (w) { toast(w, true); });
          }
          // 刷新
          var fresh = await (await fetch("/api/agent/config")).json();
          if (fresh && !fresh._error) { CONFIG = fresh; render(); }
        }
      } catch (e) {
        if (statusEl) { statusEl.textContent = "✗ 请求失败"; statusEl.style.color = "var(--err)"; }
        toast("添加请求失败: " + String(e), true);
      }
    }, btn);
  }

  /* ═══════════ Embedding 卡片 ═══════════ */

  function renderEmbedding() {
    var emb = CONFIG.embedding || {};
    var ok = emb.key_present;
    var html = "";

    html += '<div class="cfg">';
    html += cardHead(emb.provider || "—", (emb.model || "") + " / " + (emb.dim || "?") + "d", ok, null);
    html += '<div class="cfg-body">';
    html += '<div class="cfg-row"><span class="cfg-k">Provider</span><span class="cfg-v">' + esc(emb.provider || "—") + '</span></div>';
    html += '<div class="cfg-row"><span class="cfg-k">模型</span><span class="cfg-v">' + esc(emb.model || "—") + '</span></div>';
    html += '<div class="cfg-row"><span class="cfg-k">维度</span><span class="cfg-v">' + esc(String(emb.dim || "—")) + '</span></div>';
    html += '<div class="cfg-row"><span class="cfg-k">Key 状态</span>' + renderKeyStatus({
      key_env: emb.key_env, key_masked: emb.key_masked, key_present: emb.key_present
    }) + '</div>';
    html += '</div>';
    html += '<div class="cfg-foot">';
    html += '<button class="btn btn-s test-btn" data-provider="' + escAttr(emb.provider || "") + '">连接测试</button>';
    html += '<span class="test-status" style="font-size:11px;padding:0"></span>';
    html += '</div>';
    html += '</div>';

    return html;
  }

  /* ═══════════ Key 状态行 ═══════════ */

  function renderKeyStatus(keyInfo) {
    if (!keyInfo) return '<span class="cfg-v" style="color:var(--muted)">—</span>';
    if (!keyInfo.key_env) {
      return '<span class="cfg-v" style="color:var(--muted)">无需 key（本机 claude -p）</span>';
    }
    var html = '';
    if (keyInfo.key_masked) {
      html += '<span class="cfg-v mask">' + esc(keyInfo.key_masked) + '</span>';
    } else {
      html += '<span class="cfg-v" style="color:var(--muted)">占位未替换</span>';
    }
    html += keyInfo.key_present
      ? '<span class="badge ok">已配</span>'
      : '<span class="badge no">未配</span>';
    html += ' <span style="font-size:10px;color:var(--muted)">(' + esc(keyInfo.key_env) + ')</span>';
    return html;
  }

  /* ═══════════ Provider 切换 → 动态 Key 行 ═══════════ */

  function bindProviderChange() {
    document.querySelectorAll("#view-console .cfg-provider").forEach(function (sel) {
      if (sel._boundProv) return;
      sel._boundProv = true;
      sel.addEventListener("change", function () {
        var card = this.closest(".cfg");
        if (!card) return;
        var newProvider = this.value;
        var keyInfo = findKeyInfo(CONFIG.agent_keys || [], newProvider);
        var keyRow = card.querySelector("[data-key-row]");
        if (keyRow) {
          keyRow.innerHTML = '<span class="cfg-k">Key 状态</span>' + renderKeyStatus(keyInfo);
        }
        var testBtn = card.querySelector(".test-btn");
        if (testBtn) testBtn.dataset.provider = newProvider;
        // 更新状态灯
        var found = findProviderInConfig(newProvider);
        var dot = card.querySelector(".cfg-dot");
        if (dot) dot.className = "cfg-dot" + (found && found.available ? " on" : " off");
      });
    });
  }

  function findProviderInConfig(providerId) {
    var result = null;
    ["chunk", "extract"].forEach(function (r) {
      var ag = (CONFIG.agents || {})[r];
      if (!ag) return;
      var p = (ag.providers || []).find(function (x) { return x.id === providerId; });
      if (p) result = p;
    });
    return result;
  }

  /* ═══════════ 底部提示 ═══════════ */

  function updateNote() {
    var emb = CONFIG.embedding || {};
    var vars = [emb.key_env].filter(Boolean);
    var keys = CONFIG.agent_keys || [];
    keys.forEach(function (k) {
      if (k.key_env && vars.indexOf(k.key_env) === -1) vars.push(k.key_env);
    });
    var note = $("#cfg-note");
    if (!note) return;
    var list = vars.map(function (v) { return '<code>' + esc(v) + '</code>'; }).join("、");
    note.innerHTML = 'Key 不可在此填写。请在 <code>~/.memory_system/.env</code> 配置 ' + list + ' 等环境变量，保存后回此页点击连接测试。';
  }

  /* ═══════════ 保存配置 ═══════════ */

  function bindSaveButtons() {
    document.querySelectorAll("#view-console .save-btn").forEach(function (btn) {
      if (btn._boundSave) return;
      btn._boundSave = true;
      btn.addEventListener("click", function () {
        var role = this.dataset.role;
        var card = this.closest(".cfg");
        if (!card || !role) return;
        var providerSel = card.querySelector(".cfg-provider");
        var modelInput = card.querySelector(".cfg-model");
        var provider = providerSel ? providerSel.value : "";
        var model = modelInput ? modelInput.value.trim() : "";
        if (!provider && !model) { toast("未做任何修改", true); return; }

        var body = { role: role };
        if (provider) body.provider = provider;
        if (model) body.model = model;

        var statusEl = card.querySelector(".save-status");
        saveConfig(body, this, statusEl);
      });
    });
  }

  async function saveConfig(body, btn, statusEl) {
    await once("save-" + body.role, async function () {
      if (statusEl) { statusEl.textContent = "保存中…"; statusEl.style.color = "var(--muted)"; }
      try {
        var r = await postJSON("/api/agent/config", body);
        if (!r || r.error) {
          if (statusEl) { statusEl.textContent = "✗ " + (r ? r.error : "无响应"); statusEl.style.color = "var(--err)"; }
          toast("保存失败: " + (r ? r.error : "无响应"), true);
          return;
        }
        if (r.ok) {
          if (statusEl) { statusEl.textContent = "✓ 已保存"; statusEl.style.color = "var(--claude)"; }
          toast(body.role + " 配置已保存" + (r.restart_required ? "，需重启服务全局生效" : ""));
          var fresh = await (await fetch("/api/agent/config")).json();
          if (fresh && !fresh._error) { CONFIG = fresh; render(); }
        }
      } catch (e) {
        if (statusEl) { statusEl.textContent = "✗ 请求失败"; statusEl.style.color = "var(--err)"; }
        toast("保存请求失败: " + String(e), true);
      }
    }, btn);
  }

  /* ═══════════ 连接测试 ═══════════ */

  function bindTestButtons() {
    document.querySelectorAll("#view-console .test-btn").forEach(function (btn) {
      if (btn._boundTest) return;
      btn._boundTest = true;
      btn.addEventListener("click", function () {
        var provider = this.dataset.provider;
        var card = this.closest(".cfg");
        var statusEl = card ? card.querySelector(".test-status") : null;
        runTest(provider, this, statusEl);
      });
    });
  }

  async function runTest(provider, btn, statusEl) {
    if (!provider) return;

    // embedding test
    if (provider === "dashscope" || provider === (CONFIG.embedding || {}).provider) {
      await once("embed-test", async function () {
        if (statusEl) { statusEl.textContent = "测试中…"; statusEl.style.color = "var(--muted)"; }
        try {
          var r = await postJSON("/api/embedding/test", {});
          if (!r) { if (statusEl) { statusEl.textContent = "无响应"; statusEl.style.color = "var(--err)"; } return; }
          if (r.ok) {
            if (statusEl) { statusEl.textContent = "✓ " + (r.detail || "可用"); statusEl.style.color = "var(--claude)"; }
            toast("Embedding 连接成功,维度=" + (r.dim || "?"));
          } else {
            if (statusEl) { statusEl.textContent = "✗ " + (r.detail || "不可用"); statusEl.style.color = "var(--err)"; }
            toast("Embedding: " + (r.detail || "不可用"), true);
          }
        } catch (e) {
          if (statusEl) { statusEl.textContent = "✗ 请求失败"; statusEl.style.color = "var(--err)"; }
        }
      }, btn);
      return;
    }

    // agent test
    await once("test-" + provider, async function () {
      if (statusEl) { statusEl.textContent = "测试中…"; statusEl.style.color = "var(--muted)"; }
      try {
        var r2 = await postJSON("/api/agent/test", { provider: provider });
        if (!r2) { if (statusEl) { statusEl.textContent = "无响应"; statusEl.style.color = "var(--err)"; } return; }
        if (r2.ok) {
          if (statusEl) { statusEl.textContent = "✓ " + (r2.detail || "可用"); statusEl.style.color = "var(--claude)"; }
          toast(provider + " 连接成功");
        } else {
          if (statusEl) { statusEl.textContent = "✗ " + (r2.detail || "不可用"); statusEl.style.color = "var(--err)"; }
          toast(provider + ": " + (r2.detail || "不可用"), true);
        }
      } catch (e) {
        if (statusEl) { statusEl.textContent = "✗ 请求失败"; statusEl.style.color = "var(--err)"; }
      }
    }, btn);
  }

  /* ═══════════ 删除自定义 provider ═══════════ */

  function bindProviderManageButtons() {
    document.querySelectorAll("#cfg-custom-providers .edit-prov-btn").forEach(function (btn) {
      if (btn._boundEditProv) return;
      btn._boundEditProv = true;
      btn.addEventListener("click", function () {
        var row = this.closest(".cfg-provider-row");
        if (!row) return;
        updateProvider(row, this);
      });
    });
    document.querySelectorAll("#cfg-custom-providers .del-prov-btn").forEach(function (btn) {
      if (btn._boundDelProv) return;
      btn._boundDelProv = true;
      btn.addEventListener("click", function () {
        var row = this.closest(".cfg-provider-row");
        if (!row) return;
        deleteProvider(row);
      });
    });
  }

  async function updateProvider(row, btn) {
    var pid = row.dataset.providerId;
    var name = (row.querySelector(".prov-name") || {}).value || "";
    var url = (row.querySelector(".prov-url") || {}).value || "";
    var model = (row.querySelector(".prov-model") || {}).value || "";
    if (!pid || !name.trim() || !url.trim()) { toast("名称和 Base URL 必填", true); return; }
    await once("edit-provider:" + pid, async function () {
      try {
        var r = await fetch("/api/agent/providers", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: pid, name: name.trim(), base_url: url.trim(), model: model.trim() }),
        });
        var d = await r.json();
        if (!d || d.error) { toast("修改失败: " + ((d && d.error) || "无响应"), true); return; }
        toast("已修改 " + pid);
        var fresh = await (await fetch("/api/agent/config")).json();
        if (fresh && !fresh._error) { CONFIG = fresh; render(); }
      } catch (e) {
        toast("修改请求失败: " + String(e), true);
      }
    }, btn);
  }

  async function deleteProvider(row) {
    var pid = row.dataset.providerId;
    if (!pid) return;
    // 内置 provider 不可删(后端也会拒绝,但前端先判断)
    var found = findProviderInConfig(pid);
    if (found && found.builtin) { toast("内置 provider 不可删除", true); return; }
    if (!confirm("删除自定义 provider \"" + pid + "\"？此操作不可撤销。")) return;

    try {
      var r = await (await fetch("/api/agent/providers?id=" + encodeURIComponent(pid), { method: "DELETE" })).json();
      if (r && r.error) { toast("删除失败: " + r.error, true); return; }
      if (r && r.ok) {
        toast("已删除 " + pid);
        var fresh = await (await fetch("/api/agent/config")).json();
        if (fresh && !fresh._error) { CONFIG = fresh; render(); }
      }
    } catch (e) {
      toast("删除请求失败: " + String(e), true);
    }
  }

  /* ═══════════ 绑定所有事件 ═══════════ */

  function bindAll() {
    bindSaveButtons();
    bindTestButtons();
    bindProviderChange();
    bindAddForm();
    bindProviderManageButtons();
  }

  /* ═══════════ 小工具 ═══════════ */

  function cardHead(name, role, ok, meta) {
    var dotClass = ok ? "on" : "off";
    return '<div class="cfg-head">' +
      '<span class="cfg-name">' + esc(name) + '</span>' +
      '<span class="cfg-role">' + esc(role) + '</span>' +
      (meta ? '<span class="cfg-meta">' + esc(meta) + '</span>' : '') +
      '<span class="cfg-dot ' + dotClass + '"></span>' +
    '</div>';
  }

  function findKeyInfo(keys, providerId) {
    if (!keys || !keys.length) return null;
    // 精确匹配(含自定义 provider:后端 _api_agent_config 已在 agent_keys 返回)
    var m = keys.find(function (k) { return k.id === providerId; });
    if (m) return m;
    // 兼容别名
    if (providerId === "openai_compat" || providerId === "qwen" || providerId === "deepseek") {
      return { key_env: "DEEPSEEK_API_KEY", key_masked: null, key_present: false };
    }
    return null;
  }

  /* ═══════════ 暴露 ═══════════ */

  return { onShow: onShow };

})();
