"""
OpenAI 兼容接口的最小客户端

为什么自己写 60 行而不用 langchain 的 init_chat_model：
    P1 只需要"发一段 messages 拿回一段文本"，引入 langchain 会带进几十个传递依赖
    （langchain-core / langsmith / pydantic 版本约束），对"先把地基打稳"这一期是负担。
    P2 接入 deepagents 时再统一到 langchain 生态，届时这里的接口不用变。

不变的地方：调用的是同一个百炼 OpenAI 兼容端点、同一个 qwen-max，
所以 P1 的输出质量和旧项目是同一水准，不存在"临时凑合"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from src.config import settings


class LLMUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(settings.llm_base_url and settings.llm_api_key)


async def chat(messages: List[Dict[str, str]],
               temperature: float = 0.3,
               max_tokens: int = 2048,
               timeout: Optional[float] = None) -> str:
    """
    返回助手文本。失败抛 LLMUnavailable —— 由上层决定降级，
    绝不在这里"编一个像样的回答"糊过去。
    """
    if not available():
        raise LLMUnavailable("未配置 MJ_LLM_BASE_URL / MJ_LLM_API_KEY")

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout or settings.llm_timeout) as cli:
            r = await cli.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise LLMUnavailable(f"模型调用失败: {type(e).__name__} {e}") from e

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        raise LLMUnavailable(f"模型返回格式异常: {e}") from e
