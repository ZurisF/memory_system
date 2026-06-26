"""本地审核前端的后端 —— 纯标准库 http.server,零依赖零构建(S2 选型)。

只绑 127.0.0.1。静态文件(index.html/app.js)在 web/ 下。
API:
  GET  /api/transcripts            列 transcript(清洗后 0 回合的空壳已剔除)
  GET  /api/transcript?path=...    取清洗回合 + 每回合已处理标记
  GET  /api/agent/providers        列可用 agent 后端(claude_cli/deepseek/fake)
  GET  /api/segments?path=...      取该 transcript 的切块工作态(段/agent/retry)
  GET  /api/staging?path=...       取该 transcript 的提取工作态(staging 五件套/retry)
  GET  /api/memories               查看侧列表:active episodes/nodes/膜/共现 edges(无 uuid/向量/source_text)
  GET  /api/memory?public_id=...   单条 episode 详情(五件套 + source_text + 所属 nodes)
  GET  /api/node?label=...         node 详情 + 挂载 active episodes
  POST /api/select   {path, session_id, turn_idxs}  登记选段为已处理
  POST /api/chunk    {path, provider?, model?}      调切块 agent,落工作文件
  POST /api/segments {path, segments}               存人工编辑后的段(uuid 服务端重算)
  POST /api/segments/delete {session_id|path, seg_ids, force?}  删段(改 chunks;已提取段需 force)
  POST /api/extract  {path, seg_ids?, provider?, model?}  逐段提取五件套,落 staging(按块回滚)
  POST /api/confirm  {path|session_id, stage_id}    确认 staging 条目入库
  POST /api/reject   {path|session_id, stage_id, reason?}  拒绝 staging 条目(打回重做,留痕)
  POST /api/staging/edit {path|session_id, stage_id, fields}  编辑 staging 条目
  POST /api/staging/delete {path|session_id, stage_id}  干净删除未入库 staging 条目(不留痕)
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
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

# 前端 provider 选择器可见的内置后端;available() 现报是否能用。
_AGENT_PROVIDERS = ["claude_cli", "deepseek", "openai_compat", "qwen", "fake"]
_PLACEHOLDER_KEY = "[this is your api key]"


def _mask_key(env_var: str) -> str | None:
    """读取环境变量,返回掩码形式如 sk-****abcd;未配返回 None。"""
    import os as _os
    v = _os.environ.get(env_var, "").strip()
    if not v:
        return None
    if len(v) <= 8:
        return v[:2] + "****" + v[-2:]
    return v[:3] + "****" + v[-4:]


def _update_dotenv(env_path: Path, updates: dict[str, str]) -> None:
    """把 updates 里的 key=value 写回 .env 文件;已存在的 key 改值,不存在的追加。
    不改变其他行、不重排、保留注释和空行。"""
    import os as _os2
    lines = env_path.read_text("utf-8").splitlines() if env_path.exists() else []
    updated: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if stripped.startswith("export "):
            prefix, rest = "export ", stripped[len("export "):]
        else:
            prefix, rest = "", stripped
        if "=" not in rest:
            new_lines.append(line)
            continue
        key = rest.split("=", 1)[0].strip()
        if key in updates:
            val = updates[key]
            new_lines.append(f"{prefix}{key}={val}")
            updated.add(key)
        else:
            new_lines.append(line)
    # 追加未更新过的新 key
    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", "utf-8")
    # 同步到当前进程环境
    for key, val in updates.items():
        _os2.environ[key] = val


def _custom_providers_path(cfg: Config) -> Path:
    return cfg.home / "custom_providers.json"


def _load_custom_providers(cfg: Config) -> list[dict]:
    """加载用户通过控制台添加的自定义 provider;文件不存在返回 []。"""
    import json as _json
    p = _custom_providers_path(cfg)
    if not p.exists():
        return []
    try:
        data = _json.loads(p.read_text("utf-8"))
        return data.get("providers", []) if isinstance(data, dict) else []
    except (ValueError, KeyError):
        return []


def _save_custom_providers(cfg: Config, providers: list[dict]) -> None:
    """保存自定义 provider 列表到 JSON 文件。"""
    import json as _json
    p = _custom_providers_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({"providers": providers}, ensure_ascii=False, indent=2), "utf-8")


def _all_provider_ids(cfg: Config) -> list[str]:
    """内置 + 自定义 provider id 合集。"""
    custom = [cp["id"] for cp in _load_custom_providers(cfg)]
    return _AGENT_PROVIDERS + custom


def _providers_info(cfg: Config) -> list[dict]:
    import os as _os

    from memory_system.agent import get_chat_provider

    out = []
    for pid in _AGENT_PROVIDERS:
        try:
            prov = get_chat_provider(replace(cfg.agent, provider=pid))
            ok, why = prov.available()
            key_env = getattr(prov, "api_key_env", None)
            if key_env and _os.environ.get(key_env, "").strip() == _PLACEHOLDER_KEY:
                ok, why = False, f"环境变量 {key_env} 仍是占位 key"
        except Exception as e:  # noqa: BLE001
            ok, why = False, str(e)
        out.append({"id": pid, "available": ok, "reason": why,
                    "default": pid == cfg.agent.provider, "builtin": True})

    # 自定义 provider
    for cp in _load_custom_providers(cfg):
        pid = cp["id"]
        try:
            prov = get_chat_provider(replace(cfg.agent,
                provider=pid, custom_providers={pid: cp}))
            ok, why = prov.available()
            if cp.get("api_key_env") and _os.environ.get(cp["api_key_env"], "").strip() == _PLACEHOLDER_KEY:
                ok, why = False, f"环境变量 {cp['api_key_env']} 仍是占位 key"
        except Exception as e:
            ok, why = False, str(e)
        out.append({"id": pid, "available": ok, "reason": why,
                    "default": pid == cfg.agent.provider, "builtin": False,
                    "name": cp.get("name", pid), "base_url": cp.get("base_url", ""),
                    "default_model": cp.get("default_model", "")})
    return out


def _ui_segment(s: dict) -> dict:
    """送前端的段:剥掉 covered_uuids(uuid 不上台面)。"""
    return {k: v for k, v in s.items() if k != "covered_uuids"}


def _ui_episode(e: dict) -> dict:
    """送前端的 staging episode:剥掉 covered_uuids(uuid 不上台面)。source_text 保留(S5 审核要看)。"""
    return {k: v for k, v in e.items() if k != "covered_uuids"}


def _ui_staging(doc: dict | None) -> dict:
    if not doc:
        return {"episodes": [], "retry": [], "updated_at": None}
    return {
        "episodes": [_ui_episode(e) for e in doc.get("episodes", [])],
        "retry": doc.get("retry", []),
        "updated_at": doc.get("updated_at"),
    }


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
    def _valid_session_id(raw: object) -> str | None:
        sid = str(raw or "").strip()
        if not sid or "/" in sid or "\\" in sid or ".." in sid:
            return None
        return sid

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
            if u.path.startswith("/") and "/" not in u.path[1:]:
                name = u.path[1:]
                ctype = _STATIC_TYPES.get(Path(name).suffix)
                if ctype:
                    return self._static(name, ctype)
            if u.path == "/api/transcripts":
                return self._api_transcripts()
            if u.path == "/api/transcript":
                return self._api_transcript(parse_qs(u.query))
            if u.path == "/api/agent/providers":
                return self._json({"providers": _providers_info(cfg),
                                   "chunk_model": cfg.agent.chunk_model,
                                   "extract_model": cfg.agent.extract_model,
                                   "chunk_provider": cfg.agent.provider_for("chunk"),
                                   "extract_provider": cfg.agent.provider_for("extract")})
            if u.path == "/api/agent/config":
                return self._api_agent_config()
            if u.path == "/api/segments":
                return self._api_get_segments(parse_qs(u.query))
            if u.path == "/api/staging":
                return self._api_get_staging(parse_qs(u.query))
            if u.path == "/api/staging/all":
                return self._api_staging_all()
            if u.path == "/api/memories":
                return self._api_memories(parse_qs(u.query))
            if u.path == "/api/memory":
                return self._api_memory(parse_qs(u.query))
            if u.path == "/api/node":
                return self._api_node(parse_qs(u.query))
            self._send(404, b"not found", "text/plain")

        # ---- 查看侧只读 ----
        def _api_memories(self, q) -> None:
            from memory_system import views

            inc = (q.get("include_archived") or ["0"])[0] in ("1", "true", "yes")
            self._json(views.list_memories(cfg, include_archived=inc))

        def _api_memory(self, q) -> None:
            from memory_system import views

            pub = (q.get("public_id") or [""])[0].strip()
            if not pub:
                return self._json({"error": "缺 public_id"}, 400)
            mem = views.read_memory(cfg, pub)
            if mem is None:
                return self._json({"error": f"无此条目: {pub}"}, 404)
            self._json(mem)

        def _api_node(self, q) -> None:
            from memory_system import views

            label = (q.get("label") or [""])[0]
            if not label.strip():
                return self._json({"error": "缺 label"}, 400)
            nd = views.read_node_detail(cfg, label)
            if nd is None:
                return self._json({"error": f"无此节点: {label}"}, 404)
            self._json(nd)

        def _api_agent_config(self) -> None:
            """控制台:各 agent 角色的 provider/模型/key 状态(密文掩码,绝不回明文)。
            每次调用重新同步 .env,确保手动编辑 .env 后 key 状态即时刷新。"""
            import os as _os2
            from memory_system.env import load_dotenv

            # 重新加载 .env,让手动编辑的 key 即时生效(override=True 覆盖已有值)
            load_dotenv(cfg.home / ".env", override=True)
            # 同步更新内存 cfg 的 custom_providers(可能刚通过控制台添加/删除)
            from memory_system.config import _load_custom_providers_map
            new_cp = _load_custom_providers_map(cfg.home)
            if new_cp != cfg.agent.custom_providers:
                object.__setattr__(cfg, 'agent', replace(cfg.agent, custom_providers=new_cp))

            agents = {}
            for role, default_model in [("chunk", cfg.agent.chunk_model),
                                         ("extract", cfg.agent.extract_model)]:
                effective_provider = cfg.agent.provider_for(role)
                agents[role] = {
                    "provider": effective_provider,
                    "model": default_model,
                    "providers": _providers_info(cfg),
                }

            # embedding key 状态
            emb = cfg.embedding
            emb_key = _os2.environ.get(emb.api_key_env, "").strip()
            embedding = {
                "provider": emb.provider,
                "model": emb.model,
                "dim": emb.dim,
                "key_env": emb.api_key_env,
                "key_present": bool(emb_key),
                "key_masked": _mask_key(emb.api_key_env),
            }

            # 各 provider 的 key 状态(claude_cli 不走 key,fake 无 key)
            compat_key = _os2.environ.get(cfg.agent.api_key_env, "").strip()
            agent_keys = [
                {"id": "claude_cli", "key_env": None, "key_present": None, "key_masked": None},
                {"id": "deepseek", "key_env": cfg.agent.api_key_env,
                 "key_present": bool(compat_key and compat_key != _PLACEHOLDER_KEY),
                 "key_masked": _mask_key(cfg.agent.api_key_env)},
                {"id": "openai_compat", "key_env": cfg.agent.api_key_env,
                 "key_present": bool(compat_key and compat_key != _PLACEHOLDER_KEY),
                 "key_masked": _mask_key(cfg.agent.api_key_env)},
                {"id": "qwen", "key_env": cfg.agent.api_key_env,
                 "key_present": bool(compat_key and compat_key != _PLACEHOLDER_KEY),
                 "key_masked": _mask_key(cfg.agent.api_key_env)},
                {"id": "fake", "key_env": None, "key_present": None, "key_masked": None},
            ]
            # 自定义 provider 的 key 状态
            for cp in _load_custom_providers(cfg):
                ak = _os2.environ.get(cp["api_key_env"], "").strip()
                agent_keys.append({
                    "id": cp["id"],
                    "key_env": cp["api_key_env"],
                    "key_present": bool(ak and ak != "[this is your api key]"),
                    "key_masked": _mask_key(cp["api_key_env"]),
                })

            self._json({
                "agents": agents,
                "embedding": embedding,
                "agent_keys": agent_keys,
                "timeout_s": cfg.agent.timeout_s,
                "max_retries": cfg.agent.max_retries,
            })

        def _api_agent_config_post(self, body) -> None:
            """更新 agent 配置:写入 ~/.memory_system/.env,同步当前进程环境。
            provider 切换影响所有 agent 角色(共享);model 可按 role 各自设。
            变更需重启服务才能在后续 API 调用中全局生效(Config 启动时已冻结)。"""
            role = str(body.get("role", "")).strip()
            if role not in ("chunk", "extract"):
                return self._json({"error": "role 必须是 chunk 或 extract"}, 400)

            updates: dict[str, str] = {}
            # provider 切换(按 role 独立,不再共享)
            provider = str(body.get("provider", "")).strip()
            if provider and provider in _all_provider_ids(cfg):
                prov_key = {"chunk": "MEMORY_AGENT_CHUNK_PROVIDER",
                            "extract": "MEMORY_AGENT_EXTRACT_PROVIDER"}[role]
                updates[prov_key] = provider

            # model 切换(按 role)
            model = str(body.get("model", "")).strip()
            if model:
                model_key = {"chunk": "MEMORY_AGENT_CHUNK_MODEL",
                             "extract": "MEMORY_AGENT_EXTRACT_MODEL"}[role]
                updates[model_key] = model

            if not updates:
                return self._json({"error": "缺少 provider 或 model"}, 400)

            env_path = cfg.home / ".env"
            try:
                _update_dotenv(env_path, updates)
            except OSError as e:
                return self._json({"error": f"写入 .env 失败: {e}"}, 500)

            # 更新内存中的 cfg(绕过 frozen),让 GET /api/agent/config 即时反映变更
            new_agent = cfg.agent
            if "MEMORY_AGENT_CHUNK_PROVIDER" in updates:
                new_agent = replace(new_agent, chunk_provider=updates["MEMORY_AGENT_CHUNK_PROVIDER"])
            if "MEMORY_AGENT_EXTRACT_PROVIDER" in updates:
                new_agent = replace(new_agent, extract_provider=updates["MEMORY_AGENT_EXTRACT_PROVIDER"])
            if "MEMORY_AGENT_CHUNK_MODEL" in updates:
                new_agent = replace(new_agent, chunk_model=updates["MEMORY_AGENT_CHUNK_MODEL"])
            if "MEMORY_AGENT_EXTRACT_MODEL" in updates:
                new_agent = replace(new_agent, extract_model=updates["MEMORY_AGENT_EXTRACT_MODEL"])
            object.__setattr__(cfg, 'agent', new_agent)

            self._json({"ok": True, "updated": updates,
                        "restart_required": True,
                        "hint": "provider/model 变更已写入 .env 并同步当前页面;已存在的 LLM 调用路径需重启服务才能全局生效"})

        def _api_get_staging(self, q) -> None:
            from memory_system import staging_store

            sid = _valid_session_id((q.get("session_id") or [""])[0])
            if sid:
                doc = staging_store.load(cfg.staging_episodes_dir, sid)
                return self._json(_ui_staging(doc))
            path = _confine((q.get("path") or [""])[0])
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            doc = staging_store.load(cfg.staging_episodes_dir, ct.session_id)
            self._json(_ui_staging(doc))

        def _api_staging_all(self) -> None:
            """汇总磁盘上所有在处理的会话(只扫 chunks + episodes 两个工作目录,不碰全量
            transcript)。待整理区据此显示——磁盘有的工作一定列出,与浏览器候选区/缓存无关,
            关页面/换实例不丢。uuid 经 _ui_* 剥掉。"""
            from memory_system import staging_store

            sessions: dict[str, dict] = {}

            def _slot(sid: str, source_path: str) -> dict:
                s = sessions.get(sid)
                if s is None:
                    s = sessions[sid] = {
                        "session_id": sid, "source_path": source_path or "",
                        "segments": [], "episodes": [],
                        "chunk_retry": [], "retry": [], "updated_at": None,
                    }
                if source_path and not s["source_path"]:
                    s["source_path"] = source_path
                return s

            def _newer(a, b):
                return a if (a or "") >= (b or "") else b

            # 未提取的段(chunks 工作态)
            for f in sorted(cfg.chunks_dir.glob("*.json")):
                doc = segments_store.load(cfg.chunks_dir, f.stem)
                if not doc:
                    continue
                ui = _ui_doc(doc)
                s = _slot(f.stem, doc.get("source_path", ""))
                s["segments"] = ui["segments"]
                s["chunk_retry"] = ui["retry"]
                s["updated_at"] = _newer(s["updated_at"], ui.get("updated_at"))

            # 已提取的五件套(episodes 工作态)
            for f in sorted(cfg.staging_episodes_dir.glob("*.json")):
                doc = staging_store.load(cfg.staging_episodes_dir, f.stem)
                if not doc:
                    continue
                ui = _ui_staging(doc)
                s = _slot(f.stem, doc.get("source_path", ""))
                s["episodes"] = ui["episodes"]
                s["retry"] = ui["retry"]
                s["updated_at"] = _newer(s["updated_at"], ui.get("updated_at"))

            # source 是否还在(transcript ~30 天会清:清了仍可审已提取的,但不能再提取新段)
            out = []
            for s in sessions.values():
                sp = s.get("source_path") or ""
                s["source_exists"] = bool(sp) and Path(sp).exists()
                out.append(s)
            out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            self._json({"sessions": out})

        def _api_get_segments(self, q) -> None:
            path = _confine((q.get("path") or [""])[0])
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            doc = segments_store.load(cfg.chunks_dir, ct.session_id)
            self._json(_ui_doc(doc))

        def _api_transcripts(self) -> None:
            infos = discover(cfg.transcripts_root)
            # 磁盘上已动过的会话(有 chunks 段工作态或 staging 提取):列表里沉底
            touched = {p.stem for p in cfg.chunks_dir.glob("*.json")}
            touched |= {p.stem for p in cfg.staging_episodes_dir.glob("*.json")}
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
                     "turn_count": len(ct.turns), "maybe_writing": i.maybe_writing,
                     "touched": i.session_id in touched})
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
            routes = {
                "/api/select": self._api_select,
                "/api/chunk": self._api_chunk,
                "/api/segments": self._api_save_segments,
                "/api/segments/delete": self._api_delete_segments,
                "/api/extract": self._api_extract,
                "/api/confirm": self._api_confirm,
                "/api/reject": self._api_reject,
                "/api/archive": self._api_archive,
                "/api/staging/edit": self._api_staging_edit,
                "/api/staging/delete": self._api_staging_delete,
                "/api/agent/test": self._api_agent_test,
                "/api/agent/config": self._api_agent_config_post,
                "/api/agent/providers": self._api_add_provider,
                "/api/embedding/test": self._api_embedding_test,
            }
            handler = routes.get(u.path)
            if handler is None:
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求体非 JSON"}, 400)
            return handler(body)

        def do_DELETE(self) -> None:
            u = urlparse(self.path)
            if u.path == "/api/agent/providers":
                qs = parse_qs(u.query)
                pid = (qs.get("id") or [""])[0].strip()
                return self._api_remove_provider(pid)
            self._send(404, b"not found", "text/plain")

        def do_PUT(self) -> None:
            u = urlparse(self.path)
            if u.path != "/api/agent/providers":
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "请求体非 JSON"}, 400)
            return self._api_update_provider(body)

        # ---- 自定义 provider 管理 ----
        def _api_add_provider(self, body) -> None:
            """添加一个自定义 OpenAI 兼容 provider。
            自动生成 env var 名并写入占位 key 到 .env。"""
            name = str(body.get("name", "")).strip()
            base_url = str(body.get("base_url", "")).strip()
            model = str(body.get("model", "")).strip()

            if not name or not base_url:
                return self._json({"error": "name 和 base_url 必填"}, 400)
            if not base_url.startswith("https://") and not base_url.startswith("http://"):
                return self._json({"error": "base_url 必须以 http:// 或 https:// 开头"}, 400)

            # 校验:base_url 要是 API 端点(含 /v1 等版本路径),不是 Web 平台首页。
            # OpenAI 兼容 API 的 chat 路径统一为 {base_url}/chat/completions。
            import re as _re
            _stripped = base_url.rstrip("/")
            _has_api_ver = bool(_re.search(r"/v\d+", _stripped))
            _hints: list[str] = []
            if not _has_api_ver:
                _hints.append(
                    f"base_url 不含 /v1 等版本路径,实际请求将指向 "
                    f"{_stripped}/chat/completions——多数 OpenAI 兼容 API 需要 /v1 前缀,"
                    f"请确认这是正确的 API 端点而非 Web 控制台地址。"
                    f"例如 DeepSeek 应为 https://api.deepseek.com/v1 而非 https://platform.deepseek.com"
                )
            # 常见平台域名的误用
            _host = (_stripped.split("://", 1)[1] if "://" in _stripped else _stripped).split("/")[0]
            if _host in ("platform.deepseek.com", "chat.deepseek.com", "platform.openai.com"):
                _hints.append(
                    f"{_host} 是 Web 平台而非 API 端点;DeepSeek API 为 api.deepseek.com/v1,"
                    f"OpenAI API 为 api.openai.com/v1"
                )
            pid = "custom_" + _re.sub(r"[^a-z0-9_]", "_", name.lower().strip().replace(" ", "_"))
            pid = _re.sub(r"_+", "_", pid).strip("_")
            env_var = pid.upper() + "_API_KEY"

            # 去重
            existing = _load_custom_providers(cfg)
            if any(p["id"] == pid for p in existing):
                return self._json({"error": f"provider id {pid!r} 已存在"}, 409)

            placeholder = "[this is your api key]"
            cp = {
                "id": pid,
                "name": name,
                "base_url": base_url.rstrip("/"),
                "api_key_env": env_var,
                "default_model": model or "",
                "created_at": None,  # 略
            }

            # 写 .env:占位 key + 同步环境
            _update_dotenv(cfg.home / ".env", {env_var: placeholder})

            # 保存到 custom_providers.json
            existing.append(cp)
            _save_custom_providers(cfg, existing)

            # 更新内存 cfg
            new_cp_map = dict(cfg.agent.custom_providers)
            new_cp_map[pid] = {"base_url": cp["base_url"], "api_key_env": env_var,
                               "default_model": cp["default_model"]}
            object.__setattr__(cfg, 'agent', replace(cfg.agent, custom_providers=new_cp_map))

            self._json({
                "ok": True,
                "provider": cp,
                "hint": f"Key 占位已写入 .env 的 {env_var}=[this is your api key];请到 ~/.memory_system/.env 替换为真实 key 后再测试连接",
                "warnings": _hints if _hints else None,
            })

        def _api_update_provider(self, body) -> None:
            """修改自定义 OpenAI 兼容 provider 的显示名、base_url、默认模型。

            id/api_key_env 不改,避免 .env key 变量被隐式迁移。
            """
            pid = str(body.get("id", "")).strip()
            if not pid:
                return self._json({"error": "缺 id"}, 400)
            if pid in _AGENT_PROVIDERS:
                return self._json({"error": f"内置 provider {pid!r} 不可修改"}, 403)

            name = str(body.get("name", "")).strip()
            base_url = str(body.get("base_url", "")).strip()
            model = str(body.get("model", "")).strip()
            if not name or not base_url:
                return self._json({"error": "name 和 base_url 必填"}, 400)
            if not base_url.startswith("https://") and not base_url.startswith("http://"):
                return self._json({"error": "base_url 必须以 http:// 或 https:// 开头"}, 400)

            existing = _load_custom_providers(cfg)
            idx = next((i for i, p in enumerate(existing) if p.get("id") == pid), None)
            if idx is None:
                return self._json({"error": f"provider {pid!r} 不存在"}, 404)

            cp = dict(existing[idx])
            cp["name"] = name
            cp["base_url"] = base_url.rstrip("/")
            cp["default_model"] = model
            existing[idx] = cp
            _save_custom_providers(cfg, existing)

            new_cp_map = dict(cfg.agent.custom_providers)
            new_cp_map[pid] = {"base_url": cp["base_url"], "api_key_env": cp["api_key_env"],
                               "default_model": cp.get("default_model", "")}
            object.__setattr__(cfg, 'agent', replace(cfg.agent, custom_providers=new_cp_map))

            self._json({"ok": True, "provider": cp})

        def _api_remove_provider(self, pid: str) -> None:
            """删除自定义 provider(内置 provider 不可删)。"""
            if not pid:
                return self._json({"error": "缺 id"}, 400)
            if pid in _AGENT_PROVIDERS:
                return self._json({"error": f"内置 provider {pid!r} 不可删除"}, 403)

            existing = _load_custom_providers(cfg)
            cp = next((p for p in existing if p["id"] == pid), None)
            if not cp:
                return self._json({"error": f"provider {pid!r} 不存在"}, 404)

            # 不移除 .env 中的 key 变量(用户可能以后还要用);只删 JSON 条目
            existing = [p for p in existing if p["id"] != pid]
            _save_custom_providers(cfg, existing)

            # 更新内存 cfg
            new_cp_map = dict(cfg.agent.custom_providers)
            new_cp_map.pop(pid, None)
            new_agent = replace(cfg.agent, custom_providers=new_cp_map)
            env_updates: dict[str, str] = {}

            # 如果当前 provider 正是被删的,回退到默认。role 专用 provider 用空值
            # 表示回落到全局默认;否则会留下悬空 provider id。
            if cfg.agent.provider == pid:
                env_updates["MEMORY_AGENT_PROVIDER"] = "claude_cli"
                new_agent = replace(new_agent, provider="claude_cli")
            if cfg.agent.chunk_provider == pid:
                env_updates["MEMORY_AGENT_CHUNK_PROVIDER"] = ""
                new_agent = replace(new_agent, chunk_provider="")
            if cfg.agent.extract_provider == pid:
                env_updates["MEMORY_AGENT_EXTRACT_PROVIDER"] = ""
                new_agent = replace(new_agent, extract_provider="")
            if env_updates:
                _update_dotenv(cfg.home / ".env", env_updates)
            object.__setattr__(cfg, 'agent', new_agent)

            self._json({"ok": True, "removed": pid})

        # ---- embedding 连接测试 ----
        def _api_embedding_test(self, body) -> None:
            """对 embedding 端点做一次最小探活:嵌单个短词,验证连通性和维度。"""
            _ = body  # 无参数,用当前配置
            try:
                from memory_system.embedding import get_provider
                prov = get_provider(cfg.embedding)
                if cfg.embedding.provider == "fake":
                    self._json({"ok": True, "detail": "fake embedding 始终可用"})
                    return
                # 实际调用
                vec = prov.embed_one("test")
                if not vec or not isinstance(vec, list):
                    self._json({"ok": False, "detail": "返回空向量"})
                    return
                self._json({
                    "ok": True,
                    "detail": f"嵌入成功,维度={len(vec)},模型={cfg.embedding.model}",
                    "dim": len(vec),
                })
            except Exception as e:
                self._json({"ok": False, "detail": str(e)[:300]})

        # ---- S5 审核/归档 ----
        def _api_agent_test(self, body) -> None:
            """连接测试:对指定 provider 做一次极小探活(非实际 LLM 调用)。"""
            pid = str(body.get("provider", "")).strip()
            valid_ids = set(_all_provider_ids(cfg)) | {"deepseek", "openai_compat", "qwen"}
            if not pid or pid not in valid_ids:
                return self._json({"ok": False, "detail": f"未知 provider: {pid!r}"}, 400)
            if pid in ("claude_cli",):
                from memory_system.agent.claude_cli import ClaudeCliProvider
                try:
                    prov = ClaudeCliProvider()
                    ok, why = prov.available()
                except Exception as e:
                    ok, why = False, str(e)
            elif pid == "fake":
                from memory_system.agent.fake import FakeChatProvider
                prov = FakeChatProvider()
                ok, why = prov.available()
            elif pid in ("deepseek", "openai_compat", "qwen"):
                from memory_system.agent.openai_compat import OpenAICompatProvider
                try:
                    prov = OpenAICompatProvider(cfg.agent.base_url, cfg.agent.api_key_env)
                    ok, why = prov.available()
                except Exception as e:
                    ok, why = False, str(e)
            else:
                # 自定义 provider
                cp_list = _load_custom_providers(cfg)
                cp = next((p for p in cp_list if p["id"] == pid), None)
                if not cp:
                    return self._json({"ok": False, "detail": f"自定义 provider 配置缺失: {pid!r}"}, 400)
                from memory_system.agent.openai_compat import OpenAICompatProvider
                try:
                    prov = OpenAICompatProvider(cp["base_url"], cp["api_key_env"])
                    ok, why = prov.available()
                except Exception as e:
                    ok, why = False, str(e)
            self._json({"ok": ok, "detail": why})

        def _session_for_staging(self, body) -> str | None:
            """审核接口优先用 session_id;兼容旧前端传 path 的形状。"""
            sid = _valid_session_id(body.get("session_id"))
            if sid:
                return sid
            path = _confine(body.get("path", ""))
            if path is None or not path.exists():
                self._json({"error": "缺 session_id,且 path 越界或文件不存在"}, 404)
                return None
            return preview_cache.get(cfg.preview_cache_dir, path).session_id

        def _api_confirm(self, body) -> None:
            from memory_system import archive, staging_store
            from memory_system.embedding import get_provider

            session_id = self._session_for_staging(body)
            if session_id is None:
                return
            stage_id = body.get("stage_id")
            if not stage_id:
                return self._json({"error": "缺 stage_id"}, 400)
            try:
                provider = get_provider(cfg.embedding)
                pid = archive.confirm_episode(cfg, session_id, stage_id, provider)
            except archive.ArchiveError as e:
                return self._json({"error": str(e)}, 400)
            doc = staging_store.load(cfg.staging_episodes_dir, session_id)
            self._json({"ok": True, "public_id": pid, **_ui_staging(doc)})

        def _api_reject(self, body) -> None:
            from memory_system import archive, staging_store

            session_id = self._session_for_staging(body)
            if session_id is None:
                return
            stage_id = body.get("stage_id")
            if not stage_id:
                return self._json({"error": "缺 stage_id"}, 400)
            try:
                archive.reject_episode(cfg, session_id, stage_id, body.get("reason"))
            except archive.ArchiveError as e:
                return self._json({"error": str(e)}, 400)
            doc = staging_store.load(cfg.staging_episodes_dir, session_id)
            self._json({"ok": True, **_ui_staging(doc)})

        def _api_staging_edit(self, body) -> None:
            from memory_system import staging_store

            session_id = self._session_for_staging(body)
            if session_id is None:
                return
            stage_id = body.get("stage_id")
            fields = body.get("fields")
            if not stage_id or not isinstance(fields, dict):
                return self._json({"error": "缺 stage_id 或 fields"}, 400)
            try:
                staging_store.edit_episode(cfg.staging_episodes_dir, session_id, stage_id, fields)
            except KeyError as e:
                return self._json({"error": str(e)}, 404)
            doc = staging_store.load(cfg.staging_episodes_dir, session_id)
            self._json({"ok": True, **_ui_staging(doc)})

        def _api_staging_delete(self, body) -> None:
            """干净删除一条未入库的 staging episode(不留痕,区别于 reject 的打回重做)。

            只移除 episode;对应的段仍在 chunks.json,回到「未提取」可重提。
            """
            from memory_system import staging_store

            session_id = self._session_for_staging(body)
            if session_id is None:
                return
            stage_id = body.get("stage_id")
            if not stage_id:
                return self._json({"error": "缺 stage_id"}, 400)
            try:
                staging_store.remove_episode(cfg.staging_episodes_dir, session_id, stage_id)
            except KeyError as e:
                return self._json({"error": str(e)}, 404)
            doc = staging_store.load(cfg.staging_episodes_dir, session_id)
            self._json({"ok": True, **_ui_staging(doc)})

        def _api_archive(self, body) -> None:
            from memory_system import archive

            public_id = body.get("public_id")
            if not public_id:
                return self._json({"error": "缺 public_id"}, 400)
            try:
                archive.archive_episode(cfg, public_id)
            except archive.ArchiveError as e:
                return self._json({"error": str(e)}, 400)
            self._json({"ok": True, "public_id": public_id})

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

            agent_cfg = _replace(cfg.agent, provider=cfg.agent.provider_for("chunk"))
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

        def _api_extract(self, body) -> None:
            from dataclasses import replace as _replace

            from memory_system import staging_store
            from memory_system.agent import get_chat_provider
            from memory_system.extract import existing_nodes, extract_segments

            path = _confine(body.get("path", ""))
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            if not ct.turns:
                return self._json({"error": "清洗后 0 回合,无可提取内容"}, 400)

            seg_doc = segments_store.load(cfg.chunks_dir, ct.session_id)
            if not seg_doc or not seg_doc.get("segments"):
                return self._json({"error": "无切块段;请先切块并确认分段"}, 400)
            segments = seg_doc["segments"]
            want = body.get("seg_ids")
            if want:
                want = set(want)
                segments = [s for s in segments if s.get("seg_id") in want]
                if not segments:
                    return self._json({"error": f"无匹配 seg_id: {sorted(want)}"}, 400)

            agent_cfg = _replace(cfg.agent, provider=cfg.agent.provider_for("extract"))
            if body.get("provider"):
                agent_cfg = _replace(agent_cfg, provider=body["provider"])
            model = body.get("model") or agent_cfg.extract_model
            try:
                provider = get_chat_provider(agent_cfg)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            ok, why = provider.available()
            if not ok:
                return self._json({"kind": "unavailable", "error": f"provider 不可用: {why}"}, 400)

            nodes = existing_nodes(cfg.nodes_dir)
            batch = extract_segments(ct, segments, provider, nodes, model=model,
                                     timeout=agent_cfg.timeout_s, max_retries=agent_cfg.max_retries)
            sdir = cfg.staging_episodes_dir
            ts_by_turn = {t.idx: t.timestamp for t in ct.turns}
            for seg, res, src in batch.staged:
                staging_store.upsert_episode(sdir, ct.session_id, str(path), seg, res, src,
                                             created_at=ts_by_turn.get(seg["start_turn"]))
            for seg, errors in batch.failed:
                staging_store.append_retry(sdir, ct.session_id, str(path), seg,
                                           provider=agent_cfg.provider, model=model, errors=errors)
            doc = staging_store.load(sdir, ct.session_id)
            self._json({"ok": True, "staged": len(batch.staged), "failed": len(batch.failed),
                        **_ui_staging(doc)})

        def _api_save_segments(self, body) -> None:
            path = _confine(body.get("path", ""))
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            incoming = body.get("segments")
            if not isinstance(incoming, list):
                return self._json({"error": "缺 segments 列表"}, 400)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            from memory_system.chunk import uuids_by_turn, validate_segments

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
            # P1-B:段间关系校验。重叠拒存(会重复入库);空洞放行,gaps 回前端提示。
            vr = validate_segments(norm, valid_idx)
            if not vr["ok"]:
                return self._json({"error": "段重叠,会重复入库", "overlaps": vr["overlaps"]}, 400)
            doc = segments_store.save_full(cfg.chunks_dir, ct.session_id, str(path), mtime, norm)
            self._json({"ok": True, "gaps": vr["gaps"], **_ui_doc(doc)})

        def _api_delete_segments(self, body) -> None:
            """删段(单/多)。改 chunks.json,不碰 staging episode(段与已提取条目解耦)。

            body: {session_id|path, seg_ids:[...], force?:bool}
            未 force 且有段已在蒸馏区提取出 episode → 不删,回 staged 清单让前端汇总提示;
            force=true → 照删(已提取的 episode 不受影响,仍可在蒸馏区审核/拒绝/删除)。
            """
            from memory_system import staging_store

            session_id = self._session_for_staging(body)
            if session_id is None:
                return
            seg_ids = body.get("seg_ids")
            if not isinstance(seg_ids, list) or not seg_ids:
                return self._json({"error": "缺 seg_ids 列表"}, 400)
            seg_ids = [str(x) for x in seg_ids]

            # 联动检查:这些段里哪些已在蒸馏区提取出 episode
            sdoc = staging_store.load(cfg.staging_episodes_dir, session_id)
            extracted = {e.get("seg_id") for e in (sdoc.get("episodes", []) if sdoc else [])}
            staged = [sid for sid in seg_ids if sid in extracted]
            if staged and not body.get("force"):
                return self._json({"ok": False, "needs_confirm": True, "staged": staged,
                                   "message": f"{len(staged)} 个待删段已在蒸馏区有提取"}, 409)

            doc, deleted = segments_store.delete(cfg.chunks_dir, session_id, seg_ids)
            self._json({"ok": True, "deleted": deleted, "staged": staged, **_ui_doc(doc)})

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
