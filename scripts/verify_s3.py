"""S3 切块层 headless 验收(不含 GUI;fake provider 离线)。

覆盖通过门:agent 切合理段且 tag 区分、人工改段(并/分/移边界)持久、单弧线/渐变漂移/
short、超大报错不静默截断、失败可重试屡败进 retry 列表、回合区间正确回映 covered_uuids、
uuid 不上台面(UI 序列化剥除)。
跑法:python scripts/verify_s3.py
任一门失败即抛 AssertionError;全过打印 ALL PASS。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="memsys_s3_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

from memory_system import segments_store  # noqa: E402
from memory_system.agent import extract_json  # noqa: E402
from memory_system.agent.base import ChatError, ChatTimeout  # noqa: E402
from memory_system.agent.fake import FakeChatProvider, make_segments  # noqa: E402
from memory_system.chunk import (  # noqa: E402
    MAX_CHARS,
    ChunkFailed,
    OversizedError,
    manual_segments,
    run_chunk,
    uuids_by_turn,
)
from memory_system.config import load_config  # noqa: E402
from memory_system.preprocess import CleanedTranscript, Turn  # noqa: E402
from memory_system.server import _ui_segment  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
print(f"临时 home: {_TMP}")


def mk_ct(n: int, session="sess-a") -> CleanedTranscript:
    """造 n 个回合,每回合一句话 + 一个确定 uuid u{idx}。"""
    ct = CleanedTranscript(session_id=session, path=f"/fake/{session}.jsonl")
    for i in range(1, n + 1):
        ct.turns.append(Turn(idx=i, human_text=f"人类第{i}句", assistant_text=f"Claude第{i}句",
                             uuids=[f"u{i}"], human_uuid=f"u{i}"))
    return ct


def ok(msg):
    print(f"  [ok] {msg}")


# ---- 门 0:JSON 提取健壮性(围栏 / 夹带文字)----
assert extract_json('{"segments":[]}') == {"segments": []}
assert extract_json('```json\n{"a":1}\n```') == {"a": 1}
assert extract_json('好的,结果如下:\n{"a":1}\n以上。')["a"] == 1
ok("extract_json:裸 JSON / ```json 围栏 / 夹带文字 三种都抠得出")

# ---- 门 1:agent 切合理段,tag 区分同篇不同段,covered_uuids 正确回映 ----
ct = mk_ct(20)
seg_json = make_segments([
    {"start": 1, "end": 10, "tag": "前段:技术", "cut_reason": "弧线收束", "short": False, "deletions": []},
    {"start": 11, "end": 20, "tag": "后段:情感", "cut_reason": "阶段转换", "short": False, "deletions": []},
])
prov = FakeChatProvider(behaviors=[seg_json])
res = run_chunk(ct, prov, model="sonnet", timeout=10, max_retries=2)
assert len(res.segments) == 2, res.segments
assert res.segments[0]["tag"] != res.segments[1]["tag"]
assert res.segments[0]["start_turn"] == 1 and res.segments[0]["end_turn"] == 10
assert res.segments[0]["covered_uuids"] == [f"u{i}" for i in range(1, 11)]
assert res.segments[1]["covered_uuids"] == [f"u{i}" for i in range(11, 21)]
ok("agent 切两段,tag 区分,回合区间精确回映 covered_uuids")

# ---- 门 2:单弧线 → 单段;渐变漂移 → 最弱弯折;short ----
# 默认 auto_segment:<4 回合单段且 short
res_s = run_chunk(mk_ct(3), FakeChatProvider(), model="sonnet", timeout=10, max_retries=0)
assert len(res_s.segments) == 1 and res_s.segments[0]["short"] is True
assert "单弧线" in res_s.segments[0]["cut_reason"]
# >=4 回合:切两段,前段标"渐变漂移:此处为最弱弯折"
res_d = run_chunk(mk_ct(8), FakeChatProvider(), model="sonnet", timeout=10, max_retries=0)
assert len(res_d.segments) == 2
assert "渐变漂移" in res_d.segments[0]["cut_reason"]
ok("单弧线→单段且 short;渐变漂移→最弱弯折(两段)")

# ---- 门 3:超大输入 → OversizedError,不静默截断 ----
big = mk_ct(3)
big.turns[0].human_text = "x" * (MAX_CHARS + 100)
raised = False
try:
    run_chunk(big, FakeChatProvider(), model="sonnet", timeout=10, max_retries=0)
except OversizedError as e:
    raised = True
    assert e.chars > MAX_CHARS and "粗分" in str(e)
assert raised, "超大未报错(静默截断风险)"
ok("超大输入抛 OversizedError,带人工粗分出口,不静默截断")

# ---- 门 4:失败可重试(首败后成);屡败 → ChunkFailed + retry 列表 ----
# 首次超时、次次坏 JSON、第三次成功 → attempts==3
prov_retry = FakeChatProvider(behaviors=[ChatTimeout("超时"), "不是 JSON", seg_json])
res_r = run_chunk(mk_ct(20), prov_retry, model="sonnet", timeout=10, max_retries=2)
assert res_r.attempts == 3, res_r.attempts
ok("失败可重试:超时→坏JSON→成功,attempts=3")

# 屡败:全失败 → ChunkFailed(errors 记满),append_retry 落盘可读回
prov_fail = FakeChatProvider(behaviors=[ChatError("e1"), ChatError("e2"), ChatError("e3")])
failed = False
try:
    run_chunk(mk_ct(20), prov_fail, model="sonnet", timeout=10, max_retries=2)
except ChunkFailed as e:
    failed = True
    assert len(e.errors) == 3
    segments_store.append_retry(CFG.chunks_dir, "sess-fail", "/fake/sess-fail.jsonl", 1.0,
                                provider="fake", model="sonnet", error=str(e))
assert failed
doc_fail = segments_store.load(CFG.chunks_dir, "sess-fail")
assert doc_fail and len(doc_fail["retry"]) == 1
ok("屡败抛 ChunkFailed(errors 全记)且进 retry 列表持久")

# ---- 门 5:人工改段(并/分/移边界)持久;落盘重载一致 ----
ct2 = mk_ct(30)
umap = uuids_by_turn(ct2)
init = manual_segments(ct2, [(1, 10), (11, 20), (21, 30)])
doc = segments_store.save_full(CFG.chunks_dir, "sess-edit", "/fake/sess-edit.jsonl", 9.0, init)
ids = [s["seg_id"] for s in doc["segments"]]
assert all(ids), "seg_id 未分配"

# 并段:前两段合并
merged = segments_store.merge(doc["segments"], [ids[0], ids[1]], umap)
assert len(merged) == 2 and merged[0]["start_turn"] == 1 and merged[0]["end_turn"] == 20
assert merged[0]["covered_uuids"] == [f"u{i}" for i in range(1, 21)]
# 分段:把合并后的大段在回合 5 处切开
split = segments_store.split(merged, merged[0]["seg_id"], 5, umap)
# 大段无 seg_id(被切),重新保存分配
doc2 = segments_store.save_full(CFG.chunks_dir, "sess-edit", "/fake/sess-edit.jsonl", 9.0, split)
ranges = sorted([(s["start_turn"], s["end_turn"]) for s in doc2["segments"]])
assert ranges == [(1, 5), (6, 20), (21, 30)], ranges
# 移边界:把最后一段尾巴缩到 28
last_id = next(s["seg_id"] for s in doc2["segments"] if s["start_turn"] == 21)
moved = segments_store.set_boundary(doc2["segments"], last_id, 21, 28, umap)
doc3 = segments_store.save_full(CFG.chunks_dir, "sess-edit", "/fake/sess-edit.jsonl", 9.0, moved)
# 落盘重载一致
reloaded = segments_store.load(CFG.chunks_dir, "sess-edit")
rr = sorted([(s["start_turn"], s["end_turn"]) for s in reloaded["segments"]])
assert rr == [(1, 5), (6, 20), (21, 28)], rr
moved_last = next(s for s in reloaded["segments"] if s["start_turn"] == 21)
assert moved_last["covered_uuids"] == [f"u{i}" for i in range(21, 29)]
assert moved_last["origin"] == "edited"
ok("并/分/移边界生效,落盘重载逐字段一致,covered_uuids 随之重算")

# ---- 门 6:uuid 不上台面(UI 序列化剥除 covered_uuids)----
ui = _ui_segment(reloaded["segments"][0])
assert "covered_uuids" not in ui and "start_turn" in ui
ok("送前端的段剥除 covered_uuids(uuid 不上台面)")

print("S3 引擎层 ALL PASS ✅")
