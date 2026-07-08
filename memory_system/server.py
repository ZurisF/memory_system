"""本地审核前端的后端 —— 纯标准库 http.server,零依赖零构建(S2 选型)。

只绑 127.0.0.1。静态文件(index.html/app.js)在 web/ 下。
API:
  GET  /api/transcripts[?q=...]    列 transcript(空壳已剔除;q=对原始 jsonl grep,含导入目录)
  POST /api/import {filename,content}  上传一份 jsonl 落到 imports/(浏览器选择器只给内容)
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
  POST /api/extract  {path, seg_ids?, provider?, model?}  逐段提取五件套,并发+逐条落 staging(按块回滚)
  POST /api/confirm  {path|session_id, stage_id}    确认 staging 条目入库
  POST /api/reject   {path|session_id, stage_id, reason?}  拒绝 staging 条目(打回重做,留痕)
  POST /api/staging/edit {path|session_id, stage_id, fields}  编辑 staging 条目
  POST /api/staging/delete {path|session_id, stage_id}  干净删除未入库 staging 条目(不留痕)
  POST /api/staging/retry/clear {session_id|path, seg_ids}  关闭/忽略提取失败卡(清 retry 记录)
  POST /api/memory/edit {public_id, fields}  编辑已入库 episode 的正文四件(改 overview 重嵌)
  POST /api/recall   {mode, query, context?, touch, reconstruct, since?, until?, user_query?}  三路检索(+可选重构)
  DELETE /api/memory?public_id=...  真删一条 episode(碎片正本 + DB;孤儿 node 保留并回报)
  DELETE /api/node?label=...        真删一个 node(碎片 + DB;并从引用它的 episode 碎片摘除)
"""

from __future__ import annotations

import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from memory_system import preview_cache, processed, segments_store
from memory_system.agent import probe_provider, registry
from memory_system.config import Config
from memory_system.db import migrate
from memory_system.db.connection import connect
from memory_system.env import update_dotenv
from memory_system.transcript import describe, discover
from memory_system.ui_shape import ui_doc, ui_staging

_WEB = Path(__file__).parent / "web"
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

# 蒸馏区批量提取的并发段数:慢 I/O(逐段 LLM 调用)并发,落盘仍在主线程串行(见
# extract.extract_segments)。保守取 4——够提速,又不至于把 claude_cli 子进程 / API 速率打爆。
EXTRACT_MAX_WORKERS = 4

# provider 知识(内置目录 / 自定义增删 / 可用性 / key 状态 / 掩码 / 占位 key)统一在
# memory_system.agent.registry;.env 写回在 env.update_dotenv;送前端的 uuid 剥离在
# ui_shape;探活在 embedding.probe / agent.probe_provider。本文件只做 HTTP 编排。


