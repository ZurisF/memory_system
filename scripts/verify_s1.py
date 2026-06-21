"""S1 五道通过门离线验收(fake provider,临时 home,小维度)。

跑法:python scripts/verify_s1.py
任一门失败即抛 AssertionError;全过打印 ALL PASS。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 必须在 import memory_system.config 之前设好环境(load_config 读环境)
_TMP = tempfile.mkdtemp(prefix="memsys_s1_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"

from memory_system.config import load_config  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.fragments import (  # noqa: E402
    Episode,
    Node,
    parse_episode,
    serialize_episode,
    write_episode,
    write_node,
)
from memory_system.index import rebuild  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)


def _fake(dim: int = 16, model: str = "fake") -> FakeProvider:
    return FakeProvider(model=model, dim=dim)


# source_text 故意含一行 "## overview" 与管道符,压测分节锚点鲁棒性
SOURCE = """[我]: 我在种蓝莓的时候突然想到荒诞主义。
## overview
这一行是原文的一部分,不该被当成分节 header。
[Claude]: a | b | c 管道符也要原样保留。
[我]: 那句诗——"记忆应该写给你自己"。"""

EP = Episode(
    public_id="ep_2026-06-19_test01",
    overview="zuris 种蓝莓时联想到荒诞主义。Claude。记忆系统。蓝莓荒诞主义关联。",
    summary="zuris 从种蓝莓的日常滑向荒诞主义的思辨,最后落到记忆该写给谁。",
    source_text=SOURCE,
    salience_tier=3,
    status="active",
    created_at="2026-06-19T22:14:03Z",
    highlights=[
        {"tag": "关键定义", "text": "记忆应该写给你自己,而不是写给我"},
        {"tag": "诗句", "text": "第一行\n第二行带换行"},
    ],
    keywords=["蓝莓", "荒诞主义", "记忆系统"],
    nodes=["蓝莓", "荒诞主义", "记忆系统"],
    activated_at="2026-06-20T09:00:00Z",
    source_session_id="666b1f63-test",
    source_path="/Users/zuris/.claude/projects/x/abc.jsonl",
)

NODES = [
    Node(label="蓝莓", type="entity", aliases=[], created_at="2026-06-19T22:00:00Z", updated_at="2026-06-19T22:00:00Z"),
    Node(label="荒诞主义", type="concept", aliases=["absurdism"], created_at="2026-06-19T22:00:00Z", updated_at="2026-06-19T22:00:00Z"),
    Node(label="记忆系统", type="concept", aliases=["记忆库", "memory"], created_at="2026-06-19T22:00:00Z", updated_at="2026-06-19T22:00:00Z"),
]


def gate_roundtrip_serialize() -> None:
    """先验序列化↔解析自身 round-trip(含 ## 冲突行、换行 highlight)。"""
    again = parse_episode(serialize_episode(EP))
    assert again.source_text == EP.source_text, "source_text round-trip 不一致"
    assert again.highlights == EP.highlights, f"highlights round-trip 不一致: {again.highlights}"
    assert again.nodes == EP.nodes, "nodes round-trip 不一致"
    assert again.keywords == EP.keywords, "keywords round-trip 不一致"
    assert again.overview == EP.overview and again.summary == EP.summary
    assert again.salience_tier == 3 and again.status == "active"
    assert again.source_session_id == EP.source_session_id
    assert again.source_path == EP.source_path
    print("  [ok] 碎片 serialize↔parse round-trip 保真(含 ## 冲突行 / 换行 highlight)")


def _write_fixtures() -> None:
    for nd in NODES:
        write_node(CFG.nodes_dir, nd)
    write_episode(CFG.episodes_dir, EP)


def gate1_rebuild_field_parity() -> None:
    """门1:碎片 → rebuild → DB 与碎片逐字段一致。"""
    rep = rebuild(CFG, _fake())
    assert rep.episodes == 1 and rep.nodes == 3 and rep.membrane == 3 and rep.vectors == 1, \
        f"rebuild 计数异常: {rep}"
    assert not rep.stub_nodes, f"不该有桩 node: {rep.stub_nodes}"
    con = connect(CFG.db_path)
    try:
        row = con.execute("SELECT * FROM episodes WHERE public_id=?", (EP.public_id,)).fetchone()
        assert row["overview"] == EP.overview
        assert row["summary"] == EP.summary
        assert row["source_text"] == EP.source_text, "source_text 不一致"
        assert row["salience_tier"] == 3 and row["status"] == "active"
        assert row["created_at"] == EP.created_at
        assert row["last_accessed_at"] == EP.activated_at, "last_accessed_at 初值应=activated_at"
        import json
        assert json.loads(row["keywords_json"]) == EP.keywords
        assert json.loads(row["highlights_json"]) == EP.highlights
        # 膜:三个 label 都连上
        labels = {
            r["label"]
            for r in con.execute(
                "SELECT n.label FROM episode_nodes en JOIN nodes n ON n.id=en.node_id "
                "JOIN episodes e ON e.id=en.episode_id WHERE e.public_id=?",
                (EP.public_id,),
            ).fetchall()
        }
        assert labels == {"蓝莓", "荒诞主义", "记忆系统"}, f"膜 label 不符: {labels}"
        # 别名
        al = {r["alias"] for r in con.execute("SELECT alias FROM node_aliases").fetchall()}
        assert {"absurdism", "记忆库", "memory"} <= al, f"别名缺失: {al}"
    finally:
        con.close()
    print("  [ok] 门1:rebuild 后 DB 与碎片逐字段一致(含膜、别名)")


def gate2_drop_and_restore() -> None:
    """门2:删 SQLite → 单条 rebuild 无损还原(向量、FTS、膜)。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(CFG.db_path) + suffix)
        if p.exists():
            p.unlink()
    assert not CFG.db_path.exists(), "db 应已删除"
    rep = rebuild(CFG, _fake())
    assert rep.vectors == 1 and rep.membrane == 3, f"重建后计数异常: {rep}"
    con = connect(CFG.db_path)
    try:
        (vn,) = con.execute("SELECT COUNT(*) FROM episode_vectors").fetchone()
        assert vn == 1, "向量未还原"
        (fn,) = con.execute(
            "SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", ("荒诞主义",)
        ).fetchone()
        assert fn == 1, "FTS 未还原(荒诞主义 搜不到)"
        (mn,) = con.execute("SELECT COUNT(*) FROM episode_nodes").fetchone()
        assert mn == 3, "膜未还原"
    finally:
        con.close()
    print("  [ok] 门2:删库后单条 rebuild 无损还原(向量/FTS/膜)")


def gate3_dim_model_mismatch() -> None:
    """门3:写向量时维度/模型与 meta 锁不符 → 拒写并报清原因。"""
    # 此时 meta 锁已是 (fake,16)。换 dim=8 或 model 不符都应被拒。
    for bad, why in [(_fake(dim=8), "维度"), (_fake(model="other"), "模型")]:
        try:
            rebuild(CFG, bad)
        except ValueError as e:
            assert ("维度" in str(e)) or ("模型" in str(e)), f"报错信息不清: {e}"
            print(f"  [ok] 门3:{why}不符被拒 —— {e}")
        else:
            raise AssertionError(f"{why}不符竟然没被拒")


def gate4_fts_trigger_update() -> None:
    """门4:改一条 episode 的 source_text → FTS 命中随之更新。"""
    rebuild(CFG, _fake())
    con = connect(CFG.db_path)
    try:
        # 旧词命中、新词未命中(trigram 最短 3 字,用「种蓝莓」)
        (old_hit,) = con.execute(
            "SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", ("种蓝莓",)
        ).fetchone()
        assert old_hit == 1
        new_term = "量子纠缠态"
        (new_before,) = con.execute(
            "SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", (new_term,)
        ).fetchone()
        assert new_before == 0
        # 改 source_text
        con.execute(
            "UPDATE episodes SET source_text=? WHERE public_id=?",
            (f"换成讲{new_term}的内容,不再提蓝莓。", EP.public_id),
        )
        con.commit()
        (new_after,) = con.execute(
            "SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", (new_term,)
        ).fetchone()
        (old_after,) = con.execute(
            "SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", ("种蓝莓",)
        ).fetchone()
        assert new_after == 1, "新词未随 source_text 更新进 FTS"
        assert old_after == 0, "旧词仍命中,FTS 未更新"
    finally:
        con.close()
    print("  [ok] 门4:改 source_text 后 FTS 命中随触发器更新")


def gate5_cascade_no_orphan() -> None:
    """门5:删 episode / node 后,膜无孤儿行。"""
    rebuild(CFG, _fake())
    con = connect(CFG.db_path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        # 删 node「蓝莓」→ 它的膜行、别名应级联消失
        nid = con.execute("SELECT id FROM nodes WHERE label='蓝莓'").fetchone()["id"]
        con.execute("DELETE FROM nodes WHERE id=?", (nid,))
        con.commit()
        (m1,) = con.execute("SELECT COUNT(*) FROM episode_nodes WHERE node_id=?", (nid,)).fetchone()
        assert m1 == 0, "删 node 后仍有孤儿膜行"
        # 删 episode → 它的全部膜行级联消失
        eid = con.execute("SELECT id FROM episodes WHERE public_id=?", (EP.public_id,)).fetchone()["id"]
        con.execute("DELETE FROM episodes WHERE id=?", (eid,))
        con.commit()
        (m2,) = con.execute("SELECT COUNT(*) FROM episode_nodes WHERE episode_id=?", (eid,)).fetchone()
        assert m2 == 0, "删 episode 后仍有孤儿膜行"
        # FTS 也应随 episode 删除清掉
        (fn,) = con.execute(
            "SELECT COUNT(*) FROM episode_fts WHERE episode_fts MATCH ?", ("荒诞主义",)
        ).fetchone()
        assert fn == 0, "删 episode 后 FTS 仍有残留"
    finally:
        con.close()
    print("  [ok] 门5:删 episode/node 膜无孤儿行(FK 级联 + FTS 触发器)")


def main() -> None:
    print(f"临时 home: {_TMP}")
    gate_roundtrip_serialize()
    _write_fixtures()
    gate1_rebuild_field_parity()
    gate2_drop_and_restore()
    gate3_dim_model_mismatch()
    gate4_fts_trigger_update()
    gate5_cascade_no_orphan()
    print("ALL PASS ✅")


if __name__ == "__main__":
    main()
