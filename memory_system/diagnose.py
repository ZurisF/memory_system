"""diagnose claude-code —— 实测平台事实,不靠猜(phase1_build S0)。

探:transcript JSONL 在哪、长什么样、message_uuid 形态、role、isInitial 出现频次
(开场注入标记,非 resume 信号——生命周期已定论,见 session-jsonl-lifecycle.md),
落一份 markdown 报告到 diagnostics/。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from memory_system.config import Config


def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _iter_jsonl(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _scan_file(path: Path, max_lines: int = 4000) -> dict:
    top_keys: Counter = Counter()
    types: Counter = Counter()
    roles: Counter = Counter()
    uuid_keys: Counter = Counter()
    has_is_initial = 0
    sample_record: dict | None = None
    n = 0
    bad = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            if n > max_lines:
                break
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(rec, dict):
                continue
            if sample_record is None:
                sample_record = rec
            for k in rec:
                top_keys[k] += 1
                if "uuid" in k.lower():
                    uuid_keys[k] += 1
            if "type" in rec:
                types[str(rec["type"])] += 1
            msg = rec.get("message")
            if isinstance(msg, dict) and "role" in msg:
                roles[str(msg["role"])] += 1
            elif "role" in rec:
                roles[str(rec["role"])] += 1
            # isInitial 数点(开场注入标记,非 resume 信号 —— 见生命周期文档)
            if _deep_has_key(rec, "isInitial"):
                has_is_initial += 1
    return {
        "lines_scanned": n,
        "json_errors": bad,
        "top_keys": top_keys.most_common(),
        "types": types.most_common(),
        "roles": roles.most_common(),
        "uuid_keys": uuid_keys.most_common(),
        "isInitial_count": has_is_initial,
        "sample_record": sample_record,
    }


def _deep_has_key(obj, key: str, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_deep_has_key(v, key, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return any(_deep_has_key(v, key, depth + 1) for v in obj)
    return False


def diagnose_claude_code(cfg: Config) -> Path:
    root = _projects_root()
    lines: list[str] = []
    ts = datetime.now(timezone.utc).isoformat()
    lines.append("# diagnose claude-code")
    lines.append("")
    lines.append(f"> 生成于 {ts}")
    lines.append(f"> transcript 根目录: `{root}`")
    lines.append("")

    if not root.exists():
        lines.append("**未找到 transcript 根目录** —— Claude Code 可能从未在本机运行,或路径不同。")
    else:
        files = _iter_jsonl(root)
        lines.append(f"找到 **{len(files)}** 个 `.jsonl`。最近修改的前几个:")
        lines.append("")
        for p in files[:8]:
            mt = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
            lines.append(f"- `{p.relative_to(root)}`  ({p.stat().st_size} B, mtime {mt})")
        lines.append("")
        if files:
            target = files[0]
            lines.append(f"## 抽样最近文件: `{target.relative_to(root)}`")
            lines.append("")
            info = _scan_file(target)
            lines.append(f"- 扫描行数: {info['lines_scanned']}  | JSON 解析失败: {info['json_errors']}")
            lines.append(f"- 含 uuid 的字段: {info['uuid_keys'] or '（无）'}")
            lines.append(f"- type 分布: {info['types']}")
            lines.append(f"- role 分布: {info['roles']}")
            lines.append(f"- 出现 `isInitial` 的记录数: {info['isInitial_count']}")
            lines.append("")
            lines.append("### 顶层字段计数")
            for k, c in info["top_keys"]:
                lines.append(f"- `{k}`: {c}")
            lines.append("")
            lines.append("### 一条样本记录(截断)")
            lines.append("")
            lines.append("```json")
            sample = json.dumps(info["sample_record"], ensure_ascii=False, indent=2)
            lines.append(sample[:3000])
            lines.append("```")

    lines.append("")
    lines.append("## 已定论(见 project/session-jsonl-lifecycle.md)")
    lines.append("- [x] message_uuid = `uuid` 字段 + `parentUuid` 串链;一会话=一文件。")
    lines.append("- [x] `/resume` 永远原文件追加,**uuid 不跨文件**;无跨文件复刻前缀,resume 检测取消。")
    lines.append("- [x] `isInitial` 挂开场注入 attachment,几乎每个新会话都带,**不是 resume 信号**。")
    lines.append("- [x] `/clear` fork ~1945B 空壳垃圾文件;`leafUuid`(last-prompt)是 resume 定位锚。")
    lines.append("## 仍待确认")
    lines.append("- [ ] SessionStart 注入的可用形态?(S8 前实测)")

    cfg.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = cfg.diagnostics_dir / f"claude-code-{stamp}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
