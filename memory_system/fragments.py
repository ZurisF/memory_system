"""碎片读写 —— 碎片是正本,SQLite 是可重建索引(idea_v2 §12.13)。

格式:Markdown frontmatter(标量 + 块状列表)+ 正文分节。
长 prose 进正文,绕开 YAML 多行/引号地狱;零依赖手写解析,保证 round-trip。

设计要点:
- `source_text` 永远是**最后一节**,从 `## source_text` 行到 EOF 逐字读回,
  里面就算出现 `## overview` 这种行也不会被误当 header(原文里随便有 markdown)。
- `highlights` 用 fenced ```json``` 存:逐字原话可能含换行/管道符,JSON 才不丢真。
- `nodes` / `keywords` 用块状列表(每行 `  - 值`),节点 label 含逗号空格也不裂。
- frontmatter 只放标量与列表;uuid 绝不进碎片(§5),衰减时钟/向量是运行态/派生,也不进。

不进碎片的字段(运行态或派生,rebuild 时重建):
- `last_accessed_at`(衰减时钟,命中刷新;rebuild 初值 = activated_at)
- `embedding_model/dim/last_embedded_at`、向量(overview 的派生,rebuild 重嵌)
- 整数 id、fragment_path(= 文件自身)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


# ---- 数据结构(只装碎片正本字段)----


@dataclass
class Episode:
    public_id: str
    overview: str
    summary: str
    source_text: str
    salience_tier: int
    status: str
    created_at: str
    highlights: list[dict] = field(default_factory=list)  # [{"text":..,"tag":..}]
    keywords: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)  # 膜:碰到的 node label
    activated_at: str | None = None
    archived_at: str | None = None
    source_session_id: str | None = None
    source_path: str | None = None


@dataclass
class Node:
    label: str
    created_at: str
    updated_at: str
    type: str | None = None
    aliases: list[str] = field(default_factory=list)


# ---- frontmatter 原子读写 ----


def _fm_scalar(key: str, value: str | int | None) -> str:
    """标量行:None → 'key:';其余 → 'key: value'。"""
    if value is None or value == "":
        return f"{key}:"
    return f"{key}: {value}"


def _fm_list(key: str, items: list[str]) -> list[str]:
    """块状列表:空 → ['key:'];否则逐行 '  - 值'。"""
    if not items:
        return [f"{key}:"]
    return [f"{key}:"] + [f"  - {it}" for it in items]


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """切出 frontmatter(首个 --- 与下一个 --- 之间)与剩余正文。

    frontmatter 解析标量与块状列表;不递归、不支持嵌套(够用即止)。
    """
    if not text.startswith("---\n"):
        raise ValueError("碎片缺少 frontmatter 起始 '---'")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("碎片缺少 frontmatter 结束 '---'")
    fm_block = text[4:end]
    body = text[end + 5 :]

    fm: dict[str, object] = {}
    lines = fm_block.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("  - "):
            raise ValueError(f"列表项无所属键: {line!r}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.lstrip(" ")
        # 看下一行是否块状列表项 → 收集成列表
        if rest == "" and i + 1 < len(lines) and lines[i + 1].startswith("  - "):
            items: list[str] = []
            i += 1
            while i < len(lines) and lines[i].startswith("  - "):
                items.append(lines[i][4:])
                i += 1
            fm[key] = items
            continue
        fm[key] = rest  # 空串代表 None,交给上层按字段语义解释
        i += 1
    return fm, body


_HEADERS = ("overview", "summary", "highlights", "source_text")


def _split_sections(body: str) -> dict[str, str]:
    """按 '## <header>' 切正文。source_text 一旦命中就吃到 EOF(逐字,不再切)。"""
    sections: dict[str, str] = {}
    lines = body.split("\n")
    i = 0
    cur: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur is not None:
            # 去掉 header 行后紧跟的一个空行、与节尾留白,但 source_text 逐字保留
            text = "\n".join(buf)
            sections[cur] = text

    while i < len(lines):
        line = lines[i]
        m = re.fullmatch(r"## (\w+)", line)
        if m and m.group(1) in _HEADERS:
            flush()
            cur = m.group(1)
            buf = []
            i += 1
            if cur == "source_text":
                # 逐字读到 EOF
                rest = lines[i:]
                # 去掉 header 后紧邻的单个空行(serialize 时加的),其余原样
                if rest and rest[0] == "":
                    rest = rest[1:]
                # 对称去掉 serialize 末尾补的单个文件末换行(split 出的末尾空串)
                if rest and rest[-1] == "":
                    rest = rest[:-1]
                sections["source_text"] = "\n".join(rest)
                cur = None
                buf = []
                break
            continue
        buf.append(line)
        i += 1
    flush()
    # prose 节去掉首尾的装饰空行
    for k in ("overview", "summary", "highlights"):
        if k in sections:
            sections[k] = sections[k].strip("\n")
    return sections


# ---- Episode 序列化 / 解析 ----


def serialize_episode(ep: Episode) -> str:
    fm = [
        "---",
        _fm_scalar("public_id", ep.public_id),
        _fm_scalar("status", ep.status),
        _fm_scalar("salience_tier", ep.salience_tier),
        _fm_scalar("created_at", ep.created_at),
        _fm_scalar("activated_at", ep.activated_at),
        _fm_scalar("archived_at", ep.archived_at),
        _fm_scalar("source_session_id", ep.source_session_id),
        _fm_scalar("source_path", ep.source_path),
        *_fm_list("nodes", ep.nodes),
        *_fm_list("keywords", ep.keywords),
        "---",
    ]
    highlights_block = json.dumps(ep.highlights, ensure_ascii=False, indent=2)
    body = [
        "## overview",
        ep.overview,
        "",
        "## summary",
        ep.summary,
        "",
        "## highlights",
        "```json",
        highlights_block,
        "```",
        "",
        "## source_text",
        ep.source_text,
    ]
    return "\n".join(fm) + "\n" + "\n".join(body) + "\n"


def parse_episode(text: str) -> Episode:
    fm, body = _split_frontmatter(text)
    sec = _split_sections(body)

    def _s(key: str) -> str | None:
        v = fm.get(key, "")
        return v or None if isinstance(v, str) else None

    highlights = _parse_highlights(sec.get("highlights", ""))
    nodes = fm.get("nodes") if isinstance(fm.get("nodes"), list) else []
    keywords = fm.get("keywords") if isinstance(fm.get("keywords"), list) else []
    tier_raw = fm.get("salience_tier", "")
    return Episode(
        public_id=_s("public_id") or "",
        overview=sec.get("overview", ""),
        summary=sec.get("summary", ""),
        source_text=sec.get("source_text", ""),
        salience_tier=int(tier_raw) if str(tier_raw).strip() else 1,
        status=_s("status") or "staging",
        created_at=_s("created_at") or "",
        highlights=highlights,
        keywords=list(keywords),
        nodes=list(nodes),
        activated_at=_s("activated_at"),
        archived_at=_s("archived_at"),
        source_session_id=_s("source_session_id"),
        source_path=_s("source_path"),
    )


def _parse_highlights(section: str) -> list[dict]:
    """从 fenced ```json``` 取 highlights;空/无块 → []。"""
    if not section.strip():
        return []
    m = re.search(r"```json\n(.*?)\n```", section, re.DOTALL)
    raw = m.group(1) if m else section
    raw = raw.strip()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else []


# ---- Node 序列化 / 解析(纯 frontmatter,无正文)----


def serialize_node(nd: Node) -> str:
    fm = [
        "---",
        _fm_scalar("label", nd.label),
        _fm_scalar("type", nd.type),
        _fm_scalar("created_at", nd.created_at),
        _fm_scalar("updated_at", nd.updated_at),
        *_fm_list("aliases", nd.aliases),
        "---",
    ]
    return "\n".join(fm) + "\n"


def parse_node(text: str) -> Node:
    fm, _ = _split_frontmatter(text)

    def _s(key: str) -> str | None:
        v = fm.get(key, "")
        return v or None if isinstance(v, str) else None

    aliases = fm.get("aliases") if isinstance(fm.get("aliases"), list) else []
    return Node(
        label=_s("label") or "",
        created_at=_s("created_at") or "",
        updated_at=_s("updated_at") or "",
        type=_s("type"),
        aliases=list(aliases),
    )


# ---- 文件落地 ----


def episode_path(episodes_dir: Path, public_id: str) -> Path:
    return episodes_dir / f"{public_id}.md"


def _safe_node_filename(label: str) -> str:
    """label → 安全文件名;label 正本在 frontmatter,文件名只求唯一可读。"""
    slug = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", label).strip().strip(".")
    return (slug or "node")[:80]


def node_path(nodes_dir: Path, label: str) -> Path:
    return nodes_dir / f"{_safe_node_filename(label)}.md"


def write_episode(episodes_dir: Path, ep: Episode) -> Path:
    episodes_dir.mkdir(parents=True, exist_ok=True)
    p = episode_path(episodes_dir, ep.public_id)
    p.write_text(serialize_episode(ep), encoding="utf-8")
    return p


def write_node(nodes_dir: Path, nd: Node) -> Path:
    nodes_dir.mkdir(parents=True, exist_ok=True)
    p = node_path(nodes_dir, nd.label)
    p.write_text(serialize_node(nd), encoding="utf-8")
    return p


def read_episode(path: Path) -> Episode:
    return parse_episode(path.read_text(encoding="utf-8"))


def read_node(path: Path) -> Node:
    return parse_node(path.read_text(encoding="utf-8"))


def load_all_episodes(episodes_dir: Path) -> list[tuple[Path, Episode]]:
    if not episodes_dir.exists():
        return []
    return [(p, read_episode(p)) for p in sorted(episodes_dir.glob("*.md"))]


def load_all_nodes(nodes_dir: Path) -> list[tuple[Path, Node]]:
    if not nodes_dir.exists():
        return []
    return [(p, read_node(p)) for p in sorted(nodes_dir.glob("*.md"))]
