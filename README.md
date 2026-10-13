# 明辨平台（mingjian-platform）

> 从「多智能体深度调研系统」升级为**智能体平台**。
> 旧项目 `mingjian-deepsearch`（原 `deep_search_pro`）完整保留在 `D:\Projects\mingjian-deepsearch\`，**不再改动**。

当前阶段：**中台化改造已完成**（2026-10-14）· 在 P10 基础上叠加了「可运营 AI 中台」能力（多租户组织 + 子 Agent 编排 + 任务级中断续跑 + 成本审计 + 前端控制台）

> 本文件包含两套阶段记录：底部 **P1 → P10 阶段交付清单**（2026-10-07 前的既有交付），
> 以及 **「中台化改造」章节**（2026-10-13 起的新一轮 P1~P6，多租户/编排/中断/成本/审计/前端）。

---

## 为什么要另起项目

旧项目的三个结构性问题，补丁解决不了：

1. **永远在出调研报告** —— 主智能体的 system prompt 写死了「拆 3~5 个子问题 + 正文 ≥1000 字 + 必带 URL」，
   而三模式路由里 `rag_qa` 没有后端（必然失败）、`chitchat` 只覆盖问候，
   于是任何实质问题唯一能走通的路就是深度调研。
2. **「先判后做」的架构** —— 独立的意图分类节点先问一遍「你是哪种类型」，
   每轮多一次 LLM 调用，且判错整轮错。Codex / Claude Code 的做法是**边做边判**：
   模型在循环里自己决定调不调工具。
3. **没有「用户」这个概念** —— `thread_id` 由客户端自报、零鉴权、CORS `*`、
   文件按客户端传的绝对路径下载。多用户隔离等于 0。

本项目按五层重构，P1 先打地基（L0 身份与会话层）。

---

## 四档路由（旧三模式的升级）

| 档位 | 含义 | 何时用 | 线上实测（2026-09-27） |
|---|---|---|---|
| `direct` | 直接答，零检索 | 概念解释 / 闲聊 / 写代码 | **1.6s**，`tools=[]` |
| `tool` | 用工具 | 读自己上传的文件或数据 | **13s**，文件工具 |
| `rag` | 检索答 | 基于知识库作答并给引用 | **已接入**（P5 经 MCP 接入，P7 升级为 HTTP 主通道；未挂上时仍诚实降级） |
| `research` | 深度调研 | 明确要一份带引用的报告 | **125s**，出文档 + 引用 100% 可溯源 |
| `auto` | 默认交给模型判断 | 拿不准就用这个 | 8~34s，按需检索 |

关键设计：**「出不出文档」不再由档位决定**。
旧项目把文档绑死在调研档上（prompt 强制 ≥1000 字）；本平台里写不写文档由模型在循环内自己决定（P2）。

手动指定优先级高于自动判断 —— 用户永远有「你判断错了，我指定」的出口。

---

## P1 交付内容

### 多用户隔离（旧项目为 0）

| 项 | 旧项目 | 本平台 |
|---|---|---|
| 会话标识 | 客户端自报 `thread_id` | 服务端生成，三段式 `u:{user_id}:s:{session_id}` |
| 鉴权 | 无 | API Key（Bearer / X-API-Key），不传即 401 |
| Key 存储 | — | 仅存 sha256，明文只显示一次 |
| 会话查询 | 仅按 session_id | 所有查询强制带 user_id |
| 越权响应 | 能读到别人数据 | 403 / 404（当作不存在，不泄露存在性） |
| 文件 | 客户端传绝对路径 | file_id + 归属校验，存储目录按 user/session 两级 |
| CORS | `*` | 可配置，未配置时启动告警 |

### 持久化

- 业务表：`users` / `sessions` / `messages` / `files` / `usage`（SQLAlchemy Core，Postgres 生产、SQLite 本地）
- 图状态：`PostgresSaver`（`langgraph.checkpoint.postgres`，带连接池）；连不上时回退内存并在 `/health` 如实上报
- 旧项目用 `InMemorySaver`：进程重启全丢、无法多实例 —— 这里全部修掉

### 上下文处理

- 只取最近 `MJ_CONTEXT_TURNS` 轮，单条超过 `MJ_CONTEXT_MSG_CHARS` 截断

---

## 阶段交付清单（P1 → P10）

| 阶段 | 主题 | 交付要点 |
|---|---|---|
| **P1** | 身份与会话层 | API Key（仅存 sha256，明文只显示一次）；三段式 `u:{uid}:s:{sid}`；所有查询强制带 user_id；越权返回 403/404（**当作不存在，不泄露存在性**）；文件按 `user/session` 两级隔离；CORS 可配置、未配置时启动告警；`PostgresSaver` 持久化（连不上回退内存并在 `/health` 如实上报） |
| **P2** | 边做边判的执行核 | 档位从「执行器选择器」改为**能力约束与使用门槛**（旧的 `rule_predict` 已删除）；默认 `auto` 进循环，由模型自己决定调不调工具、写不写文档 |
| **P3** | 会话与产物 | 会话数据 / 中间产物 / 最终报告按会话 ID 留存，`artifacts` 识别本轮产出 |
| **P4** | rag 档通道 | 知识库问答档位与诚实降级框架 |
| **P5** | MCP 客户端与工具生态 | 自研常驻连接客户端（每 server 常驻线程 + 独立 event loop）：p50 **1720ms → 3.9ms（21.2×）**；封装 16 个业务工具（15 只读 + 写操作二次确认 token） |
| **P6** | 子 Agent 与工具下放 | `subagents/{contracts,kb_analyst,registry}.py`。实测工具 3→16 后模型选择准确率 **100% → 90%**，据此落地**分层下放**：知识库工具簇（kb_search / kb_lookup / kb_stats）下放 kb-analyst，主 Agent 剥离该簇只留派发权 |
| **P7** | Agentic RAG 接入 | `src/capabilities/agentic_rag.py` 走 HTTP 主通道（信号量 4 ≤ 服务端 `CapacityLimiter(4)`、超时 110s < 服务端 120s、只重试连接类错误一次、模块级 httpx 复用）；用 **ContextVar 探针**把证据质量接出到响应 `meta`。17/17 |
| **P8** | 并发与一致性深度优化 | ①eager 长任务丢独立线程 `_run_off_loop` + `copy_context().run()` ②限流 fail-open 令牌桶 ③引用校验扩到**聊天主干道** ④tracing 本地 JSONL 兜底 ⑤CORS fail-closed / 缓存 TTL 300 / 预算 Redis **Lua 原子**。43/43 |
| **P9** | LLM 韧性 | `src/llm/resilience.py` 错误分类 + 带抖动退避 + 三态熔断 + Redis 共享、**每模型独立熔断器**。46/46 |
| **P10** | 复盘与取舍 | 见下方「明确决定不做」 |

### 明确决定不做（记录判断，避免以后重复踩）

- **完整事件总线** —— 判定为过度设计，不做。真痛点是 slow/default **双队列拆分**与**跨进程 traceparent 传播**。

---

## 中台化改造（2026-10-13 起，新一轮 P1~P6）

> 在 P1~P10（智能体平台）基础上，叠加「可运营 AI 中台」能力。
> 这轮的 P1~P6 与上面 P1~P10 是**两套编号**，不冲突：上面是"把玩具做成平台"，这轮是"把平台做成中台"。

| 阶段 | 主题 | 交付要点 |
|---|---|---|
| **P1** | 记忆底座 + 会话恢复 | 长期记忆提取（异步、幂等、按 importance 注入）；会话中断后从 checkpoint 恢复；任务复杂度判断（`complexity.py`，simple/complex 分流信号）；修 5 个高危雷 |
| **P2** | 4 子 Agent + 编排前置层 | 四个专属子智能体：`kb-analyst`（知识）/ `db-analyst`（只读 SQL，白名单护栏）/ `web-analyst`（联网）/ `doc-analyst`（文档+引用校验）；显式编排图 `orchestrator.py`（judge 复杂度 → plan 拆解 → 注入主图并发派发） |
| **P3** | 任务级 interrupt/resume | 编排层 `confirm` 节点调 `interrupt()`，规划后暂停等人工确认；checkpoint 落盘；`POST /v1/tasks/{id}/resume` 用 `Command(resume)` 续跑 |
| **P4** | 成本核算 + 审计 + 动态注册 | token 记账（按模型）→ 成本换算（`/v1/cost`）；审计日志（`/v1/audit`，关键操作留痕）；子 Agent 按档位环境变量动态开关（`MJ_SUBAGENTS_<TIER>` / 禁用名单） |
| **P5** | 前端控制台接线 | 单文件 `src/web/index_v2.html` 接上 P3/P4 能力：中断确认 UI、token 成本面板、审计日志视图、真实子 Agent 装配清单 |
| **P6** | 多租户（组织） | 叠加式多租户：`orgs` + `org_members` 两张表，组织 CRUD + 成员管理 + 组织级记忆 + 组织级用量聚合。**个人数据保持私有，组织能力靠显式共享**，不推倒现有 user_id 隔离 |

### 中台化的三个关键架构决策

1. **编排前置层，而非重建 StateGraph**（P2）。deepagents 的 task 工具 + LangGraph `ToolNode` 已内置并发（`asyncio.gather`），且独立编译子 Agent 会丢失 deepagents 自动注入的中间件栈。所以显式图只做「复杂度判断 + 规划拆解」两件确定性的事，并发派发复用主图。
2. **中断点放在编排层 plan 确认**（P3），而非 deepagents 主图。主图的 `interrupt_on` 是「工具审批」不是「断点续跑」；规划确认才是生产环境最真实的人工介入点。
3. **叠加式多租户**（P6），而非全量 org_id 隔离。现有 7 张业务表按 user_id 做个人私有隔离（正确且测试覆盖），组织是「成员集合」这层关系叠加其上，避免牵动 46 处 user_id 改动的回归风险。

### 隔离红线（贯穿始终）

- 所有查询强制带 user_id，越权返回 403/404（当作不存在，不泄露存在性）
- 组织详情/成员/用量必须先确认「是成员」，否则 Forbidden
- 敏感文件（`.env` / `.data` / `*.db`）一律 `.gitignore` 排除，绝不入库

### 降级码一览
> 诚实原则：**降级必须可见，绝不假装这是一次正常回答。**
> 统一纪律：**不静默放行，也不擅自改写模型的话，而是把事实写进 `meta` 让上层可观测。**

| `meta.degradation` | 触发 |
|---|---|
| `rag_not_wired` | 知识库没挂上 —— **不去调模型编检索结果** |
| `llm_unavailable` | 模型侧不可用，换成兜底话术而不是抛裸异常（用户看不懂 "Connection reset by peer"） |
| `agent_error` | 执行核出错，原始 error 带出 |
| `rag_no_retrieval` | rag 档但没派发 task → 答案为模型自有知识，未经验证 |
| `rag_no_evidence` | 证据探针显示拒答或零引用 → 答案未获证据支撑 |
| `citation_unverified` | 正文存在无法在本次检索结果中溯源的链接 |

---

## 快速开始

```bash
# 1. 建虚拟环境并安装
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# 2. 配置（本地 SQLite 可直接用默认，只需填模型 Key）
cp .env.example .env

