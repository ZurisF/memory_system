"""预览缓存 —— 清洗结果的可丢弃派生物,键 = jsonl 路径 + mtime(idea_v2 §8 / S2)。

mtime 一变,键就变 → 旧缓存自然失效、重算。纯派生,随便清。
落 cfg.preview_cache_dir;文件名 = sha1(路径)+mtime,内容是 CleanedTranscript 的 JSON。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from memory_system.preprocess import CleanedTranscript, clean


def _key(path: Path, mtime: float) -> str:
    h = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
    # mtime 取整到微秒,避免浮点尾差;不同 mtime → 不同文件 → 自动失效
    return f"{h}_{int(mtime * 1_000_000)}.json"


def get(cache_dir: Path, path: Path, *, mtime: float | None = None) -> CleanedTranscript:
    """取清洗结果:命中(路径+mtime)缓存则读,否则 clean() 并写缓存。"""
    mtime = path.stat().st_mtime if mtime is None else mtime
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / _key(path, mtime)
    if cache_file.exists():
        try:
            return CleanedTranscript.from_dict(json.loads(cache_file.read_text("utf-8")))
        except (json.JSONDecodeError, KeyError):
            pass  # 缓存损坏 → 重算
    ct = clean(path)
    # 原子写 + 每线程独立 tmp 名:server 多线程可能并发重算同一份缓存,
    # 谁后 replace 谁生效(内容相同),读者永远不会看到半截 JSON。
    tmp = cache_file.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(ct.to_dict(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_file)
    sweep_stale(cache_dir, path, mtime)
    return ct


def is_cached(cache_dir: Path, path: Path, *, mtime: float | None = None) -> bool:
    mtime = path.stat().st_mtime if mtime is None else mtime
    return (cache_dir / _key(path, mtime)).exists()


def sweep_stale(cache_dir: Path, path: Path, keep_mtime: float) -> int:
    """清掉某 jsonl 的旧 mtime 缓存(只留 keep_mtime 那份)。返回清掉数。"""
    if not cache_dir.exists():
        return 0
    h = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
    keep = _key(path, keep_mtime)
    n = 0
    for f in cache_dir.glob(f"{h}_*.json"):
        if f.name != keep:
            f.unlink()
            n += 1
    return n
