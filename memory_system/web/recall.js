"use strict";

/* recall.js —— 召回屏(S6 三路检索 + 可选重构)。
 *
 * API: POST /api/recall {mode, query, context?, touch, reconstruct, since?, until?, user_query?}
 *      → {structured, reconstruction, error}
 * user_query = 模拟当轮 query(可选,只喂重构):检索词和用户真实问话经常不同,
 * 重构 prompt 以后者为核心取舍;detail 不接重构,该框隐藏。空 = 沿用检索词。
 *
 * 左栏 = 结构化(episode 三槽卡片 / detail 命中开窗 / concept node+episodes,miss 显建议);
 * 右栏 = 重构(仅 episode/concept 可点,原样重发上次查询参数 + reconstruct:true)。
 * 状态挂 ST.rcReq(最近一次成功查询的请求体)/ ST.rcStructured(最近一次 structured,便于审计)。
 * 脚本置于 body 末尾加载,DOM 已就位,顶层直接绑定(照 view.js/console.js 惯例,不等 DOMContentLoaded)。
 */

var modeSel = document.getElementById("rc-mode");
var queryInp = document.getElementById("rc-query");
var contextInp = document.getElementById("rc-context");
var uqueryInp = document.getElementById("rc-uquery");
var sinceInp = document.getElementById("rc-since");
var untilInp = document.getElementById("rc-until");
var touchChk = document.getElementById("rc-touch");
var runBtn = document.getElementById("rc-run");
var reconBtn = document.getElementById("rc-recon");
var structEl = document.getElementById("rc-structured");
var reconErrEl = document.getElementById("rc-recon-err");
var reconTextEl = document.getElementById("rc-recon-text");

function rcFmtNum(n) {
  return typeof n === "number" ? n.toFixed(3) : esc(String(n == null ? "" : n));
}

function rcHasHits(mode, s) {
  if (!s) return false;
  if (mode === "episode") return !!(s.slots && s.slots.primary && s.slots.primary.length);
  if (mode === "concept") return !!(s.episodes && s.episodes.length);
  return false;
}

/* ═══════════ 渲染:episode 卡片(主/同源/联想三槽共用)═══════════ */

function rcEpisodeCard(e, extraClass, viaLine) {
  var html = '<div class="rc-card' + (extraClass ? " " + extraClass : "") + '">';
  html += '<div class="rc-card-hd"><span class="rc-pid">' + esc(e.public_id) + '</span>';
  if (e.created_at) html += '<span class="rc-date">' + esc(e.created_at) + '</span>';
  if (e.salience_tier != null) {
    html += '<span class="tier t' + esc(String(e.salience_tier)) + '">T' + esc(String(e.salience_tier)) + '</span>';
  }
  if (e.score != null) html += '<span class="rc-score">score=' + rcFmtNum(e.score) + '</span>';
  if (e.activation != null) html += '<span class="rc-score">activation=' + rcFmtNum(e.activation) + '</span>';
  html += '</div>';
  if (e.overview) html += '<div class="rc-ov">' + esc(e.overview) + '</div>';
  if (e.summary) html += '<div class="rc-sm">' + esc(e.summary) + '</div>';
  if (viaLine) html += '<div class="rc-via">via ' + esc(viaLine) + '</div>';
  if (e.highlights && e.highlights.length) {
    html += '<div class="rc-hls">' + e.highlights.map(function (h) {
      return '<span class="rc-hl">「' + esc(h.text || "") + '」' +
        (h.tag ? '<i>' + esc(h.tag) + '</i>' : '') + '</span>';
    }).join("") + '</div>';
  }
  html += '</div>';
  return html;
}

function rcRenderEpisode(s) {
  var html = '';
  html += '<div class="rc-frame">frame_nodes: ' +
    (s.frame_nodes && s.frame_nodes.length ? s.frame_nodes.map(esc).join("、") : "(无)") + '</div>';
  html += '<div class="rc-slot-lbl">主槽 primary</div>';
  if (!s.slots.primary.length) {
    html += '<div class="rc-hint">未命中「' + esc(s.query) + '」。库为空或候选全被过滤,可换个说法再试。</div>';
  }
  s.slots.primary.forEach(function (e) { html += rcEpisodeCard(e); });
  if (s.slots.same_source.length) {
    html += '<div class="rc-slot-lbl">同源 same_source</div>';
    s.slots.same_source.forEach(function (e) { html += rcEpisodeCard(e, "rc-card-lite"); });
  }
  if (s.slots.associative.length) {
    html += '<div class="rc-slot-lbl">联想 associative</div>';
    s.slots.associative.forEach(function (e) {
      html += rcEpisodeCard(e, "rc-card-lite", (e.via_nodes || []).join("、"));
    });
  }
  return html;
}

function rcRenderDetail(s) {
  if (!s.hits.length) {
    return '<div class="rc-hint">未命中「' + esc(s.query) + '」。' +
      'FTS trigram 对少于 3 个字符的中文词不可靠,换更长/更具体的词再试。</div>';
  }
  var html = '';
  s.hits.forEach(function (h) {
    html += '<div class="rc-card">';
    html += '<div class="rc-card-hd"><span class="rc-pid">' + esc(h.public_id) + '</span>';
    if (h.created_at) html += '<span class="rc-date">' + esc(h.created_at) + '</span>';
    if (h.salience_tier != null) {
      html += '<span class="tier t' + esc(String(h.salience_tier)) + '">T' + esc(String(h.salience_tier)) + '</span>';
    }
    html += '</div>';
    html += '<div class="rc-window">' + esc(h.window) + '</div>';
    html += '</div>';
  });
  return html;
}

