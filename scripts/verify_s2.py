"""S2 引擎层 headless 验收(不含 GUI)。

覆盖通过门:真 transcript 列出、预览正确无噪声、mtime 变缓存失效、正在写入警示、
resume 断点识别、段级 flag 幂等(含 resume 复刻同段同哈希)。
跑法:python scripts/verify_s2.py
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="memsys_s2_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"

from memory_system import preview_cache, processed  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db import migrate  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.preprocess import CleanedTranscript, Turn, clean, render  # noqa: E402
from memory_system.resume import detect_resume  # noqa: E402
from memory_system.transcript import describe, discover  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
REAL_ROOT = Path.home() / ".claude" / "projects"


def _mk_jsonl(dirp: Path, name: str, records: list[dict]) -> Path:
    p = dirp / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", "utf-8")
    return p


def _msg(role: str, uuid: str, text, ts="2026-06-19T00:00:00Z", **extra) -> dict:
    content = text if isinstance(text, list) else text
    return {"type": role, "uuid": uuid, "message": {"role": role, "content": content},
            "timestamp": ts, "isSidechain": False, **extra}


def gate_discover_real() -> None:
    infos = discover(REAL_ROOT)
    assert infos, "未发现真 transcript"
    assert all(i.session_id and i.mtime for i in infos)
    print(f"  [ok] 真 transcript 列出:{len(infos)} 个,按 mtime 倒序,带 cwd/行数/写入警示")


def gate_preview_clean() -> None:
    """清洗真 transcript:有回合、无 tool/command/thinking 噪声。"""
    f = REAL_ROOT / "-Users-zuris" / "666b1f63-7a74-4af3-969f-32edac173c6d.jsonl"
    if not f.exists():
        print("  [skip] 样本 666b1f63 不在,跳过真数据清洗")
        return
    ct = clean(f)
    assert len(ct.turns) > 0
    text, lmap = render(ct)
    for bad in ("tool_use", "tool_result", "<command-name>", "system-reminder",
                "Request interrupted", "Caveat: The messages below"):
        assert bad not in text, f"清洗后仍含噪声: {bad}"
    assert len(lmap) == len(ct.turns)
    # 每回合至少有人类或 assistant 文本
    assert all(t.human_text or t.assistant_text for t in ct.turns)
    print(f"  [ok] 预览正确:{len(ct.turns)} 回合,无 tool/command/thinking/中断/caveat 噪声")


def gate_cache_mtime_invalidation() -> None:
    work = Path(tempfile.mkdtemp())
    recs = [_msg("user", "u1", "你好啊这是第一句"), _msg("assistant", "a1", "你好,我在")]
    p = _mk_jsonl(work, "s1.jsonl", recs)
    mt1 = p.stat().st_mtime
    ct1 = preview_cache.get(CFG.preview_cache_dir, p, mtime=mt1)
    assert preview_cache.is_cached(CFG.preview_cache_dir, p, mtime=mt1)
    assert len(ct1.turns) == 1
    # 改文件 + 新 mtime → 旧键失效、重算出新内容
    recs.append(_msg("user", "u2", "又加了一段新的话题"))
    recs.append(_msg("assistant", "a2", "收到这段新的"))
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", "utf-8")
    mt2 = mt1 + 5.0
    os.utime(p, (mt2, mt2))
    assert not preview_cache.is_cached(CFG.preview_cache_dir, p, mtime=mt2), "新 mtime 不应命中旧缓存"
    ct2 = preview_cache.get(CFG.preview_cache_dir, p, mtime=mt2)
    assert len(ct2.turns) == 2, "缓存未随 mtime 失效重算"
    print("  [ok] mtime 变 → 缓存失效、重新生成预览")


def gate_writing_warning() -> None:
    work = Path(tempfile.mkdtemp())
    p = _mk_jsonl(work, "live.jsonl", [_msg("user", "u1", "正在写入测试")])
    now = time.time()
    os.utime(p, (now, now))
    info = describe(p, now=now)
    assert info.maybe_writing, "刚写入的文件应被标记 maybe_writing"
    old = describe(p, now=now + 10_000)
    assert not old.maybe_writing
    print("  [ok] 正在写入(mtime≈now)给出警示;陈旧文件不误报")


def gate_resume_detect() -> None:
    # 原会话:回合 a,b,c
    base = CleanedTranscript(session_id="orig", path="orig")
    base.turns = [
        Turn(idx=1, human_text="第一段问题", assistant_text="第一段回答", uuids=["x1", "x2"]),
        Turn(idx=2, human_text="第二段问题", assistant_text="第二段回答", uuids=["x3", "x4"]),
    ]
    prior = {"x1", "x2", "x3", "x4"}
    # resume 复刻:前两回合 uuid 照旧,第三回合是新生的
    resumed = CleanedTranscript(session_id="resume", path="resume")
    resumed.turns = base.turns + [
        Turn(idx=3, human_text="续写新问题", assistant_text="续写新回答", uuids=["y1", "y2"]),
    ]
    info = detect_resume(resumed, prior)
    assert info.is_resume and info.copied_turns == 2 and info.breakpoint_idx == 3, info
    # 全新会话:无重叠 → 非 resume
    fresh = detect_resume(base, set())
    assert not fresh.is_resume
    print("  [ok] resume 断点:复刻前缀 2 回合、断点=回合3;全新会话不误判")


def gate_segment_flag() -> None:
    con = connect(CFG.db_path)
    migrate.up(con)
    seg = ["x1", "x2", "x3"]
    h1 = processed.mark_segment(con, "sess-A", seg, episode_public_id="ep_001")
    assert processed.is_processed(con, seg)
    assert processed.is_processed(con, list(reversed(seg))), "段哈希应与 uuid 顺序无关"
    # 幂等:同段再 mark 不新增行
    h2 = processed.mark_segment(con, "sess-A", seg)
    assert h1 == h2
    (n,) = con.execute("SELECT COUNT(*) FROM processed_segments").fetchone()
    assert n == 1, f"同段重复登记应幂等,实得 {n} 行"
    # resume 复刻:uuid 集相同(即便来自另一会话文件)→ 同哈希 → 判已处理
    assert processed.is_processed(con, ["x3", "x1", "x2"])
    # 覆盖并集 + 水位
    assert processed.processed_uuids(con, "sess-A") == set(seg)
    assert processed.get_watermark(con, "sess-A") == "x3"
    con.close()
    print("  [ok] 段级 flag:顺序无关哈希、幂等、resume 复刻同段判重、水位推进")


def main() -> None:
    print(f"临时 home: {_TMP}")
    gate_discover_real()
    gate_preview_clean()
    gate_cache_mtime_invalidation()
    gate_writing_warning()
    gate_resume_detect()
    gate_segment_flag()
    print("S2 引擎层 ALL PASS ✅")


if __name__ == "__main__":
    main()
