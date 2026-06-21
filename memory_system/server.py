"""本地审核前端的后端 —— 纯标准库 http.server,零依赖零构建(S2 选型)。

只绑 127.0.0.1。静态文件(index.html/app.js)在 web/ 下。
API:
  GET  /api/transcripts            列 transcript(清洗后 0 回合的空壳已剔除)
  GET  /api/transcript?path=...    取清洗回合 + 每回合已处理标记
  POST /api/select  {path, session_id, turn_idxs}   登记选段为已处理
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from memory_system import preview_cache, processed
from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.transcript import describe, discover

_WEB = Path(__file__).parent / "web"


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音默认请求日志
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _static(self, name: str, ctype: str) -> None:
            p = _WEB / name
            if not p.exists():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, p.read_bytes(), ctype)

        # ---- GET ----
        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if u.path == "/app.js":
                return self._static("app.js", "application/javascript; charset=utf-8")
            if u.path == "/api/transcripts":
                return self._api_transcripts()
            if u.path == "/api/transcript":
                return self._api_transcript(parse_qs(u.query))
            self._send(404, b"not found", "text/plain")

        def _api_transcripts(self) -> None:
            infos = discover(cfg.transcripts_root)
            items = []
            hidden_empty = 0
            for i in infos:
                # 清洗后 0 回合 = /clear 空壳等垃圾文件,剔除(人工审核前先去噪)。
                ct = preview_cache.get(cfg.preview_cache_dir, i.path, mtime=i.mtime)
                if not ct.turns:
                    hidden_empty += 1
                    continue
                items.append(
                    {"session_id": i.session_id, "path": str(i.path), "cwd": i.cwd,
                     "mtime": i.mtime, "size": i.size, "line_count": i.line_count,
                     "turn_count": len(ct.turns), "maybe_writing": i.maybe_writing})
            self._json({"root": str(cfg.transcripts_root),
                        "hidden_empty": hidden_empty, "transcripts": items})

        def _api_transcript(self, q) -> None:
            path = Path((q.get("path") or [""])[0]).expanduser()
            if not path.exists():
                return self._json({"error": "文件不存在"}, 404)
            info = describe(path)
            ct = preview_cache.get(cfg.preview_cache_dir, path, mtime=info.mtime)
            con = connect(cfg.db_path)
            try:
                pset = processed.processed_uuids(con, ct.session_id)
            finally:
                con.close()
            turns = []
            for t in ct.turns:
                ids = [u for u in t.uuids if u]
                done = bool(ids) and all(u in pset for u in ids)
                turns.append({"idx": t.idx, "human_text": t.human_text,
                              "assistant_text": t.assistant_text, "uuids": ids,
                              "msg_count": len(ids), "processed": done})
            self._json({
                "session_id": ct.session_id, "path": str(path),
                "maybe_writing": info.maybe_writing, "cwd": info.cwd,
                "turns": turns,
            })

        # ---- POST ----
        def do_POST(self) -> None:
            u = urlparse(self.path)
            if u.path != "/api/select":
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求体非 JSON"}, 400)
            self._api_select(body)

        def _api_select(self, body) -> None:
            path = Path(body.get("path", "")).expanduser()
            turn_idxs = set(body.get("turn_idxs") or [])
            if not path.exists() or not turn_idxs:
                return self._json({"error": "缺 path 或 turn_idxs"}, 400)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            uuids: list[str] = []
            for t in ct.turns:
                if t.idx in turn_idxs:
                    uuids.extend(u for u in t.uuids if u)
            if not uuids:
                return self._json({"error": "选中回合无可登记的 uuid"}, 400)
            con = connect(cfg.db_path)
            try:
                h = processed.mark_segment(con, ct.session_id, uuids)
            finally:
                con.close()
            self._json({"ok": True, "segment_hash": h, "covered": len(uuids),
                        "turns": sorted(turn_idxs)})

    return Handler


def serve(cfg: Config, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    con = connect(cfg.db_path)
    try:
        migrate.up(con)  # 确保 processed 表在
    finally:
        con.close()
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"审核前端: http://{host}:{port}  (Ctrl-C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
