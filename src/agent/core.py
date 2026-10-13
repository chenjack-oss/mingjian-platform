"""
P2 执行核：单智能体 + 工具循环 + 技能 + 上下文压缩

这是与旧项目**架构级**的区别：

    旧项目：understand(LLM 先分类) → 按 label 选执行器 → 跑重型调研图
    本平台：模型在一个 ReAct 循环里**边看边决定**调不调工具、写不写文档

「先判后做」的三个代价：
    1. 每轮多一次 LLM 调用（1~3s 纯开销）
    2. 判错整轮错，中途无法改主意
    3. 只能落到预设的 N 个桶里，无法组合（先查、再算、再写）

改成「边做边判」之后，"是否需要文档"这件事由**模型自己**决定，
而不是由上游一个分类节点预设 —— 这正是"像 Codex 一样智能"的落点。

四档在 P2 里的新语义（不再是"执行器选择器"，而是"能力/预算约束"）：
    direct   强制不带工具的单次调用（用户手动要最快路径时用）
    auto     默认：进循环，全部工具可用，模型自己决定用不用
    tool     引导优先使用文件/数据类工具
    rag      引导基于知识库作答（未接入时诚实降级）
    research 引导加载 deep-research 技能并按其方法论执行
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from src import context
from src.config import settings

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 系统提示词：平台人格，**不是**调研人格
# ---------------------------------------------------------------------------
# 旧项目的 system prompt 写死了"你是深度调研系统的主编智能体"，
# 那是"永远在出调研报告"的直接原因。这里的人格刻意保持通用。
BASE_SYSTEM_PROMPT = """你是「明辨」—— 一个智能体平台的执行核。

## 默认行为：直接回答

- **能凭已有知识答的，就直接答**，不要调用任何工具。
- **不要主动产出文档**。只有用户明确要文件（"写一份报告""整理成文档""出个文件"）时才写。
- 简洁优先。不要为了显得全面而拉长篇幅。

## 什么时候才用工具

| 情况 | 动作 |
| --- | --- |
| 需要外部事实、行业数据、新闻动态 | 联网检索 |
| 需要用户上传的文件/数据库里的内容 | 读取文件工具 |
| 问订单 / 库存 / 客户 / 物流 / 发票等业务数据 | 业务系统工具（query_order、query_stock 等） |
| 问产品手册 / 设备配置 / 内部文档等企业知识 | 用 task 工具派发给 kb-analyst 子智能体 |
| 用户明确要一份带引用的文档 | 联网检索 + 写文档 |

## 业务数据红线（硬性）

订单号、库存数量、金额这类**企业内部数据**一律以工具返回为准：
- 没查到就写"未查询到该订单/该 SKU"，**禁止凭印象编造单号、数量、金额**；
- 工具返回 `found: false` 时如实转述，不要补一个看起来合理的数字；
- `create_ticket` 是**写操作**：必须先向用户展示预览并得到明确确认，
  再带 confirm_token 调用第二次。用户没确认绝不允许自作主张创建。

不确定要不要查时，**先直接答**，结尾补一句"这个需要查最新资料的话我可以帮你检索"。

## 禁止把球踢回用户（硬性）

下面这些说法**一律不许出现**，出现即视为失败：

- "需要我帮你联网检索吗？" / "是否需要我检索？"
- "我可以为你查询，请确认" / "如果你需要，我可以进一步…"
- 结尾的 "是否需要？" / "可以吗？" / "要不要我继续？" 这类追问

原因：用户问出这种问题时，检索是**显然更好**的选择，反问只是在浪费一轮往返。
判据很简单 —— 如果这句话你自己就能判断"查了一定比不查好"，那就**直接查**，
查完再答。检索失败或查不到，才在答案里如实说明"未检索到"。
想补充延伸方向时，直接**陈述**"按技术栈/学历拆分后的差异是…"或一句
"需要更细的维度可以继续说"，不要用疑问句把球踢回来。

## 时效性判断

涉及薪资、价格、榜单、政策、版本、融资、招聘等**会随时间变化**的信息，
以及你的训练数据可能未覆盖的近期事件，**默认走联网检索**，不要凭记忆给数字。

