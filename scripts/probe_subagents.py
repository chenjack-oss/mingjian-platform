# -*- coding: utf-8 -*-
"""P6 子智能体装配探针。

分两段：
  A 静态装配（不调模型，快）：子智能体规格、主智能体工具剥离、general-purpose 是否关掉
  B 端到端（要真调模型 + 真检索，慢）：观察主智能体是不是真的通过 task 工具派发

用法：
    python scripts/probe_subagents.py            # 只跑 A
    python scripts/probe_subagents.py --e2e      # A + B
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "✅", "❌"
_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f"  {detail}" if detail else ""))


async def part_a():
    print("\n=== A. 静态装配 ===")
    from src.agent.subagents import registry, kb_analyst, contracts
    from src.agent import core

    check("子智能体总开关打开", registry.enabled())

    specs = await registry.specs_for_tier("rag")
    names = [s["name"] for s in specs]
    check("rag 档注册了 kb-analyst", kb_analyst.NAME in names, f"specs={names}")

    kb = next((s for s in specs if s["name"] == kb_analyst.NAME), None)
    if kb:
        tools = [getattr(t, "name", "") for t in kb.get("tools", [])]
        check("kb-analyst 只持有 kb_* 工具",
              tools and all(t.startswith("kb_") for t in tools), f"tools={tools}")

    check("direct 档不派发", registry.planned_for("direct") == [])
    check("tool 档不派发", registry.planned_for("tool") == [])
    check("research 档派发 kb-analyst",
          kb_analyst.NAME in registry.planned_for("research"))

    gp = next((s for s in specs if s["name"] == "general-purpose"), None)
    check("默认 general-purpose 已被占位规格顶掉",
          gp is not None and list(gp.get("tools", [])) == [],
          f"占位 tools={list(gp.get('tools', [])) if gp else 'N/A'}")

    mcp_rag = await core.mcp_tools()
    kb_all = [getattr(t, "name", "") for t in mcp_rag if getattr(t, "name", "").startswith("kb_")]
    check("kb_* MCP 工具确实存在（前提）", len(kb_all) > 0, f"{kb_all}")

    main_tools = await core.tools_for_tier("rag")
    main_names = [getattr(t, "name", "") for t in main_tools]
    check("主智能体手里已经没有 kb_* 工具",
          not any(n.startswith("kb_") for n in main_names),
          f"main_tools={main_names}")

    # 契约校验：四种退化输入都不能抛异常，且要正确降级
    r1 = contracts.coerce(None)
    r2 = contracts.coerce({"error": True, "kind": "timeout", "message": "超时"})
    r3 = contracts.coerce('```json\n{"ok": true, "answer": "A", "evidence": [{"source": "s"}]}\n```')
    r4 = contracts.coerce("随便一段人话")
    check("契约：None → tool_error", r1.degraded == contracts.TOOL_ERROR)
    check("契约：MCP 超时 → timeout", r2.degraded == contracts.TIMEOUT)
    check("契约：围栏 JSON 能解析且 ok", r3.ok is True and len(r3.evidence) == 1)
    check("契约：纯文本 → tool_error 且保留原文", r4.degraded == contracts.TOOL_ERROR and r4.answer)
    return specs


async def part_b():
    print("\n=== B. 端到端（真调模型 + 真检索，约 30~60s）===")
    from src.agent import core

    seen_tools = []
    final = ""
    async for ev in core.run_agent(
        "知识库里关于 H3C 设备的默认登录账号是什么？", [], "rag", "probe-subagent"):
        t = ev.get("type")
        if t == "tool":
            name = ev.get("name", "")
            if name and name not in seen_tools:
                seen_tools.append(name)
                print(f"    工具调用：{name}  args={str(ev.get('args', {}))[:120]}")
        elif t == "token":
            final += ev.get("text", "")
        elif t == "done":
            final = ev.get("content", "") or final

    check("主智能体用了 task 工具派发", "task" in seen_tools, f"tools={seen_tools}")
    check("最终有答案", bool(final.strip()), f"{len(final)} 字")
    print("\n答案前 300 字：")
    print("  " + final[:300].replace("\n", "\n  "))


async def main():
    await part_a()
    if "--e2e" in sys.argv:
        await part_b()

    ok = sum(1 for _, c, _ in _results if c)
    print(f"\n合计 {ok}/{len(_results)} 通过")
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
