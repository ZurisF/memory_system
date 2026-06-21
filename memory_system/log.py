"""日志:同时落文件(主目录/logs)与控制台。"""

from __future__ import annotations

import logging
from pathlib import Path

_CONFIGURED = False


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("memory_system")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.FileHandler(logs_dir / "memory_system.log", encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except OSError:
        # 主目录还没建出来时(如 init 之前)只走控制台,不致命。
        pass

    _CONFIGURED = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("memory_system")
