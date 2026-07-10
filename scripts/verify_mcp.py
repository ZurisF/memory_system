"""Offline acceptance checks for the minimal stdio MCP recall server."""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = Path(tempfile.mkdtemp(prefix="memsys_mcp_"))
HOME = TMP / "home"
EMPTY_HOME = TMP / "empty"
os.environ["MEMORY_SYSTEM_HOME"] = str(HOME)
os.environ["MEMORY_EMBED_PROVIDER"] = "fake"
os.environ["MEMORY_EMBED_DIM"] = "16"
os.environ["MEMORY_AGENT_PROVIDER"] = "fake"
os.environ["MEMORY_RECALL_CANDIDATE_MULTIPLIER"] = "1"

from memory_system.agent.base import ChatError  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.embedding.dashscope import EmbeddingError  # noqa: E402
from memory_system.embedding.fake import FakeProvider  # noqa: E402
from memory_system.fragments import Episode, Node, write_episode, write_node  # noqa: E402
from memory_system.index import rebuild  # noqa: E402
from memory_system.mcp_server import MCPServer  # noqa: E402

CFG = load_config()
QUERY = "星际航行与曲率引擎"


def ok(message: str) -> None:
    print(f"  [ok] {message}")


def build_corpus() -> None:
    for directory in CFG.all_dirs():
        directory.mkdir(parents=True, exist_ok=True)
    episodes = [
        Episode(
            public_id="ep_mcp0001", overview=QUERY, summary="主情景摘要",
            source_text=f"我们说过逐字暗号，也讨论了{QUERY}的可行性。",
            salience_tier=3, status="active", created_at="2026-07-01T10:00:00+00:00",
            activated_at="2026-07-01T10:00:00+00:00", source_session_id="mcp-session-a",
            nodes=["曲率引擎"], highlights=[{"text": "逐字暗号", "tag": "原话"}],
        ),
        Episode(
            public_id="ep_mcp0002", overview="同源的前置讨论", summary="同源摘要",
            source_text="先讨论星舰的燃料与航线。", salience_tier=2, status="active",
            created_at="2026-07-01T09:00:00+00:00", activated_at="2026-07-01T09:00:00+00:00",
            source_session_id="mcp-session-a", nodes=["曲率引擎"],
        ),
        Episode(
            public_id="ep_mcp0003", overview=QUERY, summary="第二条直接相关情景",
            source_text=f"另一次也提到{QUERY}。", salience_tier=2, status="active",
            created_at="2026-07-02T10:00:00+00:00", activated_at="2026-07-02T10:00:00+00:00",
            source_session_id="mcp-session-b", nodes=["曲率引擎"],
        ),
        Episode(
            public_id="ep_mcp0004", overview="曲率引擎的伦理边界", summary="关联情景摘要",
            source_text="从高速旅行谈到伦理限制。", salience_tier=2, status="active",
            created_at="2026-07-03T10:00:00+00:00", activated_at="2026-07-03T10:00:00+00:00",
            source_session_id="mcp-session-c", nodes=["曲率引擎"],
        ),
        Episode(
            public_id="ep_mcp0005", overview="花园灌溉记录", summary="不相关情景一",
            source_text="给番茄和薄荷浇水。", salience_tier=1, status="active",
            created_at="2026-07-04T10:00:00+00:00", activated_at="2026-07-04T10:00:00+00:00",
            source_session_id="mcp-session-d",
        ),
        Episode(
            public_id="ep_mcp0006", overview="厨房烘焙记录", summary="不相关情景二",
            source_text="烤了一盘蓝莓松饼。", salience_tier=1, status="active",
            created_at="2026-07-05T10:00:00+00:00", activated_at="2026-07-05T10:00:00+00:00",
            source_session_id="mcp-session-e",
        ),
    ]
    for episode in episodes:
        write_episode(CFG.episodes_dir, episode)
    write_node(
        CFG.nodes_dir,
        Node(label="曲率引擎", aliases=["曲率项目"], type="concept",
             created_at="2026-07-01T00:00:00+00:00", updated_at="2026-07-01T00:00:00+00:00"),
    )
    report = rebuild(CFG, FakeProvider(model="fake", dim=16))
    assert report.episodes == len(episodes), report


