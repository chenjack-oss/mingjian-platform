"""
P3 实测脚本：真异步 / 缓存 / 限流

跑法（在服务器容器内）：
    sudo docker exec mingjian-platform-api python scripts/measure_p3.py

为什么要在容器内跑而不是从外面 curl：
    容器内 http://127.0.0.1:8000 直达，不经过 Nginx，测出来的是**应用自身的开销**，
    不会被网关与公网链路污染。而"异步首字节"这类数字，混进公网延迟就没意义了。

输出全部是 **实测值**，没有任何估算。跑不出来就是跑不出来（脚本会如实标 UNSURE）。
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

BASE = os.getenv("MJ_MEASURE_BASE", "http://127.0.0.1:8000")
ADMIN = os.getenv("MJ_ADMIN_TOKEN", "")

_out = []


def log(msg: str) -> None:
    print(msg, flush=True)
    _out.append(msg)


def ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def main() -> int:
    if not ADMIN:
        log("❌ 缺少 MJ_ADMIN_TOKEN，无法创建测试用户")
        return 1

    # ---------- 0. 健康快照 ----------
    h = requests.get(f"{BASE}/health", timeout=10).json()
    log(f"\n=== P3 实测 | version={h['version']} phase={h['phase']} ===")
    log(f"redis={h.get('redis')} cache={h.get('cache')} rate_limit={h.get('rate_limit')} "
        f"async_mode={h.get('async_mode')} broker={h.get('celery_broker')} "
        f"checkpointer={h.get('checkpointer')} llm_ready={h.get('llm_ready')}")

    # ---------- 1. 造测试用户 ----------
    # 管理接口用的是 X-Admin-Token 头，不是 Bearer（见 src/identity/auth.py:66）
    r = requests.post(f"{BASE}/v1/users", json={"name": "measure-p3"},
                      headers={"X-Admin-Token": ADMIN}, timeout=10)
    if "api_key" not in r.json():
        log(f"❌ 创建用户失败：HTTP {r.status_code} {r.text[:200]}")
        return 1
    KEY = r.json()["api_key"]
    H = {"X-API-Key": KEY}
    sess = requests.post(f"{BASE}/v1/sessions", headers=H,
                         json={"title": "P3 实测"}).json()["session"]
    sid = sess["id"]
    log(f"测试会话 {sid}")

    # ---------- 2. 真异步：提交延迟 vs 完成耗时 ----------
    log("\n-- 2. 异步长任务（research 档）--")
    t0 = time.time()
    r = requests.post(f"{BASE}/v1/tasks", headers=H, timeout=30,
                      json={"session_id": sid,
                            "content": "调研 2026 年大模型应用开发工程师需要掌握的技术栈",
                            "tier": "research"})
    submit_ms = ms(t0)
    d = r.json()
    tid = d.get("task", {}).get("id", "")
    log(f"   提交返回：HTTP {r.status_code}，用时 {submit_ms}ms，"
        f"status={d.get('task', {}).get('status')}，task_id={tid}")

    # 轮询到终态
    t1 = time.time()
    final_status, poll = "", 0
    for _ in range(300):                       # 最多等 300×2s = 600s
        time.sleep(2)
        poll += 1
        g = requests.get(f"{BASE}/v1/tasks/{tid}", headers=H, timeout=10).json()
        final_status = g["task"]["status"]
        if final_status in ("success", "failure", "revoked", "stalled"):
            break
    total_ms = ms(t1)
    log(f"   终态：{final_status}，从提交到完成 {total_ms}ms（轮询 {poll} 次）")
    if final_status == "success":
        res = requests.get(f"{BASE}/v1/tasks/{tid}", headers=H, timeout=10).json()["task"]
        content = (res.get("result", {}) or {}).get("content", "")
        log(f"   报告长度 {len(content)} 字符；"
            f"artifacts={len((res.get('result', {}) or {}).get('artifacts', []))}")
    else:
        log(f"   ⚠️ 未成功：{d}")

    # ---------- 3. 幂等 ----------
    log("\n-- 3. 幂等 --")
    r2 = requests.post(f"{BASE}/v1/tasks", headers=H, timeout=30,
                       json={"session_id": sid,
                             "content": "调研 2026 年大模型应用开发工程师需要掌握的技术栈",
                             "tier": "research"})
    log(f"   重复提交 reused={r2.json().get('reused')}（True = 没派第二个任务）")

    # ---------- 4. 缓存 ----------
    log("\n-- 4. 语义缓存（direct 档）--")
    q = "用一句话解释什么是 RRF 融合排序"
    t = time.time()
    a1 = requests.post(f"{BASE}/v1/sessions/{sid}/messages", headers=H, timeout=120,
                       json={"content": q, "tier": "direct"}).json()
    cold_ms = ms(t)
    t = time.time()
    a2 = requests.post(f"{BASE}/v1/sessions/{sid}/messages", headers=H, timeout=120,
                       json={"content": q, "tier": "direct"}).json()
    warm_ms = ms(t)
    log(f"   第一次（冷）：{cold_ms}ms  cached={a1.get('meta', {}).get('cached', False)}")
    log(f"   第二次（热）：{warm_ms}ms  cached={a2.get('meta', {}).get('cached', False)}")
    hits = requests.get(f"{BASE}/v1/me", headers=H, timeout=10).json()
    n_hit = hits.get("usage", {}).get("cache_hit", 0)
    log(f"   用量里的 cache_hit 计数：{n_hit}")
    if n_hit >= 1:
        log(f"   ✅ 命中生效，加速比 {cold_ms / max(warm_ms, 1):.1f}×（{cold_ms}ms → {warm_ms}ms）")
    else:
        # 没命中时**不许**拿两次耗时算"加速比"报出去 —— 那只是模型波动，
        # 当成缓存效果讲就是造假。这里如实标注未命中。
        log(f"   ⚠️ 未命中（两次耗时 {cold_ms}ms / {warm_ms}ms 的差异与缓存无关，不作为结论）")

    # ---------- 5. 限流 ----------
    log("\n-- 5. 令牌桶限流（连打 30 次）--")
    codes = {}
    t = time.time()
    for i in range(30):
        rr = requests.post(f"{BASE}/v1/tasks", headers=H, timeout=20,
                           json={"session_id": sid, "content": f"限流压测 {i}",
                                 "tier": "direct"})
        codes[rr.status_code] = codes.get(rr.status_code, 0) + 1
    burst_ms = ms(t)
    log(f"   30 次请求用时 {burst_ms}ms，状态码分布 {codes}")
    log(f"   429 次数 = {codes.get(429, 0)}（桶容量 20 + 补充 2/s，理论应拦截约 10 次）")
    rem = requests.get(f"{BASE}/v1/me", headers=H, timeout=10).json().get("rate_remaining")
    log(f"   当前剩余令牌：{rem}")

    log("\n=== 实测结束（以上全部为真实测量值）===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
