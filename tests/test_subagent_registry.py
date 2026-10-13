# -*- coding: utf-8 -*-
"""子智能体动态注册（P4）离线测试：不依赖 LLM，只测装配逻辑与环境变量开关。

覆盖：
1. 默认装配（四子智能体在 research 全上、rag/auto 只 kb、direct/tool 空）
2. 按档位环境变量覆盖（MJ_SUBAGENTS_<TIER>）
3. 全局禁用名单（MJ_SUBAGENTS_DISABLED）
4. 空名单 = 明确下线该档全部
5. 不认识的名字被静默忽略（防拼写错误静默失效）
6. enabled_subagents() 去重、按定义顺序
"""
import os
import sys

sys.path.insert(0, ".")

from src.agent.subagents import registry

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")


def _clear_env():
    for k in list(os.environ):
        if k.startswith("MJ_SUBAGENTS"):
            os.environ.pop(k, None)


# 每个测试前清空环境变量，保证独立
_clear_env()

print("[1] 默认装配")
check("research 四子全上", registry.planned_for("research") ==
      ["kb-analyst", "db-analyst", "web-analyst", "doc-analyst"])
check("rag 只 kb", registry.planned_for("rag") == ["kb-analyst"])
check("auto 只 kb", registry.planned_for("auto") == ["kb-analyst"])
check("direct 空", registry.planned_for("direct") == [])
check("tool 空", registry.planned_for("tool") == [])
check("enabled_subagents 去重有序",
      registry.enabled_subagents() == ["kb-analyst", "db-analyst", "web-analyst", "doc-analyst"])

print("[2] 按档位环境变量覆盖")
os.environ["MJ_SUBAGENTS_RESEARCH"] = "kb-analyst, web-analyst"
check("research 覆盖为 kb+web", registry.planned_for("research") ==
      ["kb-analyst", "web-analyst"])
check("rag 不受影响", registry.planned_for("rag") == ["kb-analyst"])

print("[3] 全局禁用名单叠加")
os.environ["MJ_SUBAGENTS_DISABLED"] = "web-analyst"
check("research 剔除禁用后只剩 kb", registry.planned_for("research") == ["kb-analyst"])
check("enabled_subagents 不含被禁用", "web-analyst" not in registry.enabled_subagents())

print("[4] 空名单 = 明确下线该档全部")
_clear_env()
os.environ["MJ_SUBAGENTS_RESEARCH"] = ""
check("research 空名单 = 不派发", registry.planned_for("research") == [])
check("其他档仍默认", registry.planned_for("rag") == ["kb-analyst"])

print("[5] 不认识的名字被忽略")
_clear_env()
os.environ["MJ_SUBAGENTS_RESEARCH"] = "kb-analyst, bogus-agent, db-analyst"
check("忽略 bogus-agent", registry.planned_for("research") == ["kb-analyst", "db-analyst"])

print("[6] 总开关 enabled()")
_clear_env()
check("默认开", registry.enabled() is True)
os.environ["MJ_SUBAGENTS_ENABLED"] = "0"
check("设为 0 关闭", registry.enabled() is False)
_clear_env()

print(f"\n{'=' * 40}")
print(f"通过 {_passed}/{_passed + _failed}")
sys.exit(1 if _failed else 0)