def pids(structured: dict) -> set[str]:
    slots = structured["slots"]
    return {
        item["public_id"]
        for slot in ("primary", "same_source", "associative")
        for item in slots[slot]
    }


def tool_text(response: dict) -> str:
    content = response["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    assert isinstance(content[0]["text"], str)
    return content[0]["text"]


def assert_redline(value) -> None:
    sensitive = {"id", "episode_id", "node_id", "rowid", "embedding", "uuid"}
    uuid_pattern = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    )

    def walk(item) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                assert key.lower() not in sensitive, (key, value)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            assert uuid_pattern.search(item) is None, item

    walk(value)


class Child:
    def __init__(
        self,
        home: Path = HOME,
        *,
        env_updates: dict[str, str] | None = None,
        unset_keys: set[str] | None = None,
    ) -> None:
        env = os.environ.copy()
        env["MEMORY_SYSTEM_HOME"] = str(home)
        for key in (
            "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"
        ):
            env.pop(key, None)
        for key in unset_keys or set():
            env.pop(key, None)
        env.update(env_updates or {})
        env["no_proxy"] = "127.0.0.1,localhost"
        command = Path(sys.executable).with_name("memory-system")
        assert command.exists(), f"CLI entrypoint missing: {command}"
        self.stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(command), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, text=True, encoding="utf-8", env=env, bufsize=1,
        )
        self.lines: list[str] = []

    def _read(self) -> dict:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], 8)
        assert ready, "timed out waiting for MCP response"
        line = self.process.stdout.readline()
        assert line, f"MCP exited early with {self.process.poll()}"
        self.lines.append(line)
        parsed = json.loads(line)
        assert isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0", line
        return parsed

    def raw(self, line: str) -> dict:
        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        return self._read()

    def call(self, request_id, method: str, params: dict | None = None) -> dict:
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        response = self.raw(json.dumps(body, ensure_ascii=False))
        assert response.get("id") == request_id, (response, request_id)
        return response

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.process.stdin is not None
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self.process.stdin.write(json.dumps(body) + "\n")
        self.process.stdin.flush()

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=8)
        self.stderr.seek(0)
        stderr = self.stderr.read()
        self.stderr.close()
        assert self.process.returncode == 0, stderr
        assert self.process.stdout is not None
        self.lines.extend(self.process.stdout.readlines())
        assert all(json.loads(line).get("jsonrpc") == "2.0" for line in self.lines)


