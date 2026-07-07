"use strict";

// ---- 状态(P2:收进单一 ST 命名空间)----
// 仍走隐式全局作用域跨 8 个 JS 共享(无 ES module),但所有可变模块态都挂在 ST 这一个
// 对象上:可检索、可审计、杜绝裸全局命名碰撞。工具函数与不变配置(PALETTE/LS_KEY)仍独立。
const ST = {
  // 块 A:切段核心
  cur: null,             // {path, session_id, turns, ...}
  segs: [],              // [{seg_id,start_turn,end_turn,tag,cut_reason,short,deletions,origin}]
  providers: [],
  chunkModel: "sonnet",
  sel: new Set(),        // 选中回合 idx(建段 / 标已处理共用)

  // 块 B:子阶段 + 候选区 + 处理锁
  stage: "chunk",        // chunk(切段) | triage(待整理)
  lock: null,            // 处理锁:正在切段的 path;非空时其他条目禁点(防误触)
  segDirty: false,       // 当前切段面板有未保存的本地修改
  tlist: [],             // 当前 transcript 列表(渲染用,避免勾选就重新拉网络)
  hiddenEmpty: 0,
  cand: new Set(),       // 候选篮子:勾选的 transcript path
  edited: new Set(),     // 本次会话点过/编辑过的 path(刚编辑 flag)
  tmap: new Map(),       // path -> transcript info(候选/蒸馏显示 sid/cwd)
  segPick: new Set(),    // 切段屏:勾选的段下标(批量删段用)
  loadSeq: 0,            // loadTranscript 请求序号:连点切换时只认最新一次(后发者胜)
  sortMode: "touched",   // jsonl 列表排序:touched(动过的沉底) | time(纯 mtime)
  sortDir: "desc",       // 时间方向:desc(新→旧,默认) | asc(旧→新)
  tquery: "",            // 切段区 grep 关键词(空=全量;后端对原始 jsonl 匹配)
  touched: new Set(),    // 磁盘上动过的 path(有 chunks/staging),后端 loadList 时填,决定沉底

  // 块 C:待整理(蒸馏)三栏 —— 左 session 列表 / 中 条目编辑 / 右 选项
  tris: [],              // /api/staging/all 的 sessions(扫磁盘,与候选区无关)
  triCur: null,          // 当前选中 session_id
  selt: new Set(),       // 待整理选中项 key(session_id,kind,id) kind∈seg|ep
  undo: {},              // 每条 episode 的本地撤销栈:{epKey:[fieldsSnapshot,...]}
  tpreview: new Map(),   // path -> transcript turns 缓存(段预览用,同 path 只抓一次)

  // 块 D:召回屏(recall.js)
  rcReq: null,           // 最近一次成功查询的参数(mode/query/context/since/until/touch),「重构」原样重发
  rcStructured: null,    // 最近一次 /api/recall 的 structured(渲染态,便于审计)
};

const PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#f7768e",
                 "#7dcfff", "#e6a3c9", "#73daca", "#ff9e64", "#c0caf5"];

// ---- 本地游标持久化:刷新/换页不丢「在看哪几条」+ 刚编辑标记 + 当前子屏 ----
// 真正的数据(段/staging/碎片)都在磁盘上;这里只记浏览器侧的视图游标。
const LS_KEY = "memsys_ingest_v1";
function saveState() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(
      { cand: [...ST.cand], edited: [...ST.edited], stage: ST.stage, tri_cur: ST.triCur,
        sort: ST.sortMode, dir: ST.sortDir }));
  } catch (e) { /* 隐私模式/超额:静默,内存态仍可用 */ }
}
function restoreState() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    (s.cand || []).forEach((p) => ST.cand.add(p));
    (s.edited || []).forEach((p) => ST.edited.add(p));
    if (s.stage === "chunk" || s.stage === "triage") ST.stage = s.stage;
    if (s.tri_cur) ST.triCur = s.tri_cur;
    if (s.sort === "touched" || s.sort === "time") ST.sortMode = s.sort;
    if (s.dir === "desc" || s.dir === "asc") ST.sortDir = s.dir;
  } catch (e) { /* 坏数据:忽略,从空起 */ }
}

const $ = (s) => document.querySelector(s);

function toast(msg, bad) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show" + (bad ? " bad" : "");
  setTimeout(() => el.classList.remove("show"), 2200);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function clone(v) {
  return JSON.parse(JSON.stringify(v ?? null));
}
function fmtSize(n) { return n < 1024 ? n + "B" : (n / 1024 | 0) + "KB"; }
function fmtTime(mtime) {
  const d = new Date(mtime * 1000);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit" });
}