function rcRenderConcept(s) {
  var html = '';
  html += '<div class="rc-frame">概念「' + esc(s.node) + '」  挂载 ' + (s.episodes || []).length + ' 条情景</div>';
  if (s.alias_bridge) html += '<div class="rc-bridge">' + esc(s.alias_bridge) + '</div>';
  if (s.suggestions && s.suggestions.length) {
    html += '<div class="alert warn">没有叫「' + esc(s.node) + '」的概念(label / 别名都查过)。也许你想找:' +
      s.suggestions.map(esc).join("、") + '</div>';
  } else if (!s.episodes.length) {
    html += '<div class="rc-hint">概念下没有挂载中的 active 情景,无可重构。</div>';
  }
  (s.episodes || []).forEach(function (e) { html += rcEpisodeCard(e); });
  return html;
}

function rcRenderStructured(s) {
  if (!s) return '<div class="rc-hint">选模式、输入检索词,回车或点「查询」。</div>';
  if (s.mode === "episode") return rcRenderEpisode(s);
  if (s.mode === "detail") return rcRenderDetail(s);
  if (s.mode === "concept") return rcRenderConcept(s);
  return '<div class="rc-hint">未知结果形状。</div>';
}

/* ═══════════ 顶栏:mode 切换显隐附属输入 ═══════════ */

function rcOnModeChange() {
  var mode = modeSel.value;
  contextInp.style.display = mode === "concept" ? "" : "none";
  uqueryInp.style.display = mode === "detail" ? "none" : "";
  sinceInp.style.display = mode === "detail" ? "" : "none";
  untilInp.style.display = mode === "detail" ? "" : "none";
}

function rcUpdateReconBtn() {
  var mode = ST.rcReq ? ST.rcReq.mode : "";
  reconBtn.disabled = !(mode === "episode" || mode === "concept") || !rcHasHits(mode, ST.rcStructured);
}

/* ═══════════ 查询 / 重构 ═══════════ */

async function rcRunQuery() {
  var mode = modeSel.value;
  var query = queryInp.value.trim();
  if (!query) { toast("检索词不能为空", true); return; }
  var body = { mode: mode, query: query, touch: !!touchChk.checked, reconstruct: false };
  if (mode === "concept" && contextInp.value.trim()) body.context = contextInp.value.trim();
  if (mode !== "detail" && uqueryInp.value.trim()) body.user_query = uqueryInp.value.trim();
  if (mode === "detail") {
    if (sinceInp.value.trim()) body.since = sinceInp.value.trim();
    if (untilInp.value.trim()) body.until = untilInp.value.trim();
  }
  await once("rc-run", async function () {
    var r = await postJSON("/api/recall", body);
    if (r.structured == null) {
      structEl.innerHTML = '<div class="alert err">' + esc(r.error || "查询失败,无响应") + '</div>';
      toast(r.error || "查询失败", true);
      return;
    }
    ST.rcReq = body;
    ST.rcStructured = r.structured;
    structEl.innerHTML = rcRenderStructured(r.structured);
    reconTextEl.textContent = "查询后点「重构」,由 recall agent 生成自然语言回忆(detail 不接重构)。";
    reconTextEl.className = "rc-hint";
    reconErrEl.style.display = "none";
    rcUpdateReconBtn();
    if (r.error) toast(r.error, true);
  }, runBtn);
}

async function rcRunReconstruct() {
  if (!ST.rcReq || !ST.rcStructured) { toast("先查询一次再重构", true); return; }
  var mode = ST.rcReq.mode;
  if (mode !== "episode" && mode !== "concept") { toast("细节检索不接重构", true); return; }
  if (!rcHasHits(mode, ST.rcStructured)) { toast("无命中结果,无可重构", true); return; }
  var body = Object.assign({}, ST.rcReq, { reconstruct: true });
  // 模拟当轮 query 取输入框当前值:查询一次后改它、直接点「重构」即生效(调 prompt 的迭代路径)。
  var uq = uqueryInp.value.trim();
  if (uq) body.user_query = uq; else delete body.user_query;
  await once("rc-recon", async function () {
    var r = await postJSON("/api/recall", body);
    if (r.structured != null) {
      ST.rcStructured = r.structured;
      structEl.innerHTML = rcRenderStructured(r.structured);
    }
    if (r.reconstruction) {
      reconTextEl.textContent = r.reconstruction;
      reconTextEl.className = "rc-recon-body";
    } else {
      reconTextEl.textContent = "";
      reconTextEl.className = "rc-hint";
    }
    if (r.error) {
      reconErrEl.textContent = r.error;
      reconErrEl.style.display = "";
      if (!r.reconstruction) toast(r.error, true);
    } else {
      reconErrEl.style.display = "none";
    }
    rcUpdateReconBtn();
  }, reconBtn);
}

/* ═══════════ 绑定 ═══════════ */

if (modeSel && runBtn) {
  modeSel.addEventListener("change", rcOnModeChange);
  rcOnModeChange();
  runBtn.addEventListener("click", rcRunQuery);
  queryInp.addEventListener("keydown", function (ev) { if (ev.key === "Enter") rcRunQuery(); });
  contextInp.addEventListener("keydown", function (ev) { if (ev.key === "Enter") rcRunQuery(); });
  uqueryInp.addEventListener("keydown", function (ev) { if (ev.key === "Enter") rcRunQuery(); });
  reconBtn.addEventListener("click", rcRunReconstruct);
}
