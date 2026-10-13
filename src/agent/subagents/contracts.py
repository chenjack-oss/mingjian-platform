# -*- coding: utf-8 -*-
"""子智能体返回契约（P6）。

为什么要有契约
--------------
deepagents 把子智能体的**最后一条 AIMessage 文本**回传给主智能体
（middleware/subagents.py:743 走"从后往前找第一条非空 AIMessage"）。
也就是说子智能体回一段自由文本，主智能体拿到手之后：

  - 分不清这段话是"查到的"还是"编的"
  - 分不清"没查到"和"调用失败"
  - 没法把证据交给 citation_guard 做二次校验

契约的作用就是把这三种情况显式化：**主智能体凭 degraded 字段决定是换路子还是如实告知，
而不是自己圆回来。**

为什么校验要"容错"而不是"严格抛错"
------------------------------------
子智能体是模型，它会漏字段、会多包一层、会在失败时返回 MCP 的错误字典。
严格校验会让一次模型表达不规范演变成整个请求失败 —— 得不偿失。
所以这里的策略是：**凡是解析不出结构的，一律降级为 tool_error 而不是报错**，
主智能体看到 tool_error 就知道"这条不可信"，行为依然正确。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ---- degraded 取值（封闭集合，不许自造） ------------------------------------
# None 表示正常
NOT_WIRED = "not_wired"              # 能力没接上（如 rag MCP 没起）
TIMEOUT = "timeout"                  # 调用超时
NO_EVIDENCE = "no_evidence"          # 工具调通了但没查到（**最常见也最重要**）
TOOL_ERROR = "tool_error"            # 工具返回错误 / 契约解析失败
PERMISSION_DENIED = "permission_denied"  # 越权请求被拒（如写操作、非只读 SQL）

VALID_DEGRADED = (NOT_WIRED, TIMEOUT, NO_EVIDENCE, TOOL_ERROR, PERMISSION_DENIED)

# 给主智能体看的人话说明，进提示词用
DEGRADED_HINT = {
    NOT_WIRED: "能力未接入，不要假装用过",
    TIMEOUT: "调用超时，可换个角度重试一次或直接如实告知",
    NO_EVIDENCE: "工具调通但确实没查到，**禁止用常识补内容**",
    TOOL_ERROR: "工具报错或返回不可解析，不可信",
    PERMISSION_DENIED: "请求越权被拒（只允许只读），不要试图绕过",
}


@dataclass
class Evidence:
    """一条证据。source 是出处标识（文件名 / 表名 / URL），必填。"""
    source: str = ""
    snippet: str = ""
    score: float = 0.0

    def is_valid(self) -> bool:
        return bool(self.source.strip())


@dataclass
class SubAgentResult:
    """所有子智能体统一返回信封。"""
    ok: bool = False
    answer: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    degraded: Optional[str] = None
    tool_used: str = ""
    elapsed_ms: int = 0
    # P7 新增：证据质量信号。
    # 为什么契约里必须带：主智能体只看得到"有没有证据"，看不到"证据有多硬"。
    # 没有这两个字段，rag 档即使召回了弱证据也会被当成正常答案返回，
    # 平台边界（executor / API 响应）拿不到任何可观测的质量信号。
    #   answer_level：Agentic RAG 的置信等级 confident/cautious/refuse
    #   confidence   ：证据侧绝对相关性（dense 余弦原值，>0.7 才谈得上真相关）
    answer_level: str = ""
    confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_text(self) -> str:
        """回传给主智能体的文本（会被 subagents.py:743 取走）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def hint(self) -> str:
        return DEGRADED_HINT.get(self.degraded or "", "")


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        # 模型常见的两种污染：```json 围栏、前后废话
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:]
            s = s.strip()
        # 截取最外层花括号，容忍前后缀文字
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def coerce(raw: Any, *, tool_used: str = "", elapsed_ms: int = 0) -> SubAgentResult:
    """把子智能体的任意返回压成信封。永不抛异常。"""
    if raw is None:
        return SubAgentResult(ok=False, degraded=TOOL_ERROR,
                              tool_used=tool_used, elapsed_ms=elapsed_ms,
                              answer="子智能体无返回")

    data = _parse(raw)
    if data is None:
        # 解析不出结构：把原文留着（可能是人话，主智能体还能读），但打上不可信标记
        text = raw if isinstance(raw, str) else str(raw)
        return SubAgentResult(ok=False, answer=text[:4000], degraded=TOOL_ERROR,
                              tool_used=tool_used, elapsed_ms=elapsed_ms)

    # MCP 层已经把错误压成 {"error": True, "kind": ..., "message": ...}
    if data.get("error"):
        kind = str(data.get("kind") or "")
        deg = TIMEOUT if kind == "timeout" else (
            NOT_WIRED if kind in ("unavailable", "transport") else TOOL_ERROR)
        return SubAgentResult(ok=False, answer=str(data.get("message") or "")[:2000],
                              degraded=deg, tool_used=tool_used, elapsed_ms=elapsed_ms)

    # 兼容：MCPServer 2.x 会把 dict 再包一层 {"result": ...}
    if set(data.keys()) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]

    degraded = data.get("degraded")
    if degraded is not None:
        degraded = str(degraded)
        if degraded not in VALID_DEGRADED:
            degraded = TOOL_ERROR

    evs: List[Evidence] = []
    raw_evs = data.get("evidence") or []
    if isinstance(raw_evs, list):
        for e in raw_evs:
            if isinstance(e, dict):
                ev = Evidence(source=str(e.get("source") or ""),
                              snippet=str(e.get("snippet") or e.get("content") or "")[:1200],
                              score=float(e.get("score") or 0.0))
            elif isinstance(e, str):
                ev = Evidence(source=e)
            else:
                continue
            if ev.is_valid():
                evs.append(ev)

    answer = str(data.get("answer") or data.get("content") or "").strip()
    ok = bool(data.get("ok")) if "ok" in data else (bool(answer) and degraded is None)

    # P7：证据质量信号（容错解析：解析不出来就是 None，不因此判定失败）
    level = str(data.get("answer_level") or "").strip()
    conf: Optional[float] = None
    try:
        if data.get("confidence") is not None:
            conf = round(float(data.get("confidence")), 4)
    except (TypeError, ValueError):
        conf = None

    return SubAgentResult(
        ok=ok,
        answer=answer[:8000],
        evidence=evs,
        degraded=degraded,
        tool_used=str(data.get("tool_used") or tool_used),
        elapsed_ms=int(data.get("elapsed_ms") or elapsed_ms or 0),
        answer_level=level,
        confidence=conf,
    )


# ---- 子智能体的系统提示词尾巴：逼它输出信封 ----------------------------------
ENVELOPE_INSTRUCTION = """
【返回格式（硬性）】
你的**最后一条回复**必须是一个 JSON 信封，不要有任何其他文字，格式如下：

{"ok": true, "answer": "可直接引用的结论", "evidence": [{"source": "出处", "snippet": "原文片段", "score": 0.82}], "degraded": null, "tool_used": "你用的工具名", "elapsed_ms": 19200, "answer_level": "confident", "confidence": 0.83}

字段规则：
- ok：结论可用为 true；查不到或出错为 false
- answer：结论正文。查不到就写"未检索到相关内容"，**禁止用常识或推测补内容**
- evidence：支撑结论的证据，每条必须带 source；没有证据就给空数组 []
- degraded：正常为 null；没查到填 "no_evidence"；超时填 "timeout"；越权填 "permission_denied"；工具报错填 "tool_error"
- tool_used：你实际调用的工具名
- elapsed_ms：耗时（毫秒）
- answer_level：**原样照抄**工具返回的等级（confident / cautious / refuse），不要自己判断、不要美化
- confidence：工具返回的证据相关性数值（没有就填 null），同样不要自己编
"""
