"""Out-of-band developer grant provisioning; never imported by HTTP routes.

Run on the server with a schema-owner connection in the named environment
variable. The existing operator must prove their password via a hidden prompt.
This does not create an operator, reset their password, or rotate any API key.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os

import psycopg

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.security.passwords import normalize_username, verify_password

from .platform_schema import PLATFORM_MARKER, PLATFORM_SCHEMA


def provision(connection, username, password, *, revoke=False):
    """Caller provides an explicit management connection, not runtime identity."""
    owner = connection.execute(
        "SELECT pg_catalog.pg_get_userbyid(nspowner)=current_user "
        "AND pg_catalog.obj_description(oid,'pg_namespace')=%s "
        "FROM pg_catalog.pg_namespace WHERE nspname=%s",
        (PLATFORM_MARKER, PLATFORM_SCHEMA),
    ).fetchone()
    if not owner or not owner[0]:
        raise ValueError("Developer grants require the verified platform schema owner")
    connection.execute("SET LOCAL search_path = medical_app_platform, pg_catalog")
    connection.execute("SET LOCAL lock_timeout = '5s'")
    row = connection.execute(
        "SELECT id,password_hash,role_code,account_status FROM users "
        "WHERE username_normalized=%s FOR UPDATE",
        (normalize_username(username),),
    ).fetchone()
    if not row or row[2:] != ("operator", "active") or not verify_password(row[1], password):
        raise ValueError("Existing active operator and correct password are required")
    connection.execute(
        "INSERT INTO developer_grants(user_id,enabled,granted_at) VALUES(%s,%s,%s) "
        "ON CONFLICT(user_id) DO UPDATE SET enabled=EXCLUDED.enabled,"
        "granted_at=EXCLUDED.granted_at",
        (row[0], 0 if revoke else 1, timestamp_to_db(utc_now())),
    )
    return {"developer_user_id": int(row[0]), "granted": not revoke, "password_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", default="PLATFORM_MIGRATION_DATABASE_URL")
    parser.add_argument("--username", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    if args.confirm != PLATFORM_SCHEMA:
        print("未明确确认平台结构，尚未连接数据库。")
        return 2
    try:
        uri = os.environ[args.database_url_env]
        password = getpass.getpass("已有管理者密码（输入不可见）：")
        with psycopg.connect(uri) as connection:
            result = provision(connection, args.username, password, revoke=args.revoke)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt):
        print("开发者授权未完成，原始异常和凭据已隐藏。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
