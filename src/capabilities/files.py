"""
文件能力：读取用户上传文件 / 写文档（带引用溯源校验）

相比旧项目的两点改动：
1. **路径从"客户端可控"改成"服务端派生"**：
   旧项目是把文件名直接拼进路径，这里一律走 namespace.storage_dir(user, session)，
   工具层没有任何办法访问到别人的目录。
2. **写文档前强制做引用校验**：
   调用 citation_guard 比对正文 URL 与本次检索登记的真实 URL，
   覆盖率与可疑链接会以「引用溯源校验」章节追加进文档 —— 不静默删改。

注意：**模型自己决定要不要调 write_document**。
旧项目是 prompt 强制"正文不少于 1000 字 + 必须生成文件"，
这就是"永远在出调研报告"的直接原因；本平台把开关交回模型。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.tools import tool

from src import context
from src.capabilities import citation_guard
from src.config import settings
from src.identity.namespace import storage_dir

_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def _safe_filename(name: str) -> str:
    """只保留中英文数字与 . _ -，杜绝 ../ 这类路径穿越。"""
    base = os.path.basename(name or "").strip()
    base = _SAFE_NAME.sub("_", base)
    return base[:120] or "untitled.md"


def _session_dir() -> Path:
    uid = context.get_user_id() or "anonymous"
    sid = context.get_session_id() or "default"
    p = Path(storage_dir(settings.storage_root, uid, sid))
    p.mkdir(parents=True, exist_ok=True)
    return p


@tool
def list_my_files() -> Dict[str, Any]:
    """列出当前会话里已上传的文件（只有本用户本会话的）。"""
    d = _session_dir()
    files: List[Dict[str, Any]] = []
    for p in sorted(d.iterdir()):
        if p.is_file():
            files.append({"name": p.name, "size": p.stat().st_size})
    return {"count": len(files), "files": files}


@tool
def read_uploaded_file(filename: str) -> str:
    """读取当前会话中已上传的文件内容。传入文件名即可（不要带路径）。"""
    p = _session_dir() / _safe_filename(filename)
    if not p.exists():
        return f"文件不存在：{filename}。可用文件请先用 list_my_files 查看。"
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:20000]
    except Exception as e:
        return f"读取失败：{type(e).__name__} {e}"


@tool
def write_document(filename: str, title: str, content: str) -> Dict[str, Any]:
    """
    把内容写成 Markdown 文档并落盘。**只在确实需要交付文件时才调用它**。

    落盘前会自动做引用溯源校验，并在文末追加「引用溯源校验」章节。
    返回 coverage（可溯源比例）与 unverified（未能溯源的链接）。

    :param filename: 文件名，例如 调研报告.md
    :param title:   文档标题
    :param content: 正文（Markdown）
    """
    name = _safe_filename(filename)
    if not name.endswith(".md"):
        name += ".md"

    result = citation_guard.verify_citations(content, context.citation_key())
    body = content or ""
    # 无条件追加，不是笔误：total == 0 恰恰是最危险的盲区 —— 模型用「来源名 + 日期」
    # 代替完整 URL 时，看着有引用但点不开，等同不可溯源。build_audit_section 对
    # 0 条会输出专门的盲区提示，所以这里不能写成 `if total > 0`。
    body += citation_guard.build_audit_section(result)

    full = f"# {title}\n\n{body}"
    p = _session_dir() / name
    p.write_text(full, encoding="utf-8")

    return {
        "ok": True,
        "path": str(p),
        "filename": name,
        "chars": len(full),
        "citation_total": result["total"],
        "citation_verified": result["verified"],
        "coverage": result["coverage"],
        "unverified": result["unverified"],
    }
