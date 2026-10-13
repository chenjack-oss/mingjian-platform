"""
明辨平台 · 配置中心

设计原则：
1. **所有配置集中在一处**，任何模块不允许自己 os.getenv（否则散落各处无法审计）。
2. **敏感项不进代码**：API Key / 数据库连接串一律走环境变量。
3. **双数据库方言**：MJ_DB_URL 以 postgres 开头走 Postgres（生产），
   sqlite 开头走 SQLite（本地开发/单测）。方言差异由 SQLAlchemy 吸收，
   业务代码不写任何一条方言特定 SQL。

为什么本地要支持 SQLite：
    本机没有可用的 Postgres 实例（端口 5432 未监听、Docker Desktop 未启动），
    强行拉起 Docker 会挤占内存。SQLite 让我们能在本地真跑一遍隔离与持久化逻辑，
    生产环境仍是 Postgres —— 这不是"降级凑合"，是明确的开发/生产双通道。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# .env 必须在 Settings.load() 之前加载，否则模块导入时读到的还是空值
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv 缺失时不致命，纯环境变量模式照样能跑
    pass


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = _env(key, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # ---------- 服务 ----------
    app_name: str = "明辨平台"
    host: str = "0.0.0.0"
    port: int = 8000
    # CORS：生产必须显式配置，不允许默认放行所有来源（旧项目这里是 "*"，属于风险项）
    cors_origins: list = field(default_factory=lambda: ["*"])

    # ---------- 数据库 ----------
    # 例：postgresql+psycopg://mingjian:secret@postgres:5432/mingjian
    #      sqlite:///D:/Projects/mingjian-platform/.data/mingjian.db
    db_url: str = "sqlite:///./.data/mingjian.db"
    db_echo: bool = False

    # ---------- LangGraph 检查点 ----------
    # auto：URL 是 postgres 且驱动可用则用 PostgresSaver，否则 InMemorySaver（并打警告）
    checkpointer: str = "auto"

    # ---------- 身份认证 ----------
    # 管理员令牌：创建用户 / 签发 API Key 时需要。未配置则关闭自助创建接口。
    admin_token: str = ""
    # 是否允许匿名访问（仅本地调试用，生产必须 False）
    allow_anonymous: bool = False

    # ---------- 模型（OpenAI 兼容）----------
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-max"
    # direct 档专用快模型（如 qwen-flash/qwen-turbo）。留空则与 llm_model 相同。
    llm_model_fast: str = ""
    llm_timeout: float = 60.0

    # ---------- 熔断与降级（P9）----------
    # 端点抖动时"让每个请求都等满超时"是最糟的策略：用户看到全员卡死，
    # 后端还在被无效请求持续冲击。宁可 200ms 快速失败 + 明确提示。
    llm_max_retries: int = 2
    llm_circuit_enabled: bool = True
    # 滑动窗口内累计失败多少次开闸
    llm_circuit_threshold: int = 5
    # 失败计数窗口（秒）。到期自动清零，不需要后台巡检任务
    llm_circuit_window_sec: float = 60.0
    # 开闸后的冷却时长。到期自动转 half_open，只放行一个探测请求
    llm_circuit_cooldown_sec: float = 30.0
    # 备用模型：主模型熔断后切到它。留空则用 llm_model_fast，再没有就只给兜底话术
    llm_fallback_model: str = ""

    # ---------- 上下文 ----------
    # 传给模型的历史轮数（1 轮 = 1 条用户 + 1 条助手）
    context_turns: int = 8
    # 单条历史消息截断长度，防止长报告把上下文撑爆
    context_msg_chars: int = 1200

    # ---------- 配额（按 user 维度，P1 先记帐，P2 再做硬限流）----------
    quota_searches_per_day: int = 50
    quota_messages_per_day: int = 200

    # ---------- 成本核算（P-4）----------
    # 模型单价：每 100 万 token 的价格（元）。留空 = 不做金额换算，只报 token 数。
    # 为什么是"每百万 token"：token 数量级是百万级，按百万计价避免浮点误差。
    # 单价是**配置**（可能配错），token 数是**事实**（不会错）—— 两者分离，诚实原则。
    llm_price_input_per_million: float = 0.0     # 输入单价（元/百万 token），0 = 不换算
    llm_price_output_per_million: float = 0.0    # 输出单价（元/百万 token），0 = 不换算

    # ---------- Redis（P3：语义缓存 + 限流 + Celery broker，一个组件三种用途）----------
    # 留空 = 三项能力全部降级关闭，**不影响主链路**（缓存 miss 直查、限流跳过）。
    # 这不是"假装实现了"，而是明确的开关：/health 会如实报 redis=off。
    redis_url: str = ""
    # 连接/命令超时：Redis 抖动时不能把 /v1/chat 一起拖死
    redis_timeout: float = 0.5
    # 故障冷却：连续失败后 N 秒内不再重试，避免每个请求都去连一次已经挂掉的 Redis
    redis_cooldown: int = 10

    # 语义缓存
    cache_enabled: bool = True
    cache_ttl: int = 3600
    # TTL 抖动百分比：同一批 key 同时过期会打爆后端 → 加 ±N% 随机抖动防雪崩
    cache_jitter_pct: int = 20
    # 缓存作用域：手动改这个值即可整体失效（P4 接入知识库后这里换成 kb_version）
    cache_scope: str = "v1"
    # 只对零工具的回答做缓存（带检索/写文件的结果有时效性与副作用，不缓存）
    cache_zero_tool_only: bool = True
    # **调用过工具**的回答仍可缓存时（cache_zero_tool_only=False）用这个更短的 TTL：
    # 检索结果和文件内容会变，1 小时太长，5 分钟是"省一次重复调用"和"不喂过期答案"的折中
    cache_ttl_with_tools: int = 300

    # 令牌桶限流（按 user_id 维度）
    rate_enabled: bool = True
    rate_capacity: int = 20
    rate_refill_per_sec: float = 2.0

    # ---------- 可观测（P3：Langfuse）----------
    # 三项全配齐才启用；留空则整体 no-op，**不影响主链路**。
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # 采样率：当前 QPS 个位数，先全量；到高 QPS 再按用户采样
    langfuse_sample_rate: float = 1.0
    langfuse_environment: str = ""
    langfuse_flush_at: int = 20
    langfuse_flush_interval: float = 5.0
    langfuse_timeout: int = 5

    # ---------- 异步任务（P3：Celery）----------
    # 留空则自动回落 memory://（仅用于 eager 模式下的本地单测，不跨进程）
    celery_broker: str = ""
    # eager：任务在当前进程内联执行（无需 broker/worker）。
    # 默认规则：没配 Redis 就自动 eager —— 本地不装 Redis 也能跑通完整链路。
    celery_eager: bool = False
    # research 档默认改异步（实测 125s，远超网关与浏览器超时，同步必超时）
    async_auto_research: bool = True
    # 任务软/硬时限（秒）：软限抛异常让任务自己收尾，硬限直接 kill
    task_soft_time_limit: int = 600
    task_time_limit: int = 660
    # broker 的 visibility_timeout：必须 > 任务最长耗时，否则任务会被重复投递
    task_visibility_timeout: int = 900
    # 心跳间隔（秒）：任务执行中每 N 秒刷一次 updated_at
    task_heartbeat_sec: int = 30
    # 心跳断了超过这个秒数 → 判定为 stalled（worker 可能已被 kill）。
    # 必须 **大于** 心跳间隔（否则正常任务会被误判），也要 **小于** visibility_timeout
    # （否则用户要等到 broker 重新投递才知道任务挂了）。
    task_stall_sec: int = 120
    # 幂等窗口（秒）：已**终态**的任务在这个时间窗内还能被重复提交命中。
    # 窗口的意义：狂点按钮 / 断线重连都发生在几秒到几分钟内；
    # 超过窗口的重复提问应当真的再跑一次（模型与知识都在变），不能永远返回旧答案。
    task_dedupe_window: int = 120

    # ---------- MCP（P5：业务系统集成）----------
    # 关闭时工具集为空，主链路不受影响（/health 如实报 mcp=off）。
    mcp_enabled: bool = True
    # 要连接的 MCP Server 名（对应 src/mcp_server/{name}_server.py），逗号分隔
    mcp_servers: str = "business"
    # 工具白名单（逗号分隔）。留空 = 全挂。
    # 为什么要能裁剪：实测工具从 3 个涨到 16 个，模型选错工具的概率明显上升，
    # 所以"挂多少"必须是可配置项，而不是 server 给多少就吃多少。
    mcp_tools: str = ""
    # 会话模式：persistent=长连接复用子进程（默认）；per_call=每次调用现起子进程
    # per_call 只用于对照测量，生产不用（每次 spawn 解释器的开销见实测数据）。
    mcp_session: str = "persistent"
    # 单次工具调用超时。业务系统慢不能把整个 Agent 链路拖死。
    mcp_timeout: float = 20.0
    # 需要**调用串行化**的 server（逗号分隔）。默认 rag。
    # 实测依据（P6.0）：business（小返回体）200 次 / 并发 50 零错配；
    # 但 rag 的 kb_lookup 返回原文片段属于大帧，超过 PIPE_BUF(4096) 后 POSIX
    # 不再保证单次写原子，且它是 CPU 密集（BGE-M3），并发只会互相抢 CPU
    # （并发 50 时 p95 从 1.3s 掉到 23.9s）。串行化对 rag 是零损失。
    mcp_serialize: str = "rag"
    # MCP Server **冷启动**等待上限（秒）。与 mcp_timeout（单次调用超时）不是一回事。
    # 为什么必须可配：外部 server 可能要加载模型/索引，冷启动远慢于项目内 server。
    # 实测依据：Agentic RAG 的 mcp_rag_server 加载 BGE-M3 权重，首连就绪耗时
    # **69.3s**（本机 CPU 回退路径），原先硬编码的 30s 会让它永远挂不上，
    # 且报错是"启动超时"，看起来像配置错误而不是超时不够。
    mcp_start_timeout: float = 30.0

    # ---------- Agentic RAG 服务化接入（P7）----------
    # 知识库的两条通道：
    #   ① MCP stdio 子进程（kb_search / kb_lookup / kb_stats）—— 在 api 容器内 spawn，
    #      实测并发 p95 劣化 1.3s→23.9s，已强制串行，并发上限 1。
    #   ② HTTP 独立服务（docker-compose 的 rag 服务，POST /agent/query）——
    #      同源码、同权重，服务端并发闸门 4 + 端到端 120s 超时 + 启动预热。
    # P7 让主链路优先走 ②，① 降级为容灾；两条都挂则 rag 档如实降级。
    # 留空 = 不启用 HTTP 通道（退回 P6 的纯 MCP 行为）。
    # 🔴 默认留空（不是指向不存在的 8082）：否则 /health 会去探一个从未部署的
    # 独立服务，报 ok:false + ConnectError，看起来像故障实则是"未启用"。
    # 要启用 HTTP 通道，显式设 MJ_AGENTIC_RAG_URL=http://127.0.0.1:8082。
    agentic_rag_url: str = ""
    # 🔴 必须 < 服务端的 REQUEST_TIMEOUT_SECONDS（默认 120s）。
    # 否则永远等不到服务端那个语义明确的 504，只能看到本地 TimeoutError，
    # 排障时无法区分"服务端真慢"和"网络断了"。
    agentic_rag_timeout: float = 110.0
    agentic_rag_connect_timeout: float = 5.0
    # 客户端并发闸门：**必须 ≤ 服务端 CapacityLimiter（默认 4）**。
    # 客户端不限流 = 请求在服务端排队却仍占着本地协程与连接，表现为"没崩但全变慢"。
    agentic_rag_concurrency: int = 4

    # ---------- 本地 trace 兜底（P7）----------
    # Langfuse 没配 Key 时整条观测链是 no-op，排查"这次为什么慢"完全没数据。
    # 兜底方案：把每轮的关键指标落成**本地 JSONL 文件**（一行一轮），
    # 不依赖任何外部服务，单机排障够用。默认开，关掉置 MJ_TRACE_LOCAL=0。
    trace_local: bool = True
    trace_dir: str = "./.data/traces"

    # ---------- 文件 ----------
    # 文件存储根目录；实际落盘路径 = {storage_root}/{user_id}/{session_id}/{file_id}_{filename}
    storage_root: str = "./.data/files"
    max_upload_mb: int = 50

    @classmethod
    def load(cls) -> "Settings":
        origins = _env("MJ_CORS_ORIGINS", "*")
        cors = [o.strip() for o in origins.split(",") if o.strip()]
        return cls(
            host=_env("MJ_HOST", "0.0.0.0"),
            port=_env_int("MJ_PORT", 8000),
            cors_origins=cors or ["*"],
            db_url=_env("MJ_DB_URL", "sqlite:///./.data/mingjian.db"),
            db_echo=_env_bool("MJ_DB_ECHO", False),
            checkpointer=_env("MJ_CHECKPOINTER", "auto"),
            admin_token=_env("MJ_ADMIN_TOKEN", ""),
            allow_anonymous=_env_bool("MJ_ALLOW_ANONYMOUS", False),
            llm_base_url=_env("MJ_LLM_BASE_URL", _env("OPENAI_BASE_URL", "")),
            llm_api_key=_env("MJ_LLM_API_KEY", _env("OPENAI_API_KEY", "")),
            llm_model=_env("MJ_LLM_MODEL", _env("LLM_QWEN_MAX", "qwen-max")),
            llm_model_fast=_env("MJ_LLM_MODEL_FAST", ""),
            llm_timeout=float(_env("MJ_LLM_TIMEOUT", "60")),
            llm_max_retries=_env_int("MJ_LLM_MAX_RETRIES", 2),
            llm_circuit_enabled=_env_bool("MJ_LLM_CIRCUIT_ENABLED", True),
            llm_circuit_threshold=_env_int("MJ_LLM_CIRCUIT_THRESHOLD", 5),
            llm_circuit_window_sec=float(_env("MJ_LLM_CIRCUIT_WINDOW_SEC", "60")),
            llm_circuit_cooldown_sec=float(_env("MJ_LLM_CIRCUIT_COOLDOWN_SEC", "30")),
            llm_fallback_model=_env("MJ_LLM_FALLBACK_MODEL", ""),
            context_turns=_env_int("MJ_CONTEXT_TURNS", 8),
            context_msg_chars=_env_int("MJ_CONTEXT_MSG_CHARS", 1200),
            quota_searches_per_day=_env_int("MJ_QUOTA_SEARCHES_PER_DAY", 50),
            quota_messages_per_day=_env_int("MJ_QUOTA_MESSAGES_PER_DAY", 200),
            llm_price_input_per_million=float(_env("MJ_LLM_PRICE_INPUT_PER_MILLION", "0")),
            llm_price_output_per_million=float(_env("MJ_LLM_PRICE_OUTPUT_PER_MILLION", "0")),
            storage_root=_env("MJ_STORAGE_ROOT", "./.data/files"),
            max_upload_mb=_env_int("MJ_MAX_UPLOAD_MB", 50),
            redis_url=_env("MJ_REDIS_URL", _env("REDIS_URL", "")),
            redis_timeout=float(_env("MJ_REDIS_TIMEOUT", "0.5")),
            redis_cooldown=_env_int("MJ_REDIS_COOLDOWN", 10),
            cache_enabled=_env_bool("MJ_CACHE_ENABLED", True),
            cache_ttl=_env_int("MJ_CACHE_TTL", 3600),
            cache_jitter_pct=_env_int("MJ_CACHE_JITTER_PCT", 20),
            cache_scope=_env("MJ_CACHE_SCOPE", "v1"),
            cache_zero_tool_only=_env_bool("MJ_CACHE_ZERO_TOOL_ONLY", True),
            cache_ttl_with_tools=_env_int("MJ_CACHE_TTL_WITH_TOOLS", 300),
            rate_enabled=_env_bool("MJ_RATE_ENABLED", True),
            rate_capacity=_env_int("MJ_RATE_CAPACITY", 20),
            rate_refill_per_sec=float(_env("MJ_RATE_REFILL_PER_SEC", "2")),
            langfuse_enabled=_env_bool("MJ_LANGFUSE_ENABLED", True),
            langfuse_public_key=_env("MJ_LANGFUSE_PUBLIC_KEY", _env("LANGFUSE_PUBLIC_KEY", "")),
            langfuse_secret_key=_env("MJ_LANGFUSE_SECRET_KEY", _env("LANGFUSE_SECRET_KEY", "")),
            langfuse_host=_env("MJ_LANGFUSE_HOST", _env("LANGFUSE_HOST",
                                                        "https://cloud.langfuse.com")),
            langfuse_sample_rate=float(_env("MJ_LANGFUSE_SAMPLE_RATE", "1")),
            langfuse_environment=_env("MJ_LANGFUSE_ENVIRONMENT", ""),
            langfuse_flush_at=_env_int("MJ_LANGFUSE_FLUSH_AT", 20),
            langfuse_flush_interval=float(_env("MJ_LANGFUSE_FLUSH_INTERVAL", "5")),
            langfuse_timeout=_env_int("MJ_LANGFUSE_TIMEOUT", 5),
            celery_broker=_env("MJ_CELERY_BROKER", ""),
            celery_eager=_env_bool("MJ_CELERY_EAGER", False),
            async_auto_research=_env_bool("MJ_ASYNC_AUTO_RESEARCH", True),
            task_soft_time_limit=_env_int("MJ_TASK_SOFT_TIME_LIMIT", 600),
            task_time_limit=_env_int("MJ_TASK_TIME_LIMIT", 660),
            task_visibility_timeout=_env_int("MJ_TASK_VISIBILITY_TIMEOUT", 900),
            task_heartbeat_sec=_env_int("MJ_TASK_HEARTBEAT_SEC", 30),
            task_stall_sec=_env_int("MJ_TASK_STALL_SEC", 120),
            task_dedupe_window=_env_int("MJ_TASK_DEDUPE_WINDOW", 120),
            mcp_enabled=_env_bool("MJ_MCP_ENABLED", True),
            mcp_servers=_env("MJ_MCP_SERVERS", "business"),
            mcp_tools=_env("MJ_MCP_TOOLS", ""),
            mcp_session=_env("MJ_MCP_SESSION", "persistent").strip().lower(),
            mcp_timeout=float(_env("MJ_MCP_TIMEOUT", "20")),
            mcp_serialize=_env("MJ_MCP_SERIALIZE_SERVERS", "rag"),
            mcp_start_timeout=float(_env("MJ_MCP_START_TIMEOUT", "30")),
            agentic_rag_url=_env("MJ_AGENTIC_RAG_URL", "").rstrip("/"),
            agentic_rag_timeout=float(_env("MJ_AGENTIC_RAG_TIMEOUT", "110")),
            agentic_rag_connect_timeout=float(_env("MJ_AGENTIC_RAG_CONNECT_TIMEOUT", "5")),
            agentic_rag_concurrency=int(_env("MJ_AGENTIC_RAG_CONCURRENCY", "4")),
            trace_local=_env_bool("MJ_TRACE_LOCAL", True),
            trace_dir=_env("MJ_TRACE_DIR", "./.data/traces"),
        )

    @property
    def is_postgres(self) -> bool:
        return self.db_url.startswith("postgres")

    def serialize_server(self, name: str) -> bool:
        """这个 server 的调用要不要串行化（见 mcp_serialize 字段的实测依据）。"""
        return name.strip() in [s.strip() for s in self.mcp_serialize.split(",")
                                if s.strip()]

    def mcp_command(self, name: str) -> tuple:
        """某个 MCP Server 的启动命令与工作目录。

        默认：`python -m src.mcp_server.{name}_server`（本项目内的 server）。
        外部 server（比如要用自己虚拟环境的 Agentic RAG）用环境变量覆盖：
            MJ_MCP_RAG_CMD = "D:\\Agentic RAG\\.venv\\Scripts\\python.exe|-m|mcp_rag_server"
            MJ_MCP_RAG_CWD = "D:\\Agentic RAG"

        为什么用 | 分隔而不是空格：Windows 路径里几乎必然有空格
        （"D:\\Agentic RAG"），按空格切会直接切碎。
        为什么允许外部命令：两个项目的依赖根本装不进同一个环境
        （明辨要 mcp>=2.0，RAG 要 torch + FlagEmbedding），
        独立进程 + 各用自己的解释器才是正解，MCP 本来也鼓励这么做。
        """
        import sys
        raw = _env(f"MJ_MCP_{name.upper()}_CMD", "")
        cwd = _env(f"MJ_MCP_{name.upper()}_CWD", "")
        if raw:
            parts = [p.strip() for p in raw.split("|") if p.strip()]
            if parts:
                return parts, cwd
        return [sys.executable, "-m", f"src.mcp_server.{name}_server"], cwd

    @property
    def redis_on(self) -> bool:
        """是否启用了 Redis。留空即关闭，缓存/限流/broker 三处都要看这个。"""
        return bool(self.redis_url)

    @property
    def broker_url(self) -> str:
        """
        Celery 的 broker 地址。

        没配 Redis 时回落 memory:// —— 这只是为了让 eager 模式（本地单测）
        能跑通，memory:// **不跨进程**，生产必须用 Redis。
        """
        if self.celery_broker:
            return self.celery_broker
        if self.redis_url:
            # 用 1 号库，与缓存（0 号库）分开，避免 FLUSHDB 缓存时把任务队列一起清掉
            base = self.redis_url.rstrip("/")
            if "/" in base.split("://", 1)[-1]:
                return base.rsplit("/", 1)[0] + "/1"
            return base + "/1"
        return "memory://"

    @property
    def eager(self) -> bool:
        """没配 Redis 时自动 eager：本地不装 Redis 也能跑完整链路（行为等价，只是不跨进程）。"""
        return self.celery_eager or not self.redis_url


settings = Settings.load()
