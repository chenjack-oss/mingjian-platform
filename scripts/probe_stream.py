"""
流式接口联调：验证 SSE 真的**边跑边推**，而不是等跑完一次性返回。

跑法（必须先起真实服务，**不能用 TestClient**）：
    python -m uvicorn src.api.server:app --port 8011
    python scripts/probe_stream.py

🔴 为什么不能用 TestClient 测流式：
    httpx 的 ASGITransport 会把响应体**攒完整**再交给调用方，
    用它测出来的"首字延迟"永远等于总耗时，会把真流式误判成假流式（实测踩到）。
    验证流式必须走真实 socket。

判据（两个都成立才算真流式）：
    1. 首字延迟 明显小于 总耗时
    2. token 事件数 > 1
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()   # 管理员令牌在 .env 里，脚本自己也要读一份才能建用户

BASE = os.getenv("MJ_BASE", "http://127.0.0.1:8011")
ADMIN = os.getenv("MJ_ADMIN_TOKEN", "")


def mk_user(c: httpx.Client, name: str) -> str:
    r = c.post(BASE + "/v1/users", headers={"X-Admin-Token": ADMIN},
               json={"name": name}, timeout=20)
    return r.json()["api_key"]


def once(c: httpx.Client, key: str, sid: str, content: str, tier: str):
    t0 = time.time()
    first = None
    n_tok = 0
    tools = []
    done = None
    stamps = []
    with c.stream("POST", f"{BASE}/v1/sessions/{sid}/messages/stream",
                  headers={"X-API-Key": key},
                  json={"content": content, "tier": tier}, timeout=300) as r:
        print(f"  HTTP {r.status_code} | ct={r.headers.get('content-type')} "
              f"| x-accel-buffering={r.headers.get('x-accel-buffering')}")
        ev = None
        for line in r.iter_lines():
            line = line.rstrip("\r")
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: ") and ev:
                d = json.loads(line[6:])
                if ev == "token":
                    n_tok += 1
                    stamps.append(round(time.time() - t0, 2))
                    if first is None:
                        first = time.time() - t0
                elif ev == "tool":
                    tools.append(d.get("name"))
                elif ev == "done":
                    done = d
                ev = None
    total = time.time() - t0
    return first, total, n_tok, tools, done, stamps


def main() -> int:
    # trust_env=False：机器上若配了 HTTP_PROXY，httpx 会把 127.0.0.1 的请求也发给代理，
    # 结果拿到 502 upstream connect failed（实测踩到），看起来像"服务没起来"
    with httpx.Client(trust_env=False) as c:
        key = mk_user(c, "stream-probe")
        sid = c.post(BASE + "/v1/sessions", headers={"X-API-Key": key},
                     json={"title": "流式测试"}, timeout=20).json()["session"]["id"]

        print("=== direct 档（零工具，纯模型）===")
        f, t, n, tools, done, stamps = once(c, key, sid, "用三句话解释什么是 RAG", "direct")
        print(f"  首字 {f:.2f}s / 总耗时 {t:.2f}s / token {n} 个 / 工具 {tools}")
        print(f"  前 8 个 token 到达时刻: {stamps[:8]}")
        print(f"  判定：{'真流式 ✅' if (n > 1 and f is not None and f < t * 0.9) else '不是流式 ❌'}")

        print("\n=== auto 档（可能联网检索）===")
        f, t, n, tools, done, stamps = once(c, key, sid, "你好，介绍一下你自己", "auto")
        print(f"  首字 {f:.2f}s / 总耗时 {t:.2f}s / token {n} 个 / 工具 {tools}")
        print(f"  判定：{'真流式 ✅' if (n > 1 and f is not None and f < t * 0.9) else '不是流式 ❌'}")

        print("\n=== rag 档（调 kb_search，慢）===")
        f, t, n, tools, done, stamps = once(c, key, sid,
                                            "路由器 Web 登录的默认用户名和密码是什么？", "rag")
        print(f"  首字 {f:.2f}s / 总耗时 {t:.2f}s / token {n} 个 / 工具 {tools}")
        if done:
            print(f"  答案前 100 字：{done.get('content', '')[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