# 3. 建库 + 创建第一个用户（打印的 Key 只出现一次）
python scripts/bootstrap.py 你的名字

# 4. 冒烟测试
python scripts/smoke.py

# 5. 起服务
python -m uvicorn src.api.server:app --port 8000
```

调用示例：

```bash
curl -H "X-API-Key: mj_xxx" http://127.0.0.1:8000/v1/me
curl -X POST -H "X-API-Key: mj_xxx" -H "Content-Type: application/json" \
     -d '{"title":"测试"}' http://127.0.0.1:8000/v1/sessions
```

---

## 日常使用（三种方式）

### 方式一：网页（最直接）

打开服务器上的 <http://<你的服务器IP或域名>/mj/>，填入 API Key 即可开聊。

- 左上角 **＋新建对话**，左侧栏切历史会话
- 右上角下拉选档位，**默认 auto 就行**
- Enter 发送 / Shift+Enter 换行
- 每条回答下方显示「档位来源 + 耗时」，能直接看出这一轮是模型自己判断的还是你指定的

### 方式二：curl 调 API

```bash
KEY=mj_你的Key
BASE=http://<你的服务器IP或域名>/mj            # 本地跑改成 http://127.0.0.1:8000

# 1) 建会话
SID=$(curl -s -X POST $BASE/v1/sessions \
      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
      -d '{"title":"我的对话"}' | python -c "import sys,json;print(json.load(sys.stdin)['session']['id'])")

