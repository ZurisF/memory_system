"""删除层 headless 验收(fake chat + fake embedding,离线)。

覆盖「误入库真删」(archive.delete_episode / delete_node),区别于 archive 软降级:
- delete_episode:碎片正本删、DB(episode/向量/膜/FTS)同步删;孤儿 node 保留并在报告里点名,
  共享 node 不误报孤儿。
- delete_node:碎片正本删、DB(节点/别名/膜)级联删,并从所有引用它的 episode 碎片摘除该 label
  (报告点名被摘的 episode)。
- 回归(核心):删后 `index rebuild` **不复活**已删的 episode / node;保留的孤儿 node 仍在。
- 错误路径:删不存在的 episode / node 抛 ArchiveError。
跑法:python scripts/verify_delete.py
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="memsys_del_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

from pathlib import Path  # noqa: E402

from memory_system import archive, staging_store  # noqa: E402
from memory_system.agent.fake import FakeChatProvider, make_extraction  # noqa: E402
from memory_system.chunk import manual_segments  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding import get_provider  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.extract import extract_segments  # noqa: E402
from memory_system.fragments import (  # noqa: E402
    episode_path,
    node_path,
    read_episode,
)
from memory_system.index import rebuild  # noqa: E402
from memory_system.preprocess import CleanedTranscript, Turn  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
print(f"临时 home: {_TMP}")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def mk_ct(n: int, session: str) -> CleanedTranscript:
    ct = CleanedTranscript(session_id=session, path=f"/fake/{session}.jsonl")
    for i in range(1, n + 1):
        ct.turns.append(Turn(idx=i, human_text=f"人类第{i}句", assistant_text=f"Claude第{i}句",
                             uuids=[f"u{i}"], human_uuid=f"u{i}",
                             timestamp=f"2026-06-20T10:{i:02d}:00Z"))
    return ct


def db_counts() -> dict:
    con = connect(CFG.db_path)
    try:
        return {
            "episodes": con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "nodes": con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "vectors": con.execute("SELECT COUNT(*) FROM episode_vectors").fetchone()[0],
            "membrane": con.execute("SELECT COUNT(*) FROM episode_nodes").fetchone()[0],
            "aliases": con.execute("SELECT COUNT(*) FROM node_aliases").fetchone()[0],
        }
    finally:
        con.close()


# ============ 准备:3 段提取 → confirm 成 3 条 active 碎片 ============
# 膜布局:Solaris 跨 e1/e2(共享);弥赛亚 只在 e1;记忆系统 只在 e2;独有概念 只在 e3。
ct = mk_ct(30, session="sess-del")
segs = manual_segments(ct, [(1, 10), (11, 20), (21, 30)])
for i, s in enumerate(segs, 1):
    s["seg_id"] = f"s{i}"

b1 = make_extraction(
    overview="第一段 overview 概述。", summary="第一段弧线。",
    highlights=[{"text": "逐字高亮一", "tag": "关键定义"}],
    nodes=[{"label": "Solaris", "action": "add_alias", "new_alias": "索拉里斯", "reason": "新建带别名"},
           {"label": "弥赛亚", "action": "new", "reason": "新,仅此段"}],
    salience_tier=3)
b2 = make_extraction(
    overview="第二段 overview 概述。", summary="第二段弧线。",
    nodes=[{"label": "Solaris", "action": "match_existing", "reason": "复用"},
           {"label": "记忆系统", "action": "new", "reason": "新,仅此段"}],
    salience_tier=2)
b3 = make_extraction(
    overview="第三段 overview 概述。", summary="第三段弧线。",
    nodes=[{"label": "独有概念", "action": "new", "reason": "新,仅此段"}],
    salience_tier=1)
batch = extract_segments(ct, segs, FakeChatProvider(behaviors=[b1, b2, b3]), [],
                         model="opus", timeout=10, max_retries=0)
assert len(batch.staged) == 3, [s[0]["seg_id"] for s in batch.staged]
ts_by_turn = {t.idx: t.timestamp for t in ct.turns}
for seg, res, src in batch.staged:
    staging_store.upsert_episode(CFG.staging_episodes_dir, ct.session_id, ct.path, seg, res, src,
                                 created_at=ts_by_turn.get(seg["start_turn"]))

prov = get_provider(CFG.embedding)
pid1 = archive.confirm_episode(CFG, ct.session_id, "e1", prov)
pid2 = archive.confirm_episode(CFG, ct.session_id, "e2", prov)
pid3 = archive.confirm_episode(CFG, ct.session_id, "e3", prov)
base = db_counts()
assert base == {"episodes": 3, "nodes": 4, "vectors": 3, "membrane": 5, "aliases": 1}, base
ok("准备:3 条 active(膜 5:Solaris 跨 e1/e2,弥赛亚/记忆系统/独有概念 各一)")


# ============ 门 A:delete_episode e1 —— 碎片 + DB 同步删,孤儿点名,共享不误报 ============
rep = archive.delete_episode(CFG, pid1)
assert not episode_path(CFG.episodes_dir, pid1).exists(), "e1 碎片正本应已删"
# 孤儿:弥赛亚 仅挂 e1 → 孤儿;Solaris 还在 e2 → 不报孤儿
assert rep.orphaned_nodes == ["弥赛亚"], rep.orphaned_nodes
assert node_path(CFG.nodes_dir, "弥赛亚").exists(), "孤儿 node 碎片应保留(不随 episode 删)"
c = db_counts()
assert c["episodes"] == 2 and c["vectors"] == 2, c          # episode 行 + 向量减一
assert c["membrane"] == base["membrane"] - 2, c             # e1 的 2 条膜(Solaris/弥赛亚)级联删
assert c["nodes"] == 4, c                                   # node 行不动(孤儿保留)
con = connect(CFG.db_path)
try:
    assert con.execute("SELECT COUNT(*) FROM episodes WHERE public_id=?", (pid1,)).fetchone()[0] == 0
    # FTS:e1 独有短语(回合 5)应随触发器清掉;e2 独有短语(回合 15)仍在
    (f1,) = con.execute("SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", ("人类第5句",)).fetchone()
    (f2,) = con.execute("SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", ("人类第15句",)).fetchone()
    assert f1 == 0 and f2 == 1, (f1, f2)
finally:
    con.close()
ok("delete_episode e1:碎片删、DB(行/向量/膜/FTS)同步删、弥赛亚 点名为孤儿、Solaris 不误报")


# ============ 门 B:delete_node Solaris —— 级联删 + 从引用它的 episode 碎片摘除 ============
rep = archive.delete_node(CFG, "Solaris")
assert not node_path(CFG.nodes_dir, "Solaris").exists(), "Solaris node 碎片应已删"
# e1 已删,Solaris 现只挂 e2 → 报告应点名 e2
assert rep.dereferenced_episodes == [pid2], rep.dereferenced_episodes
e2 = read_episode(episode_path(CFG.episodes_dir, pid2))
assert "Solaris" not in e2.nodes and "记忆系统" in e2.nodes, e2.nodes  # 仅摘 Solaris,留其余
c = db_counts()
assert c["nodes"] == 3, c                                   # Solaris node 行删
assert c["aliases"] == 0, c                                 # 索拉里斯 别名随 FK 级联删
assert c["membrane"] == base["membrane"] - 2 - 1, c         # 再去掉 e2↔Solaris 一条膜
con = connect(CFG.db_path)
try:
    assert con.execute("SELECT COUNT(*) FROM nodes WHERE label='Solaris'").fetchone()[0] == 0
finally:
    con.close()
ok("delete_node Solaris:碎片删、DB(节点/别名/膜级联)删、e2 碎片摘除引用并点名")


# ============ 门 C:回归 —— 删后 rebuild 不复活已删者,保留的孤儿仍在 ============
for suffix in ("", "-wal", "-shm"):
    p = Path(str(CFG.db_path) + suffix)
    if p.exists():
        p.unlink()
rep_rb = rebuild(CFG, FakeProvider(model="fake", dim=16))
# 盘上现存碎片:episode e2/e3(2);node 记忆系统/独有概念/弥赛亚(3,弥赛亚是保留的孤儿)
assert rep_rb.episodes == 2, rep_rb
assert rep_rb.nodes == 3 and not rep_rb.stub_nodes, rep_rb   # 不该有桩(Solaris 引用已从碎片摘净)
con = connect(CFG.db_path)
try:
    assert con.execute("SELECT COUNT(*) FROM episodes WHERE public_id=?", (pid1,)).fetchone()[0] == 0, \
        "删掉的 e1 不应被 rebuild 复活"
    assert con.execute("SELECT COUNT(*) FROM nodes WHERE label='Solaris'").fetchone()[0] == 0, \
        "删掉的 Solaris 不应被 rebuild 复活(碎片引用已摘净)"
    assert con.execute("SELECT COUNT(*) FROM nodes WHERE label='弥赛亚'").fetchone()[0] == 1, \
        "保留的孤儿 node(弥赛亚)应仍在"
finally:
    con.close()
ok("回归:删后 rebuild 不复活 e1/Solaris、无桩节点、保留的孤儿弥赛亚仍在")


# ============ 门 D:错误路径 —— 删不存在的 episode / node 抛 ArchiveError ============
for fn, arg in ((archive.delete_episode, "ep_deadbeef"), (archive.delete_node, "不存在的概念")):
    raised = False
    try:
        fn(CFG, arg)
    except archive.ArchiveError:
        raised = True
    assert raised, f"{fn.__name__}({arg!r}) 应抛 ArchiveError"
ok("错误路径:删不存在的 episode / node 抛 ArchiveError")


print("删除层 ALL PASS ✅")
