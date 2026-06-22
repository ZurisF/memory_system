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

_TMP = tempfile.mkdtemp(prefix="memsys_webapi_")
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
        assert edited.get("ok") and edited["episodes"][0]["overview"] == "session-id 编辑后的 overview", edited
        ok("/api/staging/edit 支持 session_id,不依赖源 jsonl")

        rejected = _post(base, "/api/reject",
                         {"session_id": "sess-web", "stage_id": "e2", "reason": "测试拒绝"})
        assert rejected.get("ok") and [e["stage_id"] for e in rejected["episodes"]] == ["e1"], rejected
        ok("/api/reject 支持 session_id,不依赖源 jsonl")

        confirmed = _post(base, "/api/confirm", {"session_id": "sess-web", "stage_id": "e1"})
        assert confirmed.get("ok") and confirmed["public_id"].startswith("ep_"), confirmed
        assert confirmed["episodes"] == [], confirmed
        ok("/api/confirm 支持 session_id,源 jsonl 已清仍可入库")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("Web API staging contract ALL PASS ✅")


if __name__ == "__main__":
    main()