# 2) 发消息（不传 tier 就是 auto）
curl -s -X POST $BASE/v1/sessions/$SID/messages \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"content":"用一句话解释什么是 RAG","tier":"direct"}'

# 3) 读历史
curl -s -H "Authorization: Bearer $KEY" $BASE/v1/sessions/$SID
```

返回体里这几个字段最有用：

| 字段 | 含义 |
|---|---|
| `tools_used` | 这一轮实际调用了哪些工具（`[]` 表示直接答） |
| `artifacts` | 产出的文件名与大小（只在真的写了文档时非空） |
| `meta.search_used` | 联网检索次数 |
| `meta.degradation` | 降级原因，空串表示正常（取值见上文「降级码一览」） |
| `meta.evidence` | 知识库证据质量探针：`answer_level` / `confidence` / `citations` / `iterations` / `channel` |
| `meta.citation_check` | 正文引用校验：总数 / 已溯源 / 覆盖率 / 无法溯源的链接 |

### 方式三：Python 一行接入

```python
import requests
BASE, KEY = "http://<你的服务器IP或域名>/mj", "mj_你的Key"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

sid = requests.post(f"{BASE}/v1/sessions", json={"title": "py"},
                    headers=H, timeout=30).json()["session"]["id"]
r = requests.post(f"{BASE}/v1/sessions/{sid}/messages",
                  json={"content": "苏州 AI 岗位行情", "tier": "auto"},
                  headers=H, timeout=300).json()
