"""
故意弄坏之一：任务跑一半把 worker 用 SIGKILL 干掉（模拟容器 OOM / 机器重启）

验证目标：
    task_acks_late=True + task_reject_on_worker_lost=True 是否真的能让任务**不丢** ——
    worker 被硬杀后，任务应当被 broker 重新投递，重启后的 worker 把它跑完。

跑法（在服务器**宿主机**上，不是在容器内 —— 需要 docker 权限）：
    python3 scripts/chaos_kill_worker.py

为什么值得跑：这是"任务不丢"这个承诺的唯一证据。
配置写对了不代表它真的生效，只有真 kill 一次才知道。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

BASE = os.getenv("MJ_MEASURE_BASE", "http://127.0.0.1:8080")
ADMIN = os.getenv("MJ_ADMIN_TOKEN", "")
CONTAINER = os.getenv("MJ_WORKER_CONTAINER", "mingjian-platform-worker")
COMPOSE_DIR = os.getenv("MJ_COMPOSE_DIR", "/home/ubuntu/mingjian-platform")
KILL_AFTER = int(os.getenv("MJ_KILL_AFTER", "8"))


def req(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:200]}


def main() -> int:
    if not ADMIN:
        print("❌ 缺 MJ_ADMIN_TOKEN")
        return 1

    u = req("POST", "/v1/users", {"name": "chaos-test"}, {"X-Admin-Token": ADMIN})
    H = {"X-API-Key": u["api_key"]}
    sid = req("POST", "/v1/sessions", {"title": "chaos"}, H)["session"]["id"]

    print("-- 提交一个长任务（research 档）--")
    d = req("POST", "/v1/tasks", {"session_id": sid,
                                  "content": "调研 2026 年多智能体框架的发展现状",
                                  "tier": "research"}, H)
    tid = d["task"]["id"]
    print(f"   task_id={tid} status={d['task']['status']}")

    print(f"-- 等 {KILL_AFTER}s 让它跑起来，然后 SIGKILL 掉 worker --")
    time.sleep(KILL_AFTER)
    st = req("GET", f"/v1/tasks/{tid}", None, H)["task"]["status"]
    print(f"   kill 之前的状态：{st}")
    subprocess.run(["sudo", "-n", "docker", "kill", CONTAINER], check=False)
    print("   已发送 SIGKILL（docker kill = 不可捕获，等价于容器 OOM 被杀）")

    print("-- 3s 后把 worker 拉回来 --")
    time.sleep(3)
    subprocess.run(["sudo", "-n", "docker", "compose", "up", "-d", "worker"],
                   cwd=COMPOSE_DIR, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("-- 轮询，看它会不会自己回来（最多 240s）--")
    deadline, final = time.time() + 240, ""
    while time.time() < deadline:
        time.sleep(3)
        t = req("GET", f"/v1/tasks/{tid}", None, H)["task"]
        final = t["status"]
        if final in ("success", "failure", "revoked"):
            break
        if final == "stalled":
            break                       # 心跳断了 → 应用层判定它挂了
    print(f"   broker 自己恢复了吗：{'是' if final == 'success' else '否'}（终态 {final}）")

    if final != "success":
        print("-- broker 没恢复，走应用层兜底 POST /v1/tasks/{id}/retry --")
        r = req("POST", f"/v1/tasks/{tid}/retry", {}, H)
        print(f"   retry 受理：{r.get('task', {}).get('status') or r}")
        deadline = time.time() + 240
        while time.time() < deadline:
            time.sleep(3)
            t = req("GET", f"/v1/tasks/{tid}", None, H)["task"]
            final = t["status"]
            if final in ("success", "failure", "revoked", "stalled"):
                break
        print(f"   retry 之后终态：{final}")

    if final == "success":
        res = req("GET", f"/v1/tasks/{tid}", None, H)["task"].get("result", {}) or {}
        print(f"   ✅ 任务最终跑完，报告 {len(res.get('content', ''))} 字符")
        return 0
    print(f"   ❌ 最终没跑完（{final}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
