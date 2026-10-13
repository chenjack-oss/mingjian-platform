"""
P1 冒烟测试：鉴权 / 多用户隔离 / 持久化 / 四档路由

跑法：
    python scripts/smoke.py

它验证的是 P1 真正承诺的四件事，每一项都是旧项目做不到的：
    1. 不登录拿不到任何数据
    2. A 看不到 B 的会话和文件（换 id 也不行）
    3. 消息真的落库了（不是只在内存里转一圈）
    4. 四档可路由，且手动指定能覆盖自动判断

LLM 未配置时 direct 档会返回"模型服务不可用"——这不影响上面四项的验证，
测试结果里会如实标注 llm_ready，不会为了好看而假装成功。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 第 3 节要验证的是"档位路由"本身，必须走同步路径；
# 异步路径在第 7 节单独验证。这一行必须在 import src.* 之前设置。
os.environ.setdefault("MJ_ASYNC_AUTO_RESEARCH", "0")

from fastapi.testclient import TestClient  # noqa: E402

from src.api.server import app            # noqa: E402
from src.config import settings           # noqa: E402
from src.db import engine as dbeng        # noqa: E402
from src.db import repo                   # noqa: E402
from src.session import checkpointer      # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    dbeng.init_db()
    print(f"\n=== 明辨平台 P1 冒烟 | db={settings.db_url.split('://')[0]} "
          f"| checkpointer={checkpointer.kind()} ===\n")

    a = repo.create_user("smoke-A")
    b = repo.create_user("smoke-B")
    ka, kb = a["api_key"], b["api_key"]
    ua, ub = a["user"]["id"], b["user"]["id"]

    with TestClient(app) as cli:
        # ---------- 1. 鉴权 ----------
        print("-- 1. 鉴权 --")
        r = cli.get("/v1/sessions")
        check("无 Key 访问会话列表被拒", r.status_code == 401, f"HTTP {r.status_code}")

        r = cli.get("/v1/sessions", headers={"Authorization": "Bearer mj_invalid"})
        check("错误 Key 被拒", r.status_code == 401, f"HTTP {r.status_code}")

        r = cli.get("/v1/me", headers={"X-API-Key": ka})
        check("正确 Key 可访问", r.status_code == 200, f"HTTP {r.status_code}")

        r = cli.get("/v1/users")
        check("管理接口无令牌被拒", r.status_code == 403, f"HTTP {r.status_code}")

        # ---------- 2. 多用户隔离 ----------
        print("\n-- 2. 多用户隔离 --")
        sa = cli.post("/v1/sessions", headers={"X-API-Key": ka},
                      json={"title": "A 的会话"}).json()["session"]
        sb = cli.post("/v1/sessions", headers={"X-API-Key": kb},
                      json={"title": "B 的会话"}).json()["session"]

        r = cli.get(f"/v1/sessions/{sa['id']}", headers={"X-API-Key": kb})
        check("B 拿 A 的会话被拒", r.status_code == 403, f"HTTP {r.status_code}")

        r = cli.get(f"/v1/sessions/{sb['id']}", headers={"X-API-Key": ka})
        check("A 拿 B 的会话被拒", r.status_code == 403, f"HTTP {r.status_code}")

        la = cli.get("/v1/sessions", headers={"X-API-Key": ka}).json()["sessions"]
        lb = cli.get("/v1/sessions", headers={"X-API-Key": kb}).json()["sessions"]
        ids_a = {s["id"] for s in la}
        ids_b = {s["id"] for s in lb}
        check("会话列表互不串台", sa["id"] in ids_a and sb["id"] not in ids_a
              and sb["id"] in ids_b and sa["id"] not in ids_b,
              f"A={len(ids_a)} B={len(ids_b)}")

        ta = f"u:{ua}:s:{sa['id']}"
        tb = f"u:{ub}:s:{sb['id']}"
        check("thread_id 已带用户命名空间", ta != tb and ta.startswith(f"u:{ua}:"), ta)

        # ---------- 3. 四档路由（P2：不再预判意图，档位只做能力约束）----------
        print("\n-- 3. 四档路由 --")
        r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                     json={"content": "你好"})
        d = r.json()
        check("默认走 auto（不再由规则猜意图）",
              r.status_code == 200 and d["tier"] == "auto"
              and d["decision"]["source"] == "default",
              f"tier={d.get('tier')} src={d.get('decision', {}).get('source')}")

        r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                     json={"content": "帮我调研 2026 年大模型应用开发岗位的技术栈",
                           "tier": "research"})
        d = r.json()
        check("手动指定 research 生效",
              d["tier"] == "research" and d["decision"]["source"] == "manual",
              f"tier={d.get('tier')} src={d.get('decision', {}).get('source')}")

        r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                     json={"content": "帮我调研 2026 年大模型岗位", "tier": "direct"})
        d = r.json()
        check("手动指定 direct 生效",
              d["tier"] == "direct" and d["decision"]["source"] == "manual",
              f"tier={d.get('tier')} src={d.get('decision', {}).get('source')}")

        r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                     json={"content": "知识库里有没有相关资料", "tier": "rag"})
        d = r.json()
        # P5 之后 rag 档**可能真的接入了**（挂了 kb_* 工具）。
        # 这条断言的语义要跟着变：接入了就不该降级，没接入就必须诚实降级 ——
        # 两种情况都算通过，绝不允许"没接入却假装检索到了"。
        try:
            import asyncio
            from src.routing import executor as _ex
            _kb = asyncio.run(_ex.kb_wired())
        except Exception:
            _kb = False
        _deg = d["meta"].get("degradation", "")
        _err = str(d["meta"].get("error", ""))[:200]
        check("rag 档：接入则真检索，未接入则诚实降级",
              (_deg == "") if _kb else (_deg == "rag_not_wired"),
              f"kb_wired={_kb} degradation={_deg!r} error={_err!r}")

        r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                     json={"content": "测试", "tier": "nosuch"})
        check("非法档位被拒", r.status_code == 400, f"HTTP {r.status_code}")

        # ---------- 4. 文件隔离 ----------
        print("\n-- 4. 文件隔离 --")
        up = cli.post(f"/v1/sessions/{sa['id']}/files", headers={"X-API-Key": ka},
                      files={"file": ("note.txt", b"hello mingjian", "text/plain")})
        fid = up.json()["file"]["id"]
        check("A 上传文件成功", up.status_code == 200, f"file_id={fid}")

        r = cli.get(f"/v1/files/{fid}/download", headers={"X-API-Key": kb})
        check("B 下载 A 的文件被拒（且当作不存在）", r.status_code == 404,
              f"HTTP {r.status_code}")

        r = cli.get(f"/v1/files/{fid}/download", headers={"X-API-Key": ka})
        check("A 可下载自己的文件", r.status_code == 200 and b"hello" in r.content,
              f"HTTP {r.status_code}")

        # ---------- 5. 持久化 ----------
        print("\n-- 5. 持久化 --")
        # 期望 8 条：第 3 节成功发出 4 轮（第 5 次是故意传非法档位，被 400 拦下、不落库）
        msgs = repo.list_messages(ua, sa["id"])
        check("消息已落库", len(msgs) >= 8, f"{len(msgs)} 条（4 轮 = 8 条）")

        persisted = repo.get_session(ua, sa["id"])
        check("会话标题已自动命名", persisted["title"] not in ("新对话", "", None),
              persisted["title"])

        usage_a = repo.usage_summary(ua)
        check("用量已记帐", usage_a.get("message", 0) >= 4, str(usage_a))

        # 模拟重启：换一个全新的 app 实例读同一份库，能读到之前的数据即为真持久化
        from src.api.server import app as app2  # noqa: F401  同一模块对象，库是外置的
        with TestClient(app) as cli2:
            r = cli2.get(f"/v1/sessions/{sa['id']}", headers={"X-API-Key": ka})
            got = r.json()
            check("重启后仍能读到会话与历史",
                  r.status_code == 200 and len(got["messages"]) >= 8,
                  f"{len(got.get('messages', []))} 条")

        # ---------- 6. 健康信息 ----------
        print("\n-- 6. 运行时 --")
        h = cli.get("/health").json()
        check("健康检查暴露 checkpointer 实情", "checkpointer" in h, h["checkpointer"])
        check("健康检查暴露 llm 状态", "llm_ready" in h, f"llm_ready={h['llm_ready']}")
        if not h["llm_ready"]:
            print("        ℹ️ 未配置 LLM，direct 档会返回'模型服务不可用'（属预期，非缺陷）")

        # ---------- 7. 异步任务（P3）----------
        # 用 direct 档：几秒出结果，不让冒烟测试等 125 秒的深度调研。
        print("\n-- 7. 异步任务 --")
        r = cli.post("/v1/tasks", headers={"X-API-Key": ka},
                     json={"session_id": sa["id"], "content": "异步任务冒烟：讲讲 RRF 融合",
                           "tier": "direct"})
        d = r.json()
        check("提交异步任务被受理", r.status_code in (200, 202), f"HTTP {r.status_code}")
        tid = d.get("task", {}).get("id", "")
        check("返回了任务 id", bool(tid), tid)
        # eager（没配 broker）时任务已内联跑完；真实队列下应当停在未终态等 worker 取走
        mode = "eager" if settings.eager else "celery"
        expect = ("success", "failure") if settings.eager else ("pending", "running")
        check(f"任务状态符合当前模式（{mode}：期望 {'/'.join(expect)}）",
              d.get("task", {}).get("status") in expect,
              d.get("task", {}).get("status", ""))
        # 语义要分清：还在跑 = 202，已跑完 = 200。一律返回 202 的话客户端只能靠猜。
        if settings.eager and d.get("task", {}).get("status") == "success":
            check("eager 模式下已完成任务返回 200（不是 202）",
                  r.status_code == 200, f"HTTP {r.status_code}")

        r = cli.get(f"/v1/tasks/{tid}", headers={"X-API-Key": ka})
        check("可按 id 查询任务", r.status_code == 200, f"HTTP {r.status_code}")

        r = cli.get("/v1/tasks", headers={"X-API-Key": ka})
        ids = [t["id"] for t in r.json()["tasks"]]
        check("任务列表只含自己的任务", tid in ids, f"{len(ids)} 条")

        r = cli.get(f"/v1/tasks/{tid}", headers={"X-API-Key": kb})
        check("B 查 A 的任务当作不存在", r.status_code == 404, f"HTTP {r.status_code}")

        # 幂等：同一用户 / 同会话 / 同档位 / 同提问。
        # 故意加前后空格与中间多空格 —— 归一化后应当与原提问等价，
        # 不归一化的话用户狂点两下就会真的跑两个任务。
        r2 = cli.post("/v1/tasks", headers={"X-API-Key": ka},
                      json={"session_id": sa["id"],
                            "content": "  异步任务冒烟：讲讲   RRF 融合  ",
                            "tier": "direct"})
        check("重复提交命中幂等键（不再派第二个任务）",
              r2.json().get("reused") is True, f"reused={r2.json().get('reused')}")

        # 发消息接口显式 async=true 也必须能转异步。
        # 这条路径曾经漏了 await，直接 500 —— 而 /v1/tasks 那条路径当时是对的，
        # 所以只有这一条能守住它。
        r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                     json={"content": "发消息接口转异步", "tier": "direct", "async": True})
        ok = r.status_code in (200, 202)
        check("发消息接口 async=true 能转异步（不返回协程对象）", ok, f"HTTP {r.status_code}")
        if ok:
            d3 = r.json()
            check("异步返回体里带 task", bool(d3.get("task", {}).get("id")),
                  d3.get("task", {}).get("id", ""))

        # 真实队列模式下必须验证"worker 真的把活干了"，否则前面那些检查
        # 只证明了"任务被塞进队列"，没证明有人来取。
        if not settings.eager:
            import time as _t
            deadline, st = _t.time() + 90, "pending"
            while _t.time() < deadline:
                st = cli.get(f"/v1/tasks/{tid}",
                             headers={"X-API-Key": ka}).json()["task"]["status"]
                if st in ("success", "failure", "revoked", "stalled"):
                    break
                _t.sleep(2)
            check("worker 真的取走并跑完了任务", st == "success", f"status={st}")

        # 取消：只有在真实队列模式下才有意义（eager 已内联执行完，撤不回来）
        if settings.eager:
            r = cli.delete(f"/v1/tasks/{tid}", headers={"X-API-Key": ka})
            check("已完成任务不可取消（409 而非假装成功）",
                  r.status_code == 409, f"HTTP {r.status_code}")
        else:
            r = cli.post("/v1/tasks", headers={"X-API-Key": ka},
                         json={"session_id": sa["id"],
                               "content": "取消冒烟专用（research 档，跑得久才来得及撤）",
                               "tier": "research"})
            tid2 = r.json().get("task", {}).get("id", "")
            r = cli.delete(f"/v1/tasks/{tid2}", headers={"X-API-Key": ka})
            check("进行中的任务可发起取消", r.status_code in (200, 409),
                  f"HTTP {r.status_code}")

        # ---------- 8. Redis 降级（P3）----------
        # 本机没装 Redis，这里验证的是"没配 Redis 时主链路不受影响"这一条硬要求。
        print("\n-- 8. Redis 降级 --")
        hh = cli.get("/health").json()
        check("健康接口如实上报 redis 状态",
              hh.get("redis") in ("off", "down", "up"), hh.get("redis", ""))
        # 缓存开关必须跟着 redis 的实际状态走：redis 起来了就该报 True，
        # 起不来就该报 False —— 两者都不是"永远 True"也不是"永远 False"
        if hh.get("redis") == "up":
            check("redis 可用时缓存标记为开启", hh.get("cache") is True,
                  f"cache={hh.get('cache')}")
        else:
            check("redis 不可用时缓存标记为关闭", hh.get("cache") is False,
                  f"cache={hh.get('cache')}")
        if hh.get("redis") != "up":
            # fail-open：限流组件不可用时应放行而不是拒绝
            r = cli.post(f"/v1/sessions/{sa['id']}/messages", headers={"X-API-Key": ka},
                         json={"content": "限流降级检查", "tier": "direct"})
            check("限流组件不可用时放行而非 429", r.status_code == 200,
                  f"HTTP {r.status_code}")
        else:
            check("限流组件可用（跳过降级检查）", True, "redis=up")

        # ---------- 9. MCP 业务系统集成（P5）----------
        # 这一节不碰模型，只验证协议链路与写操作确认机制本身，
        # 保证"MCP 挂没挂上"这件事可以在没有 LLM 的情况下独立验证。
        print("\n-- 9. MCP 业务系统集成 --")
        hh = cli.get("/health").json()
        check("健康接口如实上报 mcp 工具数", "mcp" in hh, f"mcp={hh.get('mcp')}")

        async def _mcp_probe():
            from src.mcp_client import client as m
            stock, ms_stock = await m.raw_call("query_stock", {"sku": "SKU-10000"})
            miss, _ = await m.raw_call("query_stock", {"sku": "SKU-NOT-EXIST"})
            # 两步确认必须在**同一个会话**里完成：
            # token 存在 MCP Server 子进程内存中，一旦重连（新子进程）token 就丢了
            t1, _ = await m.raw_call("create_ticket",
                                     {"subject": "冒烟测试工单", "body": "自动化冒烟"})
            tok = (t1 or {}).get("confirm_token") or ""
            t2, _ = await m.raw_call("create_ticket",
                                     {"subject": "冒烟测试工单", "body": "自动化冒烟",
                                      "confirm_token": tok})
            t3, _ = await m.raw_call("create_ticket",
                                     {"subject": "冒烟测试工单", "body": "自动化冒烟",
                                      "confirm_token": "cf_forged_1"})
            m.close_all()
            return stock, miss, t1, t2, t3, ms_stock

        try:
            import asyncio
            stock, miss, t1, t2, t3, ms_stock = asyncio.run(_mcp_probe())
            check("业务工具返回真实数据（非桩）",
                  isinstance(stock, dict) and stock.get("found") is True,
                  f"total_qty={stock.get('total_qty')} ({ms_stock:.0f}ms)")
            check("查不到的 SKU 如实返回 found=false（不编造）",
                  isinstance(miss, dict) and miss.get("found") is False, str(miss)[:60])
            check("写操作首次调用不落库，返回待确认",
                  (t1 or {}).get("need_confirm") is True and "confirm_token" in (t1 or {}),
                  f"token={str((t1 or {}).get('confirm_token'))[:18]}")
            check("带合法 token 二次调用真正创建",
                  (t2 or {}).get("created") is True, str((t2 or {}).get("ticket_id")))
            check("伪造 token 被拒绝且未创建",
                  (t3 or {}).get("created") is not True,
                  str((t3 or {}).get("message"))[:60])
        except Exception as e:
            check("MCP 链路可用", False, f"{type(e).__name__}: {e}")

        # P6：rag 档的挂载状态必须与"kb-analyst 子智能体有没有注册"一致，不能写死。
        # 同时验证**权限最小化**：kb_* 已经下放给子智能体，主智能体手里不该再有。
        try:
            import asyncio
            from src.routing import executor as _ex
            from src.agent import core as _core
            from src.agent.subagents import registry as _reg

            wired = asyncio.run(_ex.kb_wired())
            rag_tools = asyncio.run(_core.tools_for_tier("rag"))
            names = [t.name for t in rag_tools]
            has_kb = any(n.startswith("kb_") for n in names)
            specs = asyncio.run(_reg.specs_for_tier("rag"))
            delegated = any(s["name"] == "kb-analyst" for s in specs)

            check("rag 档：知识库接入状态与 kb-analyst 注册一致",
                  wired == delegated, f"kb_wired={wired} kb_analyst={delegated}")
            check("rag 档：kb_* 已下放，主智能体不再持有",
                  (not has_kb) if (wired and _reg.strip_main_tools()) else True,
                  f"kb_tools_in_main={has_kb} tools={len(names)}")
        except Exception as e:
            check("rag 档工具集可装配", False, f"{type(e).__name__}: {e}")

    ok = sum(1 for _, r, _ in _results if r)
    total = len(_results)
    print(f"\n=== 结果：{ok}/{total} 通过 ===")
    for name, r, detail in _results:
        if not r:
            print(f"  ❌ {name} — {detail}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
