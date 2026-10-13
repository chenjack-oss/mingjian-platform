"""
统一日志设施（替代散落各处的 print）

为什么必须换掉 print（P8 优化，原 🟡 待优化项 ⑫）：
    1. **没有时间戳、没有级别、没有模块名**。线上排障时 `[agent]` `[mcp]` `[cors]`
       `[checkpointer]` 全糊在一起，分不清先后，也分不清哪条是警告哪条只是信息。
    2. **不可关闭、不可分流**。诊断信息混在业务输出里，正常请求也在刷屏。
    3. **容器里缓冲行为不确定**。stdout 在 tty 下是行缓冲、重定向到文件或
       docker logs 时是块缓冲，多条 print 的相对顺序可能与真实时序不一致 ——
       排查"这次为什么慢"时这足以把人带偏。

设计（刻意保持最小）：
    - 只用标准库 logging，不引入 structlog/loguru：本项目已经有一堆重依赖，
      观测这块能不加重就不加。
    - configure_logging() 幂等，可重复调用（uvicorn --reload 会多次 import）。
    - 级别由 MJ_LOG_LEVEL 控制，默认 INFO。
    - 格式带 时间 / 级别 / 模块名，足够单机排障；要接采集器时换 formatter 即可。

用法：
    from src.observe.logging_setup import get_logger
    logger = get_logger(__name__)
    logger.info("已装配图：tier=%s tools=%d", tier, len(tools))
    # 降级/异常路径用 warning，不要静默 —— 静默降级是最难排查的一类问题
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

_CONFIGURED = False

# 默认格式：时间 | 级别 | 模块 | 消息
# 模块名取最后两段（src.agent.core -> agent.core），太长的话每行一半是前缀
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


def _log_level() -> int:
    raw = (os.getenv("MJ_LOG_LEVEL") or "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def configure_logging(level: Optional[int] = None, force: bool = False,
                      stream=None) -> None:
    """
    :param stream: 输出流，默认 stdout。
        ⚠️ MCP stdio server **必须传 sys.stderr** —— 它的 stdout 是 JSON-RPC
        协议通道，往 stdout 写一个字符都会破坏帧结构，客户端只会看到
        "Connection closed"，真正的错误反而被吞掉。
    """
    """配置根 logger。幂等：重复调用只会更新级别，不会重复加 handler。"""
    global _CONFIGURED
    lvl = level if level is not None else _log_level()

    root = logging.getLogger()
    root.setLevel(lvl)

    if _CONFIGURED and not force:
        return

    if force:
        for h in list(root.handlers):
            root.removeHandler(h)

    if not root.handlers:
        h = logging.StreamHandler(stream if stream is not None else sys.stdout)
        h.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        root.addHandler(h)

    # 第三方库的 DEBUG 太吵，单独压到 WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio",
                  "langchain_core", "openai", "mcp"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """拿一个 logger。模块顶部调用一次即可。"""
    if not _CONFIGURED:
        configure_logging()
    # 只保留最后两段，避免 "src.agent.subagents.registry" 这种长前缀
    parts = name.split(".")
    short = ".".join(parts[-2:]) if len(parts) > 2 else name
    return logging.getLogger(short)
