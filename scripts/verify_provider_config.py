"""Provider config regression tests.

Focus:
- Builtin OpenAI-compatible providers remain visible to the Web UI.
- Placeholder API keys are not treated as available.
- Editing/deleting a custom provider works and clears role-specific provider overrides.
- CLI chunk/extract defaults honor MEMORY_AGENT_CHUNK_PROVIDER / MEMORY_AGENT_EXTRACT_PROVIDER.
- Recall dedicated provider channel (MEMORY_AGENT_RECALL_PROVIDER): GET/POST via
  /api/agent/config, empty string = follow global, reconstruct resolves it.
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

    # ---- recall_model 门:GET 含 recall_model / POST 改写 .env 后 GET 读回新值 ----
    cfg_recall = _get(base, "/api/agent/config")
    recall0 = cfg_recall["agents"].get("recall")
    assert recall0 and recall0.get("model") == "sonnet", cfg_recall  # 默认 sonnet
    ok("HTTP:GET /api/agent/config 含 recall(model=sonnet)")

    upd_recall = _post(base, "/api/agent/config", {"role": "recall", "model": "haiku"})
    assert upd_recall.get("ok") and "MEMORY_AGENT_RECALL_MODEL" in upd_recall["updated"], upd_recall
    env_text = (CFG.home / ".env").read_text("utf-8")
    assert "MEMORY_AGENT_RECALL_MODEL=haiku" in env_text, env_text
    cfg_recall2 = _get(base, "/api/agent/config")
    assert cfg_recall2["agents"]["recall"]["model"] == "haiku", cfg_recall2
    ok("HTTP:POST recall model 改写 .env,GET 读回 haiku")

    # ---- recall 专用 provider 通道(S6 Phase 2)----
    # GET:provider 如实返回 override 原值(未设时空串 = 跟随全局);providers 与 chunk 同源同形状
    assert recall0.get("provider") == "", recall0
    chunk_ids = [p["id"] for p in cfg_recall["agents"]["chunk"]["providers"]]
    recall_ids = [p["id"] for p in recall0.get("providers", [])]
    assert recall_ids == chunk_ids and recall_ids, (recall_ids, chunk_ids)
    ok("HTTP:GET recall 节含 provider(空串=跟随全局)+ providers 与 chunk 同源同形状")

    # POST 非空:写 MEMORY_AGENT_RECALL_PROVIDER 到 .env,GET 读回;model 可同包提交
    upd_rp = _post(base, "/api/agent/config",
                   {"role": "recall", "provider": "fake", "model": "sonnet"})
    assert upd_rp.get("ok"), upd_rp
    assert upd_rp["updated"].get("MEMORY_AGENT_RECALL_PROVIDER") == "fake", upd_rp
    assert upd_rp["updated"].get("MEMORY_AGENT_RECALL_MODEL") == "sonnet", upd_rp
    env_lines = (CFG.home / ".env").read_text("utf-8").splitlines()
    assert "MEMORY_AGENT_RECALL_PROVIDER=fake" in env_lines, env_lines
    cfg_rp = _get(base, "/api/agent/config")
    assert cfg_rp["agents"]["recall"]["provider"] == "fake", cfg_rp
    # 全局 agent provider 不受影响(chunk 无 override 时回落全局 deepseek)
    assert cfg_rp["agents"]["chunk"]["provider"] == "deepseek", cfg_rp
    assert not any(l.startswith("MEMORY_AGENT_PROVIDER=") for l in env_lines), env_lines
    ok("HTTP:POST recall provider=fake 写 .env 并回读;全局 provider 不受影响")

    # 设了 recall_provider 后,reconstruct 的 provider 解析走专用通道(fake 捕获);全局不动
    from memory_system.recall import reconstruct as _recon
    captured: list[str] = []
    _orig_gcp = _recon.get_chat_provider

    def _capture_gcp(agent_cfg):
        captured.append(agent_cfg.provider)
        return _orig_gcp(agent_cfg)

    _recon.get_chat_provider = _capture_gcp
    try:
        _structured = {"slots": {"primary": []}, "frame_nodes": []}
        cfg_dedicated = replace(CFG, agent=replace(
            CFG.agent, provider="deepseek", recall_provider="fake"))
        text = _recon.run(cfg_dedicated, "episode", _structured, "provider 通道验收")
        assert captured[-1] == "fake" and text, (captured, text)
        assert cfg_dedicated.agent.provider == "deepseek", cfg_dedicated.agent
        cfg_follow = replace(CFG, agent=replace(
            CFG.agent, provider="fake", recall_provider=""))
        _recon.run(cfg_follow, "episode", _structured, "provider 通道验收")
        assert captured[-1] == "fake", captured  # 空串跟随全局(此处全局=fake)
    finally:
        _recon.get_chat_provider = _orig_gcp
    ok("reconstruct:recall_provider=fake 时拿到 fake、全局 provider 不变;空串跟随全局")

    # POST 空串:清 override 回「跟随全局」,.env 行落成空值
    upd_rp0 = _post(base, "/api/agent/config", {"role": "recall", "provider": ""})
    assert upd_rp0.get("ok"), upd_rp0
    assert upd_rp0["updated"].get("MEMORY_AGENT_RECALL_PROVIDER") == "", upd_rp0
    env_lines0 = (CFG.home / ".env").read_text("utf-8").splitlines()
    assert "MEMORY_AGENT_RECALL_PROVIDER=" in env_lines0, env_lines0
    cfg_rp0 = _get(base, "/api/agent/config")
    assert cfg_rp0["agents"]["recall"]["provider"] == "", cfg_rp0
    ok("HTTP:POST recall provider 空串清 override,.env 行为空值,GET 读回空串")
finally:
    httpd.shutdown()
    httpd.server_close()

print("Provider config regressions ALL PASS ✅")
