"use strict";

// 当前 transcript 状态
let CUR = null;            // {path, session_id, turns, ...}
const SEL = new Set();     // 选中的回合 idx

const $ = (s) => document.querySelector(s);

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1800);
}

function fmtSize(n) { return n < 1024 ? n + "B" : (n / 1024 | 0) + "KB"; }
function fmtTime(mtime) {
  const d = new Date(mtime * 1000);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit" });
}

async function loadList() {
  const r = await fetch("/api/transcripts");
  const d = await r.json();
  const box = $("#t-items");
  box.innerHTML = "";
  if (d.hidden_empty) {
    const note = document.createElement("div");
    note.className = "list-note";
    note.textContent = `已隐藏 ${d.hidden_empty} 个空会话(/clear 空壳等)`;
    box.appendChild(note);
  }
  d.transcripts.forEach((t) => {
    const el = document.createElement("div");
    el.className = "t-item";
    el.dataset.path = t.path;
    el.innerHTML =
      `<div><span class="sid">${t.session_id.slice(0, 8)}</span>` +
      (t.maybe_writing ? `<span class="badge">正在写入</span>` : "") + `</div>` +
      `<div class="meta">${fmtTime(t.mtime)} · ${t.turn_count}回合 · ${fmtSize(t.size)}</div>` +
      `<div class="meta">${t.cwd || ""}</div>`;
    el.onclick = () => loadTranscript(t.path, el);
    box.appendChild(el);
  });
}

async function loadTranscript(path, el) {
  document.querySelectorAll(".t-item.active").forEach((e) => e.classList.remove("active"));
  if (el) el.classList.add("active");
  SEL.clear();
  $("#hint").style.display = "none";
  const r = await fetch("/api/transcript?path=" + encodeURIComponent(path));
  const d = await r.json();
  if (d.error) { toast(d.error); return; }
  CUR = d;
  render();
}

function render() {
  const d = CUR;
  $("#main-title").textContent = d.session_id.slice(0, 8) +
    (d.maybe_writing ? "  ⚠ 可能正在写入" : "");
  const box = $("#turns");
  box.innerHTML = "";
  d.turns.forEach((t) => {
    const el = document.createElement("div");
    el.className = "turn" + (t.processed ? " done" : "") +
                   (SEL.has(t.idx) ? " sel" : "");
    el.dataset.idx = t.idx;
    let html = "";
    if (t.processed) html += `<span class="done-tag">✓ 已处理</span>`;
    html += `<div class="tmeta">回合 ${t.idx} · ${t.msg_count} msg</div>`;
    if (t.human_text)
      html += `<div><span class="who me">[我]</span> <span class="txt">${esc(t.human_text)}</span></div>`;
    if (t.assistant_text)
      html += `<div><span class="who claude">[Claude]</span> <span class="txt">${esc(t.assistant_text)}</span></div>`;
    el.innerHTML = html;
    el.onclick = () => toggle(t.idx, el);
    box.appendChild(el);
  });
  updateBar();
}

function esc(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function toggle(idx, el) {
  if (SEL.has(idx)) { SEL.delete(idx); el.classList.remove("sel"); }
  else { SEL.add(idx); el.classList.add("sel"); }
  updateBar();
}

function updateBar() {
  const n = SEL.size;
  $("#mark").disabled = n === 0;
  $("#sel-info").textContent = n === 0 ? "未选" :
    `已选 ${n} 回合(${[...SEL].sort((a, b) => a - b).join(",")})`;
}

async function markSelected() {
  if (!CUR || SEL.size === 0) return;
  const r = await fetch("/api/select", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: CUR.path, session_id: CUR.session_id,
      turn_idxs: [...SEL] }),
  });
  const d = await r.json();
  if (d.error) { toast("失败: " + d.error); return; }
  toast(`已登记 ${d.turns.length} 回合 / ${d.covered} 条消息`);
  SEL.clear();
  await loadTranscript(CUR.path, document.querySelector(".t-item.active"));
}

$("#mark").onclick = markSelected;
loadList();