print(r["tools_used"], r["elapsed_sec"], r["content"][:200])
```

> 认证两种方式都行：`Authorization: Bearer <key>` 或 `X-API-Key: <key>`。

### 给别人发 Key

Key 只在创建时明文显示一次，库里只有 sha256，**丢了对不起 —— 只能重签**：

```bash
# 容器内执行
docker exec mingjian-platform-api python scripts/bootstrap.py 张三
docker exec mingjian-platform-api python scripts/bootstrap.py --list
docker exec mingjian-platform-api python scripts/bootstrap.py --rotate <user_id>
```

也可以走 HTTP（需管理员令牌 `MJ_ADMIN_TOKEN`）：

```bash
curl -X POST $BASE/v1/users -H "X-API-Key: $ADMIN_TOKEN" \
     -H "Content-Type: application/json" -d '{"name":"张三"}'
```

---

## P3：异步长任务 · 缓存 · 限流

### 为什么要有异步任务

`research` 档实测 **125 秒**。同步接口在这个时长下必死 —— 网关先 499，
前端再超时重发，用户看到的是"一直转圈然后失败"。所以长任务必须异步化。

```bash
# 提交：立刻返回（< 200ms）
curl -X POST http://localhost:8000/v1/tasks -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","content":"调研 2026 大模型应用开发技术栈","tier":"research"}'
# → 202 {"task":{"id":"t_xxx","status":"pending",...}}

# 轮询
curl http://localhost:8000/v1/tasks/t_xxx -H "X-API-Key: $KEY"
# → status: pending → running → success（结果就在 result 字段里）
```

状态机：`pending → running → success / failure / revoked / stalled`
（`stalled` 是 Celery 没有的状态：running 超过 900s 没更新，说明 worker 大概已经被 kill 了）

### 三个设计决定

| 决定 | 选择 | 理由 |
|---|---|---|
| 结果存哪 | **Postgres 的 `tasks` 表**，不是 Celery 的 result backend | "我上周跑的那份报告呢"是产品需求，必须是可查询的行；Redis 查得到但查不快、也不持久 |
| broker | **Redis**（1 号库） | 缓存本来就必须要 Redis，复用它做 broker = 零新增组件。单机 4C7.4G 放不下 Kafka |
| 缓存与限流 | Redis 挂了就**降级放行**（fail-open） | 因限流组件故障而拒绝正常请求，影响远大于短暂放开限流 |

### 缓存的两个硬约束

1. **只缓存"零工具"的回答**。本轮只要调用过检索/读文件/写文档，结果就带时效性或副作用，
   缓存住等于把一次性的结果当永久答案。宁可少命中，也不能答错。
2. **key 里必须带上文摘要**。多轮会话里同一个"它多少钱"在不同上文下答案完全不同，
   只看当前提问会把上一轮的答案错喂出来。

TTL 还带 ±20% 抖动 —— 不抖的话同一批 key 会同时过期，那一瞬间全部 miss 同时打到模型。

### 没配 Redis 会怎样

什么都不影响。`MJ_REDIS_URL` 留空时：缓存跳过、限流放行、任务改在当前进程内联执行（eager）。
`/health` 会如实报 `redis: off`，不会假装功能存在。

---

## 部署（服务器 <你的IP>，与 Yuxi 共存）

```bash
docker compose up -d --build
```

- 平台对外端口 **8080**（Yuxi 占用 80）
- Postgres 只在内部网络，不与 Yuxi 的 6432 冲突
- 顶层 `name: mingjian-platform` + 独立网络，避免 compose 互相顶掉容器

---

## 路线图

> 早期路线图（下表）已被上方两套交付清单取代，此处保留仅供追溯版本演进。
> 实际进度见：① **P1 → P10 阶段交付清单**（平台化）② **「中台化改造」章节**（中台化）。

| 期 | 内容 | 实际落点 |
|---|---|---|
| P1 ✅ | 身份与会话层 · PostgresSaver · 四档路由骨架 | 已交付 |
| P2 ✅ | 执行核重写：deepagents 单智能体 + skills | 已交付 |
| P3 ✅ | 异步长任务 · Redis 语义缓存 · 令牌桶限流 | 已交付 |
| P4 | 并发与延迟：Send 并发派发子任务 | 已交付（见上方 P8 并发优化 + 中台化 P2 编排） |
| P5 | 接 Agentic RAG 容器；MCP 适配器 | 已交付（见上方 P7） |
| P6 | 前端演进：产物内联预览 + 进度条 + 插件管理 | 已交付（见中台化 P5 前端控制台） |

> ⚠️ 口径红线：所有实测数据必须真跑，绝不为了好看编造数字。
> 效果不好就如实写「无显著差异」——这跟 Agentic RAG 项目里把「诚实项」写进报告是同一个原则。
