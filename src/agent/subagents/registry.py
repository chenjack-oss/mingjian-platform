# -*- coding: utf-8 -*-
"""子智能体注册表（P6）：按档位装配，并关掉默认 general-purpose 子智能体。

两个必须知道的事实（都对着 deepagents 0.7.19 源码核过）
--------------------------------------------------------
1. **默认会塞一个 general-purpose 子智能体进来**（graph.py:797-861）：
   条件是 `gp_profile.enabled is not False`，且它会拿到**主智能体的全部工具**
   （`"tools": _tools or []`）。
   这正好破坏我们"主智能体不握业务工具"的设计 —— 主智能体一转手就能通过它
   拿到所有工具，权限最小化形同虚设。所以必须显式关掉：
   `general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)`

2. **子智能体无状态**（subagents.py:545 `create_sub_agent` 不接 checkpointer），
   跑完即焚。好处是绕开与 AsyncPostgresSaver 的 thread_id 冲突；
   代价是子智能体的过程只能靠 trace 复盘 → Langfuse 必须开。

坑：关掉 general-purpose 的正确姿势不是传参
------------------------------------------
`create_deep_agent` 的 docstring（graph.py:411）写着可以传
`general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)`，
但 **0.7.19 的函数签名里根本没有这个参数**（实测 TypeError）。
它属于 harness profile，而 profile 是按模型推导出来的，不给外部覆盖。

可行办法是**自己注册一个同名规格**：graph.py:798 的注入条件是
`not any(spec["name"] == "general-purpose" for spec in inline_subagents)` ——
只要清单里已经有叫 general-purpose 的，默认那个就不会被塞进来。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from . import db_analyst, doc_analyst, kb_analyst, web_analyst

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

# 档位 → 派发给哪些子智能体（文书分档：不是所有事都走完整链路）
#   direct  零工具、最快，不派发
#   tool    读用户上传的文件，主智能体自己干，不派发
#   auto    模型自己判断，给知识库子智能体
#   rag     检索答，知识库子智能体
#   research 深度调研，四大能力域全上（知识/数据/外部/产出）
#
# 这是**默认**装配。运行时可被环境变量覆盖（见 _env_subagents_for_tier），
# 支持按档位动态开关子智能体，为插件化 / A/B 打基础。
_DEFAULT_TIER_SUBAGENTS = {
    "direct": [],
    "tool": [],
    "auto": [kb_analyst.NAME],
    "rag": [kb_analyst.NAME],
    "research": [kb_analyst.NAME, db_analyst.NAME,
                 web_analyst.NAME, doc_analyst.NAME],
}

_BUILDERS = {
    kb_analyst.NAME: kb_analyst.build,
    db_analyst.NAME: db_analyst.build,
    web_analyst.NAME: web_analyst.build,
    doc_analyst.NAME: doc_analyst.build,
}

# 所有**已实现**的子智能体名（_BUILDERS 的键即能力全集）。
# 动态开关 / 禁用名单里出现不在这集合里的名字，会被静默忽略，避免拼写错误静默失效。
_ALL_KNOWN = frozenset(_BUILDERS.keys())

# 这些子智能体的工具没有统一前缀（kb 有 "kb_" 前缀，但 db/web/doc 是零散工具名），
# 所以主智能体剥离时按**具体工具名**摘，而不是按前缀。
_DELEGATED_TOOL_NAMES = {
    db_analyst.NAME: {"list_tables", "get_table_schema", "execute_readonly_sql"},
    web_analyst.NAME: {"internet_search"},
    doc_analyst.NAME: {"write_document"},
}


def enabled() -> bool:
    """总开关。出问题时可以一键回到 P5 行为。"""
    return os.getenv("MJ_SUBAGENTS_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def _env_list(key: str) -> Optional[List[str]]:
    """读逗号分隔的环境变量名单。未设置返回 None（走默认），设置了但为空返回 []。

    为什么区分「未设置」和「设为空」：
        未设置 → 用 _DEFAULT_TIER_SUBAGENTS 的默认装配；
        设为空串 → 明确表示「这一档一个子智能体都不派」（比如临时下线 research 全部）。
    """
    raw = os.getenv(key)
    if raw is None:
        return None
    cleaned = [x.strip() for x in raw.split(",") if x.strip()]
    return cleaned


def _disabled() -> set:
    """全局禁用名单：MJ_SUBAGENTS_DISABLED=web-analyst,doc-analyst。

    与档位名单叠加生效（先按档位取，再剔除禁用的）。用于临时下线某个出故障的子智能体，
    不用改代码、不用重启就能止血。
    """
    return {n for n in (_env_list("MJ_SUBAGENTS_DISABLED") or []) if n in _ALL_KNOWN}


def planned_for(tier: str) -> List[str]:
    """这一档最终派发哪些子智能体（已合并默认装配 + 环境变量覆盖 + 全局禁用）。"""
    # 1) 取该档位的名单：优先环境变量 MJ_SUBAGENTS_<TIER>，否则默认
    override = _env_list(f"MJ_SUBAGENTS_{tier.upper()}")
    if override is not None:
        base = [n for n in override if n in _ALL_KNOWN]  # 忽略不认识的名字
    else:
        base = list(_DEFAULT_TIER_SUBAGENTS.get(tier, []))
    # 2) 剔除全局禁用
    off = _disabled()
    return [n for n in base if n not in off]


def enabled_subagents() -> List[str]:
    """返回全平台**当前生效**的子智能体名（去重、按定义顺序），供 /health 与启动日志核对。

    这是「动态注册」的可观测出口：运维/面试都能从这里一眼看到实际装配了哪些能力。
    """
    seen: List[str] = []
    for tier in _DEFAULT_TIER_SUBAGENTS:
        for name in planned_for(tier):
            if name not in seen:
                seen.append(name)
    return seen


def strip_main_tools() -> bool:
    """挂了子智能体的档位，是否把对应工具从主智能体手里摘掉。

    摘掉才有意义：主智能体手里还有 kb_search 的话，它会直接调而不派发，
    子智能体就永远不会被用到，隔离上下文和契约也就都白做了。
    """
    return os.getenv("MJ_SUBAGENTS_STRIP_MAIN_TOOLS", "1").strip().lower() not in ("0", "false", "no")


def general_purpose_suppressor() -> Dict[str, Any]:
    """顶掉 deepagents 默认注入的 general-purpose 子智能体。

    默认那个会拿到**主智能体的全部工具**（graph.py:837-841 `"tools": _tools or []`），
    主智能体一转手就能通过它拿回所有权限，我们刚做的工具裁剪就白做了。

    做法：注册一个**同名、零工具**的占位规格。graph.py:798 看到同名就不注入默认那个。
    """
    return {
        "name": "general-purpose",
        "description": (
            "保留位，**不要派任务给它**。本平台的外部能力已按职责下放给专属子智能体"
            "（如 kb-analyst 负责知识库），需要外部数据时派给对应的专属子智能体。"
        ),
        "system_prompt": "你没有可用工具。若被误派，直接说明本占位不处理任务。",
        "tools": [],
    }


def audit(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把每个子智能体**实际拿到的工具**列出来，供启动日志与 /health 核对。

    为什么需要它（原 🟡 风险项）：
        general-purpose 抑制靠的是 deepagents 的内部行为 —— graph.py:798
        "看到同名就不注入默认那个"。版本一变就可能失效，而失效的表现是
        "权限悄悄回到主智能体手里"，行为上完全正常，不看清单根本发现不了。

    这里刻意**不去 patch 第三方**：跟着内部实现改代码只会让耦合更深。
    取而代之的是把事实打印出来 —— 让回归能被看见，比让它在暗处生效更可靠。
    """
    rows = []
    for s in specs or []:
        tools = s.get("tools") or []
        rows.append({
            "name": s.get("name", ""),
            "tools": [getattr(t, "name", str(t)) for t in tools],
            "n_tools": len(tools),
        })
    return rows


