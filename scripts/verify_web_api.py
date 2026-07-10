"""Web API smoke test for the ingest/staging GUI contract.

Focus: S5 staging review endpoints must work with session_id alone, so already
extracted episodes remain editable/rejectable/confirmable after the source jsonl
has been cleaned up.
Run: python scripts/verify_web_api.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request
from urllib.error import HTTPError
from urllib.parse import quote

_TMP = tempfile.mkdtemp(prefix="memsys_webapi_")
_ROOT = Path(_TMP) / "transcripts"
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_TRANSCRIPTS_ROOT"] = str(_ROOT)
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"

from memory_system import prompt_store  # noqa: E402
from memory_system.chunk import load_chunk_prompt  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db import migrate  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.fragments import Episode, Node, write_episode, write_node  # noqa: E402
from memory_system.index import rebuild  # noqa: E402
from memory_system.server import make_handler  # noqa: E402


CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
_ROOT.mkdir(parents=True, exist_ok=True)


def _row(kind: str, uuid: str, content, ts: str) -> dict:
    role = "user" if kind == "user" else "assistant"
    return {
        "type": kind,
        "uuid": uuid,
        "timestamp": ts,
        "isSidechain": False,
        "message": {"role": role, "content": content},
    }


def _mk_transcript() -> Path:
    p = _ROOT / "sess-web.jsonl"
    rows = []
    for i in range(1, 5):
        rows.append(_row("user", f"u{i}", f"人类第{i}句", f"2026-06-19T22:{i:02d}:00Z"))
        rows.append(_row("assistant", f"a{i}", [{"type": "text", "text": f"Claude第{i}句"}],
                         f"2026-06-19T22:{i:02d}:10Z"))
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")
    return p


def _post(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_status(base: str, path: str, body: dict) -> tuple[int, dict]:
    """POST 并返回 (status, body),用于断言 4xx(如删段 409 needs_confirm)。"""
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def main() -> None:
    print(f"临时 home: {_TMP}")
    con = connect(CFG.db_path)
    try:
        migrate.up(con)
    finally:
        con.close()

    src = _mk_transcript()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(CFG))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        transcript = _get(base, "/api/transcript?path=" + quote(str(src)))
        expected_turn_keys = {
            "idx", "human_text", "assistant_text", "msg_count", "processed"}
        assert transcript["turns"] and all(
            set(turn) == expected_turn_keys for turn in transcript["turns"]), transcript
        assert all("uuids" not in turn for turn in transcript["turns"]), transcript
        ok("GET /api/transcript:回合载荷不含 uuids,其余字段契约不变")

        selected = _post(base, "/api/select", {"path": str(src), "turn_idxs": [1]})
        assert selected.get("ok") and selected["turns"] == [1], selected
        transcript_after = _get(base, "/api/transcript?path=" + quote(str(src)))
        assert transcript_after["turns"][0]["processed"] is True, transcript_after
        assert transcript_after["turns"][1]["processed"] is False, transcript_after
        ok("POST /api/select:只传 turn_idxs 仍可在服务端映射 uuid 并回显 processed")

        segs = [
            {"start_turn": 1, "end_turn": 2, "tag": "前半", "cut_reason": "测试", "short": True,
             "deletions": [], "origin": "manual"},
            {"start_turn": 3, "end_turn": 4, "tag": "后半", "cut_reason": "测试", "short": True,
             "deletions": [], "origin": "manual"},
        ]
        saved = _post(base, "/api/segments", {"path": str(src), "segments": segs})
        assert saved.get("ok") and [s["seg_id"] for s in saved["segments"]] == ["s1", "s2"], saved
        ok("/api/segments 保存并分配 seg_id")

        ext = _post(base, "/api/extract", {"path": str(src), "seg_ids": ["s1", "s2"]})
        assert ext.get("ok") and ext["staged"] == 2 and len(ext["episodes"]) == 2, ext
        ok("/api/extract(fake) 生成 staging episodes")

        all_before = _get(base, "/api/staging/all")
        sess = next(s for s in all_before["sessions"] if s["session_id"] == "sess-web")
        assert sess["source_exists"] is True and len(sess["episodes"]) == 2, sess

        src.unlink()
        all_after = _get(base, "/api/staging/all")
        sess = next(s for s in all_after["sessions"] if s["session_id"] == "sess-web")
        assert sess["source_exists"] is False and len(sess["episodes"]) == 2, sess
        ok("/api/staging/all 保留源文件已清的 staging 会话")

        edited = _post(base, "/api/staging/edit",
                       {"session_id": "sess-web", "stage_id": "e1",
                        "fields": {"overview": "session-id 编辑后的 overview"}})
        # 按 stage_id 找条目:episodes 顺序随并发提取的完成序漂移([0] 会间歇性押错)
        e1 = next(e for e in edited["episodes"] if e["stage_id"] == "e1")
        assert edited.get("ok") and e1["overview"] == "session-id 编辑后的 overview", edited
        ok("/api/staging/edit 支持 session_id,不依赖源 jsonl")

        rejected = _post(base, "/api/reject",
                         {"session_id": "sess-web", "stage_id": "e2", "reason": "测试拒绝"})
        assert rejected.get("ok") and [e["stage_id"] for e in rejected["episodes"]] == ["e1"], rejected
        ok("/api/reject 支持 session_id,不依赖源 jsonl")

        confirmed = _post(base, "/api/confirm", {"session_id": "sess-web", "stage_id": "e1"})
        assert confirmed.get("ok") and confirmed["public_id"].startswith("ep_"), confirmed
        assert confirmed["episodes"] == [], confirmed
        ok("/api/confirm 支持 session_id,源 jsonl 已清仍可入库")

        # ---- 删段回归门:段↔episode 解耦 + 已提取段不带 force 回 409 ----
        src2 = _ROOT / "sess-del.jsonl"
        rows2 = []
        for i in range(1, 5):
            rows2.append(_row("user", f"x{i}", f"删段人类第{i}句", f"2026-06-20T10:{i:02d}:00Z"))
            rows2.append(_row("assistant", f"y{i}", [{"type": "text", "text": f"删段Claude第{i}句"}],
                              f"2026-06-20T10:{i:02d}:10Z"))
        src2.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows2) + "\n", "utf-8")
        dsegs = [
            {"start_turn": 1, "end_turn": 2, "tag": "前", "cut_reason": "t", "short": True,
             "deletions": [], "origin": "manual"},
            {"start_turn": 3, "end_turn": 4, "tag": "后", "cut_reason": "t", "short": True,
             "deletions": [], "origin": "manual"},
        ]
        ds = _post(base, "/api/segments", {"path": str(src2), "segments": dsegs})
        assert ds.get("ok") and [s["seg_id"] for s in ds["segments"]] == ["s1", "s2"], ds
        # 只提取 s1 → e1;s2 保持未提取
        de = _post(base, "/api/extract", {"path": str(src2), "seg_ids": ["s1"]})
        assert de.get("ok") and de["staged"] == 1, de

        # (1) 无 episode 的段 s2:干净删
        d1 = _post(base, "/api/segments/delete", {"session_id": "sess-del", "seg_ids": ["s2"]})
        assert d1.get("ok") and d1["deleted"] == 1 and d1.get("staged") == [], d1
        ok("删段:无 episode 的段干净删")

        # (2) 有 episode 的段 s1 不带 force → 409 needs_confirm,且段未删
        st, d2 = _post_status(base, "/api/segments/delete",
                              {"session_id": "sess-del", "seg_ids": ["s1"]})
        assert st == 409 and d2.get("needs_confirm") and d2.get("staged") == ["s1"], (st, d2)
        segs_now = _get(base, "/api/segments?path=" + quote(str(src2)))
        assert [s["seg_id"] for s in segs_now["segments"]] == ["s1"], segs_now
        ok("删段:已提取段不带 force 回 409,段未删")

        # (3) force 删成功,且已提取 episode e1 不受影响
        st, d3 = _post_status(base, "/api/segments/delete",
                              {"session_id": "sess-del", "seg_ids": ["s1"], "force": True})
        assert st == 200 and d3.get("ok") and d3["deleted"] == 1, (st, d3)
        sess_del = next(s for s in _get(base, "/api/staging/all")["sessions"]
                        if s["session_id"] == "sess-del")
        assert [e["stage_id"] for e in sess_del["episodes"]] == ["e1"], \
            "force 删段绝不影响已提取 episode"
        ok("删段:force 删成功,已提取 episode 不受影响")

        # (4) staging delete:干净撤 e1
        d4 = _post(base, "/api/staging/delete", {"session_id": "sess-del", "stage_id": "e1"})
        assert d4.get("ok"), d4
        left = [s for s in _get(base, "/api/staging/all")["sessions"]
                if s["session_id"] == "sess-del"]
        assert not left or all(e["stage_id"] != "e1" for e in left[0]["episodes"]), \
            "staging delete 应撤掉 e1"
        ok("删 staging:干净撤掉条目")

        # ---- 过程 Prompt API 门 ----
        # prompt 正本在 git 仓库的包目录(非临时 home);测试改动后必须在 finally 回写原文。
        orig_prompts = {p["name"]: p["content"] for p in prompt_store.list_prompts()}
        EXPECT_KEYS = {"chunk_system", "extract_system", "recall_episode_system",
                       "recall_concept_system", "opening_system", "tool_episode_desc",
                       "tool_detail_desc", "tool_concept_desc"}

        listed = _get(base, "/api/prompts")
        got_keys = {p["name"] for p in listed["prompts"]}
        assert got_keys == EXPECT_KEYS, got_keys
        assert all(p["content"].strip() for p in listed["prompts"]), "八个 prompt 内容不应为空"
        assert {p["process"] for p in listed["prompts"]} == {
            "chunk", "extract", "recall", "mcp"
        }, listed
        assert listed["process_labels"]["mcp"] == "选路", listed
        assert all(
            p["process_label"] == listed["process_labels"][p["process"]]
            for p in listed["prompts"]
        ), listed
        ok("GET /api/prompts:八键齐全,含 mcp/选路 标签与非空内容")

        # 缓存即时生效证明:先触发 chunk prompt 的 lru_cache(载入原值),POST 新值后应立即读到新值
        cached_before = load_chunk_prompt()
        assert cached_before == orig_prompts["chunk_system"], "缓存应先载入原值"
        new_body = orig_prompts["chunk_system"] + "\n# verify_web_api 临时追加行\n"
        saved_p = _post(base, "/api/prompts", {"name": "chunk_system", "content": new_body})
        assert saved_p.get("ok"), saved_p
        roundtrip = next(p for p in saved_p["prompts"] if p["name"] == "chunk_system")
        assert roundtrip["content"] == new_body, "POST 后回读内容应一致"
        assert _get(base, "/api/prompts")
        assert load_chunk_prompt() == new_body, "写回后 lru_cache 应已失效并读到新值(即时生效)"
        ok("POST /api/prompts:改一个再读回一致,且 lru_cache 已即时刷新")

        # MCP 选路描述也走同一个白名单与 tmp+os.replace 原子写;POST 后文件与 API 立即可见。
        desc_path = Path(prompt_store.__file__).resolve().parent / "prompts" / "tool_episode_desc.txt"
        inode_before = desc_path.stat().st_ino
        new_desc = orig_prompts["tool_episode_desc"].rstrip("\n") + \
            "\nverify_web_api 原子写标记\n"
        saved_desc = _post(base, "/api/prompts", {
            "name": "tool_episode_desc", "content": new_desc
        })
        assert saved_desc.get("ok"), saved_desc
        desc_roundtrip = next(
            p for p in saved_desc["prompts"] if p["name"] == "tool_episode_desc"
        )
        assert desc_roundtrip["content"] == new_desc
        assert desc_path.read_text(encoding="utf-8") == new_desc
        assert desc_path.stat().st_ino != inode_before, "tmp+os.replace 应以新 inode 原子替换正本"
        assert not desc_path.with_suffix(desc_path.suffix + ".tmp").exists()
        ok("POST /api/prompts:选路描述原子写生效,无残留 tmp")

        # 白名单外的 name → 400(堵越权 / 路径穿越)
        st_bad, bad = _post_status(base, "/api/prompts",
                                   {"name": "../../etc/passwd", "content": "x"})
        assert st_bad == 400 and "error" in bad, (st_bad, bad)
        st_bad2, bad2 = _post_status(base, "/api/prompts",
                                     {"name": "nonexistent_prompt", "content": "x"})
        assert st_bad2 == 400, (st_bad2, bad2)
        ok("POST /api/prompts:白名单外 name 回 400")

        # 空 content(strip 后为空)→ 400
        st_empty, empty = _post_status(base, "/api/prompts",
                                       {"name": "extract_system", "content": "   \n\t "})
        assert st_empty == 400 and "error" in empty, (st_empty, empty)
        # 400 拒绝后正本不应被清空
        still = next(p for p in _get(base, "/api/prompts")["prompts"] if p["name"] == "extract_system")
        assert still["content"] == orig_prompts["extract_system"], "空 content 被拒后正本不应改变"
        ok("POST /api/prompts:空 content 回 400,正本不被污染")
    finally:
        # 无论断言是否通过,回写 prompt 正本原文(正本在仓库内,绝不许被测试污染)
        try:
            for _name, _content in orig_prompts.items():
                prompt_store.write_prompt(_name, _content)
        except NameError:
            pass  # 尚未采集到原文(前置断言更早失败),无需回写
        httpd.shutdown()
        httpd.server_close()

    print("Web API staging contract ALL PASS ✅")


def verify_recall_api() -> None:
    """POST /api/recall 门(S6 三路检索 + 可选重构,fake embedding/chat 离线)。

    独立临时 home + 独立 CFG + 独立 httpd 实例:与 main() 的 ingest/staging 语料互不干扰
    (main() 库里已有 fake embedding 造出的其他 episode,混一个库断言 top-1 会被非语义碰撞污染)。
    """
    tmp2 = tempfile.mkdtemp(prefix="memsys_recallapi_")
    os.environ["MEMORY_SYSTEM_HOME"] = tmp2
    cfg2 = load_config()
    for d in cfg2.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    print(f"临时 home(recall): {tmp2}")

    con = connect(cfg2.db_path)
    try:
        migrate.up(con)
    finally:
        con.close()

    # 造一条 active episode(挂一个 node)+ rebuild(真实 fake 向量/FTS/膜),供三路检索测。
    ep_r1 = Episode(
        public_id="ep_rcep0001", overview="召回屏门测试概览", summary="召回屏门测试摘要",
        source_text="召回屏门测试的原文,包含独有短语『召回门专用短语』。",
        salience_tier=2, status="active", created_at="2026-06-01T09:00:00+00:00",
        activated_at="2026-06-01T09:00:00+00:00", nodes=["召回门概念"])
    write_episode(cfg2.episodes_dir, ep_r1)
    write_node(cfg2.nodes_dir, Node(label="召回门概念", type="concept",
                                    created_at="t0", updated_at="t0"))
    rrep = rebuild(cfg2, FakeProvider(model="fake", dim=16))
    assert rrep.episodes == 1 and rrep.vectors == 1, rrep

    def _clock(pid: str) -> str | None:
        c = connect(cfg2.db_path)
        try:
            return c.execute(
                "SELECT last_accessed_at FROM episodes WHERE public_id=?", (pid,)).fetchone()[0]
        finally:
            c.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg2))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # (1) episode 命中,结构化形状对;touch 默认 false → 不刷时钟
        before_clock = _clock("ep_rcep0001")
        rc1 = _post(base, "/api/recall", {"mode": "episode", "query": "召回屏门测试概览"})
        assert rc1.get("error") is None, rc1
        prim = rc1["structured"]["slots"]["primary"]
        assert prim and prim[0]["public_id"] == "ep_rcep0001", rc1
        assert set(prim[0].keys()) == {"public_id", "overview", "summary", "highlights",
                                       "source_text", "created_at", "salience_tier", "score"}, prim[0]
        assert rc1["reconstruction"] is None, rc1
        assert _clock("ep_rcep0001") == before_clock, "touch 默认 false,不该刷时钟"
        ok("POST /api/recall episode:结构化命中形状对,touch 默认 false 不刷时钟")

        # (2) reconstruct=true 走 fake chat,返回非空文本(结构化结果照常在)
        rc2 = _post(base, "/api/recall", {"mode": "episode", "query": "召回屏门测试概览",
                                          "reconstruct": True})
        assert rc2.get("error") is None, rc2
        assert isinstance(rc2.get("reconstruction"), str) and rc2["reconstruction"].strip(), rc2
        assert rc2["structured"]["slots"]["primary"][0]["public_id"] == "ep_rcep0001", rc2
        ok("POST /api/recall episode reconstruct=true:fake chat 返回非空文本")

        # (2') user_query(模拟当轮 query)被接受:只喂重构,检索结果不受影响
        rc2b = _post(base, "/api/recall", {"mode": "episode", "query": "召回屏门测试概览",
                                           "reconstruct": True, "user_query": "我们当时聊了什么?"})
        assert rc2b.get("error") is None, rc2b
        assert isinstance(rc2b.get("reconstruction"), str) and rc2b["reconstruction"].strip(), rc2b
        assert rc2b["structured"]["slots"]["primary"][0]["public_id"] == "ep_rcep0001", rc2b
        ok("POST /api/recall episode + user_query:模拟当轮 query 被接受,检索不受影响")

        # (3) detail + reconstruct → 400(细节不接重构)
        st_d, rc3 = _post_status(base, "/api/recall", {"mode": "detail", "query": "召回门专用短语",
                                                        "reconstruct": True})
        assert st_d == 400 and "error" in rc3, (st_d, rc3)
        ok("POST /api/recall detail+reconstruct=true:400(细节不接重构)")

        # (4) detail 命中(不带 reconstruct),hits 形状对
        rc4 = _post(base, "/api/recall", {"mode": "detail", "query": "召回门专用短语"})
        assert rc4["structured"]["hits"] and \
            rc4["structured"]["hits"][0]["public_id"] == "ep_rcep0001", rc4
        ok("POST /api/recall detail:命中 hits 形状对")

        # (5) concept miss → 200 + suggestions("你是不是想找")
        rc5 = _post(base, "/api/recall", {"mode": "concept", "query": "召回门"})
        assert rc5["structured"]["episodes"] == [] and \
            "召回门概念" in rc5["structured"]["suggestions"], rc5
        assert rc5.get("error"), rc5
        ok("POST /api/recall concept miss:200 + 建议列表透传")

        # (6) concept 命中,挂载 episode 正确
        rc6 = _post(base, "/api/recall", {"mode": "concept", "query": "召回门概念"})
        assert [e["public_id"] for e in rc6["structured"]["episodes"]] == ["ep_rcep0001"], rc6
        ok("POST /api/recall concept:命中挂载 episode")

        # (7) 空 query / 坏 mode → 400
        st_e, rc7 = _post_status(base, "/api/recall", {"mode": "episode", "query": ""})
        assert st_e == 400, (st_e, rc7)
        st_m, rc8 = _post_status(base, "/api/recall", {"mode": "bogus", "query": "x"})
        assert st_m == 400, (st_m, rc8)
        ok("POST /api/recall:空 query / 坏 mode 均 400")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("POST /api/recall 门 ALL PASS ✅")


if __name__ == "__main__":
    main()
    verify_recall_api()
