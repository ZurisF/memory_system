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


# 由 .env(或控制台写回)设置进 os.environ 的键。重复 load 时这些键允许刷新
# (跟随 .env 的手动编辑);shell 里真实 export 的键不在此集合,永不被 .env 覆盖。
_dotenv_owned: set[str] = set()


def load_dotenv(path: Path, *, override: bool = False) -> dict[str, str]:
    """把 path 里的键值灌进 os.environ。返回实际生效的键值(用于日志/调试)。

    override=False(默认)也会刷新「本来就是 .env 灌进来的键」——这样运行中手动编辑
    .env 后重新 load 即生效,而 shell export 的真 key 始终优先、绝不被占位值覆盖。
    """
    if not path.exists():
        return {}
    applied: dict[str, str] = {}
    for key, val in parse_env(path.read_text(encoding="utf-8")).items():
        if override or key not in os.environ or key in _dotenv_owned:
            if os.environ.get(key) != val:
                os.environ[key] = val
            _dotenv_owned.add(key)
            applied[key] = val
    return applied


def update_dotenv(path: Path, updates: dict[str, str]) -> None:
    """把 updates 里的 key=value 写回 .env 文件,并同步到当前进程 os.environ。

    已存在的 key 改值,不存在的追加;不改其他行、不重排、保留注释和空行。
    控制台改 provider/model、加自定义 provider 占位 key 都走这里。
    """
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
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
            new_lines.append(f"{prefix}{key}={updates[key]}")
            updated.add(key)
        else:
            new_lines.append(line)
    # 追加未更新过的新 key
    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写:.env 是 key 落点,绝不让写中途崩溃留半截文件
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", "utf-8")
    os.replace(tmp, path)
    # 同步到当前进程环境;这些键此后归 .env 管(允许后续 load 刷新)
    for key, val in updates.items():
        os.environ[key] = val
        _dotenv_owned.add(key)
