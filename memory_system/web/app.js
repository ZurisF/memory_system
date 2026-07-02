"use strict";

$("#run-chunk").onclick = runChunk;
$("#make-seg").onclick = makeSegFromSel;
$("#mark").onclick = markSelected;
$("#seg-all").onclick = toggleAllSegs;
$("#seg-del").onclick = () => once("seg-del", deletePickedSegs, $("#seg-del"));
$("#sort-toggle").onclick = () => {
  ST.sortMode = ST.sortMode === "touched" ? "time" : "touched";
  $("#sort-toggle").textContent = ST.sortMode === "touched" ? "动过的沉底" : "按时间";
  saveState();
  renderListItems();
};
$("#sort-dir").onclick = () => {
  ST.sortDir = ST.sortDir === "desc" ? "asc" : "desc";
  $("#sort-dir").textContent = ST.sortDir === "desc" ? "时间↓" : "时间↑";
  saveState();
  renderListItems();
};

// 切段区 grep:回车搜索;清空按钮复位为全量。搜索走后端(对原始 jsonl 匹配)。
$("#t-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const v = e.target.value.trim();
    $("#t-search-clear").style.display = v ? "" : "none";
    loadList(v);
  } else if (e.key === "Escape") {
    e.target.value = ""; $("#t-search-clear").style.display = "none"; loadList("");
  }
});
$("#t-search-clear").onclick = () => {
  $("#t-search").value = ""; $("#t-search-clear").style.display = "none"; loadList("");
};

// 导入:按钮触发隐藏的文件选择器,选完即上传刷新。
$("#import-btn").onclick = () => $("#import-file").click();
$("#import-file").onchange = (e) => {
  importFiles(e.target.files);
  e.target.value = "";  // 复位,允许再次选同名文件
};

// 块 B:确认分段(存段→待整理)、退出处理(解锁)、子阶段切换
$("#confirm-segs").onclick = () => once("confirm-segs", async () => {
  if (await saveSegs()) {       // saveSegs 已解锁/标记/纳入候选
    setStage("triage");
    toast("已确认分段,进入待整理区");
  }
}, $("#confirm-segs"));
$("#exit-proc").onclick = () => {
  if (ST.segDirty && !window.confirm("当前分段还没保存,退出会丢失本地修改。确认退出?")) return;
  ST.segDirty = false;
  ST.lock = null;
  $("#exit-proc").style.display = "none";
  renderListItems();
  toast("已退出处理");
};
document.querySelectorAll("#substage button").forEach((b) =>
  b.onclick = () => setStage(b.dataset.stage));

// 块 C:待整理批量工具条(此前定义了 doExtractSel/doConfirmSel/doRejectSel 却漏绑)
$("#t-all").onclick = toggleAllTri;
$("#t-extract").onclick = doExtractSel;
$("#t-confirm").onclick = doConfirmSel;
$("#t-reject").onclick = doRejectSel;
$("#t-delete").onclick = doDeleteSel;
$("#t-refresh").onclick = () => loadTriageAll();

// 启动:先恢复本地游标(候选区/刚编辑/子屏),再拉列表;列表到位后把视图切回上次所在子屏。
restoreState();
$("#sort-toggle").textContent = ST.sortMode === "touched" ? "动过的沉底" : "按时间";
$("#sort-dir").textContent = ST.sortDir === "desc" ? "时间↓" : "时间↑";
loadProviders();   // 内部已兜底网络错误(toast)
loadList().then(() => { if (ST.stage === "triage") setStage("triage"); })
  .catch((e) => toast("启动加载失败: " + e, true));
