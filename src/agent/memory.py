# -*- coding: utf-8 -*-
"""
长期记忆（P-1 记忆底座）：用户级 + 组织级共享。

设计原则（对应中台总体方案 §3）：
- **异步提炼，不阻塞主链路**：记忆是"增强"不是"必需"，提炼塞进请求链路
  会把 1.5s 变 3s+。所以提炼在 send_message 返回后走后台（Celery / 线程）。
- **分层**：fact（稳定事实）/ preference（偏好）/ episode（情节，P-1 暂不做）。
- **注入时"无关请忽略"**：防止过时记忆带偏模型。
- **用户可见可删**：隐私合规底线（尤其企业数据）。

为什么不用全局变量存记忆：多租户 + 多进程，记忆必须落库（Postgres/SQLite），
按 owner_id 隔离，复用现有 SQLAlchemy 层。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src import context
from src.db import repo
from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

# 注入上限：太多记忆会挤占上下文，且过时记忆比没有记忆更糟
_MAX_INJECT = 20


def _model_fast():
    """快模型（用于提炼，避免用主模型烧钱）。失败则退回主模型名。"""
    from src.config import settings
    return settings.llm_model_fast or settings.llm_model


async def extract_memories(user_id: str, session_id: str) -> None:
    """异步提炼：把该会话自上次提炼以来的新消息，抽成用户事实/偏好。

    幂等：memory_extracts 记录 last_message_id，重复调用不会重复提炼。
    提炼失败不抛出（记忆是增强，不是必需），只记日志。
    """
    try:
        last = repo.get_memory_extract(session_id)
        # 拉取该会话中 id > last 的消息
        from src.db.engine import messages
        from src.db import engine as db
        rows = db.fetch_all(
            messages.select()
            .where(messages.c.session_id == session_id)
            .where(messages.c.id > last)
            .order_by(messages.c.id.asc())
            .limit(50)
        )
        if not rows:
            return

        new_last = max(r["id"] for r in rows)
        dialogue = "\n".join(
            f"{r['role']}: {r['content']}" for r in rows if r.get("content")
        )[:6000]

        facts = await _llm_extract(user_id, dialogue)
        for f in facts:
            content = str(f.get("content", "")).strip()
            kind = str(f.get("kind", "")).strip()
            if not content or kind not in ("fact", "preference"):
                continue
            imp = int(f.get("importance", 1) or 1)
            imp = max(1, min(5, imp))
            repo.upsert_memory(user_id, "user", kind, content,
                               source_session=session_id, importance=imp)

        repo.set_memory_extract(session_id, new_last)
        logger.info(f"[memory] 提炼完成：session={session_id} +{len(facts)} 条")
    except Exception as e:          # noqa: BLE001 记忆增强，失败不致命
        logger.warning(f"[memory] 提炼失败（忽略）: {type(e).__name__}: {e}")


async def _llm_extract(user_id: str, dialogue: str) -> List[Dict[str, Any]]:
    """用快模型提炼事实/偏好。返回 [{"content","kind","importance"}, ...]。"""
    import json
    prompt = (
        "从下面这段对话中提取关于用户的【稳定、跨会话有效】的事实和偏好。\n"
        "规则：\n"
        "1. 只提取长期有效的信息（如身份、职业、技术栈、做事风格、明确的偏好）；\n"
        "2. 不要提取临时/会话性的内容（如\"帮我查一下\"\"这次的结果\"）；\n"
        "3. 输出 JSON 数组，每项 {\"content\":\"...\",\"kind\":\"fact|preference\","
        "\"importance\":1~5}；\n"
        "4. 没有可提取的就输出 []。\n\n"
        f"对话：\n{dialogue}"
    )
    try:
        from src.agent.core import build_model
        model = build_model("direct")
        resp = await model.ainvoke([("human", prompt)])
        text = getattr(resp, "content", "") or ""
        # 容错：剥掉可能的 ```json 围栏
        s = text.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:]
            s = s.strip()
        i, j = s.find("["), s.rfind("]")
        if i >= 0 and j > i:
            s = s[i:j + 1]
        data = json.loads(s)
        return data if isinstance(data, list) else []
    except Exception as e:          # noqa: BLE001
        logger.warning(f"[memory] LLM 提炼失败: {type(e).__name__}: {e}")
        return []


def format_memories(mems: List[Dict[str, Any]]) -> str:
    """把记忆列表格式化成注入提示词的文本块。空列表返回空串。"""
    if not mems:
        return ""
    lines = ["## 关于用户的已知信息（若与当前请求无关请忽略）"]
    for m in mems:
        tag = "偏好" if m.get("kind") == "preference" else "事实"
        lines.append(f"- [{tag}] {m.get('content', '')}")
    return "\n".join(lines) + "\n"


def inject_memories(owner_id: str, org_id: str = "") -> str:
    """装配记忆文本块：用户级 + 组织级（若提供 org_id）。"""
    parts = []
    try:
        user_mems = repo.list_memories(owner_id, "user",
                                       kinds=("fact", "preference"),
                                       limit=_MAX_INJECT)
        parts.append(format_memories(user_mems))
    except Exception as e:          # noqa: BLE001
        logger.warning(f"[memory] 用户记忆读取失败: {type(e).__name__}: {e}")
    if org_id:
        try:
            org_mems = repo.list_memories(org_id, "org",
                                          kinds=("fact", "preference"),
                                          limit=_MAX_INJECT)
            parts.append(format_memories(org_mems))
        except Exception as e:      # noqa: BLE001
            logger.warning(f"[memory] 组织记忆读取失败: {type(e).__name__}: {e}")
    return "\n".join(p for p in parts if p)
