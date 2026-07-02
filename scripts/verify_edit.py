"""编辑写回层 headless 验收(fake chat + fake embedding,离线)。

覆盖 editor.edit_episode(正文四件:overview/summary/highlights/salience_tier):
- 改 summary/highlights/tier **不重嵌**(向量字节不变);改 overview **重嵌**(向量变 + last_embedded_at 刷新)。
- 碎片正本与 DB 同步;source_text / nodes / 身份字段原样保留。
- 校验:空 overview/summary、tier∉{1,2,3}、highlights>3、非白名单字段(source_text/nodes)→ EditError。
- no-op(值没变)→ changed=[],不写盘不重嵌。
- 回归:编辑落到正本——删库后 index rebuild 重建出的是**编辑后**的内容。
跑法:python scripts/verify_edit.py
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="memsys_edit_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

from pathlib import Path  # noqa: E402

import sqlite_vec  # noqa: E402

from memory_system import archive, editor, staging_store  # noqa: E402
from memory_system.agent.fake import FakeChatProvider, make_extraction  # noqa: E402
from memory_system.chunk import manual_segments  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding import get_provider  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.extract import extract_segments  # noqa: E402
from memory_system.fragments import episode_path, read_episode  # noqa: E402
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


def vec_of(pid: str) -> bytes:
    con = connect(CFG.db_path)
    try:
        return con.execute(
            "SELECT embedding FROM episode_vectors ev JOIN episodes e ON e.id=ev.episode_id "
            "WHERE e.public_id=?", (pid,)).fetchone()[0]
    finally:
        con.close()


def db_row(pid: str) -> dict:
    con = connect(CFG.db_path)
    try:
        r = con.execute("SELECT overview, summary, highlights_json, salience_tier, "
                        "source_text, last_embedded_at FROM episodes WHERE public_id=?",
                        (pid,)).fetchone()
        return dict(r) if r else {}
    finally:
        con.close()


# ============ 准备:confirm 一条 active 碎片(带膜/高光)============
ct = mk_ct(10, session="sess-edit")
segs = manual_segments(ct, [(1, 10)])
segs[0]["seg_id"] = "s1"
b1 = make_extraction(
    overview="原始 overview 概述。", summary="原始 summary。",
    highlights=[{"text": "原始高光一", "tag": "关键"}],
    nodes=[{"label": "概念甲", "action": "new", "reason": "新"}],
    salience_tier=1)
batch = extract_segments(ct, segs, FakeChatProvider(behaviors=[b1]), [],
                         model="opus", timeout=10, max_retries=0)
ts_by_turn = {t.idx: t.timestamp for t in ct.turns}
for seg, res, src in batch.staged:
    staging_store.upsert_episode(CFG.staging_episodes_dir, ct.session_id, ct.path, seg, res, src,
                                 created_at=ts_by_turn.get(seg["start_turn"]))
prov = get_provider(CFG.embedding)
pid = archive.confirm_episode(CFG, ct.session_id, "e1", prov)
ep0 = read_episode(episode_path(CFG.episodes_dir, pid))
src_text0 = ep0.source_text
vec0 = vec_of(pid)
ok(f"准备:confirm 一条 active 碎片 {pid}")


# ============ 门 A:改 summary/highlights/tier —— 不重嵌,向量字节不变 ============
rep = editor.edit_episode(CFG, pid, {
    "summary": "改后的 summary。",
    "highlights": [{"text": "新高光A", "tag": "T"}, {"text": "新高光B", "tag": ""}],
    "salience_tier": 3,
}, prov)
assert rep.reembedded is False, rep
assert set(rep.changed) == {"summary", "highlights", "salience_tier"}, rep.changed
assert vec_of(pid) == vec0, "overview 没变,向量字节不该变"
ep1 = read_episode(episode_path(CFG.episodes_dir, pid))
assert ep1.summary == "改后的 summary。" and ep1.salience_tier == 3, ep1
assert ep1.highlights == [{"text": "新高光A", "tag": "T"}, {"text": "新高光B", "tag": ""}], ep1.highlights
assert ep1.overview == "原始 overview 概述。", "overview 未动应保持原值"
r = db_row(pid)
assert r["summary"] == "改后的 summary。" and r["salience_tier"] == 3, r
assert ep1.source_text == src_text0 and ep1.nodes == ["概念甲"], "source_text/nodes 不该动"
ok("改 summary/highlights/tier:碎片+DB 同步、向量不变、source_text/nodes 保留")


# ============ 门 B:改 overview —— 重嵌,向量字节变 + last_embedded_at 刷新 ============
last_emb_before = db_row(pid)["last_embedded_at"]
rep = editor.edit_episode(CFG, pid, {"overview": "全新的 overview 内容。"}, prov)
assert rep.reembedded is True and rep.changed == ["overview"], rep
assert vec_of(pid) != vec0, "overview 变了,向量字节应变"
exp_vec = sqlite_vec.serialize_float32(FakeProvider(model="fake", dim=16).embed(["全新的 overview 内容。"])[0])
assert vec_of(pid) == exp_vec, "向量应是编辑后 overview 的嵌入"
ep2 = read_episode(episode_path(CFG.episodes_dir, pid))
assert ep2.overview == "全新的 overview 内容。", ep2.overview
r = db_row(pid)
assert r["overview"] == "全新的 overview 内容。", r
assert r["last_embedded_at"] != last_emb_before, "重嵌应刷新 last_embedded_at"
assert ep2.source_text == src_text0, "改 overview 不该动 source_text"
ok("改 overview:重嵌、向量字节变、last_embedded_at 刷新、碎片+DB 同步")


# ============ 门 C:no-op(值没变)—— changed=[],不重嵌 ============
vec_b = vec_of(pid)
rep = editor.edit_episode(CFG, pid, {"overview": "全新的 overview 内容。",
                                     "salience_tier": 3}, prov)
assert rep.changed == [] and rep.reembedded is False, rep
assert vec_of(pid) == vec_b, "no-op 不该改向量"
ok("no-op 编辑:changed=[]、不重嵌、向量不变")


# ============ 门 D:校验 —— 坏值/越权字段抛 EditError ============
cases = [
    ({"overview": "   "}, "空 overview"),
    ({"summary": ""}, "空 summary"),
    ({"salience_tier": 4}, "tier 越界"),
    ({"salience_tier": "x"}, "tier 非整数"),
    ({"highlights": [{"text": "1"}, {"text": "2"}, {"text": "3"}, {"text": "4"}]}, "highlights>3"),
    ({"highlights": [{"text": ""}]}, "highlights 空 text"),
    ({"source_text": "想改原文"}, "source_text 不可编辑"),
    ({"nodes": ["概念乙"]}, "nodes 不可编辑"),
    ({"public_id": "ep_xxxx"}, "身份字段不可编辑"),
]
for fields, why in cases:
    raised = False
    try:
        editor.edit_episode(CFG, pid, fields, prov)
    except editor.EditError:
        raised = True
    assert raised, f"应抛 EditError: {why}"
# 校验失败不留痕:碎片仍是门 B 之后的状态
ep_after = read_episode(episode_path(CFG.episodes_dir, pid))
assert ep_after.overview == "全新的 overview 内容。" and ep_after.salience_tier == 3, ep_after
ok("校验:空值/越界/越权字段(source_text/nodes/public_id)全抛 EditError,碎片不受损")


# ============ 门 E:无此碎片 / 无 DB 索引 ============
raised = False
try:
    editor.edit_episode(CFG, "ep_deadbeef", {"summary": "x"}, prov)
except editor.EditError:
    raised = True
assert raised, "编辑不存在的碎片应抛 EditError"
ok("错误路径:编辑不存在的 episode 抛 EditError")


# ============ 门 F:回归 —— 编辑落到正本,删库 rebuild 重建出编辑后的内容 ============
for suffix in ("", "-wal", "-shm"):
    p = Path(str(CFG.db_path) + suffix)
    if p.exists():
        p.unlink()
rebuild(CFG, FakeProvider(model="fake", dim=16))
r = db_row(pid)
assert r["overview"] == "全新的 overview 内容。", "rebuild 后应是编辑后的 overview(碎片是正本)"
assert r["summary"] == "改后的 summary。" and r["salience_tier"] == 3, r
# rebuild 的向量来自编辑后的 overview
assert vec_of(pid) == exp_vec, "rebuild 向量应来自编辑后的 overview"
ok("回归:编辑落到碎片正本,删库 rebuild 无损还原编辑后的内容 + 向量")


print("编辑写回层 ALL PASS ✅")
