"""P3：可观测（Langfuse）。未配置 Key 时全部为 no-op，绝不阻断主链路。"""

from src.observe.tracing import end_trace, enabled, start_trace

__all__ = ["enabled", "start_trace", "end_trace"]