def _raw_grep(path: Path, needle: str) -> bool:
    """对原始 jsonl 文本(+ 路径串)做大小写不敏感子串匹配;needle 须已 lower。

    不清洗、不解析——噪声(system/tool/uuid)可被命中,误差可接受(ARCHITECTURE §10 取舍)。
    """
    if needle in str(path).lower():
        return True
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def make_handler(cfg: Config):
    def _valid_session_id(raw: object) -> str | None:
        sid = str(raw or "").strip()
        if not sid or "/" in sid or "\\" in sid or ".." in sid:
            return None
        return sid

    def _confine(raw: str) -> Path | None:
        """把传入 path 限制在 transcripts_root 或 imports_dir 内;越界/无效返回 None
        (堵任意文件读)。导入的 jsonl 落在 imports_dir,故两根都放行。"""
        if not raw:
            return None
        try:
            p = Path(raw).expanduser().resolve()
            bases = [cfg.transcripts_root.resolve(), cfg.imports_dir.resolve()]
        except (OSError, RuntimeError):
            return None
        for base in bases:
            if p == base or base in p.parents:
                return p
        return None

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
                return self._api_transcripts(parse_qs(u.query))
            if u.path == "/api/transcript":
                return self._api_transcript(parse_qs(u.query))
            if u.path == "/api/agent/providers":
                return self._json({"providers": registry.providers_info(cfg),
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
            if u.path == "/api/prompts":
                return self._api_prompts()
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

            # 重新加载 .env:只刷新「本来就来自 .env 的键」(env._dotenv_owned),
            # shell export 的真 key 永不被 .env 的占位/旧值覆盖(修复:旧代码
            # override=True 会让打开一次控制台就把 export 的真 key 冲成占位 key)。
            load_dotenv(cfg.home / ".env")
            # 同步更新内存 cfg 的 custom_providers(可能刚通过控制台添加/删除)。
            # cfg.agent 热改统一在 CUSTOM_LOCK 下做,防多线程用陈旧快照互相覆盖。
            with registry.CUSTOM_LOCK:
                new_cp = registry.custom_map(cfg.home)
                if new_cp != cfg.agent.custom_providers:
                    object.__setattr__(cfg, 'agent', replace(cfg.agent, custom_providers=new_cp))

            providers = registry.providers_info(cfg)
            agents = {}
            for role, default_model in [("chunk", cfg.agent.chunk_model),
                                         ("extract", cfg.agent.extract_model)]:
                agents[role] = {
                    "provider": cfg.agent.provider_for(role),
                    "model": default_model,
                    "providers": providers,
                }
            # 重构(recall)专用 provider 通道(S6 Phase 2):provider 如实返回 override 原值
            # (空串 = 跟随全局,由前端显示「跟随全局」,不在此解析成有效 provider);
            # providers 列表与 chunk/extract 同源同形状。
            agents["recall"] = {
                "model": cfg.agent.recall_model,
                "provider": cfg.agent.recall_provider,
                "providers": providers,
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
                "key_masked": registry.mask_key(emb.api_key_env),
            }

            # 各 provider 的 key 状态(内置无 key 项报 None;compat 家族共用;自定义各自一份)
            agent_keys = registry.agent_key_status(cfg)

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
            if role not in ("chunk", "extract", "recall"):
                return self._json({"error": "role 必须是 chunk、extract 或 recall"}, 400)

            updates: dict[str, str] = {}
            # provider 切换(按 role 独立,不再共享)。recall 接受空串 = 清 override 跟随全局
            # (S6 Phase 2);chunk/extract 沿用旧语义:空串视作「未改」。
            provider = str(body.get("provider", "")).strip()
            if role == "recall" and "provider" in body:
                if provider == "" or provider in registry.all_provider_ids(cfg):
                    updates["MEMORY_AGENT_RECALL_PROVIDER"] = provider
            elif role != "recall" and provider and provider in registry.all_provider_ids(cfg):
                prov_key = {"chunk": "MEMORY_AGENT_CHUNK_PROVIDER",
                            "extract": "MEMORY_AGENT_EXTRACT_PROVIDER"}[role]
                updates[prov_key] = provider

            # model 切换(按 role)
            model = str(body.get("model", "")).strip()
            if model:
                model_key = {"chunk": "MEMORY_AGENT_CHUNK_MODEL",
                             "extract": "MEMORY_AGENT_EXTRACT_MODEL",
                             "recall": "MEMORY_AGENT_RECALL_MODEL"}[role]
                updates[model_key] = model

            if not updates:
                return self._json({"error": "缺少 provider 或 model"}, 400)

            env_path = cfg.home / ".env"
            with registry.CUSTOM_LOCK:
                try:
                    update_dotenv(env_path, updates)
                except OSError as e:
                    return self._json({"error": f"写入 .env 失败: {e}"}, 500)

                # 更新内存中的 cfg(绕过 frozen),让 GET /api/agent/config 即时反映变更
                new_agent = cfg.agent
                if "MEMORY_AGENT_CHUNK_PROVIDER" in updates:
                    new_agent = replace(new_agent, chunk_provider=updates["MEMORY_AGENT_CHUNK_PROVIDER"])
                if "MEMORY_AGENT_EXTRACT_PROVIDER" in updates:
                    new_agent = replace(new_agent, extract_provider=updates["MEMORY_AGENT_EXTRACT_PROVIDER"])
                if "MEMORY_AGENT_RECALL_PROVIDER" in updates:
                    new_agent = replace(new_agent, recall_provider=updates["MEMORY_AGENT_RECALL_PROVIDER"])
                if "MEMORY_AGENT_CHUNK_MODEL" in updates:
                    new_agent = replace(new_agent, chunk_model=updates["MEMORY_AGENT_CHUNK_MODEL"])
                if "MEMORY_AGENT_EXTRACT_MODEL" in updates:
                    new_agent = replace(new_agent, extract_model=updates["MEMORY_AGENT_EXTRACT_MODEL"])
                if "MEMORY_AGENT_RECALL_MODEL" in updates:
                    new_agent = replace(new_agent, recall_model=updates["MEMORY_AGENT_RECALL_MODEL"])
                object.__setattr__(cfg, 'agent', new_agent)

            self._json({"ok": True, "updated": updates,
                        "restart_required": True,
                        "hint": "provider/model 变更已写入 .env 并同步当前页面;已存在的 LLM 调用路径需重启服务才能全局生效"})

        # ---- 过程 prompt 正本(切块/提取/重构)----
        def _api_prompts(self) -> None:
            """列出五个过程 prompt 正本(键/所属过程/内容)。正本是文件、不进 DB。"""
            from memory_system import prompt_store

            self._json({"prompts": prompt_store.list_prompts(),
                        "process_labels": prompt_store.PROCESS_LABELS})

        def _api_save_prompt(self, body) -> None:
            """写回一个过程 prompt 正本。白名单外的 name / 空 content → 400。

            切块/提取的 prompt 加载有 @lru_cache,prompt_store.write_prompt 写盘后即清缓存,
            故改完即时生效、无需重启;重构每次现读。回最新的五键列表供前端刷新。
            """
            from memory_system import prompt_store

            name = str(body.get("name", "")).strip()
            content = body.get("content")
            if not name:
                return self._json({"error": "缺 name"}, 400)
            if not isinstance(content, str):
                return self._json({"error": "缺 content(须为字符串)"}, 400)
            try:
                prompt_store.write_prompt(name, content)
            except prompt_store.PromptError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"写入 prompt 失败: {e}"}, 500)
            self._json({"ok": True, "name": name,
                        "prompts": prompt_store.list_prompts()})

        def _api_get_staging(self, q) -> None:
            from memory_system import staging_store

            sid = _valid_session_id((q.get("session_id") or [""])[0])
            if sid:
                doc = staging_store.load(cfg.staging_episodes_dir, sid)
                return self._json(ui_staging(doc))
            path = _confine((q.get("path") or [""])[0])
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            ct = preview_cache.get(cfg.preview_cache_dir, path)
            doc = staging_store.load(cfg.staging_episodes_dir, ct.session_id)
            self._json(ui_staging(doc))

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
                ui = ui_doc(doc)
                s = _slot(f.stem, doc.get("source_path", ""))
                s["segments"] = ui["segments"]
                s["chunk_retry"] = ui["retry"]
                s["updated_at"] = _newer(s["updated_at"], ui.get("updated_at"))

            # 已提取的五件套(episodes 工作态)
            for f in sorted(cfg.staging_episodes_dir.glob("*.json")):
                doc = staging_store.load(cfg.staging_episodes_dir, f.stem)
                if not doc:
                    continue
                ui = ui_staging(doc)
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
            self._json(ui_doc(doc))

        def _api_transcripts(self, q) -> None:
            # q:对原始 jsonl 文本做子串 grep(不清洗,噪声可接受——见 ARCHITECTURE §10)。
            # 命中才清洗,故搜索时反而省下大量 clean 成本。空 q = 全量列表。
            needle = (q.get("q") or [""])[0].strip().lower()
            imports = {p.resolve() for p in cfg.imports_dir.glob("*.jsonl")} \
                if cfg.imports_dir.exists() else set()
            infos = discover(cfg.transcripts_root) + \
                discover(cfg.imports_dir, pattern="*.jsonl")
            infos.sort(key=lambda i: i.mtime, reverse=True)
            # 磁盘上已动过的会话(有 chunks 段工作态或 staging 提取):列表里沉底
            touched = {p.stem for p in cfg.chunks_dir.glob("*.json")}
            touched |= {p.stem for p in cfg.staging_episodes_dir.glob("*.json")}
            items = []
            hidden_empty = 0
            for i in infos:
                if needle and not _raw_grep(i.path, needle):
                    continue
                # 清洗后 0 回合 = /clear 空壳等垃圾文件,剔除(人工审核前先去噪)。
                ct = preview_cache.get(cfg.preview_cache_dir, i.path, mtime=i.mtime)
                if not ct.turns:
                    hidden_empty += 1
                    continue
                items.append(
                    {"session_id": i.session_id, "path": str(i.path), "cwd": i.cwd,
                     "mtime": i.mtime, "size": i.size,
                     "turn_count": len(ct.turns), "maybe_writing": i.maybe_writing,
                     "touched": i.session_id in touched,
                     "imported": i.path.resolve() in imports})
            self._json({"root": str(cfg.transcripts_root),
                        "imports_root": str(cfg.imports_dir),
                        "query": needle, "hidden_empty": hidden_empty,
                        "transcripts": items})

        def _api_transcript(self, q) -> None:
            path = _confine((q.get("path") or [""])[0])
            if path is None or not path.exists():
                return self._json({"error": "路径越界或文件不存在"}, 404)
            try:
                info = describe(path)
            except OSError:  # exists 到 stat 之间被清理
                return self._json({"error": "文件已被清理"}, 404)
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
                "/api/memory/edit": self._api_edit_memory,
                "/api/recall": self._api_recall,
                "/api/staging/edit": self._api_staging_edit,
                "/api/staging/delete": self._api_staging_delete,
                "/api/staging/retry/clear": self._api_clear_retry,
                "/api/agent/test": self._api_agent_test,
                "/api/agent/config": self._api_agent_config_post,
                "/api/agent/providers": self._api_add_provider,
                "/api/embedding/test": self._api_embedding_test,
                "/api/import": self._api_import,
                "/api/prompts": self._api_save_prompt,
            }
            handler = routes.get(u.path)
            if handler is None:
                return self._send(404, b"not found", "text/plain")
            body = self._read_json_body()
            if body is None:
                return
            return handler(body)

        # 请求体上限:导入大 jsonl 也远够;再大直接拒,防把内存吃穿(仅绑本机,防御性)。
        _MAX_BODY = 128 * 1024 * 1024

        def _read_json_body(self) -> dict | None:
            """读并解析 POST/PUT 请求体。非法 Content-Length / 超限 / 非 JSON → 回错并返回 None。"""
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._json({"error": "Content-Length 非法"}, 400)
                return None
            if n < 0 or n > self._MAX_BODY:
                self._json({"error": f"请求体过大(>{self._MAX_BODY // (1024 * 1024)}MB)"}, 413)
                return None
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "请求体非 JSON"}, 400)
                return None
            return body

        def do_DELETE(self) -> None:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/api/agent/providers":
                return self._api_remove_provider((qs.get("id") or [""])[0].strip())
            if u.path == "/api/memory":
                return self._api_delete_memory((qs.get("public_id") or [""])[0].strip())
            if u.path == "/api/node":
                return self._api_delete_node((qs.get("label") or [""])[0])
            self._send(404, b"not found", "text/plain")

        def do_PUT(self) -> None:
            u = urlparse(self.path)
            if u.path != "/api/agent/providers":
                return self._send(404, b"not found", "text/plain")
            body = self._read_json_body()
            if body is None:
                return
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

            from datetime import datetime, timezone

            placeholder = registry.PLACEHOLDER_KEY
            with registry.CUSTOM_LOCK:
                # 去重(锁内读改写,防并发添加互相覆盖)
                existing = registry.load_custom(cfg.home)
                if any(p["id"] == pid for p in existing):
                    return self._json({"error": f"provider id {pid!r} 已存在"}, 409)

                cp = {
                    "id": pid,
                    "name": name,
                    "base_url": base_url.rstrip("/"),
                    "api_key_env": env_var,
                    "default_model": model or "",
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }

                # 写 .env:占位 key + 同步环境
                update_dotenv(cfg.home / ".env", {env_var: placeholder})

                # 保存到 custom_providers.json
                existing.append(cp)
                registry.save_custom(cfg.home, existing)

                # 更新内存 cfg
                new_cp_map = dict(cfg.agent.custom_providers)
                new_cp_map[pid] = {"base_url": cp["base_url"], "api_key_env": env_var,
                                   "default_model": cp["default_model"]}
                object.__setattr__(cfg, 'agent', replace(cfg.agent, custom_providers=new_cp_map))

            self._json({
                "ok": True,
                "provider": cp,
                "hint": f"Key 占位已写入 .env 的 {env_var}={placeholder};请到 ~/.memory_system/.env 替换为真实 key 后再测试连接",
                "warnings": _hints if _hints else None,
            })

        def _api_update_provider(self, body) -> None:
            """修改自定义 OpenAI 兼容 provider 的显示名、base_url、默认模型。

            id/api_key_env 不改,避免 .env key 变量被隐式迁移。
            """
            pid = str(body.get("id", "")).strip()
            if not pid:
                return self._json({"error": "缺 id"}, 400)
            if registry.is_builtin(pid):
                return self._json({"error": f"内置 provider {pid!r} 不可修改"}, 403)

            name = str(body.get("name", "")).strip()
            base_url = str(body.get("base_url", "")).strip()
            model = str(body.get("model", "")).strip()
            if not name or not base_url:
                return self._json({"error": "name 和 base_url 必填"}, 400)
            if not base_url.startswith("https://") and not base_url.startswith("http://"):
                return self._json({"error": "base_url 必须以 http:// 或 https:// 开头"}, 400)

            with registry.CUSTOM_LOCK:
                existing = registry.load_custom(cfg.home)
                idx = next((i for i, p in enumerate(existing) if p.get("id") == pid), None)
                if idx is None:
                    return self._json({"error": f"provider {pid!r} 不存在"}, 404)

                cp = dict(existing[idx])
                cp["name"] = name
                cp["base_url"] = base_url.rstrip("/")
                cp["default_model"] = model
                existing[idx] = cp
                registry.save_custom(cfg.home, existing)

                new_cp_map = dict(cfg.agent.custom_providers)
                new_cp_map[pid] = {"base_url": cp["base_url"], "api_key_env": cp["api_key_env"],
                                   "default_model": cp.get("default_model", "")}
                object.__setattr__(cfg, 'agent', replace(cfg.agent, custom_providers=new_cp_map))

            self._json({"ok": True, "provider": cp})

        def _api_remove_provider(self, pid: str) -> None:
            """删除自定义 provider(内置 provider 不可删)。"""
            if not pid:
                return self._json({"error": "缺 id"}, 400)
            if registry.is_builtin(pid):
                return self._json({"error": f"内置 provider {pid!r} 不可删除"}, 403)

            with registry.CUSTOM_LOCK:
                existing = registry.load_custom(cfg.home)
                cp = next((p for p in existing if p["id"] == pid), None)
                if not cp:
                    return self._json({"error": f"provider {pid!r} 不存在"}, 404)

                # 不移除 .env 中的 key 变量(用户可能以后还要用);只删 JSON 条目
                existing = [p for p in existing if p["id"] != pid]
                registry.save_custom(cfg.home, existing)

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
                if cfg.agent.recall_provider == pid:
                    env_updates["MEMORY_AGENT_RECALL_PROVIDER"] = ""
                    new_agent = replace(new_agent, recall_provider="")
                if env_updates:
                    update_dotenv(cfg.home / ".env", env_updates)
                object.__setattr__(cfg, 'agent', new_agent)

            self._json({"ok": True, "removed": pid})

        # ---- embedding 连接测试 ----
        def _api_embedding_test(self, body) -> None:
            """对 embedding 端点做一次最小探活(探活逻辑在 embedding.probe,这里只编排)。"""
            _ = body  # 无参数,用当前配置
            from memory_system import embedding

            ok, detail, dim = embedding.probe(cfg.embedding)
            out: dict = {"ok": ok, "detail": detail}
            if dim is not None:
                out["dim"] = dim
            self._json(out)

        # ---- S5 审核/归档 ----
        def _api_agent_test(self, body) -> None:
            """连接测试:对指定 provider 做一次极小探活(非实际 LLM 调用)。

            探活逻辑在 agent.probe_provider;这里只做请求校验(未知 id 回 400)与编排。
            """
            pid = str(body.get("provider", "")).strip()
            if not pid or pid not in set(registry.all_provider_ids(cfg)):
                return self._json({"ok": False, "detail": f"未知 provider: {pid!r}"}, 400)
            ok, why = probe_provider(cfg.agent, pid, registry.custom_map(cfg.home))
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
            self._json({"ok": True, "public_id": pid, **ui_staging(doc)})

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
            self._json({"ok": True, **ui_staging(doc)})

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
            self._json({"ok": True, **ui_staging(doc)})

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
            self._json({"ok": True, **ui_staging(doc)})

        def _api_clear_retry(self, body) -> None:
            """关闭/忽略提取失败卡:按 seg_id 从 retry 列表移除(不动 episodes、不留痕)。

            body: {session_id|path, seg_ids:[...]}。人工判定这段不再重试时清掉告警。
            """
            from memory_system import staging_store

            session_id = self._session_for_staging(body)
            if session_id is None:
                return
            seg_ids = body.get("seg_ids")
            if not isinstance(seg_ids, list) or not seg_ids:
                return self._json({"error": "缺 seg_ids 列表"}, 400)
            staging_store.clear_retry(cfg.staging_episodes_dir, session_id, [str(x) for x in seg_ids])
            doc = staging_store.load(cfg.staging_episodes_dir, session_id)
            self._json({"ok": True, **ui_staging(doc)})

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

        def _api_edit_memory(self, body) -> None:
            """编辑已入库 episode 的正文四件(overview/summary/highlights/salience_tier)。

            改 overview 才重嵌(用当前 embedding provider)。回更新后的整条 memory + 报告
            (changed/reembedded),供前端刷新面板。source_text/nodes 不可改(editor 白名单挡)。
            """
            from memory_system import editor, views
            from memory_system.embedding import get_provider

            public_id = str(body.get("public_id", "")).strip()
            fields = body.get("fields")
            if not public_id:
                return self._json({"error": "缺 public_id"}, 400)
            if not isinstance(fields, dict):
                return self._json({"error": "缺 fields"}, 400)
            try:
                rep = editor.edit_episode(cfg, public_id, fields, get_provider(cfg.embedding))
            except editor.EditError as e:
                return self._json({"error": str(e)}, 400)
            self._json({"ok": True, "public_id": rep.public_id,
                        "changed": rep.changed, "reembedded": rep.reembedded,
                        "memory": views.read_memory(cfg, public_id)})

        # ---- 召回屏(S6 三路检索 + 可选重构)----
        def _api_recall(self, body) -> None:
            """三路检索。body: {mode, query, context?, touch:false, reconstruct:false, since?, until?, user_query?}。

            user_query(可选)= 模拟当轮 query:检索词与用户真实问话经常不同,重构 prompt
            以后者为核心取舍;召回屏调 prompt 时用它测这个差异。空 = 沿用检索词(现状)。

            响应恒为 {"structured":…, "reconstruction": 文本|null, "error": 人读消息|null}:
            - touch 默认 false(召回屏默认只读窥视,勾「刷时钟」才透传 true);
            - reconstruct=true 且 mode∈{episode,concept} 时调 reconstruct.run;ChatError 不 5xx——
              结构化结果照常返回,reconstruction=null + error 带人读消息(前端降级展示);
            - detail 请求 reconstruct 直接 400(细节不接重构,逐字保真是它的输出);
            - concept miss(NodeMissError)回 200:episodes 空 + suggestions 透传("你是不是想找");
            - meta 锁拒检 / embedding 不可用等运行期异常折成带人读消息的 200 响应,不裸 500。
            """
            from memory_system.agent.base import ChatError
            from memory_system.embedding.dashscope import EmbeddingError
            from memory_system.log import setup_logging
            from memory_system.recall import recall_concept, recall_detail, recall_episode, reconstruct
            from memory_system.recall.concept import NodeMissError

            mode = str(body.get("mode", "")).strip()
            if mode not in ("episode", "detail", "concept"):
                return self._json({"error": "mode 必须是 episode、detail 或 concept"}, 400)
            query = str(body.get("query", "")).strip()
            if not query:
                return self._json({"error": "缺 query"}, 400)
            touch = bool(body.get("touch", False))
            want_rec = bool(body.get("reconstruct", False))
            if want_rec and mode == "detail":
                return self._json({"error": "细节检索不接重构:开窗/逐字就是它的输出"}, 400)

            def _opt(key: str) -> str | None:
                s = str(body.get(key) or "").strip()
                return s or None

            def _degraded(msg: str) -> None:
                # 运行期检索失败:不裸 500,折成人读消息(前端显降级提示条)。
                self._json({"structured": None, "reconstruction": None, "error": msg})

            context = _opt("context")
            try:
                if mode == "detail":
                    structured = recall_detail(cfg, query, since=_opt("since"),
                                               until=_opt("until"), touch=touch)
                elif mode == "episode":
                    structured = recall_episode(cfg, query, touch=touch)
                else:
                    try:
                        structured = recall_concept(cfg, query, context=context, touch=touch)
                    except NodeMissError as e:
                        # miss 是正常业务结果:200,建议透传给前端("你是不是想找")。
                        return self._json({
                            "structured": {"mode": "concept", "node": e.query,
                                           "alias_bridge": None, "episodes": [],
                                           "suggestions": e.suggestions},
                            "reconstruction": None, "error": str(e)})
            except ValueError as e:  # meta 锁不符等(照 CLI「检索拒绝」语义)
                return _degraded(f"检索拒绝: {e}")
            except EmbeddingError as e:
                return _degraded(f"embedding 不可用: {e}")

            reconstruction = None
            err = None
            if want_rec:
                has_hits = bool(structured["slots"]["primary"]) if mode == "episode" \
                    else bool(structured["episodes"])
                if not has_hits:
                    err = "无命中结果,无可重构"
                else:
                    # 模拟当轮 query 给了就原样当用户问话;否则沿用检索词
                    # (concept 拼语境,照 CLI _recall_concept 惯例)。
                    sim = _opt("user_query")
                    user_query = sim or (query if mode == "episode" or not context
                                         else f"{query}(语境: {context})")
                    setup_logging(cfg.logs_dir)  # reconstruct 要写候选集日志(召回可重放)
                    try:
                        reconstruction = reconstruct.run(cfg, mode, structured, user_query)
                    except ChatError as e:
                        err = f"重构失败(结构化结果照常展示): {e}"
            self._json({"structured": structured, "reconstruction": reconstruction,
                        "error": err})

        def _api_delete_memory(self, public_id: str) -> None:
            """真删一条 episode(碎片正本 + DB 索引/膜/向量/FTS,区别于 archive 软降级)。

            回 orphaned_nodes:删后不再挂任何 episode、碎片仍在的 node,供前端提示用户再清理。
            """
            from memory_system import archive

            if not public_id:
                return self._json({"error": "缺 public_id"}, 400)
            try:
                rep = archive.delete_episode(cfg, public_id)
            except archive.ArchiveError as e:
                return self._json({"error": str(e)}, 404)
            self._json({"ok": True, "public_id": rep.public_id,
                        "orphaned_nodes": rep.orphaned_nodes})

        def _api_delete_node(self, label: str) -> None:
            """真删一个 node(碎片 + DB 节点/别名/膜),并从所有引用它的 episode 碎片摘除该 label。

            回 dereferenced_episodes:被摘除引用的 episode public_id(episode 本身保留)。
            """
            from memory_system import archive

            if not (label or "").strip():
                return self._json({"error": "缺 label"}, 400)
            try:
                rep = archive.delete_node(cfg, label)
            except archive.ArchiveError as e:
                return self._json({"error": str(e)}, 404)
            self._json({"ok": True, "label": rep.label,
                        "dereferenced_episodes": rep.dereferenced_episodes})

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
                                   "errors": e.errors, **ui_doc(doc)}, 502)
            doc = segments_store.record_agent_run(cfg.chunks_dir, ct.session_id,
                                                  str(path), mtime, res)
            self._json({"ok": True, **ui_doc(doc)})

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
            sdir = cfg.staging_episodes_dir
            ts_by_turn = {t.idx: t.timestamp for t in ct.turns}

            # 逐条落盘:每段一完成立即写 staging,故批量提取中途退出已完成的不丢。
            # extract_segments 保证回调都在主线程串行调用(无并发写 staging 文件)。
            def _on_staged(seg, res, src):
                staging_store.upsert_episode(sdir, ct.session_id, str(path), seg, res, src,
                                             created_at=ts_by_turn.get(seg["start_turn"]))

            def _on_failed(seg, errors):
                staging_store.append_retry(sdir, ct.session_id, str(path), seg,
                                           provider=agent_cfg.provider, model=model, errors=errors)

            batch = extract_segments(ct, segments, provider, nodes, model=model,
                                     timeout=agent_cfg.timeout_s, max_retries=agent_cfg.max_retries,
                                     max_workers=EXTRACT_MAX_WORKERS,
                                     on_staged=_on_staged, on_failed=_on_failed)
            doc = staging_store.load(sdir, ct.session_id)
            self._json({"ok": True, "staged": len(batch.staged), "failed": len(batch.failed),
                        **ui_staging(doc)})

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
            self._json({"ok": True, "gaps": vr["gaps"], **ui_doc(doc)})

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
            self._json({"ok": True, "deleted": deleted, "staged": staged, **ui_doc(doc)})

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

        def _api_import(self, body) -> None:
            """接收前端上传的 jsonl 文本,落到 imports_dir,刷新后即出现在切段区列表。

            浏览器文件选择器只给内容、不给真实磁盘路径,故走上传:{filename, content}。
            落点是数据主目录 imports/(transcripts_root 之外的第二发现根),不污染
            ~/.claude/projects;与正本/工作态无关,可删。
            """
            raw_name = str(body.get("filename", "")).strip()
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                return self._json({"error": "缺 content 或为空"}, 400)
            # 文件名只取 basename(堵路径穿越),落到 .jsonl 后缀
            stem = Path(raw_name).name.strip() or "imported"
            if not stem.lower().endswith(".jsonl"):
                stem += ".jsonl"
            # 粗校验:至少一行能解析成 JSON 对象,否则不像 transcript(坏行宽容跳过)
            ok_line = False
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if isinstance(json.loads(line), dict):
                        ok_line = True
                        break
                except json.JSONDecodeError:
                    continue
            if not ok_line:
                return self._json({"error": "内容里没有 JSON 对象行,不像 Claude transcript"}, 400)
            # 落盘:同名加数字后缀去重
            cfg.imports_dir.mkdir(parents=True, exist_ok=True)
            dest = cfg.imports_dir / stem
            if dest.exists():
                base, n = dest.stem, 1
                while dest.exists():
                    dest = cfg.imports_dir / f"{base}-{n}.jsonl"
                    n += 1
            try:
                dest.write_text(content, encoding="utf-8")
            except OSError as e:
                return self._json({"error": f"写入失败: {e}"}, 500)
            self._json({"ok": True, "path": str(dest), "session_id": dest.stem})

    return Handler


def serve(cfg: Config, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    for d in cfg.all_dirs():  # 幂等:补齐目录布局(如 imports/),老主目录无需重 init
        d.mkdir(parents=True, exist_ok=True)
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