def privilege_leak(specs: List[Dict[str, Any]]) -> List[str]:
    """自检：占位智能体必须是零工具的；非零工具说明抑制被绕过了。

    返回异常描述（空列表 = 正常）。只告警不阻断：
    阻断会让整个档位不可用，而这里更可能是"升级后需要重新确认"而非"正在被攻击"。
    """
    bad = []
    for row in audit(specs):
        if row["name"] == "general-purpose" and row["n_tools"] > 0:
            bad.append(f"占位智能体拿到了 {row['n_tools']} 个工具，抑制可能被绕过")
    return bad


async def specs_for_tier(tier: str) -> List[Dict[str, Any]]:
    """按档位装配子智能体规格。失败的子智能体不注册（主智能体看不到就不会派）。"""
    if not enabled():
        return []
    out: List[Dict[str, Any]] = []
    for name in planned_for(tier):
        builder = _BUILDERS.get(name)
        if builder is None:
            continue
        try:
            spec = await builder()
        except Exception as e:
            logger.warning(f"[subagent] {name} 装配失败（跳过）: {type(e).__name__}: {e}")
            continue
        if spec:
            out.append(spec)

    # 占位规格放在最后：主智能体看到的可用清单里，专属子智能体排在前面
    if out:
        out.append(general_purpose_suppressor())
    return out


def delegated_tool_prefixes(tier: str) -> List[str]:
    """这个档位里，哪些工具已下放（按**前缀**摘，如 kb_）。"""
    names = set(planned_for(tier))
    prefixes: List[str] = []
    if kb_analyst.NAME in names:
        prefixes.append(kb_analyst.TOOL_PREFIX)
    return prefixes


def delegated_tool_names(tier: str) -> set:
    """这个档位里，哪些工具已下放（按**具体工具名**摘，如 internet_search/write_document）。

    为什么要有两套（前缀 vs 具名）：
    kb 工具命名整齐（kb_search/kb_lookup/kb_stats），按前缀 `kb_` 摘最省事且不会漏；
    但 db/web/doc 的工具名是零散的（list_tables / internet_search / write_document），
    没有公共前缀，只能按名摘。两套并存，各自覆盖各自的子智能体。
    """
    out: set = set()
    for name in planned_for(tier):
        out |= _DELEGATED_TOOL_NAMES.get(name, set())
    return out
