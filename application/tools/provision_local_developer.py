"""Explicit local-only developer provisioning with hidden password prompts.

No default credentials are embedded. This is an owner-operated setup command,
not an app API, and must never be invoked by an unauthenticated GUI action.
"""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.developer import provision_local_developer


def provision(database, username, password, *, actor=None):
    """Existing operator verification or authenticated creation; never reset silently."""
    service = AuthService(database)
    if actor is not None:
        operator = service.authenticate(*actor)
        if operator.role_code != "operator":
            raise ValueError("必须由已有管理者验证后创建。")
        user = service.create_staff(operator.id, username, password, "operator")
    else:
        user = service.authenticate(username, password)
    if user.role_code != "operator":
        raise ValueError("开发权限只能授予管理者，不能自动提升普通会员。")
    provision_local_developer(database, user.id)
    return {"local_developer_granted": True, "username": user.username,
            "cloud_changed": False, "password_printed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--create-with-operator", help="Existing local operator username")
    parser.add_argument("--confirm-local-change", action="store_true")
    args = parser.parse_args()
    path = args.database.resolve()
    if not args.confirm_local_change or not path.is_file() or args.database.is_symlink():
        raise SystemExit("需要明确确认及已有本地数据库；原数据未修改。")
    if any(parent.is_symlink() or parent.is_junction() for parent in path.parents):
        raise SystemExit("数据库路径不允许链接。")
    password = getpass.getpass("开发者账户密码（不显示）：")
    actor = None
    if args.create_with_operator:
        actor = (args.create_with_operator, getpass.getpass("已有管理者密码（不显示）："))
    result = provision(Database(path), args.username, password, actor=actor)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
