# -*- coding: utf-8 -*-
"""多租户（P-6）离线自测：组织 + 成员，叠加式隔离。

不依赖 LLM，只测数据模型与隔离边界：
  1. 组织 CRUD（创建者自动 owner）
  2. 成员管理（加入/角色/移除）
  3. 越权隔离（非成员查详情/用量抛 Forbidden，不泄露存在性）
  4. 组织级记忆（复用 memories 表 scope=org）
  5. 组织级 token 用量聚合
  6. owner 保护（不能移除/改角色，删除组织仅 owner）

跑法：python tests/test_orgs.py
"""
from __future__ import annotations

import os
import sys
import tempfile

# 关键：必须在 import src 之前，把 DB 指向一个隔离的临时 SQLite 库，
# 否则会读写项目真实数据（.data/mingjian.db）。settings 是模块级单例，import 时读取。
_tmp = tempfile.mkdtemp(prefix="mj_orgs_test_")
os.environ["MJ_DB_URL"] = f"sqlite:///{_tmp}/test.db"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import engine as db        # noqa: E402
from src.db import repo                # noqa: E402

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


# 三个测试用户
alice = repo.create_user("Alice")["user"]["id"]
bob = repo.create_user("Bob")["user"]["id"]
carol = repo.create_user("Carol")["user"]["id"]

print("[1] 组织 CRUD")
org = repo.create_org(alice, "研发一组")
check("创建返回组织", org["id"].startswith("o_"))
check("创建者自动成为 owner", repo.get_membership(org["id"], alice)["role"] == "owner")
check("列表包含新组织", any(o["id"] == org["id"] for o in repo.list_orgs(alice)))
check("非成员不在列表", all(o["id"] != org["id"] for o in repo.list_orgs(bob)))

print("[2] 成员管理")
repo.add_member(org["id"], bob, "member")
repo.add_member(org["id"], carol, "admin")
check("bob 成为 member", repo.get_membership(org["id"], bob)["role"] == "member")
check("carol 成为 admin", repo.get_membership(org["id"], carol)["role"] == "admin")
check("成员数 3", len(repo.list_members(org["id"])) == 3)
repo.update_member_role(org["id"], bob, "admin")
check("bob 升 admin", repo.get_membership(org["id"], bob)["role"] == "admin")
repo.remove_member(org["id"], carol)
check("carol 被移除", repo.get_membership(org["id"], carol) is None)
check("成员数回到 2", len(repo.list_members(org["id"])) == 2)

print("[3] 越权隔离")
try:
    repo.get_org(carol, org["id"])
    check("非成员查详情抛 Forbidden", False)
except repo.Forbidden:
    check("非成员查详情抛 Forbidden", True)
check("bob（成员）可查详情", repo.get_org(bob, org["id"])["id"] == org["id"])

print("[4] 组织级记忆（复用 scope=org）")
repo.upsert_memory(org["id"], "org", "fact", "团队采用双周迭代")
mems = repo.list_memories(org["id"], scope="org")
check("组织级记忆已存", len(mems) == 1 and mems[0]["scope"] == "org")
# 组织记忆不影响用户私有记忆
check("用户私有记忆为空", len(repo.list_memories(alice, scope="user")) == 0)

print("[5] 组织级 token 用量聚合")
repo.add_usage(alice, "token_input", 100, model="qwen-max")
repo.add_usage(alice, "token_output", 50, model="qwen-max")
repo.add_usage(bob, "token_input", 30, model="qwen-max")
repo.add_usage(carol, "token_input", 999, model="qwen-max")  # carol 已被移除，不计入
agg = repo.org_token_usage(org["id"])
check("聚合只含成员（carol 已移除不计）",
      agg.get("qwen-max", {}).get("input_tokens") == 130)
check("输出 token 聚合", agg.get("qwen-max", {}).get("output_tokens") == 50)
check("org_member_ids 只含 alice+bob",
      sorted(repo.org_member_ids(org["id"])) == sorted([alice, bob]))

print("[6] owner 保护")
try:
    repo.update_member_role(org["id"], alice, "member")
    check("改 owner 角色抛 Forbidden", False)
except repo.Forbidden:
    check("改 owner 角色抛 Forbidden", True)
try:
    repo.remove_member(org["id"], alice)
    check("移除 owner 抛 Forbidden", False)
except repo.Forbidden:
    check("移除 owner 抛 Forbidden", True)
try:
    repo.delete_org(bob, org["id"])  # bob 现在是 admin，不是 owner
    check("非 owner 删组织抛 Forbidden", False)
except repo.Forbidden:
    check("非 owner 删组织抛 Forbidden", True)
repo.delete_org(alice, org["id"])
check("owner 删除组织成功", repo.get_membership(org["id"], alice) is None)
check("删除后组织级记忆清理",
      len(repo.list_memories(org["id"], scope="org")) == 0)

print("\n" + "=" * 62)
print(f"通过 {_passed} / {_passed + _failed}")
for _ in range(_failed):
    pass
sys.exit(1 if _failed else 0)