def verify_protocol_and_tools() -> None:
    child = Child()
    episode_desc_path = (
        Path(__file__).resolve().parents[1]
        / "memory_system" / "prompts" / "tool_episode_desc.txt"
    )
    original_episode_desc = episode_desc_path.read_text(encoding="utf-8")
    try:
        init = child.call("init", "initialize", {
            "protocolVersion": "2025-06-18", "clientInfo": {"name": "verify", "version": "1"}
        })
        result = init["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert result["capabilities"] == {"tools": {}}
        assert set(result["serverInfo"]) == {"name", "version"}

        child.notify("notifications/initialized")
        listed_response = child.call("list-after-notification", "tools/list", {})
        tools = listed_response["result"]["tools"]
        assert [tool["name"] for tool in tools] == [
            "memory_recall_episode", "memory_recall_detail", "memory_recall_concept"
        ]
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        descriptions = {tool["name"]: tool["description"] for tool in tools}
        assert all(description.strip() for description in descriptions.values())
        assert "raw" not in schemas["memory_recall_episode"]["properties"]
        assert "raw" not in schemas["memory_recall_concept"]["properties"]
        assert schemas["memory_recall_detail"]["properties"]["raw"] == {"type": "boolean"}

        changed_desc = original_episode_desc.rstrip("\n") + "\nverify_mcp 同进程热读标记\n"
        episode_desc_path.write_text(changed_desc, encoding="utf-8")
        changed_tools = child.call("list-after-edit", "tools/list", {})["result"]["tools"]
        changed_episode = next(
            tool for tool in changed_tools if tool["name"] == "memory_recall_episode"
        )
        assert changed_episode["description"] == changed_desc.strip()

        episode_desc_path.unlink()
        fallback_tools = child.call("list-after-delete", "tools/list", {})["result"]["tools"]
        fallback_episode = next(
            tool for tool in fallback_tools if tool["name"] == "memory_recall_episode"
        )
        assert fallback_episode["description"] == descriptions["memory_recall_episode"]

        unknown = child.call(30, "unknown/method", {})
        assert unknown["error"]["code"] == -32601
        bad_json = child.raw("{not json")
        assert bad_json["id"] is None and bad_json["error"]["code"] == -32700
        assert "tools" in child.call(31, "tools/list", {})["result"], "server died after bad JSON"

        bad_calls = [
            {"name": "not_a_tool", "arguments": {}},
            {"name": "memory_recall_episode", "arguments": {}},
            {"name": "memory_recall_episode", "arguments": {"query": 3}},
            {"name": "memory_recall_episode", "arguments": {"query": QUERY, "extra": True}},
        ]
        for index, params in enumerate(bad_calls, 40):
            response = child.call(index, "tools/call", params)
            assert response["error"]["code"] == -32602, response

        detail = child.call(50, "tools/call", {
            "name": "memory_recall_detail", "arguments": {"query": "逐字暗号"}
        })
        detail_raw = json.loads(tool_text(detail))
        assert detail_raw["hits"] and "逐字暗号" in detail_raw["hits"][0]["window"]

        episode = child.call(51, "tools/call", {
            "name": "memory_recall_episode", "arguments": {"query": QUERY, "raw": True}
        })
        episode_raw = json.loads(tool_text(episode))
        assert episode_raw["slots"]["primary"]

        concept = child.call(52, "tools/call", {
            "name": "memory_recall_concept", "arguments": {"node": "曲率引擎", "raw": True}
        })
        concept_raw = json.loads(tool_text(concept))
        assert concept_raw["episodes"] and concept_raw["node"] == "曲率引擎"

        miss = tool_text(child.call(53, "tools/call", {
            "name": "memory_recall_concept", "arguments": {"node": "曲率"}
        }))
        assert "相近" in miss and "曲率引擎" in miss and "memory_recall_episode" in miss
        detail_miss = tool_text(child.call(54, "tools/call", {
            "name": "memory_recall_detail", "arguments": {"query": "不存在的精确原话"}
        }))
        assert "未命中" in detail_miss and "memory_recall_episode" in detail_miss

        for value in (detail_raw, episode_raw, concept_raw):
            assert_redline(value)
    finally:
        episode_desc_path.write_text(original_episode_desc, encoding="utf-8")
        child.close()
    ok("JSON-RPC、三 tool、描述同进程热读/缺文件回退、raw/miss 与 stdout 纪律")


def verify_session() -> None:
    child = Child()
    seen: set[str] = set()
    first_ids: set[str] = set()
    try:
        for index in range(6):
            response = child.call(100 + index, "tools/call", {
                "name": "memory_recall_episode", "arguments": {"query": QUERY, "raw": True}
            })
            text = tool_text(response)
            current = pids(json.loads(text))
            if not current:
                break
            assert seen.isdisjoint(current), (seen, current)
            if not first_ids:
                first_ids = current
            seen.update(current)
        else:
            raise AssertionError("same-process dedup never exhausted the fixture")
        assert first_ids
        empty_text = tool_text(child.call(120, "tools/call", {
            "name": "memory_recall_episode", "arguments": {"query": QUERY}
        }))
        assert "库中无相关记忆" in empty_text
    finally:
        child.close()

    con = connect(CFG.db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT session_key, tool FROM injected_log WHERE tool='memory_recall_episode'"
        ).fetchall()
        before_sessions = {row["session_key"] for row in rows}
        assert len(before_sessions) >= 1 and {row["tool"] for row in rows} == {"memory_recall_episode"}
    finally:
        con.close()

    fresh = Child()
    try:
        response = fresh.call(200, "tools/call", {
            "name": "memory_recall_episode", "arguments": {"query": QUERY, "raw": True}
        })
        fresh_ids = pids(json.loads(tool_text(response)))
        assert fresh_ids and fresh_ids & first_ids, (fresh_ids, first_ids)
    finally:
        fresh.close()

    con = connect(CFG.db_path)
    try:
        after_sessions = {row[0] for row in con.execute(
            "SELECT DISTINCT session_key FROM injected_log WHERE tool='memory_recall_episode'"
        )}
        assert len(after_sessions - before_sessions) == 1, (before_sessions, after_sessions)
        assert all(key.startswith("mcp-") and len(key) == 16 for key in after_sessions)
    finally:
        con.close()
    ok("同进程 session 硬去重、episode 空手、tool 列与跨进程新 session")


def verify_empty_and_degradation() -> None:
    empty = Child(EMPTY_HOME)
    try:
        text = tool_text(empty.call(300, "tools/call", {
            "name": "memory_recall_episode", "arguments": {"query": QUERY}
        }))
        assert "库中无相关记忆" in text
    finally:
        empty.close()

    server = MCPServer(CFG, session_key="mcp-testerrors")
    with patch("memory_system.mcp_server.reconstruct.run", side_effect=ChatError("injected chat")):
        response = server.handle({
            "jsonrpc": "2.0", "id": 301, "method": "tools/call",
            "params": {"name": "memory_recall_concept", "arguments": {"node": "曲率引擎"}},
        })
    degraded = tool_text(response)
    assert "重构失败" in degraded and "injected chat" in degraded and '"episodes"' in degraded
    assert_redline(json.loads(degraded.split("\n", 1)[1]))

    with patch("memory_system.mcp_server.recall_episode",
               side_effect=EmbeddingError("injected embedding")):
        response = server.handle({
            "jsonrpc": "2.0", "id": 302, "method": "tools/call",
            "params": {"name": "memory_recall_episode", "arguments": {"query": QUERY}},
        })
    embedding_text = tool_text(response)
    assert "embedding 不可用" in embedding_text and "injected embedding" in embedding_text
    assert server.handle({
        "jsonrpc": "2.0", "id": 303, "method": "tools/list", "params": {}
    })["result"]["tools"], "server object died after EmbeddingError"

    # 再走真实 CLI 子进程全链路。两个 provider 都在读取 key 时失败,不会发起网络请求。
    chat_child = Child(
        env_updates={
            "MEMORY_AGENT_PROVIDER": "deepseek",
            "MEMORY_AGENT_RECALL_PROVIDER": "",
            "MEMORY_AGENT_KEY_ENV": "DEEPSEEK_API_KEY",
        },
        unset_keys={"DEEPSEEK_API_KEY"},
    )
    try:
        chat_text = tool_text(chat_child.call(304, "tools/call", {
            "name": "memory_recall_concept", "arguments": {"node": "曲率引擎"},
        }))
        assert "重构失败" in chat_text and "DEEPSEEK_API_KEY 未设置" in chat_text
        assert '"episodes"' in chat_text
        assert chat_child.call(305, "tools/list", {})["result"]["tools"]
    finally:
        chat_child.close()

    embedding_child = Child(
        EMPTY_HOME,
        env_updates={
            "MEMORY_EMBED_PROVIDER": "dashscope",
            "MEMORY_EMBED_MODEL": "text-embedding-v4",
            "MEMORY_EMBED_DIM": "16",
            "MEMORY_EMBED_KEY_ENV": "DASHSCOPE_API_KEY",
        },
        unset_keys={"DASHSCOPE_API_KEY"},
    )
    try:
        embedding_text = tool_text(embedding_child.call(306, "tools/call", {
            "name": "memory_recall_episode", "arguments": {"query": QUERY},
        }))
        assert "embedding 不可用" in embedding_text and "DASHSCOPE_API_KEY 未设置" in embedding_text
        assert embedding_child.call(307, "tools/list", {})["result"]["tools"]
    finally:
        embedding_child.close()

    ok("空库、ChatError/EmbeddingError 注入与真实子进程降级,失败后 server 继续响应")


if __name__ == "__main__":
    print(f"临时 home: {HOME}")
    build_corpus()
    verify_protocol_and_tools()
    verify_session()
    verify_empty_and_degradation()
    print("verify_mcp: ALL GREEN")