## 诚实底线

- 不确定的事实要明说不确定，**禁止编造具体数字、比例、样本量、URL**。
- 检索不到就写"未检索到"，不要用常识补。
- 引用必须给完整 URL，且逐字复制自检索结果，禁止拼接。

## 工具使用纪律

- 相同关键词不要重复检索（工具层有缓存与预算熔断，重复会被拒绝）
- 工具返回"预算耗尽"或"未检索到"时，立即停止检索并基于已有信息作答
- 工具报错不要反复重试同一操作，换角度或直接说明

当前日期：{CURRENT_DATE}
"""


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
_models: Dict[str, Any] = {}


def build_model(tier: str = "auto"):
    """构造 OpenAI 兼容模型对象（按档位缓存）。

    注意：**传模型对象而不是字符串**。
    deepagents 的 resolve_model 会对字符串做 apply_provider_profile，
    按 provider 自动替换提示词与工具集，行为不可控。

    为什么 direct 档要单独配快模型：
    direct 的定位是"零工具、最快路径"，此时延迟几乎全部来自模型本身。
    用 MJ_LLM_MODEL_FAST（如 qwen-flash / qwen-turbo）能实打实砍掉首字延迟，
    而不是靠提示词恳求模型"简短一点"。
    """
    if tier in _models:
        return _models[tier]

    model_name = settings.llm_model
    if tier == "direct" and settings.llm_model_fast:
        model_name = settings.llm_model_fast

    try:
        from langchain_openai import ChatOpenAI
        from src.llm import resilience

        kw: Dict[str, Any] = {
            "model": model_name,
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "temperature": float(os.getenv("MJ_LLM_TEMPERATURE", "0.3")),
            "timeout": settings.llm_timeout,
            # 🔴 必须是 0：SDK 内部若再重试一层，会与我们的熔断重试相乘（2×2=4 次），
            # 而且 SDK 那层不区分错误类型，等于把熔断绕过去一半。
            "max_retries": 0,
        }
        # 熔断挂在 HTTP 层：粒度是**每一次模型调用**，而不是一整轮对话。
        sync_cli, async_cli = resilience.build_http_clients(
            model_name, settings.llm_timeout)
        if async_cli is not None:
            kw["http_client"] = sync_cli
            kw["http_async_client"] = async_cli

        m = ChatOpenAI(**kw)
        _primary: Any = m

        # 🔴 2026-10-14 修复：不再用 with_fallbacks 装配备用模型。
        # with_fallbacks 会把 ChatOpenAI 包成 RunnableWithFallbacks —— 它不是
        # BaseChatModel 子类，deepagents 的 resolve_model 的 isinstance 检查拦不住它，
        # 于是把它当模型字符串去 get_provider_profile → spec.partition(":") →
        # AttributeError('ChatOpenAI' object has no attribute 'partition')，工具调用
        # 路径 500。direct 档正常（单模型）、auto/tool/research 档必崩（带 fallback）。
        #
        # 备用模型的降级职责已由 resilience 层承担（HTTP 层熔断 + executor 的
        # is_llm_down 降级话术），不需要 langchain 的 RunnableWithFallbacks 再叠一层
        # —— 两套降级语义重叠且互相冲突。这里只返回真正的 BaseChatModel。
        # 备用模型仍会注册进 resilience（build_http_clients 各自一个熔断器），
        # 未来要用时在 executor 层按 is_llm_down 切，而不是在模型对象层包装。
        fb_name = (settings.llm_fallback_model
                   or (settings.llm_model_fast if model_name != settings.llm_model_fast
                       else ""))
        if fb_name and fb_name != model_name:
            # 只为备用模型建熔断器（resilience 用它做独立记账），不包装主模型
            resilience.build_http_clients(fb_name, settings.llm_timeout)
            logger.info(f"[agent] 备用模型已注册熔断器（不包装）：{model_name} → {fb_name}")
    except Exception as e:  # langchain-openai 缺失时给出可操作的提示
        raise RuntimeError(
            f"模型后端不可用（{type(e).__name__}: {e}）。"
            "请确认已安装 langchain-openai 并配置 MJ_LLM_BASE_URL / MJ_LLM_API_KEY。"
        ) from e
    _models[tier] = m
    logger.info(f"[agent] 模型已构造：tier={tier} model={model_name}")
    return m


# ---------------------------------------------------------------------------
# 工具集（按档位裁剪）
# ---------------------------------------------------------------------------
_mcp_tools_cache: Optional[List[Any]] = None


async def mcp_tools() -> List[Any]:
    """MCP 业务工具（进程内缓存一次）。

    为什么要缓存：每次建图都去 list_tools 会重复走一次 JSON-RPC 往返；
    而工具集在这台机器上是不变的，缓存一次即可。
    失败返回 [] —— MCP Server 起不来不该让整个平台不可用。
    """
    global _mcp_tools_cache
    if _mcp_tools_cache is not None:
        return _mcp_tools_cache
    try:
        from src.mcp_client.client import langchain_tools
        _mcp_tools_cache = await langchain_tools()
    except Exception as e:
        logger.warning(f"[agent] MCP 工具加载失败（降级为空）: {type(e).__name__}: {e}")
        _mcp_tools_cache = []
    return _mcp_tools_cache


async def tools_for_tier(tier: str) -> List[Any]:
    from src.capabilities.files import (list_my_files, read_uploaded_file,
                                        write_document)
    from src.capabilities.search import internet_search
    from src.agent.subagents import registry

    if tier == "direct":
        return []

    # P6：已经下放给子智能体的工具，主智能体手里要摘掉。
    # 不摘的话主智能体会直接调而不派发，子智能体永远用不上，
    # 上下文隔离和返回契约也就都白做了。
    # 剥离有两套：kb 工具按前缀 `kb_` 摘，db/web/doc 工具按具体名摘。
    def _strip(tools: List[Any]) -> List[Any]:
        if not registry.strip_main_tools():
            return tools
        prefixes = registry.delegated_tool_prefixes(tier)
        names = registry.delegated_tool_names(tier)
        if not prefixes and not names:
            return tools
        kept = [t for t in tools
                if not (any(getattr(t, "name", "").startswith(p) for p in prefixes)
                        or getattr(t, "name", "") in names)]
        if len(kept) != len(tools):
            logger.info(f"[agent] 主智能体已剥离下放工具：{len(tools) - len(kept)} 个"
                        f"（下放给 {registry.planned_for(tier)}）")
        return kept

    async def _mcp_kept() -> List[Any]:
        return _strip(await mcp_tools())

    if tier == "tool":
        # tool 档不挂子智能体，文件工具由主智能体自己握（写文档在这里是主智能体直用）
        return [list_my_files, read_uploaded_file, write_document] + await _mcp_kept()

    if tier == "rag":
        # P5：知识库已通过 MCP 接入（kb_search / kb_lookup / kb_stats）。
        # P6：这三个工具改由 kb-analyst 子智能体持有，主智能体只剩派发权。
        return [list_my_files, read_uploaded_file, write_document] + await _mcp_kept()

    # auto / research：内置工具也参与剥离（research 档下 web/doc 子智能体后，
    # 主智能体手里的 internet_search / write_document 要摘掉，否则不派发）
    builtins = [internet_search, list_my_files, read_uploaded_file, write_document]
    return _strip(builtins) + await _mcp_kept()


# ---------------------------------------------------------------------------
# 中间件（0.7.19 的必需补偿 + 上下文压缩）
# ---------------------------------------------------------------------------
def build_middleware():
    """
    0.7.19 有两个必须手动补的东西（旧项目验证过）：
      1. TodoListMiddleware —— 0.7.x 默认装配里被移除了
      2. BASE_AGENT_PROMPT —— 改名成私有变量且默认不拼（本平台自己写了系统提示词，
         不需要那套"教模型用 write_todos/文件系统"的内置指令，故不拼）
    另外加上 SummarizationMiddleware 做真正的上下文压缩（P1 只做了窗口截断）。
    """
    mw = []
    try:
        from langchain.agents.middleware import TodoListMiddleware
        mw.append(TodoListMiddleware())
    except Exception as e:
        logger.warning(f"[agent] TodoListMiddleware 不可用（跳过）: {e}")

    try:
        from langchain.agents.middleware import SummarizationMiddleware
        # 注意参数名（实测 langchain 1.4.2）：
        #   trigger=("tokens", N)  /  keep=("messages", N)
        # 不是旧版的 max_tokens_before_summary / messages_to_keep，写错会 TypeError。
        mw.append(SummarizationMiddleware(
            model=build_model(),
            trigger=("tokens", int(os.getenv("MJ_SUMMARY_TRIGGER", "6000"))),
            keep=("messages", int(os.getenv("MJ_SUMMARY_KEEP", "20"))),
        ))
    except Exception as e:
        # 参数或类不存在都不致命：退化为 P1 的窗口截断
        logger.warning(f"[agent] SummarizationMiddleware 未启用（回退窗口截断）: {e}")
    return mw


# ---------------------------------------------------------------------------
# 装配图（按档位缓存）
# ---------------------------------------------------------------------------
_graphs: Dict[str, Any] = {}


def _skills_dir() -> Optional[List[str]]:
    d = Path(os.getenv("MJ_SKILLS_DIR", str(Path(__file__).resolve().parents[2] / "skills")))
    if d.is_dir():
        return [str(d)]
    logger.info(f"[agent] skills 目录不存在，跳过挂载: {d}")
    return None


async def get_graph(tier: str) -> Any:
    """
    按档位装配并缓存图。

    改成 async 的原因：checkpointer 在 Postgres 路径要 await setup()，
    不能在同步函数里硬跑事件循环（会和 FastAPI 的循环打架）。

    为什么按档位分别建图而不是一张图通吃：
    direct 档不带任何工具时，模型没有任何"可以调工具"的选项，
    "直接答"才是强制的而不是靠提示词恳求 —— 这是用结构保证行为。
    """
    if tier in _graphs:
        return _graphs[tier]

    from deepagents import create_deep_agent
    from src.session.checkpointer import get_checkpointer

    import datetime as _dt
    system = BASE_SYSTEM_PROMPT.replace(
        "{CURRENT_DATE}", _dt.datetime.now().strftime("%Y-%m-%d"))

    checkpointer, _kind = await get_checkpointer()

    tools = await tools_for_tier(tier)

    # P6：子智能体装配。registry 里用一个同名零工具的占位规格顶掉默认的
    # general-purpose 子智能体 —— 默认那个会自动拿到主智能体的全部工具
    # （graph.py:837-841），等于把刚摘掉的权限又从后门塞回去。
    from src.agent.subagents import registry
    subagents = await registry.specs_for_tier(tier)
    extra: Dict[str, Any] = {}
    if registry.enabled():
        extra["subagents"] = subagents

    graph = create_deep_agent(
        model=build_model(tier),
        system_prompt=system,
        tools=tools,
        middleware=build_middleware(),
        skills=_skills_dir(),
        checkpointer=checkpointer,
        **extra,
    )
    _graphs[tier] = graph
    logger.info(f"[agent] 已装配图：tier={tier} tools={len(tools)} "
                f"subagents={[s['name'] for s in subagents]}")
    # 子智能体各拿到哪些工具必须留痕：权限裁剪一旦被第三方升级悄悄改回去，
    # 行为上完全正常，只有这份清单能暴露（见 registry.audit 的说明）。
    for row in registry.audit(subagents):
        logger.info(f"[agent]   subagent {row['name']}: "
                    f"{row['n_tools']} 个工具 {row['tools']}")
    for leak in registry.privilege_leak(subagents):
        logger.warning(f"[agent] 权限自检未通过：{leak}")
    return graph


# ---------------------------------------------------------------------------
# 档位引导语（不是分类结果，只是给模型的先验）
# ---------------------------------------------------------------------------
TIER_GUIDANCE = {
    "tool": "（当前档位：用工具。优先读取已上传的文件或数据，不要凭记忆猜。）",
    # 🔴 这一段必须与 tools_for_tier 的实际工具表保持一致。
    # P6 把 kb_* 三个工具下放给 kb-analyst 子智能体后，主智能体手里已经没有它们了；
    # 而这里的旧文案还在命令模型"调用 kb_search / kb_lookup"—— 模型找不到工具，
    # 就只能凭记忆作答并**编造出处**（实测：凭空写出《NR 系列 Web 手册 V1.02》
    # 和 source ID kb-h3c-nr-web-v102-202403，索引里根本没有这份文档）。
    # 这正是整套 RAG 架构要防的幻觉，而它恰恰是被"提示词与工具表脱节"放出来的。
    "rag": (
        "（当前档位：检索答。\n"
        "🔴 知识库工具 kb_search / kb_lookup / kb_stats **不在你手里** —— "
        "它们已下放给子智能体 kb-analyst。要查知识库，必须用 task 工具派发给 kb-analyst，"
        "把用户的原始问题写进 description，不要自己改写或先猜一个答案。\n"
        "硬性：\n"
        "1. 涉及产品手册、设备配置、内部文档的问题，**禁止凭记忆回答**；\n"
        "2. **绝对禁止编造文档名、章节号、source ID** —— 只能原样引用 "
        "kb-analyst 返回的 evidence 里的 source 字段；\n"
        "3. 每个关键结论都要注明它来自 evidence 中的哪个 source；\n"
        "4. kb-analyst 返回 degraded（not_wired / no_evidence / timeout）或 evidence 为空时，"
        "如实告知用户「知识库中未检索到」，**禁止用常识补内容**。）"
    ),
    # auto 档同样被下放了 kb_*（TIER_SUBAGENTS 里 auto 也列了 kb-analyst），
    # 所以同一条纪律必须在这里再说一遍，否则自动档会重蹈 rag 档的覆辙。
    "auto": (
        "（当前档位：自动。能凭已有知识答的就直接答，不要为了显得全面去调工具。\n"
        "但涉及产品手册、设备配置、内部文档等企业知识时：**知识库工具不在你手里**，"
        "必须用 task 工具派发给 kb-analyst 子智能体，"
        "禁止凭记忆编造文档名或出处。）"
    ),
    # research 档把技能里的硬性红线**直接抄进提示词**。
    # 原因：技能全文依赖模型主动加载，实测它会偷懒不读，
    # 于是"每条结论附原文 URL"这条就没执行，报告里 0 条链接。
    # 红线不能赌，必须常驻。
    "research": (
        "（当前档位：深度调研。执行路径：拆解子问题 → 多步检索 → 交叉验证 → 成稿。\n"
        "企业知识（产品手册 / 设备配置 / 内部文档）相关的子问题，"
        "知识库工具不在你手里，用 task 工具派发给 kb-analyst 子智能体；"
        "联网事实走 internet_search。两者都禁止编造来源。\n"
        "成稿硬性要求，缺一条即视为失败：\n"
        "1. 每个关键数据/结论后面必须跟**完整来源 URL**（https:// 全链接），"
        "逐字复制自检索结果里的 url 字段，禁止只写来源名、禁止凭印象拼接；\n"
        "2. 禁止编造数字、比例、样本量，没查到就写「未检索到」；\n"
        "3. 末尾附「未检索到的内容清单」；\n"
        "4. 系统会在落盘前校验正文链接是否为本次真实检索到的链接，"
        "对不上的会在文末「引用溯源校验」章节被逐条列出。\n"
        "只在用户明确要文件时才调用 write_document。）"
    ),
}


def compose_input(query: str, tier: str) -> str:
    g = TIER_GUIDANCE.get(tier)
    return f"{query}\n\n{g}" if g else query


# ---------------------------------------------------------------------------
# 执行（产出事件流）
# ---------------------------------------------------------------------------
async def run_agent(query: str, history: List[Dict[str, str]], tier: str,
                    thread_id: str) -> AsyncIterator[Dict[str, Any]]:
    """
    异步产出事件：{"type": "token"|"tool"|"done"|"error", ...}

    为什么做成事件流而不是等跑完再返回：
    旧项目的调研要 165~186s 且全程只在节点边界推事件，中间几十秒完全黑屏。
    有了 token 级事件，用户"感知延迟"会大幅下降（P3 再把它接到前端）。
    """
    from langchain_core.messages import AIMessageChunk, HumanMessage

    graph = await get_graph(tier)
    messages = []
    # P-1：长期记忆注入。放在 history 最前面，作为一条带标记的"上下文提示"，
    # 而不是 system prompt（system 是档位相关的，记忆是用户相关的，两者正交）。
    # 用 ContextVar 拿 user_id —— run_agent 本身不接收 user_id 参数，
    # 避免改 executor/service 的调用链；请求边界已在 bind() 时 set 好。
    # 记忆是"增强"不是"必需"：读取失败、为空都静默跳过，绝不阻塞主链路。
    try:
        from src import context as _ctx
        from src.agent import memory as _mem
        _uid = _ctx.get_user_id() or ""
        if _uid:
            _block = _mem.inject_memories(_uid)
            if _block:
                messages.append(HumanMessage(content=_block))
    except Exception:   # noqa: BLE001 记忆注入失败不致命
        pass
    for h in history or []:
        if h.get("role") == "user":
            messages.append(HumanMessage(content=h.get("content", "")))
        else:
            from langchain_core.messages import AIMessage
            messages.append(AIMessage(content=h.get("content", "")))
    messages.append(HumanMessage(content=compose_input(query, tier)))

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": int(os.getenv("MJ_RECURSION_LIMIT", "80")),
    }

    final_text = ""
    # 只保留**最后一条** AI 消息的文本。
    # 一轮 ReAct 里模型会先吐一段"我将：1. 检索…"再调工具，检索完再吐最终答案。
    # 不加区分地全拼起来，最终答案里就会混进这段中间自白（实测踩到）。
    # AIMessageChunk.id 在同一条消息的各分片间不变、跨消息必变，正好用作分段依据。
    last_msg_id: Optional[str] = None
    # 流式 tool_call 是分片的：name 只在首片出现，后续分片 name 为空、
    # index 相同。不去重会向前端推一堆空名事件，UI 上表现为"工具名一闪而过"。
    seen_tool_calls: set = set()
    # token 计量：OpenAI 兼容端的 usage 只在**最后一个分片**出现
    # （stream_options.include_usage）。绝不能累加 —— 上游给的若是累计值，
    # 累加会直接翻倍，数字就假了。取最后一个非空值是两种情况下都安全的做法。
    usage_seen: Dict[str, int] = {}
    try:
        async for chunk in graph.astream({"messages": messages},
                                         config=config,
                                         stream_mode="messages"):
            msg, meta = chunk if isinstance(chunk, tuple) else (chunk, {})
            node = (meta or {}).get("langgraph_node", "")

            if isinstance(msg, AIMessageChunk):
                mid = getattr(msg, "id", None)
                if mid and mid != last_msg_id:
                    last_msg_id = mid
                    final_text = ""

                text = getattr(msg, "content", "") or ""
                if isinstance(text, list):      # 多模态内容块
                    text = "".join(
                        p.get("text", "") for p in text
                        if isinstance(p, dict) and p.get("type") == "text")
                if text:
                    final_text += text
                    yield {"type": "token", "text": text}

                um = getattr(msg, "usage_metadata", None)
                if um:
                    cand = {k: int(v) for k, v in dict(um).items()
                            if isinstance(v, (int, float)) and v}
                    if cand:
                        usage_seen = cand

                for tc in getattr(msg, "tool_calls", None) or []:
                    name = tc.get("name", "")
                    if not name:
                        continue
                    key = tc.get("index", name)
                    if key in seen_tool_calls:
                        continue
                    seen_tool_calls.add(key)
                    yield {"type": "tool", "name": name,
                           "args": tc.get("args", {}), "node": node}

    except Exception as e:
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
        return

    if not final_text:
        final_text = "（模型未返回内容）"
    yield {"type": "done", "content": final_text, "usage": usage_seen}
