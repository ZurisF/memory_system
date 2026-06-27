"""S4 提取层 headless 验收(不含 GUI;fake provider 离线)。

覆盖通过门(phase1_build §S4):
- 一段提取五件套,字段符合契约(overview/summary 非空、highlights 形态、salience 合理)。
- node 三选一:命中→label;近义→label+new_alias;新概念→new。
- highlights 逐字、0–3、宁缺毋滥(无则 [])。
- 5 切 3 成 2 坏 → 3 进 staging、2 进 retry、好块照常可读。
- 重试坏块能恢复;多次失败有人工提醒(ExtractFailed)。
- 严校挡坏五件套:空 overview、salience 越界、highlights>3、node action 非法 → 触发重试/失败。
- staging 落盘往返一致;uuid(covered_uuids)不上台面(UI 剥除)。
- source_text 按回合区间渲染、无回合号脚手架。
跑法:python scripts/verify_s4.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="memsys_s4_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"

from memory_system import staging_store  # noqa: E402
from memory_system.agent.base import ChatError, ChatTimeout  # noqa: E402
from memory_system.agent.fake import FakeChatProvider, make_extraction  # noqa: E402
from memory_system.chunk import manual_segments  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.extract import (  # noqa: E402
    ExtractFailed,
    existing_nodes,
    extract_segments,
    render_existing_nodes,
    run_extract,
)
from memory_system.fragments import Node, write_node  # noqa: E402
from memory_system.preprocess import CleanedTranscript, Turn, render_source_text  # noqa: E402
from memory_system.ui_shape import ui_episode as _ui_episode  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
print(f"临时 home: {_TMP}")


def mk_ct(n: int, session="sess-x") -> CleanedTranscript:
    ct = CleanedTranscript(session_id=session, path=f"/fake/{session}.jsonl")
    for i in range(1, n + 1):
        ct.turns.append(Turn(idx=i, human_text=f"人类第{i}句", assistant_text=f"Claude第{i}句",
                             uuids=[f"u{i}"], human_uuid=f"u{i}"))
    return ct


def ok(msg):
    print(f"  [ok] {msg}")


TIMEOUT = 10

# ---- 门 0:source_text 按回合区间渲染,无回合号脚手架 ----
ct = mk_ct(6)
src = render_source_text(ct, 2, 4)
assert "【回合" not in src, "source_text 不应含回合号脚手架"
assert "人类第2句" in src and "Claude第4句" in src
assert "人类第1句" not in src and "人类第5句" not in src, "越界回合不应入选"
assert src.count("---") == 2, "三回合应有两处 --- 分隔"
ok("source_text 按回合区间渲染,无回合号脚手架,边界精确")

# ---- 门 1:一段提取五件套,字段符合契约 ----
good = make_extraction(
    overview="zuris Claude 测试 overview。",
    summary="zuris 与 Claude 的一段对话,起承转合俱全。",
    highlights=[{"text": "记忆应该写给你自己,而不是写给我", "tag": "关键定义"}],
    nodes=[{"label": "弥赛亚", "action": "new", "reason": "核心概念"}],
    salience_tier=3, salience_reason="高情感密度",
)
res = run_extract(src, [], FakeChatProvider(behaviors=[good]),
                  model="opus", timeout=TIMEOUT, max_retries=2)
assert res.overview and res.summary
assert res.salience_tier == 3
assert len(res.highlights) == 1 and res.highlights[0]["tag"] == "关键定义"
assert res.model == "opus"
ok("一段提取出五件套,overview/summary 非空、salience 合理、字段齐")

# ---- 门 2:node 三选一(命中 / 别名 / 新建)----
three = make_extraction(
    overview="ov", summary="su",
    nodes=[
        {"label": "Solaris", "action": "match_existing", "reason": "已有概念"},
        {"label": "记忆系统", "action": "add_alias", "new_alias": "记忆库", "reason": "别名"},
        {"label": "blaspheme", "action": "new", "reason": "新概念"},
    ],
    salience_tier=2,
)
res2 = run_extract("x", [], FakeChatProvider(behaviors=[three]),
                   model="opus", timeout=TIMEOUT, max_retries=0)
by_label = {n["label"]: n for n in res2.nodes}
assert by_label["Solaris"]["action"] == "match_existing" and "new_alias" not in by_label["Solaris"]
assert by_label["记忆系统"]["action"] == "add_alias" and by_label["记忆系统"]["new_alias"] == "记忆库"
assert by_label["blaspheme"]["action"] == "new"
ok("node 三选一正确:命中→label;近义→label+new_alias;新概念→new")

# add_alias 缺 new_alias → 严校拒(触发重试,屡败 ExtractFailed)
bad_alias = make_extraction("ov", "su", nodes=[{"label": "x", "action": "add_alias", "reason": "r"}])
try:
    run_extract("x", [], FakeChatProvider(behaviors=[bad_alias]), model="opus", timeout=TIMEOUT, max_retries=0)
    raise AssertionError("add_alias 缺 new_alias 未被拒")
except ExtractFailed:
    pass
ok("add_alias 缺 new_alias 被严校拒(不静默放行)")

# ---- 门 3:highlights 逐字、0–3、宁缺毋滥 ----
empty_hl = make_extraction("ov", "su", highlights=[])
res3 = run_extract("x", [], FakeChatProvider(behaviors=[empty_hl]), model="opus", timeout=TIMEOUT, max_retries=0)
assert res3.highlights == [], "无高光应为空列表"
# >3 条 → 严校拒
four = make_extraction("ov", "su", highlights=[{"text": f"句{i}", "tag": "情绪锚"} for i in range(4)])
try:
    run_extract("x", [], FakeChatProvider(behaviors=[four]), model="opus", timeout=TIMEOUT, max_retries=0)
    raise AssertionError("highlights>3 未被拒")
except ExtractFailed:
    pass
# text 逐字保留(含特殊符号不被改写)
verbatim = "记忆应该写给你自己,而不是写给我 | 带管道符"
hl1 = make_extraction("ov", "su", highlights=[{"text": verbatim, "tag": "关键定义"}])
res3b = run_extract("x", [], FakeChatProvider(behaviors=[hl1]), model="opus", timeout=TIMEOUT, max_retries=0)
assert res3b.highlights[0]["text"] == verbatim, "highlight text 必须逐字保留"
ok("highlights 逐字保留、空则 []、>3 被拒(宁缺毋滥)")

# ---- 门 4:严校挡坏五件套(空 overview / salience 越界 / node action 非法)----
for bad, why in [
    (make_extraction("", "su"), "空 overview"),
    (make_extraction("ov", ""), "空 summary"),
    (make_extraction("ov", "su", salience_tier=9), "salience 越界"),
    (make_extraction("ov", "su", nodes=[{"label": "x", "action": "foo", "reason": "r"}]), "action 非法"),
    (make_extraction("ov", "su", nodes=[{"label": "坏\nlabel", "action": "new", "reason": "r"}]), "label 含换行"),
]:
    try:
        run_extract("x", [], FakeChatProvider(behaviors=[bad]), model="opus", timeout=TIMEOUT, max_retries=0)
        raise AssertionError(f"坏五件套未被拒: {why}")
    except ExtractFailed:
        pass
ok("严校挡坏五件套:空 overview/summary、salience 越界、action 非法、label 含换行 全拒")

# ---- 门 5:重试(首败后成);屡败 → ExtractFailed ----
prov_retry = FakeChatProvider(behaviors=[ChatTimeout("超时"), "不是 JSON", good])
res5 = run_extract(src, [], prov_retry, model="opus", timeout=TIMEOUT, max_retries=2)
assert res5.attempts == 3, res5.attempts
try:
    run_extract(src, [], FakeChatProvider(behaviors=[ChatError("e1"), ChatError("e2"), ChatError("e3")]),
                model="opus", timeout=TIMEOUT, max_retries=2)
    raise AssertionError("屡败未抛 ExtractFailed")
except ExtractFailed as e:
    assert len(e.errors) == 3
ok("失败可重试:超时→坏JSON→成功 attempts=3;屡败抛 ExtractFailed(errors 全记)")

# ---- 门 6:5 切 3 成 2 坏 → 3 进 staging、2 进 retry、好块可读 ----
ct5 = mk_ct(50, session="sess-batch")
segs = manual_segments(ct5, [(1, 10), (11, 20), (21, 30), (31, 40), (41, 50)])
for i, s in enumerate(segs, 1):
    s["seg_id"] = f"s{i}"
# 段 2、4 注定坏(空 overview),其余成功:按段顺序排好行为脚本
behaviors = []
for i in range(1, 6):
    if i in (2, 4):
        behaviors.append(make_extraction("", "坏段"))          # max_retries=0 → 一次即败
    else:
        behaviors.append(make_extraction(f"ov{i}", f"su{i}",
                                         nodes=[{"label": f"n{i}", "action": "new", "reason": "r"}],
                                         salience_tier=1))
prov_batch = FakeChatProvider(behaviors=behaviors)
batch = extract_segments(ct5, segs, prov_batch, [], model="opus", timeout=TIMEOUT, max_retries=0)
assert len(batch.staged) == 3, [s[0]["seg_id"] for s in batch.staged]
assert len(batch.failed) == 2, [s[0]["seg_id"] for s in batch.failed]
staged_ids = {s[0]["seg_id"] for s in batch.staged}
failed_ids = {s[0]["seg_id"] for s in batch.failed}
assert staged_ids == {"s1", "s3", "s5"} and failed_ids == {"s2", "s4"}
# 落 staging + retry
for seg, r, srctext in batch.staged:
    staging_store.upsert_episode(CFG.staging_episodes_dir, ct5.session_id, ct5.path, seg, r, srctext)
for seg, errs in batch.failed:
    staging_store.append_retry(CFG.staging_episodes_dir, ct5.session_id, ct5.path, seg,
                               provider="fake", model="opus", errors=errs)
ok("按块回滚:5 段 3 成 2 坏 → 3 进 staging、2 进 retry,坏块不拖好块")

# ---- 门 7:staging 落盘往返一致;好块可读;source_text 落地 ----
doc = staging_store.load(CFG.staging_episodes_dir, ct5.session_id)
assert doc and len(doc["episodes"]) == 3 and len(doc["retry"]) == 2
ep1 = next(e for e in doc["episodes"] if e["seg_id"] == "s1")
assert ep1["status"] == "staging" and ep1["overview"] == "ov1"
assert ep1["start_turn"] == 1 and ep1["end_turn"] == 10
assert "[我]: 人类第1句" in ep1["source_text"] and "Claude第10句" in ep1["source_text"]
assert ep1["covered_uuids"] == [f"u{i}" for i in range(1, 11)]
# episodes 按回合排序
assert [e["start_turn"] for e in doc["episodes"]] == [1, 21, 41]
ok("staging 落盘往返逐字段一致,source_text/covered_uuids 落地,好块可读")

# 重提取坏块恢复:s2 重跑成功 → 进 staging、retry 清掉该段
res_fix = run_extract(render_source_text(ct5, 11, 20), [], FakeChatProvider(behaviors=[make_extraction("ov2", "su2")]),
                      model="opus", timeout=TIMEOUT, max_retries=0)
seg2 = next(s for s in segs if s["seg_id"] == "s2")
staging_store.upsert_episode(CFG.staging_episodes_dir, ct5.session_id, ct5.path, seg2, res_fix,
                             render_source_text(ct5, 11, 20))
doc2 = staging_store.load(CFG.staging_episodes_dir, ct5.session_id)
assert len(doc2["episodes"]) == 4 and {e["seg_id"] for e in doc2["episodes"]} >= {"s1", "s2", "s3", "s5"}
assert all(r["seg_id"] != "s2" for r in doc2["retry"]), "成功后该段 retry 应清掉"
assert len(doc2["retry"]) == 1 and doc2["retry"][0]["seg_id"] == "s4"
ok("重试坏块恢复:s2 重提取进 staging,其 retry 记录被清,s4 仍挂 retry")

# ---- 门 8:uuid 不上台面(UI 剥 covered_uuids,source_text 保留)----
ui = _ui_episode(ep1)
assert "covered_uuids" not in ui, "covered_uuids 不得上台面"
assert "source_text" in ui and "overview" in ui, "source_text/五件套应送前端"
ok("送前端的 staging episode 剥除 covered_uuids,source_text 保留(供 S5 审核)")

# ---- 门 9:existing_nodes 读 active node 碎片喂三选一 ----
assert existing_nodes(CFG.nodes_dir) == [], "S4 初始无 active node"
write_node(CFG.nodes_dir, Node(label="Solaris", created_at="t", updated_at="t", aliases=["索拉里斯"]))
nodes = existing_nodes(CFG.nodes_dir)
assert len(nodes) == 1 and nodes[0]["label"] == "Solaris" and "索拉里斯" in nodes[0]["aliases"]
assert "Solaris" in render_existing_nodes(nodes) and "索拉里斯" in render_existing_nodes(nodes)
ok("existing_nodes 读 active node 碎片(label+aliases),喂提取三选一")

print("S4 提取层 ALL PASS ✅")
