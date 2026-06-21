"""本地审核前端的后端 —— 纯标准库 http.server,零依赖零构建(S2 选型)。

只绑 127.0.0.1。静态文件(index.html/app.js)在 web/ 下。
API:
  GET  /api/transcripts            列 transcript(清洗后 0 回合的空壳已剔除)
  GET  /api/transcript?path=...    取清洗回合 + 每回合已处理标记
  GET  /api/agent/providers        列可用 agent 后端(claude_cli/deepseek/fake)
  GET  /api/segments?path=...      取该 transcript 的切块工作态(段/agent/retry)
  POST /api/select   {path, session_id, turn_idxs}  登记选段为已处理
  POST /api/chunk    {path, provider?, model?}      调切块 agent,落工作文件
  POST /api/segments {path, segments}               存人工编辑后的段(uuid 服务端重算)
"""

from __future__ import annotations

import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from memory_system import preview_cache, processed, segments_store
from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.transcript import describe, discover

_WEB = Path(__file__).parent / "web"

# 前端 provider 选择器可见的后端;available() 现报是否能用。
_AGENT_PROVIDERS = ["claude_cli", "deepseek", "fake"]


def _providers_info(cfg: Config) -> list[dict]:
    from memory_system.agent import get_chat_provider

    out = []
    for pid in _AGENT_PROVIDERS:
        try:
            prov = get_chat_provider(replace(cfg.agent, provider=pid))
            ok, why = prov.available()
        except Exception as e:  # noqa: BLE001
            ok, why = False, str(e)
        out.append({"id": pid, "available": ok, "reason": why,
                    "default": pid == cfg.agent.provider})
    return out


def _ui_segment(s: dict) -> dict:
    """送前端的段:剥掉 covered_uuids(uuid 不上台面)。"""
    return {k: v for k, v in s.items() if k != "covered_uuids"}


def _ui_doc(doc: dict | None) -> dict:
    if not doc:
        return {"segments": [], "agent": None, "retry": [], "source_mtime": None}
    return {
        "segments": [_ui_segment(s) for s in doc.get("segments", [])],
        "agent": doc.get("agent"),
        "retry": doc.get("retry", []),
        "source_mtime": doc.get("source_mtime"),
        "updated_at": doc.get("updated_at"),
    }


def make_handler(cfg: Config):
    def _confine(raw: str) -> Path | None:
        """把传入 path 限制在 transcripts_root 内;越界/无效返回 None(堵任意文件读)。"""
        if not raw:
            return None
        try:
            p = Path(raw).expanduser().resolve()
            base = cfg.transcripts_root.resolve()
        except (OSError, RuntimeError):
            return None
        if p != base and base not in p.parents:
            return None
        return p

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
            if u.path == "/api/agent/providers":
                return self._json({"providers": _providers_info(cfg),
                                   "chunk_model": cfg.agent.chunk_model})
            if u.path == "/api/segments":
                return self._api_get_segments(parse_qs(u.query))
            self._send(404, b"not found", "text/plain")

        def _api_get_segments(self, q) -> None:
            path = _confine((q.get("path") or [""])[0])
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            doc = segments_store.load(cfg.chunks_dir, ct.session_id)
            self._json(_ui_doc(doc))

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
            path = _confine((q.get("path") or [""])[0])
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
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
            if u.path not in ("/api/select", "/api/chunk", "/api/segments"):
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求体非 JSON"}, 400)
            if u.path == "/api/select":
                return self._api_select(body)
            if u.path == "/api/chunk":
                return self._api_chunk(body)
            return self._api_save_segments(body)

        def _api_chunk(self, body) -> None:
            from dataclasses import replace as _replace

            from memory_system.agent import get_chat_provider
            from memory_system.chunk import ChunkFailed, OversizedError, run_chunk

            path = _confine(body.get("path", ""))
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            if not ct.turns:
                return self._json({"error": "清洗后 0 回合,无可切内容"}, 400)
            mtime = path.stat().st_mtime

            agent_cfg = cfg.agent
            if body.get("provider"):
                agent_cfg = _replace(agent_cfg, provider=body["provider"])
            model = body.get("model") or agent_cfg.chunk_model
            try:
                provider = get_chat_provider(agent_cfg)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            ok, why = provider.available()
            if not ok:
                return self._json({"kind": "unavailable", "error": f"provider 不可用: {why}"}, 400)

            try:
                res = run_chunk(ct, provider, model=model, timeout=agent_cfg.timeout_s,
                                max_retries=agent_cfg.max_retries)
            except OversizedError as e:
                return self._json({"kind": "oversized", "error": str(e),
                                   "chars": e.chars, "limit": e.limit}, 413)
            except ChunkFailed as e:
                segments_store.append_retry(cfg.chunks_dir, ct.session_id, str(path), mtime,
                                            provider=agent_cfg.provider, model=model, error=str(e))
                doc = segments_store.load(cfg.chunks_dir, ct.session_id)
                return self._json({"kind": "failed", "error": str(e),
                                   "errors": e.errors, **_ui_doc(doc)}, 502)
            doc = segments_store.record_agent_run(cfg.chunks_dir, ct.session_id,
                                                  str(path), mtime, res)
            self._json({"ok": True, **_ui_doc(doc)})

        def _api_save_segments(self, body) -> None:
            path = _confine(body.get("path", ""))
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            incoming = body.get("segments")
            if not isinstance(incoming, list):
                return self._json({"error": "缺 segments 列表"}, 400)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            from memory_system.chunk import uuids_by_turn

            umap = uuids_by_turn(ct)
            valid_idx = set(umap)
            mtime = path.stat().st_mtime
            norm: list[dict] = []
            for s in incoming:
                try:
                    start = int(s["start_turn"]); end = int(s["end_turn"])
                except (KeyError, TypeError, ValueError):
                    return self._json({"error": f"段缺/坏 start_turn·end_turn: {s}"}, 400)
                if start > end or start not in valid_idx or end not in valid_idx:
                    return self._json({"error": f"段回合越界: {start}-{end}"}, 400)
                # uuid 不信前端,一律服务端按回合区间重算
                norm.append({
                    "seg_id": s.get("seg_id") or "",
                    "start_turn": start, "end_turn": end,
                    "tag": str(s.get("tag", "")).strip(),
                    "cut_reason": str(s.get("cut_reason", "")).strip(),
                    "short": bool(s.get("short", False)),
                    "deletions": [d for d in (s.get("deletions") or []) if isinstance(d, dict)],
                    "origin": s.get("origin") or "edited",
                    "covered_uuids": segments_store.recompute_uuids(start, end, umap),
                })
            doc = segments_store.save_full(cfg.chunks_dir, ct.session_id, str(path), mtime, norm)
            self._json({"ok": True, **_ui_doc(doc)})

        def _api_select(self, body) -> None:
            path = _confine(body.get("path", ""))
            turn_idxs = set(body.get("turn_idxs") or [])
            if path is None or not path.exists() or not turn_idxs:
                return self._json({"error": "路径越界、缺 path 或 turn_idxs"}, 400)
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
