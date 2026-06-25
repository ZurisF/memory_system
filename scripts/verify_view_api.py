"""查看侧只读 API 回归:/api/memories、/api/memory、/api/node。

聚焦本轮目标——**显示侧数据要跟数据库/碎片对上**:
  - confirm 出带膜的 active episode 后,/api/memories 能列到,且 node↔node 共现 edges
    按 episode_nodes 正确算出(含 via 共享情景)。
  - /api/memory 单条返回五件套 + source_text + 所属 nodes,绝不漏 uuid。
  - /api/node 返回别名/type + 挂载的 active episodes。
  - archive 后默认列表剔除、include_archived=1 仍在;坏 public_id 不穿越。

默认 fake 提取产出 nodes=[],故先用 /api/staging/edit 给 staging 注入可预测的膜,
再 confirm —— 借此构造确定性的共现边。
Run: .venv/bin/python scripts/verify_view_api.py
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

_TMP = tempfile.mkdtemp(prefix="memsys_viewapi_")
_ROOT = Path(_TMP) / "transcripts"
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_TRANSCRIPTS_ROOT"] = str(_ROOT)
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"

from memory_system.config import load_config  # noqa: E402
from memory_system.db import migrate  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.server import make_handler  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)
_ROOT.mkdir(parents=True, exist_ok=True)


def _row(kind: str, uuid: str, content, ts: str) -> dict:
    role = "user" if kind == "user" else "assistant"
    return {
        "type": kind, "uuid": uuid, "timestamp": ts, "isSidechain": False,
        "message": {"role": role, "content": content},
    }


def _mk_transcript() -> Path:
    p = _ROOT / "sess-view.jsonl"
    rows = []
    for i in range(1, 5):
        rows.append(_row("user", f"u{i}", f"人类第{i}句", f"2026-06-21T09:{i:02d}:00Z"))
        rows.append(_row("assistant", f"a{i}", [{"type": "text", "text": f"Claude第{i}句"}],
                         f"2026-06-21T09:{i:02d}:10Z"))
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")
    return p


def _post(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = request.Request(base + path, data=data, method="POST",
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_status(base: str, path: str) -> tuple[int, dict]:
    try:
        with request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _no_uuid(obj) -> bool:
    """递归确认返回里不含任何 uuid 痕迹(两条红线之一)。"""
    if isinstance(obj, dict):
        if any("uuid" in str(k).lower() for k in obj):
            return False
        return all(_no_uuid(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_no_uuid(v) for v in obj)
    return True


def main() -> None:
    print(f"临时 home: {_TMP}")
    con = connect(CFG.db_path)
    try:
        migrate.up(con)
    finally:
        con.close()

    src = _mk_transcript()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(CFG))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # ---- seed:两段 → 提取 → 注入可预测膜 → confirm ----
        segs = [
            {"start_turn": 1, "end_turn": 2, "tag": "前", "cut_reason": "t", "short": True,
             "deletions": [], "origin": "manual"},
            {"start_turn": 3, "end_turn": 4, "tag": "后", "cut_reason": "t", "short": True,
             "deletions": [], "origin": "manual"},
        ]
        assert _post(base, "/api/segments", {"path": str(src), "segments": segs}).get("ok")
        ext = _post(base, "/api/extract", {"path": str(src), "seg_ids": ["s1", "s2"]})
        assert ext.get("ok") and ext["staged"] == 2, ext

        # e1 膜:切块 + 原子性(带别名 atomicity);e2 膜:原子性(复用) + 前端
        _post(base, "/api/staging/edit", {"session_id": "sess-view", "stage_id": "e1", "fields": {
            "overview": "原子性与切块的段",
            "nodes": [{"label": "切块", "action": "new"},
                      {"label": "原子性", "action": "add_alias", "new_alias": "atomicity"}],
        }})
        _post(base, "/api/staging/edit", {"session_id": "sess-view", "stage_id": "e2", "fields": {
            "overview": "前端的段",
            "nodes": [{"label": "原子性", "action": "new"},
                      {"label": "前端", "action": "new"}],
        }})
        c1 = _post(base, "/api/confirm", {"session_id": "sess-view", "stage_id": "e1"})
        c2 = _post(base, "/api/confirm", {"session_id": "sess-view", "stage_id": "e2"})
        ep1, ep2 = c1["public_id"], c2["public_id"]
        assert ep1.startswith("ep_") and ep2.startswith("ep_"), (c1, c2)
        ok(f"seed 两条 active episode 入库:{ep1} / {ep2}")

        # ---- /api/memories ----
        mem = _get(base, "/api/memories")
        assert _no_uuid(mem), "memories 不得含 uuid"
        assert all("source_text" not in e for e in mem["episodes"]), "列表不带 source_text"
        by_pub = {e["public_id"]: e for e in mem["episodes"]}
        assert set(by_pub) == {ep1, ep2}, mem["episodes"]
        assert by_pub[ep1]["nodes"] == sorted(["切块", "原子性"]), by_pub[ep1]
        assert by_pub[ep2]["nodes"] == sorted(["原子性", "前端"]), by_pub[ep2]
        ok("/api/memories 列出 active episodes + 膜,无 uuid/source_text")

        ncount = {n["label"]: n["episode_count"] for n in mem["nodes"]}
        assert ncount.get("原子性") == 2 and ncount.get("切块") == 1 and ncount.get("前端") == 1, ncount
        aliases = {n["label"]: n["aliases"] for n in mem["nodes"]}
        assert aliases.get("原子性") == ["atomicity"], aliases
        ok("/api/memories nodes:episode_count 与 aliases 跟库对上")

        assert len(mem["membrane"]) == 4, mem["membrane"]
        edges = {tuple(sorted([e["a"], e["b"]])): e["via"] for e in mem["edges"]}
        assert edges.get(tuple(sorted(["切块", "原子性"]))) == [ep1], mem["edges"]
        assert edges.get(tuple(sorted(["前端", "原子性"]))) == [ep2], mem["edges"]
        assert len(edges) == 2, mem["edges"]
        ok("/api/memories 共现 edges 正确(含 via 共享情景)")

        # ---- /api/memory ----
        d = _get(base, "/api/memory?public_id=" + ep1)
        assert _no_uuid(d), "memory 不得含 uuid"
        assert d["overview"] == "原子性与切块的段" and d["source_text"], d
        assert d["status"] == "active" and d["nodes"] == sorted(["切块", "原子性"]), d
        assert "summary" in d and "highlights" in d, d
        ok("/api/memory 返回五件套 + source_text + 膜,无 uuid")

        # ---- /api/node ----
        nd = _get(base, "/api/node?label=" + quote("原子性"))
        assert nd["label"] == "原子性" and nd["aliases"] == ["atomicity"] and nd["type"] is None, nd
        node_pubs = {e["public_id"] for e in nd["episodes"]}
        assert node_pubs == {ep1, ep2}, nd["episodes"]
        assert all("overview" in e and "salience_tier" in e for e in nd["episodes"]), nd
        ok("/api/node 返回别名/type + 挂载 active episodes")

        # ---- archive 后默认剔除、include_archived 仍在 ----
        assert _post(base, "/api/archive", {"public_id": ep2}).get("ok")
        m2 = _get(base, "/api/memories")
        assert set(e["public_id"] for e in m2["episodes"]) == {ep1}, "archive 后默认列表应剔除 ep2"
        n2 = {n["label"]: n["episode_count"] for n in m2["nodes"]}
        assert n2.get("原子性") == 1 and n2.get("前端") == 0, n2
        assert all(tuple(sorted(["前端", "原子性"])) != tuple(sorted([e["a"], e["b"]]))
                   for e in m2["edges"]), "archive 后该共现边应消失"
        m3 = _get(base, "/api/memories?include_archived=1")
        assert set(e["public_id"] for e in m3["episodes"]) == {ep1, ep2}, m3["episodes"]
        arch = _get(base, "/api/memory?public_id=" + ep2)
        assert arch["status"] == "archived", arch
        ok("/api/memories 默认只 active,include_archived=1 含归档;边随展示集联动")

        # ---- 红线:坏 public_id 不穿越文件系统 ----
        st, _ = _get_status(base, "/api/memory?public_id=" + quote("../../../etc/passwd"))
        assert st == 404, st
        st, _ = _get_status(base, "/api/node?label=" + quote("不存在的节点"))
        assert st == 404, st
        ok("坏 public_id/label 被挡(404),不穿越")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("View API read contract ALL PASS ✅")


if __name__ == "__main__":
    main()
