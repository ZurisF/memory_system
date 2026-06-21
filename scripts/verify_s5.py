"""S5 审核/归档层 headless 验收(fake chat + fake embedding,离线)。

覆盖通过门(phase1_build §S5)+ P1-B:
- P1-B validate_segments:重叠被拒、空洞返回 gap、完整覆盖通过。
- staging 编辑往返一致(只动可编辑字段)。
- confirm:staging→active,碎片落地 + DB 一致(episodes/膜/向量/FTS);public_id=ep_<8hex>;
  node 三选一落地(match_existing 复用、add_alias 合并别名、new 建碎片);膜正确;uuid 不进碎片。
- 别名合并幂等:两条 episode 对同一 node add_alias 不长出重复 node。
- reject:从 staging 移除留痕、不进 DB;archive:active→archived,碎片+DB 同步。
- 回归:confirm 出的碎片删库后 index rebuild 无损还原(碎片是正本,archived 状态保留)。
跑法:python scripts/verify_s5.py
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="memsys_s5_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

from pathlib import Path  # noqa: E402

from memory_system import archive, staging_store  # noqa: E402
from memory_system.agent.fake import FakeChatProvider, make_extraction  # noqa: E402
from memory_system.chunk import manual_segments, validate_segments  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding import get_provider  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.extract import extract_segments  # noqa: E402
from memory_system.fragments import (  # noqa: E402
    Node,
    episode_path,
    node_path,
    read_episode,
    read_node,
    write_node,
)
from memory_system.index import rebuild  # noqa: E402
from memory_system.preprocess import CleanedTranscript, Turn  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
print(f"临时 home: {_TMP}")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def mk_ct(n: int, session: str = "sess-s5") -> CleanedTranscript:
    ct = CleanedTranscript(session_id=session, path=f"/fake/{session}.jsonl")
    for i in range(1, n + 1):
        ct.turns.append(Turn(idx=i, human_text=f"人类第{i}句", assistant_text=f"Claude第{i}句",
                             uuids=[f"u{i}"], human_uuid=f"u{i}",
                             timestamp=f"2026-06-19T22:{i:02d}:00Z"))
    return ct


# ============ 门 A:P1-B validate_segments ============
ALL = set(range(1, 31))
overlap = [{"seg_id": "a", "start_turn": 1, "end_turn": 10},
           {"seg_id": "b", "start_turn": 8, "end_turn": 15}]
vr = validate_segments(overlap, ALL)
assert not vr["ok"] and vr["overlaps"] and vr["overlaps"][0]["range"] == [8, 10], vr
gapped = [{"seg_id": "a", "start_turn": 1, "end_turn": 10},
          {"seg_id": "b", "start_turn": 21, "end_turn": 30}]
vr = validate_segments(gapped, ALL)
assert vr["ok"] and vr["gaps"] == [[11, 20]], vr  # 空洞放行,只回 gap 提示
full = [{"seg_id": "a", "start_turn": 1, "end_turn": 15},
        {"seg_id": "b", "start_turn": 16, "end_turn": 30}]
vr = validate_segments(full, ALL)
assert vr["ok"] and vr["gaps"] == [], vr
ok("P1-B:重叠被拒(回冲突区间)、空洞放行回 gap、完整覆盖通过")


# ============ 准备 staging:3 段提取落 staging(带 created_at)============
# 预置两个 active node 碎片,供 confirm 时 match_existing / add_alias 落地。
write_node(CFG.nodes_dir, Node(label="Solaris", type="concept",
                               created_at="t0", updated_at="t0", aliases=["索拉里斯"]))
write_node(CFG.nodes_dir, Node(label="记忆系统", type="concept",
                               created_at="t0", updated_at="t0", aliases=["记忆库"]))

ct = mk_ct(30)
segs = manual_segments(ct, [(1, 10), (11, 20), (21, 30)])
for i, s in enumerate(segs, 1):
    s["seg_id"] = f"s{i}"

b1 = make_extraction(
    overview="zuris Claude 弥赛亚 记忆系统 Solaris 测试 overview。",
    summary="一段起承转合俱全的对话。",
    highlights=[{"text": "记忆应该写给你自己,而不是写给我", "tag": "关键定义"}],
    nodes=[
        {"label": "Solaris", "action": "match_existing", "reason": "已有"},
        {"label": "记忆系统", "action": "add_alias", "new_alias": "记忆库2", "reason": "别名"},
        {"label": "弥赛亚", "action": "new", "reason": "新概念"},
    ],
    salience_tier=3, salience_reason="高",
)
b2 = make_extraction(
    overview="第二段 记忆系统 再提。", summary="第二段弧线。",
    nodes=[{"label": "记忆系统", "action": "add_alias", "new_alias": "memory2", "reason": "再加别名"}],
    salience_tier=2,
)
b3 = make_extraction(
    overview="第三段 拟拒。", summary="第三段。",
    nodes=[{"label": "弃用概念", "action": "new", "reason": "将被拒"}],
    salience_tier=1,
)
batch = extract_segments(ct, segs, FakeChatProvider(behaviors=[b1, b2, b3]), [],
                         model="opus", timeout=10, max_retries=0)
assert len(batch.staged) == 3, [s[0]["seg_id"] for s in batch.staged]
ts_by_turn = {t.idx: t.timestamp for t in ct.turns}
for seg, res, src in batch.staged:
    staging_store.upsert_episode(CFG.staging_episodes_dir, ct.session_id, ct.path, seg, res, src,
                                 created_at=ts_by_turn.get(seg["start_turn"]))
doc = staging_store.load(CFG.staging_episodes_dir, ct.session_id)
assert [e["stage_id"] for e in doc["episodes"]] == ["e1", "e2", "e3"]
assert doc["episodes"][0]["created_at"] == "2026-06-19T22:01:00Z", "段首 timestamp 应入 staging"
ok("staging 就绪:3 段落盘,段首 timestamp 作 created_at 存入")


# ============ 门 B:staging 编辑往返 ============
staging_store.edit_episode(CFG.staging_episodes_dir, ct.session_id, "e2",
                           {"overview": "编辑后的 overview", "covered_uuids": ["黑客注入"]})
e2 = staging_store.get_episode(CFG.staging_episodes_dir, ct.session_id, "e2")
assert e2["overview"] == "编辑后的 overview" and e2["origin"] == "edited"
assert e2["covered_uuids"] != ["黑客注入"], "covered_uuids 不在可编辑白名单,应不被改"
ok("staging 编辑:五件套可改、origin→edited;工作态字段(covered_uuids)不可越权改")


# ============ 门 C:confirm e1 → active,碎片 + DB 一致,uuid 不进碎片 ============
prov = get_provider(CFG.embedding)
pid = archive.confirm_episode(CFG, ct.session_id, "e1", prov)
assert pid.startswith("ep_") and len(pid) == 11, pid  # ep_ + 8 hex
fp = episode_path(CFG.episodes_dir, pid)
ep = read_episode(fp)
assert ep.status == "active" and ep.activated_at
assert ep.created_at == "2026-06-19T22:01:00Z", "created_at 应取段首发生时间"
assert ep.source_session_id == ct.session_id
assert set(ep.nodes) == {"Solaris", "记忆系统", "弥赛亚"}
assert ep.highlights == [{"text": "记忆应该写给你自己,而不是写给我", "tag": "关键定义"}]
assert ep.keywords == []
raw = fp.read_text(encoding="utf-8")
assert all(f"u{i}" not in raw for i in range(1, 11)), "uuid 绝不进碎片"
# node 三选一落地(碎片层)
assert node_path(CFG.nodes_dir, "弥赛亚").exists(), "new node 应建碎片"
ms = read_node(node_path(CFG.nodes_dir, "记忆系统"))
assert "记忆库" in ms.aliases and "记忆库2" in ms.aliases, "add_alias 应并入别名"
sol = read_node(node_path(CFG.nodes_dir, "Solaris"))
assert sol.aliases == ["索拉里斯"], "match_existing 不应改动目标 node"
# DB 一致
con = connect(CFG.db_path)
try:
    row = con.execute("SELECT * FROM episodes WHERE public_id=?", (pid,)).fetchone()
    assert row and row["status"] == "active" and row["overview"] == ep.overview
    assert row["last_accessed_at"] == ep.activated_at
    labels = {r["label"] for r in con.execute(
        "SELECT n.label FROM episode_nodes en JOIN nodes n ON n.id=en.node_id "
        "JOIN episodes e ON e.id=en.episode_id WHERE e.public_id=?", (pid,))}
    assert labels == {"Solaris", "记忆系统", "弥赛亚"}, labels
    (vn,) = con.execute("SELECT COUNT(*) FROM episode_vectors").fetchone()
    assert vn == 1, "向量未增量插入"
    (fn,) = con.execute("SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?",
                        ("人类第3句",)).fetchone()
    assert fn == 1, "FTS 未随 confirm 同步"
    (ms_alias,) = con.execute(
        "SELECT COUNT(*) FROM node_aliases WHERE alias='记忆库2'").fetchone()
    assert ms_alias == 1, "新别名未进 DB"
finally:
    con.close()
# 已确认从 staging 移除
assert staging_store.get_episode(CFG.staging_episodes_dir, ct.session_id, "e1") is None
ok("confirm e1→active:碎片落地、DB(episode/膜/向量/FTS/别名)一致、uuid 不进碎片、staging 清")


# ============ 门 D:confirm e2,别名合并幂等(不长重复 node)============
pid2 = archive.confirm_episode(CFG, ct.session_id, "e2", prov)
con = connect(CFG.db_path)
try:
    (cnt,) = con.execute("SELECT COUNT(*) FROM nodes WHERE label='记忆系统'").fetchone()
    assert cnt == 1, "同一 node 不应因二次 add_alias 重复建行"
    # e2 的膜连到同一个 记忆系统 node
    labels2 = {r["label"] for r in con.execute(
        "SELECT n.label FROM episode_nodes en JOIN nodes n ON n.id=en.node_id "
        "JOIN episodes e ON e.id=en.episode_id WHERE e.public_id=?", (pid2,))}
    assert labels2 == {"记忆系统"}, labels2
    (a2,) = con.execute("SELECT COUNT(*) FROM node_aliases WHERE alias='memory2'").fetchone()
    assert a2 == 1, "二次别名未进 DB"
finally:
    con.close()
ms2 = read_node(node_path(CFG.nodes_dir, "记忆系统"))
assert {"记忆库", "记忆库2", "memory2"} <= set(ms2.aliases)
ok("confirm e2:别名合并幂等,记忆系统 仍单 node、累积三个别名")


# ============ 门 E:reject e3 → 移出 staging 留痕,不进 DB ============
archive.reject_episode(CFG, ct.session_id, "e3", reason="测试拒绝")
doc = staging_store.load(CFG.staging_episodes_dir, ct.session_id)
assert all(e["stage_id"] != "e3" for e in doc["episodes"])
assert doc["episodes"] == [], "三条都消费完(2 确认 1 拒)"
assert any(r["stage_id"] == "e3" and r["reason"] == "测试拒绝" for r in doc.get("rejected", []))
assert not node_path(CFG.nodes_dir, "弃用概念").exists(), "拒绝不应落地其 node"
con = connect(CFG.db_path)
try:
    (en,) = con.execute("SELECT COUNT(*) FROM episodes").fetchone()
    assert en == 2, "DB 只该有 2 条确认的 episode"
finally:
    con.close()
ok("reject e3:移出 staging 留痕 rejected、node 不落地、DB 不增")


# ============ 门 F:archive pid(active → archived)============
archive.archive_episode(CFG, pid)
ep_a = read_episode(episode_path(CFG.episodes_dir, pid))
assert ep_a.status == "archived" and ep_a.archived_at
con = connect(CFG.db_path)
try:
    row = con.execute("SELECT status, archived_at FROM episodes WHERE public_id=?", (pid,)).fetchone()
    assert row["status"] == "archived" and row["archived_at"]
finally:
    con.close()
ok("archive:active→archived,碎片与 DB status 同步")


# ============ 门 G:回归 —— 删库后从碎片 rebuild 无损(碎片是正本)============
for suffix in ("", "-wal", "-shm"):
    p = Path(str(CFG.db_path) + suffix)
    if p.exists():
        p.unlink()
rep = rebuild(CFG, FakeProvider(model="fake", dim=16))
assert rep.episodes == 2, f"应重建 2 episode: {rep}"
assert rep.nodes == 3, f"应重建 3 node(Solaris/记忆系统/弥赛亚): {rep}"
assert rep.membrane == 4, f"膜应为 4(e1 三连 + e2 一连): {rep}"
assert rep.vectors == 2 and not rep.stub_nodes
con = connect(CFG.db_path)
try:
    row = con.execute("SELECT status FROM episodes WHERE public_id=?", (pid,)).fetchone()
    assert row["status"] == "archived", "archived 状态应从碎片还原"
    (fn,) = con.execute("SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?",
                        ("人类第3句",)).fetchone()
    assert fn == 1, "rebuild 后 FTS 未还原"
finally:
    con.close()
ok("回归:删库 rebuild 无损还原(2 episode/3 node/4 膜/2 向量,archived 状态保留)")

print("S5 审核/归档层 ALL PASS ✅")
