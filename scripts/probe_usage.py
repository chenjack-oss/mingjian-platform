"""
探针：看模型网关到底有没有返回 token usage

跑法（容器内）：
    docker exec mingjian-platform-api python scripts/probe_usage.py

为什么要单独探一次：
    Langfuse 的成本数字好不好用，完全取决于上游给不给 usage。
    不给就是不给，不能用估算值填 —— 所以必须**实测确认**，
    而不是"理论上 langchain 会带上"就写进文档。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("MJ_MEASURE_BASE", "http://127.0.0.1:8000")
ADMIN = os.getenv("MJ_ADMIN_TOKEN", "")


def req(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"_body": e.read().decode()[:300]}


def main() -> int:
    if not ADMIN:
        print("❌ 缺 MJ_ADMIN_TOKEN")
        return 1
    st, u = req("POST", "/v1/users", {"name": "probe-usage"}, {"X-Admin-Token": ADMIN})
    if st != 200:
        print("❌ 建用户失败", st, u)
        return 1
    H = {"X-API-Key": u["api_key"]}
    st, s = req("POST", "/v1/sessions", {"title": "probe"}, H)
    sid = s["session"]["id"]

    print("-- health --")
    st, h = req("GET", "/health")
    print(f"   version={h.get('version')} langfuse={h.get('langfuse')} "
          f"redis={h.get('redis')} async_mode={h.get('async_mode')}")

    # 每次带一个时间戳：否则第二次跑会命中语义缓存，测到的就不是真实调用了
    stamp = str(int(time.time()))
    for tier in ("direct", "auto"):
        print(f"\n-- tier={tier} --")
        t0 = time.time()
        st, d = req("POST", f"/v1/sessions/{sid}/messages",
                    {"content": f"用一句话说明 Langfuse 是做什么的（探针 {stamp}）",
                     "tier": tier}, H)
        ms = int((time.time() - t0) * 1000)
        meta = d.get("meta", {}) if st == 200 else {}
        print(f"   HTTP {st}  用时 {ms}ms  cached={meta.get('cached', False)} "
              f"tools={meta.get('tools_used')}")
        print(f"   usage = {meta.get('usage') or '❌ 上游未返回'}")

    print("\n-- 结论 --")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
