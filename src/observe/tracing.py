"""
Langfuse 观测（P3）

**为什么明辨需要它**：我们已经在记"花了多少时间、调了哪些工具"，但那些是**离散的日志**。
日志能回答"这一轮花了 40 秒"，回答不了"这 40 秒里，是哪一步拖慢的"。
Trace 是带父子关系的因果链 —— 排查"为什么这次慢"只能靠它。

**三条硬规则**：
1. **没配 Key 就整体 no-op**。观测是可选增强，不是主链路依赖。
2. **上报失败绝不抛到业务层**。为了"看一眼 trace"把对话搞挂，是本末倒置。
3. **不伪造数据**。模型网关没返回 usage 就是没有，`usage_details` 留空，
   绝不用估算值填进去 —— 假的成本数字比没有更危险。

已验证的 SDK API（langfuse 4.15.6，本机实际 import 检查过）：
    Langfuse(public_key=, secret_key=, host=, sample_rate=, tracing_enabled=, environment=)
    client.start_observation(name=, as_type="span"|"generation", input=, metadata=,
                             model=, usage_details=)   -> span（根 span 即一条 trace）
    span.start_observation(...)                        -> 子 span（形成因果链）
    span.update(output=, metadata=, usage_details=) / span.end()
    client.flush()
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from typing import Any, Dict, Optional

from src.config import settings

_client = None
_client_tried = False
_last_error = ""

# ---------- 本地 trace 兜底 ----------
# 背景：Langfuse 三项 key 缺一项就是 no-op，而子智能体是**无状态**的（跑完即焚，
# 见 subagents/registry.py 注释），过程只能靠 trace 复盘 —— 没有 trace 等于无法排障。
# 所以没配 Langfuse 时不再"什么都不做"，而是落本地 JSONL：一行一轮，
# 含耗时/工具链/证据质量/降级原因。单机排障够用，也方便事后导入任何观测系统。
_local_lock = threading.RLock()
_local_last_error = ""


def _local_path() -> str:
    d = settings.trace_dir or "./.data/traces"
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"trace-{dt.date.today().isoformat()}.jsonl")


def _write_local(rec: Dict[str, Any]) -> None:
    global _local_last_error
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _local_lock:
            with open(_local_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:                  # noqa: BLE001 观测不能拖垮主链路
        _local_last_error = f"{type(e).__name__}: {e}"


def _clip(v: Any, n: int = 500) -> Any:
    # 结构化数据（工具链、证据列表）保持原样：str() 之后就没法事后聚合了，
    # 而这类字段本来就是短列表，不存在"太长"的问题。
    if isinstance(v, (list, tuple, dict)):
        return v
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"


class _LocalSpan:
    """本地 span：只记名字与耗时，够回答"哪一步慢"。"""

    def __init__(self, parent: "_LocalTrace", name: str):
        self._p, self._name, self._t0 = parent, name, time.perf_counter()

    def span(self, name: str = "", as_type: str = "span", **kw):
        return _LocalSpan(self._p, name)     # 层级压平：本地文件不建树，靠名字表达

    def start(self, name: str = "", as_type: str = "span", **kw):
        return _LocalSpan(self._p, name)

    def update(self, *a, **k):
        return self

    def end(self, output: Any = None, metadata: Optional[Dict] = None, **kw):
        self._p._spans.append({
            "name": self._name,
            "elapsed_ms": int((time.perf_counter() - self._t0) * 1000),
        })
        return self


class _LocalTrace:
    def __init__(self, name: str, user_id: str = "", session_id: str = "",
                 input: Any = None, metadata: Optional[Dict] = None):
        self._t0 = time.perf_counter()
        self._spans: list = []
        self._rec: Dict[str, Any] = {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "name": name, "user_id": user_id, "session_id": session_id,
            "input": _clip(input),
            "metadata": {k: _clip(v, 200) for k, v in (metadata or {}).items()},
        }

    def span(self, name: str = "", as_type: str = "span", **kw):
        return _LocalSpan(self, name)

    def start(self, name: str = "", as_type: str = "span", **kw):
        return _LocalSpan(self, name)

    def update(self, *a, **k):
        return self

    def end(self, output: Any = None, metadata: Optional[Dict] = None,
            usage_details: Optional[Dict] = None, **kw):
        self._rec["elapsed_ms"] = int((time.perf_counter() - self._t0) * 1000)
        self._rec["output"] = _clip(output, 1000)
        if metadata:
            self._rec["metadata"].update(
                {k: _clip(v, 200) for k, v in metadata.items()})
        if usage_details:
            self._rec["usage"] = usage_details
        if self._spans:
            self._rec["spans"] = self._spans
        _write_local(self._rec)
        return self


def local_enabled() -> bool:
    """Langfuse 可用时不双写（省一次文件 IO），只在 Langfuse 缺席时兜底。"""
    return bool(settings.trace_local) and not enabled()


# ---------------------------------------------------------------------------
# 惰性客户端
# ---------------------------------------------------------------------------
def _get():
    global _client, _client_tried, _last_error
    if not enabled():
        return None
    if _client is not None:
        return _client
    if _client_tried:
        return None
    _client_tried = True
    try:
        from langfuse import Langfuse
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            sample_rate=settings.langfuse_sample_rate,
            environment=settings.langfuse_environment or None,
            # 批量上报：攒够 N 条或每 N 秒发一次，避免每个请求都打一次网络
            flush_at=settings.langfuse_flush_at,
            flush_interval=settings.langfuse_flush_interval,
            timeout=settings.langfuse_timeout,
        )
        return _client
    except Exception as e:                  # noqa: BLE001  装不了/连不上都降级为 no-op
        _last_error = f"{type(e).__name__}: {e}"
        return None


def enabled() -> bool:
    return bool(settings.langfuse_enabled
                and settings.langfuse_public_key
                and settings.langfuse_secret_key)


def health() -> Dict[str, Any]:
    """如实上报：off（没配）/ down（配了但初始化失败）/ on。"""
    if not enabled():
        return {"langfuse": "off"}
    if _get() is None:
        return {"langfuse": "down", "last_error": _last_error}
    return {"langfuse": "on", "host": settings.langfuse_host,
            "sample_rate": settings.langfuse_sample_rate}


# ---------------------------------------------------------------------------
# no-op 句柄：结构与真实句柄完全一致，业务代码不需要写任何 if
# ---------------------------------------------------------------------------
class _NoOpSpan:
    def start(self, *a, **k):
        return _NoOpSpan()

    def end(self, *a, **k):
        return self

    def update(self, *a, **k):
        return self


class _NoOpTrace:
    def span(self, *a, **k):
        return _NoOpSpan()

    def end(self, *a, **k):
        return self


class _Span:
    def __init__(self, obj):
        self._obj = obj

    def start(self, name: str, as_type: str = "span", **kw):
        try:
            return _Span(self._obj.start_observation(name=name, as_type=as_type, **kw))
        except Exception:                   # noqa: BLE001
            return _NoOpSpan()

    def end(self, output: Any = None, metadata: Optional[Dict] = None,
            usage_details: Optional[Dict] = None, **kw):
        try:
            payload = {k: v for k, v in kw.items() if v is not None}
            if output is not None:
                payload["output"] = output
            if metadata:
                payload["metadata"] = metadata
            if usage_details:
                payload["usage_details"] = usage_details
            self._obj.update(**payload).end()
        except Exception:                   # noqa: BLE001
            pass
        return self


class _Trace(_Span):
    def span(self, name: str, as_type: str = "span", **kw):
        return self.start(name, as_type=as_type, **kw)


# ---------------------------------------------------------------------------
# 对外只有两个函数
# ---------------------------------------------------------------------------
def start_trace(name: str, *, user_id: str = "", session_id: str = "",
                input: Any = None, metadata: Optional[Dict] = None) -> Any:
    """
    开一条 trace。**失败一律返回 no-op 句柄**，调用方可以当它一定存在。
    """
    cli = _get()
    if cli is None:
        # 以前这里直接 no-op，观测等于没有。现在落本地 JSONL 兜底。
        if local_enabled():
            return _LocalTrace(name, user_id=user_id, session_id=session_id,
                               input=input, metadata=metadata)
        return _NoOpTrace()
    try:
        obj = cli.start_observation(
            name=name, as_type="span", input=input,
            metadata={**(metadata or {}), "user_id": user_id, "session_id": session_id},
        )
        return _Trace(obj)
    except Exception:                       # noqa: BLE001
        return _NoOpTrace()


def end_trace(tr: Any, output: Any = None, metadata: Optional[Dict] = None,
              usage_details: Optional[Dict] = None) -> None:
    """
    结束 trace 并触发一次 flush。

    flush 放在这里而不是后台线程：当前 QPS 是个位数，同步 flush 的开销（毫秒级）
    远比引入一个后台线程带来的生命周期问题划算。到高 QPS 时再改成后台批量。
    """
    if tr is None:
        return
    try:
        tr.end(output=output, metadata=metadata, usage_details=usage_details)
    except Exception:                       # noqa: BLE001
        return
    cli = _get()
    if cli is None:
        return
    try:
        cli.flush()
    except Exception:                       # noqa: BLE001
        pass


def usage_details_from(usage: Optional[Dict[str, int]]) -> Optional[Dict[str, int]]:
    """
    把 LangChain 的 usage_metadata 转成 Langfuse v4 的 usage_details。

    键名用 v4 的 input/output/total（OpenTelemetry gen_ai 语义约定）。
    **拿不到就返回 None** —— 宁可 trace 里这一格空着，也不用估算值填。
    """
    if not usage:
        return None
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (inp + out))
    if inp == 0 and out == 0:
        return None
    return {"input": inp, "output": out, "total": total}
