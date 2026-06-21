"""零依赖 .env 加载器。

规则(刻意保守,避开常见引号坑):
- 整行 `#` 注释和空行跳过。
- 允许 `export KEY=VALUE` 前缀。
- 值两端若是成对的单/双引号,剥掉一层;引号内内容原样保留(不再剥空格、不解析转义)。
- 不剥行内注释(key/值里可能含 `#`,宁可保真)。
- **已存在的环境变量优先**(override=False):真实 export 压过 .env,符合 dotenv 习惯。
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]  # 只剥一层成对引号,内部不动
        out[key] = val
    return out


def load_dotenv(path: Path, *, override: bool = False) -> dict[str, str]:
    """把 path 里的键值灌进 os.environ。返回实际生效的键值(用于日志/调试)。"""
    if not path.exists():
        return {}
    applied: dict[str, str] = {}
    for key, val in parse_env(path.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = val
            applied[key] = val
    return applied
