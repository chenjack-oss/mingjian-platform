"""
四档：P2 之后语义变了

**P1（已废弃的做法）**：档位是"执行器选择器" —— 先用规则/LLM 猜一个档位，再决定跑什么。
    问题：判错整轮错，且候选集里没有"直接答"这一档，实质问题全被挤进深度调研。

**P2（现在）**：档位是**能力约束与使用门槛**，不再承担"猜用户意图"的职责。
    默认 `auto` = 进执行核循环，由模型自己决定调不调工具、写不写文档。

| 档位 | 语义 | 工具集 |
| --- | --- | --- |
| `auto` | 默认。模型自己判断要不要查、要不要写 | 全部 |
| `direct` | 强制零工具（用户明确要最快路径时用） | 无 |
| `tool` | 引导优先用文件/数据工具 | 文件类 |
| `rag` | 基于知识库作答（**P5 经 MCP 接入 kb_search/kb_lookup，未挂上时诚实降级**） | 文件类 + kb_* |
| `research` | 引导加载 deep-research 技能并按方法论执行 | 全部 + 技能 |

手动指定仍然优先 —— 自动一定会误判，用户必须有"你判断错了，我指定"的出口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

DIRECT, TOOL, RAG, RESEARCH = "direct", "tool", "rag", "research"
AUTO = "auto"

ALL_TIERS = [DIRECT, TOOL, RAG, RESEARCH]
SELECTABLE = [AUTO, *ALL_TIERS]

TIER_META: Dict[str, Dict[str, str]] = {
    AUTO:     {"label": "自动", "desc": "模型自己判断要不要检索、要不要写文档（默认）"},
    DIRECT:   {"label": "直接答", "desc": "强制零工具，单次调用，最快"},
    TOOL:     {"label": "用工具", "desc": "优先读取已上传的文件与数据"},
    RAG:      {"label": "检索答", "desc": "基于知识库作答并给出引用（P5：经 MCP 接入 kb_search/kb_lookup）"},
    RESEARCH: {"label": "深度调研", "desc": "加载 deep-research 技能：多步检索 + 交叉验证 + 按需出文档"},
}

# 需要走执行核（agent 循环）的档位
AGENT_TIERS = [AUTO, DIRECT, TOOL, RESEARCH]


@dataclass
class TierDecision:
    tier: str
    source: str          # manual / session / default
    reason: str = ""

    def to_dict(self) -> Dict:
        return {"tier": self.tier, "source": self.source, "reason": self.reason,
                "label": TIER_META.get(self.tier, {}).get("label", self.tier)}


def resolve_tier(requested: Optional[str] = None,
                 session_preferred: str = AUTO) -> TierDecision:
    """
    档位决策：**手动 > 会话偏好 > 默认 auto**

    注意这里**不再有任何"猜意图"的逻辑**（旧的 rule_predict 已删除）。
    猜意图是 P1 的做法，也是"永远在出调研报告"的帮凶之一 ——
    现在意图判断交回模型，档位只表达"我允许你用什么、希望你怎么做"。
    """
    if requested and requested != AUTO:
        r = requested.strip().lower()
        if r not in ALL_TIERS:
            raise ValueError(f"未知档位: {requested}，可选 {SELECTABLE}")
        return TierDecision(r, "manual", "用户手动指定")

    if session_preferred and session_preferred != AUTO:
        sp = session_preferred.strip().lower()
        if sp in ALL_TIERS:
            return TierDecision(sp, "session", "沿用本会话的档位偏好")

    return TierDecision(AUTO, "default", "默认：由模型自己决定是否检索/写文档")
