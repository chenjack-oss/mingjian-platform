# -*- coding: utf-8 -*-
"""长期记忆（P-1 记忆底座）离线自测：验证记忆写读链路不再静默失败。

背景（🔴 曾发现的隐患）：
  upsert_memory 调 keys.new_id("mem")，而 ID_PREFIX 原本缺 "mem" 键 → KeyError。
  由于 extract_memories 有 try/except 兜底 + service 用 asyncio.create_task
  fire-and-forget + 外层再兜底，这个 KeyError 从 P-1 起一直是"静默失败"——
  记忆功能从来没真正工作，但既不报错也不影响主链路，回归测试也测不到。

  本测试直接覆盖 repo.upsert_memory（不再 KeyError）+ 幂等 + 提取进度，
  让这条链路从此有回归护栏。

跑法：python tests/test_memory.py
"""
from __future__ import annotations

import os
import sys
import tempfile

# import src 之前设临时库，避免污染真实数据
_tmp = tempfile.mkdtemp(prefix="mj_memory_test_")
os.environ["MJ_DB_URL"] = f"sqlite:///{_tmp}/test.db"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import engine as db        # noqa: E402
from src.db import repo                # noqa: E402
from src.identity import keys          # noqa: E402

db.init_db()

_passed = 0
_failed = 0


def check(name: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [ok] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


user = repo.create_user("测试用户")["user"]["id"]

print("[1] upsert_memory 不再 KeyError（核心 bug 回归）")
try:
    mid = repo.upsert_memory(user, "user", "fact", "用户是数据科学本科", importance=3)
    check("写入成功且返回 m_ 前缀 id", mid.startswith("m_"))
except KeyError as e:
    check(f"upsert_memory 仍 KeyError: {e}", False)

print("[2] 幂等：同 content 更新 importance 而非新建")
repo.upsert_memory(user, "user", "fact", "用户是数据科学本科", importance=5)
mems = repo.list_memories(user, "user", kinds=("fact",))
check("同 content 不重复建", len(mems) == 1)
check("importance 取较大值", mems[0]["importance"] == 5)

print("[3] 不同 kind/content 各自独立")
repo.upsert_memory(user, "user", "preference", "喜欢表格化输出", importance=2)
all_mems = repo.list_memories(user, "user")
check("fact + preference 共 2 条", len(all_mems) == 2)

print("[4] 提取进度（memory_extracts 幂等游标）")
sid = repo.create_session(user)["id"]
check("初始进度为 0", repo.get_memory_extract(sid) == 0)
repo.set_memory_extract(sid, 7)
check("设置后进度为 7", repo.get_memory_extract(sid) == 7)
repo.set_memory_extract(sid, 12)  # 重复 set 是 upsert，不是累加
check("重复 set 是 upsert 非累加", repo.get_memory_extract(sid) == 12)

print("[5] keys.ID_PREFIX 完整性（防再漏键）")
for kind in ("user", "session", "file", "task", "mem", "org"):
    check(f"ID_PREFIX 含 {kind}", kind in keys.ID_PREFIX)

print("\n" + "=" * 62)
print(f"通过 {_passed} / {_passed + _failed}")
sys.exit(1 if _failed else 0)
