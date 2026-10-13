"""
初始化数据库并创建第一个用户

用法：
    python scripts/bootstrap.py 你的名字
    python scripts/bootstrap.py --list

设计说明：
    API Key 只在创建时明文显示一次，之后库里只有 sha256。
    不做"忘记 Key 帮你找回"，只支持重新签发（rotate）——这是安全上的正确取舍。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings          # noqa: E402
from src.db import engine as dbeng       # noqa: E402
from src.db import repo                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="明辨平台 · 初始化")
    ap.add_argument("name", nargs="?", default="", help="要创建的用户名")
    ap.add_argument("--role", default="user")
    ap.add_argument("--list", action="store_true", help="列出现有用户")
    ap.add_argument("--rotate", metavar="USER_ID", help="重新签发某用户的 API Key")
    args = ap.parse_args()

    dbeng.init_db()
    print(f"[bootstrap] 数据库已就绪: {settings.db_url.split('://')[0]}")

    if args.list:
        for u in repo.list_users():
            print(f"  {u['id']}  {u['name']:<16} {u['role']:<8} "
                  f"{u['api_key_prefix']}…  {u['status']}")
        return 0

    if args.rotate:
        if repo.get_user_by_id(args.rotate) is None:
            print(f"[bootstrap] 用户不存在: {args.rotate}")
            return 1
        print(f"[bootstrap] 新 Key: {repo.rotate_api_key(args.rotate)}")
        return 0

    if not args.name:
        ap.print_help()
        return 1

    res = repo.create_user(args.name, args.role)
    u = res["user"]
    print()
    print("  用户已创建")
    print(f"    user_id : {u['id']}")
    print(f"    name    : {u['name']}")
    print(f"    role    : {u['role']}")
    print(f"    API Key : {res['api_key']}")
    print()
    print("  ⚠️ 这串 Key 只显示这一次，请立即保存。")
    print("  调用方式：Authorization: Bearer <上面的 Key>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
