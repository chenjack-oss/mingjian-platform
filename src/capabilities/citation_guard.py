"""
引用溯源校验器（从旧项目平移，行为保持一致）

为什么需要它（旧项目实测发现的真缺陷）：
    模型生成报告时会编造 URL —— 实测出现过
    `https://www.zhipin.com/job_detail/1a2b3c4d5e6f7g8h.html`
    这种明显拼出来的假链接。对"调研"这个产品形态来说，不可溯源 = 报告不可用。

做法（用确定性代码约束 LLM 的不确定性）：
    1. 搜索工具真实返回后，把结果里的 URL 登记进本次会话的「可信 URL 池」
    2. 写文档前，正则抽正文里的 URL 与可信池逐一比对
    3. 比不上的**不静默删除**（会丢信息，且模型下轮会补一个别的假链接），
       而是在文末追加「引用溯源校验」章节显式列出，保留可审计性
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Set
from urllib.parse import urlsplit

_url_registry: Dict[str, Set[str]] = {}
_registry_lock = threading.RLock()

_URL_RE = re.compile(r"https?://[^\s\(\)\[\]\"'<>）】]+")

# 图片 CDN / 站点图标之类不参与校验，纯噪声
_IGNORE_HOSTS = ("byteimg.com", "gstatic.com", "w3.org")


def _norm(url: str) -> str:
    """归一化：小写 host、去末尾斜杠与锚点，保证比对稳定"""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.netloc or "").lower()
        path = (parts.path or "").rstrip("/")
        return f"{parts.scheme}://{host}{path}"
    except Exception:
        return (url or "").strip().rstrip("/")


def register_urls(key: str, urls: List[str]) -> None:
    with _registry_lock:
        bucket = _url_registry.setdefault(key or "global", set())
        for u in urls or []:
            if u:
                bucket.add(_norm(u))


def get_registered(key: str) -> Set[str]:
    with _registry_lock:
        return set(_url_registry.get(key or "global", set()))


def clear(key: str) -> None:
    with _registry_lock:
        _url_registry.pop(key or "global", None)


def verify_citations(content: str, key: str) -> Dict[str, Any]:
    found = _URL_RE.findall(content or "")
    seen, urls = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    urls = [u for u in urls if not any(h in u for h in _IGNORE_HOSTS)]

    trusted = get_registered(key)
    verified, unverified = [], []
    for u in urls:
        (verified if _norm(u) in trusted else unverified).append(u)

    total = len(urls)
    return {
        "total": total,
        "verified": len(verified),
        "unverified": unverified,
        "coverage": round(len(verified) / total, 3) if total else 1.0,
    }


def build_audit_section(result: Dict[str, Any]) -> str:
    total = result["total"]
    verified = result["verified"]
    unverified = result["unverified"]
    coverage = result["coverage"]

    lines = ["", "---", "", "## 引用溯源校验（系统自动生成）", ""]

    if total == 0:
        # 盲区：模型改用"来源名 + 日期"而不给完整 URL 时正则抽不到，
        # 看着有引用但点不开，等同不可溯源，必须显式提示。
        lines += [
            "- ⚠️ **正文中未包含任何可点击的来源 URL**（共检测到 0 条链接）",
            "- 当前引用若为「来源名 + 日期」形式，读者无法自行核验原始信源",
            "",
            "> 说明：本节由系统在文件落盘前自动追加。带引用的文档要求关键结论附完整 URL，"
            "请补充检索结果中的原始链接。",
            "",
        ]
        return "\n".join(lines)

    lines += [
        f"- 正文引用总数：**{total}**",
        f"- 可在本次检索结果中溯源：**{verified}**（覆盖率 {coverage:.0%}）",
    ]
    if unverified:
        lines.append(f"- 未能溯源（**{len(unverified)}** 条，请人工核实后再引用）：")
        for u in unverified:
            lines.append(f"  - ⚠️ {u}")
        lines += [
            "",
            "> 说明：本节由系统在文件落盘前自动追加。未溯源的链接并非一定错误，"
            "但本次检索结果中未出现，可能存在链接拼接或记忆偏差，请谨慎引用。",
        ]
    else:
        lines.append("- 未发现不可溯源的引用。")
    lines.append("")
    return "\n".join(lines)
