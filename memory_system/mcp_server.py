"""Minimal stdio JSON-RPC server exposing the three recall tools."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import TextIO

from memory_system import __version__
from memory_system.agent.base import ChatError
from memory_system.config import Config
from memory_system.embedding.dashscope import EmbeddingError
from memory_system.log import get_logger, setup_logging
from memory_system.recall import recall_concept, recall_detail, recall_episode, reconstruct
from memory_system.recall.concept import NodeMissError

_EPISODE_DESC = (
    "回忆一段往事。只有模糊印象、想知道‘聊过什么/大意’时用。"
    "拿不准用哪个时，用这个。"
)
_DETAIL_DESC = (
    "逐字找原文。需要确切措辞、原话引用、按时间翻找时用。"
    "query 填字面词句，中文至少 3 字。"
)
_CONCEPT_DESC = (
    "调档。要找关于某人、某项目或某概念的全部记忆时用。"
    "node 填概念名（非句子）；不确定名字先用 memory_recall_episode。"
)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _tool_description(filename: str, fallback: str) -> str:
    """Read one routing description for each tools/list call; never cache it."""
    try:
        return (_PROMPT_DIR / filename).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return fallback


def _tools() -> list[dict]:
    """Build current tool definitions, falling back if a description cannot be read."""
    return [
        {
            "name": "memory_recall_episode",
            "description": _tool_description("tool_episode_desc.txt", _EPISODE_DESC),
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "memory_recall_detail",
            "description": _tool_description("tool_detail_desc.txt", _DETAIL_DESC),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "since": {"type": "string"},
                    "until": {"type": "string"},
                    "raw": {"type": "boolean"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_recall_concept",
            "description": _tool_description("tool_concept_desc.txt", _CONCEPT_DESC),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["node"],
            },
        },
    ]


class InvalidParams(ValueError):
    """The request shape is valid JSON-RPC, but method params are invalid."""


def _result_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _error(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _require_object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidParams(f"{label} 必须是 object")
    return value


def _required_text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams(f"{key} 必须是非空字符串")
    return value.strip()


def _optional_text(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParams(f"{key} 必须是字符串")
    return value.strip() or None


def _optional_bool(args: dict, key: str) -> bool:
    value = args.get(key, False)
    if not isinstance(value, bool):
        raise InvalidParams(f"{key} 必须是 boolean")
    return value


def _check_keys(args: dict, allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise InvalidParams(f"未知参数: {', '.join(unknown)}")


class MCPServer:
    """One stdio process is one recall session."""

    def __init__(self, cfg: Config, *, session_key: str | None = None) -> None:
        self.cfg = cfg
        self.session_key = session_key or f"mcp-{uuid.uuid4().hex[:12]}"

    def handle(self, request) -> dict | None:
        """Handle one decoded JSON-RPC message; notifications return None."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error(request.get("id") if isinstance(request, dict) else None,
                          -32600, "Invalid Request")

        is_notification = "id" not in request
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return None if is_notification else _error(request_id, -32600, "Invalid Request")
        if method == "notifications/initialized":
            return None

        try:
            result = self._dispatch(method, request.get("params", {}))
        except InvalidParams as exc:
            return None if is_notification else _error(request_id, -32602, str(exc))

        if result is None:
            return None if is_notification else _error(request_id, -32601, "Method not found")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params) -> dict | None:
        if method == "initialize":
            body = _require_object(params, "params")
            version = body.get("protocolVersion")
            if not isinstance(version, str) or not version:
                raise InvalidParams("protocolVersion 必须是非空字符串")
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memory-system", "version": __version__},
            }
        if method == "tools/list":
            _require_object(params, "params")
            return {"tools": _tools()}
        if method == "tools/call":
            body = _require_object(params, "params")
            name = body.get("name")
            if not isinstance(name, str):
                raise InvalidParams("name 必须是字符串")
            args = _require_object(body.get("arguments", {}), "arguments")
            try:
                return self._call_tool(name, args)
            except InvalidParams:
                raise
            except (ValueError, EmbeddingError) as exc:
                prefix = "embedding 不可用" if isinstance(exc, EmbeddingError) else "检索拒绝"
                return _result_text(f"{prefix}: {exc}\n请检查 embedding 配置与数据库 meta 锁。")
            except Exception:  # keep a single failed tool call from killing the stdio process
                get_logger().exception("MCP tool 调用异常: %s", name)
                return _result_text("检索暂时不可用，请查看 memory_system 日志后重试。")
        return None

    def _call_tool(self, name: str, args: dict) -> dict:
        if name == "memory_recall_episode":
            _check_keys(args, {"query", "raw"})
            query = _required_text(args, "query")
            raw = _optional_bool(args, "raw")
            structured = recall_episode(
                self.cfg,
                query,
                touch=True,
                session_key=self.session_key,
                injected_tool="memory_recall_episode",
            )
            if raw:
                return _result_text(_json_text(structured))
            if not structured["slots"]["primary"]:
                return _result_text("库中无相关记忆。可以换一种说法再试。")
            try:
                return _result_text(reconstruct.run(self.cfg, "episode", structured, query))
            except ChatError as exc:
                return _result_text(
                    f"重构失败，已降级为结构化结果: {exc}\n{_json_text(structured)}"
                )

        if name == "memory_recall_detail":
            _check_keys(args, {"query", "since", "until", "raw"})
            query = _required_text(args, "query")
            structured = recall_detail(
                self.cfg,
                query,
                since=_optional_text(args, "since"),
                until=_optional_text(args, "until"),
                raw=_optional_bool(args, "raw"),
                touch=True,
            )
            if not structured["hits"]:
                return _result_text(
                    f"未命中「{query}」。\n"
                    "提示: 库内原文没有逐字包含该词的段落（少于 3 字的短词已自动走子串回退），"
                    "换更具体的词或调整时间窗再试；若只是模糊印象，改用 memory_recall_episode。"
                )
            return _result_text(_json_text(structured))

        if name == "memory_recall_concept":
            _check_keys(args, {"node", "context", "raw"})
            node = _required_text(args, "node")
            context = _optional_text(args, "context")
            raw = _optional_bool(args, "raw")
            try:
                structured = recall_concept(
                    self.cfg, node, context=context, touch=True
                )
            except NodeMissError as exc:
                suggestions = "、".join(exc.suggestions) or "无"
                return _result_text(
                    f"没有叫「{exc.query}」的概念。相近: {suggestions}；"
                    "若只是模糊印象，改用 memory_recall_episode。"
                )
            if raw:
                return _result_text(_json_text(structured))
            if not structured["episodes"]:
                return _result_text(
                    f"概念「{structured['node']}」下没有挂载中的 active 情景。"
                    "若只是模糊印象，改用 memory_recall_episode。"
                )
            user_query = node if not context else f"{node}(语境: {context})"
            try:
                return _result_text(
                    reconstruct.run(self.cfg, "concept", structured, user_query)
                )
            except ChatError as exc:
                return _result_text(
                    f"重构失败，已降级为结构化结果: {exc}\n{_json_text(structured)}"
                )

        raise InvalidParams(f"未知 tool: {name}")


def serve(cfg: Config, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Run the newline-delimited stdio transport until EOF."""
    setup_logging(cfg.logs_dir)
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    server = MCPServer(cfg)
    for line in inp:
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = _error(None, -32700, "Parse error")
        else:
            response = server.handle(request)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            out.flush()
