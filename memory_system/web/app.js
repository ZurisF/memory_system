"use strict";

$("#run-chunk").onclick = runChunk;
$("#make-seg").onclick = makeSegFromSel;
$("#mark").onclick = markSelected;
$("#seg-all").onclick = toggleAllSegs;
$("#seg-del").onclick = deletePickedSegs;
$("#sort-toggle").onclick = () => {
  SORT_MODE = SORT_MODE === "touched" ? "time" : "touched";
  $("#sort-toggle").textContent = SORT_MODE === "touched" ? "动过的沉底" : "按时间";
  saveState();
  renderListItems();
};

// 块 B:确认分段(存段→待整理)、退出处理(解锁)、子阶段切换
$("#confirm-segs").onclick = async () => {
  if (await saveSegs()) {       // saveSegs 已解锁/标记/纳入候选
    setStage("triage");
    toast("已确认分段,进入待整理区");
  }
};
$("#exit-proc").onclick = () => {
  if (SEG_DIRTY && !window.confirm("当前分段还没保存,退出会丢失本地修改。确认退出?")) return;
  SEG_DIRTY = false;
  LOCK = null;
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
$("#sort-toggle").textContent = SORT_MODE === "touched" ? "动过的沉底" : "按时间";
loadProviders();
loadList().then(() => { if (STAGE === "triage") setStage("triage"); });
