"use strict";

// ---- 状态 ----
let CUR = null;            // {path, session_id, turns, ...}
let SEGS = [];            // [{seg_id,start_turn,end_turn,tag,cut_reason,short,deletions,origin}]
let PROVIDERS = [];
let CHUNK_MODEL = "sonnet";
const SEL = new Set();     // 选中回合 idx(建段 / 标已处理共用)

// 块 B:子阶段 + 候选区 + 处理锁
let STAGE = "chunk";       // chunk(切段) | triage(待整理)
let LOCK = null;           // 处理锁:正在切段的 path;非空时其他条目禁点(防误触)
let SEG_DIRTY = false;     // 当前切段面板有未保存的本地修改
let TLIST = [];            // 当前 transcript 列表(渲染用,避免勾选就重新拉网络)
let HIDDEN_EMPTY = 0;
const CAND = new Set();    // 候选篮子:勾选的 transcript path
const EDITED = new Set();  // 本次会话点过/编辑过的 path(刚编辑 flag)
const TMAP = new Map();    // path -> transcript info(候选/蒸馏显示 sid/cwd)
const SEG_PICK = new Set(); // 切段屏:勾选的段下标(批量删段用)
let SORT_MODE = "touched"; // jsonl 列表排序:touched(动过的沉底) | time(纯 mtime)
let SORT_DIR = "desc";     // 时间方向:desc(新→旧,默认) | asc(旧→新)
let TQUERY = "";           // 切段区 grep 关键词(空=全量;后端对原始 jsonl 匹配)
const TOUCHED = new Set(); // 磁盘上动过的 path(有 chunks/staging),后端 loadList 时填,决定沉底

// 块 C:待整理(蒸馏)三栏 —— 左 session 列表 / 中 条目编辑 / 右 选项
let TRIS = [];             // /api/staging/all 的 sessions(扫磁盘,与候选区无关)
let TRI_CUR = null;        // 当前选中 session_id
const SELT = new Set();    // 待整理选中项 key(session_id,kind,id) kind∈seg|ep
const UNDO = {};           // 每条 episode 的本地撤销栈:{epKey:[fieldsSnapshot,...]}
const TPREVIEW = new Map(); // path -> transcript turns 缓存(段预览用,同 path 只抓一次)

const PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#f7768e",
                 "#7dcfff", "#e6a3c9", "#73daca", "#ff9e64", "#c0caf5"];

// ---- 本地游标持久化:刷新/换页不丢「在看哪几条」+ 刚编辑标记 + 当前子屏 ----
// 真正的数据(段/staging/碎片)都在磁盘上;这里只记浏览器侧的视图游标。
const LS_KEY = "memsys_ingest_v1";
function saveState() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(
      { cand: [...CAND], edited: [...EDITED], stage: STAGE, tri_cur: TRI_CUR,
        sort: SORT_MODE, dir: SORT_DIR }));
  } catch (e) { /* 隐私模式/超额:静默,内存态仍可用 */ }
}
function restoreState() {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    (s.cand || []).forEach((p) => CAND.add(p));
    (s.edited || []).forEach((p) => EDITED.add(p));
    if (s.stage === "chunk" || s.stage === "triage") STAGE = s.stage;
    if (s.tri_cur) TRI_CUR = s.tri_cur;
    if (s.sort === "touched" || s.sort === "time") SORT_MODE = s.sort;
    if (s.dir === "desc" || s.dir === "asc") SORT_DIR = s.dir;
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
