"""明辨平台（mingjian-platform）"""

# P1：多用户隔离 + PostgresSaver 持久化 + 四档骨架
# P2：执行核重做（边做边判）+ 真实模型接入 + 引用溯源
# P3：异步长任务（Celery + Postgres 任务表）+ Redis 语义缓存/令牌桶限流
# P5：MCP 业务系统集成（独立 Server 进程 + 自研 client 适配层 + 写操作二次确认）
__version__ = "0.4.0-p5"
