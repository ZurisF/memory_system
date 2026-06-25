/* view.js —— 三视图导航(写入|查看|控制台,切换=显隐不销毁) + 查看侧 galaxy。
 *
 * galaxy 移植自 front_references/browse.html,改两点:
 *   1) 容器内定位(坐标用 canvas.getBoundingClientRect 换算,不用 window/fixed);
 *   2) mock → 真数据:首次切到「查看」拉 /api/memories;点节点拉 /api/node、点条目拉 /api/memory。
 * node↔node 边 = 后端按共享情景算的共现边(edges[].via 即解释)。本轮只读,不做编辑写回。
 */
(function () {

/* ═══════════ 三视图导航(显隐冻结) ═══════════ */

var VIEWS = { ingest: "view-ingest", graph: "view-graph", console: "view-console" };
var curView = "ingest";

function switchView(name) {
  if (!VIEWS[name] || name === curView) return;
  curView = name;
  Object.keys(VIEWS).forEach(function (k) {
    document.getElementById(VIEWS[k]).hidden = (k !== name);
  });
  document.querySelectorAll("#topnav .nav-btn").forEach(function (b) {
    b.classList.toggle("active", b.dataset.view === name);
  });
  if (name === "graph") Graph.onShow(); else Graph.onHide();
}

document.querySelectorAll("#topnav .nav-btn").forEach(function (b) {
  b.addEventListener("click", function () { switchView(b.dataset.view); });
});

/* ═══════════ galaxy ═══════════ */

var Graph = (function () {

  var TYPE_COLORS = {
    project: [104, 136, 176], module: [92, 153, 104], concept: [196, 160, 80],
    service: [136, 104, 168], config: [120, 126, 136],
  };
  var DEFAULT_COLOR = [140, 144, 152];
  var REPULSION = 3200, SPRING_K = 0.006, SPRING_LEN = 120, DAMPING = 0.88,
      CENTER_PULL = 0.001, EP_ORBIT = 65;

  var canvas, ctx, dpr = window.devicePixelRatio || 1, W = 1, H = 1;
  var nodes = [], nodeMap = {}, edges = [], episodes = [];
  var cam = { x: 0, y: 0, z: 1, tx: 0, ty: 0, tz: 1 };
  var drag = null, hover = null, focused = null, selectedEp = null;
  var simRunning = true, simTick = 0, t0 = 0;
  var inited = false, loading = false, running = false, rafId = 0;

  function el(id) { return document.getElementById(id); }

  /* ---- 尺寸 / 坐标 ---- */
  function resize() {
    var box = el("view-graph");
    W = box.clientWidth || 1; H = box.clientHeight || 1;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
  }
  function localXY(e) {
    var r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  function w2s(wx, wy) { return { x: (wx - cam.x) * cam.z + W / 2, y: (wy - cam.y) * cam.z + H / 2 }; }
  function s2w(sx, sy) { return { x: (sx - W / 2) / cam.z + cam.x, y: (sy - H / 2) / cam.z + cam.y }; }

  /* ---- 数据装载 ---- */
  function buildData(data) {
    nodes = []; nodeMap = {}; edges = []; episodes = [];
    var raw = data.nodes || [];
    raw.forEach(function (n, i) {
      var a = (i / Math.max(1, raw.length)) * Math.PI * 2;
      var r = 80 + Math.random() * 120;
      var node = {
        id: n.label, type: n.type, aliases: n.aliases || [], epCount: n.episode_count || 0,
        x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0, seed: Math.random(),
      };
      nodes.push(node); nodeMap[n.label] = node;
    });
    (data.edges || []).forEach(function (e) {
      if (nodeMap[e.a] && nodeMap[e.b]) edges.push({ a: nodeMap[e.a], b: nodeMap[e.b], via: e.via || [] });
    });
    // 每条 episode 挂到它的每个 node(共享情景:同一条绕多个 node)
    (data.episodes || []).forEach(function (ep) {
      (ep.nodes || []).forEach(function (label) {
        var n = nodeMap[label];
        if (n) episodes.push({
          id: ep.public_id, node: n, tier: ep.salience_tier || 1,
          overview: ep.overview || "", angle: 0, x: 0, y: 0, vis: 0,
        });
      });
    });
    el("g-empty").hidden = nodes.length > 0;
    simRunning = true; simTick = 0;
  }

  function load() {
    loading = true;
    fetch("/api/memories").then(function (r) { return r.json(); }).then(function (d) {
      loading = false;
      if (d.error) { (window.toast || function () {})("加载记忆失败:" + d.error, true); return; }
      buildData(d);
    }).catch(function (err) {
      loading = false; (window.toast || function () {})("加载记忆失败:" + err, true);
    });
  }

  /* ---- 力导向 ---- */
  function simulate() {
    if (!simRunning) return;
    var i, j, n1, n2, dx, dy, d, f, e;
    for (i = 0; i < nodes.length; i++) {
      n1 = nodes[i];
      for (j = i + 1; j < nodes.length; j++) {
        n2 = nodes[j];
        dx = n2.x - n1.x; dy = n2.y - n1.y; d = Math.sqrt(dx * dx + dy * dy) + 1;
        f = REPULSION / (d * d);
        n1.vx -= dx / d * f; n1.vy -= dy / d * f; n2.vx += dx / d * f; n2.vy += dy / d * f;
      }
    }
    for (i = 0; i < edges.length; i++) {
      e = edges[i]; dx = e.b.x - e.a.x; dy = e.b.y - e.a.y; d = Math.sqrt(dx * dx + dy * dy) + 1;
      f = SPRING_K * (d - SPRING_LEN);
      e.a.vx += dx / d * f; e.a.vy += dy / d * f; e.b.vx -= dx / d * f; e.b.vy -= dy / d * f;
    }
    var totalV = 0;
    for (i = 0; i < nodes.length; i++) {
      n1 = nodes[i];
      n1.vx -= n1.x * CENTER_PULL; n1.vy -= n1.y * CENTER_PULL;
      n1.vx *= DAMPING; n1.vy *= DAMPING; n1.x += n1.vx; n1.y += n1.vy;
      totalV += Math.abs(n1.vx) + Math.abs(n1.vy);
    }
    simTick++;
    if (simTick > 300 && totalV < 0.5) simRunning = false;
  }

  function lerp(a, b, t) { return a + (b - a) * t; }
  function updateCamera() {
    cam.x = lerp(cam.x, cam.tx, .08); cam.y = lerp(cam.y, cam.ty, .08); cam.z = lerp(cam.z, cam.tz, .08);
  }

  /* ---- 命中 ---- */
  function nodeRadius(n) { return 3 + Math.sqrt(n.epCount) * 2.2; }
  function hitNode(sx, sy) {
    var best = null, bestD = Infinity;
    for (var i = 0; i < nodes.length; i++) {
      var p = w2s(nodes[i].x, nodes[i].y), r = nodeRadius(nodes[i]) * cam.z + 8;
      var d = Math.hypot(p.x - sx, p.y - sy);
      if (d < r && d < bestD) { best = nodes[i]; bestD = d; }
    }
    return best;
  }
  function hitEpisode(sx, sy) {
    if (!focused) return null;
    for (var i = 0; i < episodes.length; i++) {
      var ep = episodes[i];
      if (ep.node !== focused || ep.vis < .3) continue;
      var p = w2s(ep.x, ep.y);
      if (Math.hypot(p.x - sx, p.y - sy) < 12) return ep;
    }
    return null;
  }

  /* ---- 渲染 ---- */
  function rgb(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }
  function nodeColor(n) { return TYPE_COLORS[n.type] || DEFAULT_COLOR; }

  function render() {
    var t = (performance.now() - t0) * 0.001;
    ctx.save(); ctx.scale(dpr, dpr);
    ctx.fillStyle = "#0c0d0f"; ctx.fillRect(0, 0, W, H);
    var z = cam.z, i, p, q, a, e, n, c, r, g, br;

    for (i = 0; i < edges.length; i++) {
      e = edges[i]; p = w2s(e.a.x, e.a.y); q = w2s(e.b.x, e.b.y);
      var ea = 0.035;
      if (hover && (e.a === hover || e.b === hover)) ea = 0.16;
      else if (focused && (e.a === focused || e.b === focused)) ea = 0.10;
      else if (focused) ea = 0.015;
      ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y);
      ctx.strokeStyle = "rgba(255,255,255," + ea + ")"; ctx.lineWidth = .5; ctx.stroke();
    }

    for (i = 0; i < nodes.length; i++) {
      n = nodes[i]; c = nodeColor(n); p = w2s(n.x, n.y); r = nodeRadius(n);
      br = .82 + .18 * Math.sin(t * .5 + n.seed * 6.28);
      var dimmed = focused && n !== focused, na = dimmed ? .15 : br;
      if (n === hover) na = 1;
      var gr = r * 3 * z;
      if (gr > 2) {
        g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, gr);
        g.addColorStop(0, rgb(c, .25 * na)); g.addColorStop(.5, rgb(c, .06 * na)); g.addColorStop(1, rgb(c, 0));
        ctx.fillStyle = g; ctx.fillRect(p.x - gr, p.y - gr, gr * 2, gr * 2);
      }
      ctx.beginPath(); ctx.arc(p.x, p.y, r * z, 0, Math.PI * 2); ctx.fillStyle = rgb(c, .7 * na); ctx.fill();
      if (r * z > 2) {
        ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(1, r * z * .35), 0, Math.PI * 2);
        ctx.fillStyle = rgb([255, 255, 255], .5 * na); ctx.fill();
      }
      var la = 0;
      if (z > 1.2) la = Math.min(1, (z - 1.2) / 1.5);
      if (dimmed) la *= .2;
      if (n === hover) la = 1;
      if (la > .02) {
        ctx.font = "500 11px -apple-system,system-ui,sans-serif";
        ctx.fillStyle = "rgba(255,255,255," + la * .72 + ")"; ctx.textAlign = "center";
        ctx.fillText(n.id, p.x, p.y + r * z + 14);
      }
    }

    if (focused) {
      var feps = episodes.filter(function (ep) { return ep.node === focused; });
      for (i = 0; i < feps.length; i++) {
        var ep = feps[i];
        var ta = (i / feps.length) * Math.PI * 2 - Math.PI / 2;
        ep.angle = lerp(ep.angle, ta, .06);
        ep.x = focused.x + Math.cos(ep.angle) * EP_ORBIT;
        ep.y = focused.y + Math.sin(ep.angle) * EP_ORBIT;
        ep.vis = lerp(ep.vis, 1, .06);
        if (ep.vis < .01) continue;
        p = w2s(ep.x, ep.y); var epC = nodeColor(focused); a = ep.vis;
        var eg = 18 * z;
        if (eg > 3) {
          g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, eg);
          g.addColorStop(0, rgb(epC, .15 * a)); g.addColorStop(1, rgb(epC, 0));
          ctx.fillStyle = g; ctx.fillRect(p.x - eg, p.y - eg, eg * 2, eg * 2);
        }
        ctx.beginPath(); ctx.arc(p.x, p.y, 2.5 * z, 0, Math.PI * 2); ctx.fillStyle = rgb(epC, .6 * a); ctx.fill();
        if (ep === selectedEp) { ctx.strokeStyle = rgb([255, 255, 255], .4); ctx.lineWidth = 1; ctx.stroke(); }
        if (z > 1.5 && a > .3) {
          var ela = Math.min(1, (z - 1.5) / 2) * a;
          ctx.font = "11px -apple-system,system-ui,sans-serif";
          ctx.fillStyle = "rgba(200,208,216," + ela * .6 + ")"; ctx.textAlign = "center";
          var txt = ep.overview.length > 16 ? ep.overview.slice(0, 16) + "…" : ep.overview;
          ctx.fillText(txt, p.x, p.y + 2.5 * z + 13);
        }
      }
    }
    episodes.forEach(function (ep) { if (!focused || ep.node !== focused) ep.vis = lerp(ep.vis, 0, .08); });

    ctx.restore();
    var mode = z < 1.8 ? "galaxy" : z < 4 ? "solar" : "detail";
    el("g-zoom").innerHTML = '<span class="lv">' + z.toFixed(1) + "x</span> · " + mode;
    el("g-back").classList.toggle("show", !!focused);
  }

  function loop() {
    if (!running) return;
    simulate(); updateCamera(); render();
    rafId = requestAnimationFrame(loop);
  }

  function unfocus() { focused = null; selectedEp = null; cam.tx = 0; cam.ty = 0; cam.tz = 1; }

  /* ---- 面板 ---- */
  function esc(s) { return (window.esc ? window.esc(s) : String(s == null ? "" : s)); }
  function tierClass(t) { return t === 3 ? "gp-t3" : t === 2 ? "gp-t2" : "gp-t1"; }
  function openPanel(html) { el("gp-content").innerHTML = html; el("g-panel").classList.add("open"); }
  function closePanel() { el("g-panel").classList.remove("open"); }

  function showNodePanel(label) {
    var n = nodeMap[label]; if (!n) return;
    openPanel('<div class="gp-head"><div class="gp-label">' + esc(label) +
      '</div><div class="gp-type">加载中…</div></div>');
    fetch("/api/node?label=" + encodeURIComponent(label)).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { openPanel('<div class="gp-head"><div class="gp-label">' + esc(label) + '</div></div><div class="gp-body gp-val">' + esc(d.error) + '</div>'); return; }
      var h = '<div class="gp-head"><div class="gp-label">' + esc(d.label) +
        '</div><div class="gp-type">' + esc(d.type || "node") + '</div></div><div class="gp-body">';
      if (d.aliases && d.aliases.length) {
        h += '<div class="gp-lbl">别名</div><div class="gp-chips">';
        d.aliases.forEach(function (a) { h += '<span class="gp-chip">' + esc(a) + '</span>'; });
        h += '</div>';
      }
      h += '<div class="gp-lbl">挂载条目 (' + d.episodes.length + ')</div>';
      d.episodes.forEach(function (ep) {
        h += '<div class="gp-ep" data-ep="' + esc(ep.public_id) + '"><span class="gp-tier ' +
          tierClass(ep.salience_tier) + '">T' + ep.salience_tier + '</span><div class="id">' +
          esc(ep.public_id) + '</div><div class="ov">' + esc(ep.overview) + '</div></div>';
      });
      h += '<div class="gp-note">只读 · 编辑写回下一轮</div></div>';
      openPanel(h);
      el("gp-content").querySelectorAll(".gp-ep").forEach(function (card) {
        card.addEventListener("click", function () { showEpisodePanel(card.dataset.ep); });
      });
    });
  }

  function showEpisodePanel(pub) {
    openPanel('<div class="gp-head"><div class="gp-label">' + esc(pub) + '</div><div class="gp-type">加载中…</div></div>');
    fetch("/api/memory?public_id=" + encodeURIComponent(pub)).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { openPanel('<div class="gp-head"><div class="gp-label">' + esc(pub) + '</div></div><div class="gp-body gp-val">' + esc(d.error) + '</div>'); return; }
      var h = '<div class="gp-head"><div class="gp-label">' + esc(d.public_id) +
        ' <span class="gp-tier ' + tierClass(d.salience_tier) + '">T' + d.salience_tier + '</span></div>' +
        '<div class="gp-type">' + esc(d.status) + (d.source_session_id ? " · " + esc(d.source_session_id) : "") + '</div></div>';
      h += '<div class="gp-body">';
      h += '<div class="gp-lbl">Overview</div><div class="gp-val">' + esc(d.overview) + '</div>';
      h += '<div class="gp-lbl">Summary</div><div class="gp-val">' + esc(d.summary) + '</div>';
      if (d.highlights && d.highlights.length) {
        h += '<div class="gp-lbl">高光</div>';
        d.highlights.forEach(function (hl) {
          h += '<div class="gp-val">· ' + esc(hl.text) + (hl.tag ? ' <span class="gp-chip">' + esc(hl.tag) + '</span>' : "") + '</div>';
        });
      }
      h += '<div class="gp-lbl">原文 source_text</div><div class="gp-mono">' + esc(d.source_text) + '</div>';
      if (d.nodes && d.nodes.length) {
        h += '<div class="gp-lbl">所属 node</div><div class="gp-chips">';
        d.nodes.forEach(function (lb) { h += '<span class="gp-chip gp-node-link" data-node="' + esc(lb) + '">' + esc(lb) + '</span>'; });
        h += '</div>';
      }
      h += '<div class="gp-note">只读 · 编辑写回下一轮</div></div>';
      openPanel(h);
      el("gp-content").querySelectorAll(".gp-node-link").forEach(function (chip) {
        chip.style.cursor = "pointer";
        chip.addEventListener("click", function () {
          var n = nodeMap[chip.dataset.node];
          if (n) { focused = n; selectedEp = null; cam.tx = n.x; cam.ty = n.y; cam.tz = Math.max(cam.tz, 2.5); }
          showNodePanel(chip.dataset.node);
        });
      });
    });
  }

  /* ---- 交互绑定(一次)---- */
  function bind() {
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      var s = e.deltaY > 0 ? .88 : 1.14, nz = Math.max(.3, Math.min(12, cam.tz * s));
      var m = localXY(e), wp = s2w(m.x, m.y);
      cam.tz = nz; cam.tx = wp.x - (m.x - W / 2) / nz; cam.ty = wp.y - (m.y - H / 2) / nz;
    }, { passive: false });

    canvas.addEventListener("mousedown", function (e) {
      var m = localXY(e); drag = { sx: m.x, sy: m.y, cx: cam.tx, cy: cam.ty, moved: false };
    });
    canvas.addEventListener("mousemove", function (e) {
      var m = localXY(e);
      if (drag) {
        var dx = m.x - drag.sx, dy = m.y - drag.sy;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        cam.tx = drag.cx - dx / cam.z; cam.ty = drag.cy - dy / cam.z;
      }
      hover = hitNode(m.x, m.y);
      canvas.style.cursor = hover ? "pointer" : (drag ? "grabbing" : "grab");
    });
    window.addEventListener("mouseup", function (e) {
      if (drag && !drag.moved && curView === "graph") {
        var m = localXY(e), ep = hitEpisode(m.x, m.y);
        if (ep) { selectedEp = ep; showEpisodePanel(ep.id); }
        else {
          var n = hitNode(m.x, m.y);
          if (n) {
            if (focused !== n) { focused = n; selectedEp = null; cam.tx = n.x; cam.ty = n.y; cam.tz = Math.max(cam.tz, 2.5); }
            showNodePanel(n.id);
          } else { if (focused) unfocus(); closePanel(); }
        }
      }
      drag = null;
    });

    el("g-back").addEventListener("click", function () { unfocus(); closePanel(); });
    el("gp-close").addEventListener("click", closePanel);
    window.addEventListener("resize", function () { if (curView === "graph") resize(); });
  }

  /* ---- 生命周期 ---- */
  return {
    onShow: function () {
      if (!inited) {
        canvas = el("g-canvas"); ctx = canvas.getContext("2d");
        t0 = performance.now(); bind(); load(); inited = true;
        setTimeout(function () { var h = el("g-help"); if (h) h.style.opacity = "0"; }, 4000);
      }
      resize();
      if (!running) { running = true; rafId = requestAnimationFrame(loop); }
    },
    onHide: function () { running = false; if (rafId) cancelAnimationFrame(rafId); },
  };
})();

})();
