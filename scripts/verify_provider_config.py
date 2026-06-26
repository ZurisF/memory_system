"""Provider config regression tests.

Focus:
- Builtin OpenAI-compatible providers remain visible to the Web UI.
- Placeholder API keys are not treated as available.
- Editing/deleting a custom provider works and clears role-specific provider overrides.
- CLI chunk/extract defaults honor MEMORY_AGENT_CHUNK_PROVIDER / MEMORY_AGENT_EXTRACT_PROVIDER.
Run: .venv/bin/python scripts/verify_provider_config.py
"""

from __future__ import annotations

import os
import tempfile
import json
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib import request
from urllib.parse import urlencode

_TMP = tempfile.mkdtemp(prefix="memsys_provider_")
os.environ["MEMORY_SYSTEM_HOME"] = _TMP
os.environ["MEMORY_AGENT_PROVIDER"] = "deepseek"
os.environ["MEMORY_AGENT_CHUNK_PROVIDER"] = "custom_tmp"
os.environ["MEMORY_AGENT_EXTRACT_PROVIDER"] = "fake"
os.environ["DEEPSEEK_API_KEY"] = "sk-test"
os.environ["CUSTOM_TMP_API_KEY"] = "[this is your api key]"

from memory_system.agent import get_chat_provider  # noqa: E402
from memory_system.cli import cmd_index  # noqa: E402
from memory_system.config import load_config  # noqa: E402
from memory_system.db import migrate  # noqa: E402
from memory_system.db.connection import connect  # noqa: E402
from memory_system.agent.registry import all_provider_ids, providers_info  # noqa: E402
from memory_system.server import make_handler  # noqa: E402

CFG = load_config()
for d in CFG.all_dirs():
    d.mkdir(parents=True, exist_ok=True)


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _post(base: str, path: str, body: dict) -> dict:
    req = request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _put(base: str, path: str, body: dict) -> dict:
    req = request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="PUT",
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _delete(base: str, path: str, qs: dict) -> dict:
    req = request.Request(base + path + "?" + urlencode(qs), method="DELETE")
    with request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


(CFG.home / "custom_providers.json").write_text(
    """
{
  "providers": [
    {
      "id": "custom_tmp",
      "name": "Tmp",
      "base_url": "https://api.example.com/v1",
      "api_key_env": "CUSTOM_TMP_API_KEY",
      "default_model": "demo"
    }
  ]
}
""".strip()
    + "\n",
    "utf-8",
)
CFG = replace(CFG, agent=replace(CFG.agent, custom_providers={
    "custom_tmp": {
        "base_url": "https://api.example.com/v1",
        "api_key_env": "CUSTOM_TMP_API_KEY",
        "default_model": "demo",
    }
}))

ids = all_provider_ids(CFG)
assert {"claude_cli", "deepseek", "openai_compat", "qwen", "fake", "custom_tmp"} <= set(ids), ids
ok("内置 OpenAI 兼容 provider 仍在 Web provider 列表")

info = {p["id"]: p for p in providers_info(CFG)}
assert info["deepseek"]["available"], info["deepseek"]
assert not info["custom_tmp"]["available"], info["custom_tmp"]
assert "占位" in info["custom_tmp"]["reason"], info["custom_tmp"]
ok("占位 key 不再被视作可用 provider")

chunk_cfg = replace(CFG.agent, provider=CFG.agent.provider_for("chunk"))
extract_cfg = replace(CFG.agent, provider=CFG.agent.provider_for("extract"))
assert chunk_cfg.provider == "custom_tmp", chunk_cfg
assert extract_cfg.provider == "fake", extract_cfg
assert get_chat_provider(extract_cfg).id == "fake"
ok("按角色 provider_for() 可分别驱动 chunk/extract 默认")

blocked = cmd_index(replace(CFG, embedding=replace(CFG.embedding, provider="dashscope")),
                    SimpleNamespace(action="rebuild", provider="fake"))
assert blocked == 1, blocked
ok("真实 embedding 配置下拒绝 index rebuild --provider fake")

con = connect(CFG.db_path)
try:
    migrate.up(con)
finally:
    con.close()

httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(CFG))
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
try:
    upd = _put(base, "/api/agent/providers",
               {"id": "custom_tmp", "name": "Tmp2",
                "base_url": "https://api.changed.example/v1", "model": "changed-model"})
    assert upd.get("ok") and upd["provider"]["name"] == "Tmp2", upd
    cfg_after_update = _get(base, "/api/agent/config")
    prov = next(p for p in cfg_after_update["agents"]["chunk"]["providers"] if p["id"] == "custom_tmp")
    assert prov["base_url"] == "https://api.changed.example/v1", prov
    assert prov["default_model"] == "changed-model", prov
    ok("HTTP:自定义 provider 可修改")

    rem = _delete(base, "/api/agent/providers", {"id": "custom_tmp"})
    assert rem.get("ok"), rem
    cfg_after_delete = _get(base, "/api/agent/config")
    assert cfg_after_delete["agents"]["chunk"]["provider"] == "deepseek", cfg_after_delete
    assert all(p["id"] != "custom_tmp" for p in cfg_after_delete["agents"]["chunk"]["providers"])
    ok("HTTP:删除自定义 provider 后清理 role override")
finally:
    httpd.shutdown()
    httpd.server_close()

print("Provider config regressions ALL PASS ✅")
