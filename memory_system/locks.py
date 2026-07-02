"""进程内锁注册表 —— 给「读整份 JSON → 改 → 写回」的存储做互斥。

server 是 ThreadingHTTPServer(每请求一线程):同一会话的工作态文件若被两条请求
交错地 load→改→_write,后写覆盖先写(丢更新)。tmp+rename 只保证不出半截文件,
不防丢更新;这里按 key(如 "chunks:<session_id>")发进程内可重入锁,把每次
读改写整体括起来。可重入(RLock):store 内部函数互相调用不自锁死。

只防线程,不防跨进程——本地单服务进程,够用;真到多进程再升级文件锁。
"""

from __future__ import annotations

import threading

_REGISTRY: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


def lock_for(key: str) -> threading.RLock:
    """取(或创建)key 对应的可重入锁。同 key 永远拿到同一把。"""
    with _GUARD:
        lk = _REGISTRY.get(key)
        if lk is None:
            lk = _REGISTRY[key] = threading.RLock()
        return lk
