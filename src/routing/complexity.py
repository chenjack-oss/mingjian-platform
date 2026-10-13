# -*- coding: utf-8 -*-
"""
任务复杂度判断（P-1 轻量版）

这是中台方案 §4「轻量规划层」的第一块：在**不改执行行为**的前提下，
给每个请求打一个复杂度标签，供 P-2 的显式图骨架决定走哪条路。

设计纪律（吸收两个项目的教训）：
- 旧项目"永远在出调研报告"= 规划做过头 → 简单任务绝不强制进规划，必须能直答。
- 现平台"ReAct 太自由" = 复杂任务缺少确定的协作拓扑 → 复杂任务才标记为需要规划。

P-1 阶段做什么、不做什么：
- 做：纯规则快速分类（零成本、零额外 LLM 调用），把 complexity 写进 meta。
- 不做：不改变执行路径（简单/复杂此刻都还走同一个 run_agent），
        因为显式图骨架是 P-2 的工作。这里只是把"信号"先埋好，让 P-2 能接。

为什么 P-1 用规则而不是 LLM 打分：
- LLM 打分要把整段请求再跑一次模型，直接翻倍延迟与成本，违背"简单任务直答"的初衷。
- 规则的误判代价低：它只影响一个 meta 标签，不影响答案正确性。
- P-2 如果发现规则不够，再升级成"规则预筛 + 疑似复杂才 LLM 复核"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# 复杂意图的强信号词（命中即倾向 complex）。
# 这些词几乎必然意味着"多步 / 多数据源 / 需要产出"。
_COMPLEX_HINTS = (
    "调研", "分析", "对比", "比较", "总结", "归纳", "评估", "复盘",
    "规划", "方案", "报告", "综述", "梳理", "盘点", "竞品",
    "为什么", "怎么办", "如何", "怎么做", "可行性",
)

# 明显简单 / 事实型问题（命中即倾向 simple），用于兜住"问句里恰好带了个复杂词"的情况。
_SIMPLE_HINTS = (
    "你好", "谢谢", "你是谁", "几点", "今天几号", "什么时间", "多少钱",
)

# 简单任务的判定阈值：低于这个字数，且没有复杂信号词 → 大概率一句话能答。
_SIMPLE_MAX_LEN = 20


@dataclass
class Complexity:
    level: str          # simple / complex
    reason: str         # 判定依据（可观测）
    needs_planning: bool  # 是否建议走规划路径（P-2 显式图消费此字段）

    def to_dict(self) -> Dict:
        return {"level": self.level, "reason": self.reason,
                "needs_planning": self.needs_planning}


def judge(query: str) -> Complexity:
    """纯规则复杂度判断。绝不发起网络 / 模型调用，同步、零成本。"""
    q = (query or "").strip()
    if not q:
        return Complexity("simple", "空请求", False)

    # 1) 明显简单 → 直接判 simple，优先级最高（兜住复杂词误命中）
    if any(h in q for h in _SIMPLE_HINTS):
        return Complexity("simple", "命中简单意图词", False)

    # 2) 命中复杂信号词 → complex
    hits = [h for h in _COMPLEX_HINTS if h in q]
    if hits:
        return Complexity("complex", f"命中复杂意图词：{','.join(hits)}", True)

    # 3) 多子问题：出现两个及以上的编号（1. 2. 3. / ① ② ③ 等），无论换行还是行内。
    #    必须放短句判断之前 —— 编号多子问题往往是短句，但它是明确的复杂信号。
    import re
    nums = re.findall(r"[①②③④⑤]|（[一二三四五]）|(?:^|\s)\d+[.、)]", q)
    if len(nums) >= 2:
        return Complexity("complex", f"检测到 {len(nums)} 个编号子问题", True)

    # 4) 长文本 → 倾向 complex（长提问通常不是一句话能答的事实型）
    if len(q) > 120:
        return Complexity("complex", f"提问较长（{len(q)} 字），可能需拆解", True)

    # 5) 短句 → simple
    if len(q) <= _SIMPLE_MAX_LEN:
        return Complexity("simple", f"短句（{len(q)} 字）", False)

    # 6) 默认：中等长度、无强信号 → simple（宁简勿繁，简单任务绝不被强制规划）
    return Complexity("simple", "无强复杂度信号，按简单处理", False)
