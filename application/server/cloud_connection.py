"""Local, opt-in Supabase credential configuration and read-only connection checks.

This tool never changes cloud tables, starts the API, or reads desktop records.
Administrative migration credentials are not application runtime credentials.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import ssl
import sys
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import psycopg
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from .local_secret_store import (
    LocalSecretStoreError,
    load_secret_payload,
    save_secret_payload,
)
from .schema import PRIVATE_SCHEMA

PROJECT_FILE = Path(__file__).with_name("supabase-project.json")
DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / ".local" / "supabase-connection.json"


class CloudConfigurationError(ValueError):
    """Safe user-facing messages only; never embed connection input or DB errors."""


def project_url() -> str:
    value = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))["project_url"]
    project_reference(value)
    return value


def project_reference(value: str) -> str:
    try:
        parsed = urlsplit(value)
        match = re.fullmatch(r"([a-z]{20})\.supabase\.co", parsed.hostname or "")
        if (
            parsed.scheme != "https"
            or not match
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return match[1]
    except (ValueError, TypeError):
        raise CloudConfigurationError("项目地址必须是正确的 Supabase HTTPS 项目入口。") from None


def validated_url(uri: str, expected_project_url: str) -> URL:
    ref = project_reference(expected_project_url)
    try:
        if not isinstance(uri, str) or len(uri) > 8192 or any(ord(char) < 32 for char in uri):
            raise ValueError
        result = make_url(uri.strip())
        if result.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
            raise ValueError
        if result.port != 5432 or result.database != "postgres" or not result.username:
            raise ValueError
        pooler = re.fullmatch(r"[a-z0-9-]+\.pooler\.supabase\.com", result.host or "")
        direct = result.host == f"db.{ref}.supabase.co"
        if pooler:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\." + ref, result.username):
                raise ValueError
        elif not direct or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result.username):
            raise ValueError
        # No libpq hostaddr, service, passfile, options or alternate host override.
        if set(result.query) - {"sslmode", "sslrootcert"}:
            raise ValueError
        if any(not isinstance(value, str) for value in result.query.values()):
            raise ValueError
        return result.set(drivername="postgresql+psycopg")
    except (TypeError, ValueError, AttributeError, ArgumentError):
        raise CloudConfigurationError(
            "连接串不符合本项目：请从 Connect 复制 Session pooler URI（端口 5432），"
            "用户名须包含当前项目编号；也支持本项目的 Direct URI。不支持 6543 或额外连接参数。"
        ) from None


def ca_file(value: str | None) -> str:
    path = Path(value.strip().strip('"') if value and value.strip() else certifi.where())
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        path = path.resolve(strict=True)
        ssl.create_default_context(cafile=str(path))
    except (OSError, ValueError, ssl.SSLError):
        raise CloudConfigurationError("根证书文件无效，请选择可信的 PEM/CRT 根证书文件。") from None
    return str(path)


def connection_payload(uri: str, password: str, certificate: str | None) -> dict:
    public_url = project_url()
    parsed = validated_url(uri, public_url)
    if (
        not isinstance(password, str)
        or not password
        or len(password) > 1024
        or "\x00" in password
        or password in {"[YOUR-PASSWORD]", "YOUR-PASSWORD"}
    ):
        raise CloudConfigurationError("请填写真实数据库密码，不是 API Key 或占位文字。")
    parsed = parsed.set(
        password=password, query={"sslmode": "verify-full", "sslrootcert": ca_file(certificate)}
    )
    return {
        "project_url": public_url,
        "purpose": "migration-readonly-preflight",
        "migration_database_url": parsed.render_as_string(hide_password=False),
    }


def hidden_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise CloudConfigurationError("需要在你自己的交互终端中填写，不允许回显或重定向密码。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except (getpass.GetPassWarning, EOFError):
            raise CloudConfigurationError(
                "当前终端无法安全隐藏输入，已停止且未读取明文密码。"
            ) from None


def configure(path: Path, *, replace: bool = False) -> dict:
    print("项目：" + project_url())
    print("在 Supabase Connect → Session pooler 复制 URI；连接串和数据库密码均隐藏输入。")
    print("只保存到本机当前 Windows 用户的加密配置，不连接云端，不建表，不进入应用发布包。")
    if path.exists() and not replace:
        raise CloudConfigurationError("已有配置，未覆盖。确需更新时请显式使用 --replace。")
    uri = hidden_input("粘贴 Session pooler URI 后回车（输入不可见）：")
    validated_url(uri, project_url())
    password = hidden_input("数据库密码（不是 Supabase 网站登录密码，也不是 API Key）：")
    certificate = input("根证书文件路径（留空先使用受信任公共 CA，失败时再选项目证书）：")
    payload = connection_payload(uri, password, certificate)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_secret_payload(path, payload, replace=replace)
    return {
        "status": "saved",
        "project_url": project_url(),
        "profile": str(path.resolve()),
        "cloud_connected": False,
        "tables_changed": False,
    }


def probe_profile(path: Path) -> dict:
    try:
        payload = load_secret_payload(path)
    except LocalSecretStoreError:
        if not path.exists():
            raise CloudConfigurationError(
                "尚未配置数据库连接，请先在本机运行 configure，再进行只读检查。"
            ) from None
        raise
    if (
        set(payload) != {"project_url", "purpose", "migration_database_url"}
        or payload["project_url"] != project_url()
        or payload["purpose"] != "migration-readonly-preflight"
    ):
        raise CloudConfigurationError("加密配置不属于当前项目或用途不正确，已停止连接。")
    uri = validated_url(payload["migration_database_url"], project_url())
    if not uri.password or uri.query.get("sslmode") != "verify-full":
        raise CloudConfigurationError("配置缺少密码或严格证书验证，已停止连接。")
    certificate = ca_file(uri.query.get("sslrootcert"))
    # Independent kwargs prevent URL query injection and correctly preserve @:%/ in passwords.
    with psycopg.connect(
        host=uri.host,
        port=5432,
        dbname=uri.database,
        user=uri.username,
        password=uri.password,
        sslmode="verify-full",
        sslrootcert=certificate,
        connect_timeout=8,
        application_name="medical-app-readonly-preflight",
    ) as connection:
        connection.read_only = True  # Applied before the first SQL / BEGIN.
        if not connection.pgconn.ssl_in_use:
            raise CloudConfigurationError("未建立 TLS 加密连接，已停止检查。")
        try:
            connection.execute("SET LOCAL statement_timeout = '5000ms'")
            row = connection.execute(
                "SELECT current_setting('server_version'), current_database(), current_user, "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s)",
                (PRIVATE_SCHEMA,),
            ).fetchone()
            if not row:
                raise CloudConfigurationError("数据库未返回检查结果。")
            result = {
                "status": "connected_readonly",
                "project_url": project_url(),
                "client_tls": True,
                "certificate_mode": "verify-full",
                "postgres_version": row[0],
                "database": row[1],
                "database_role": row[2],
                "private_schema_exists": bool(row[3]),
                "tables_changed": False,
                "health_records_read": 0,
                "desktop_sync_enabled": False,
                "warning": "这里只验证连接，不代表完整数据库、权限或桌面云同步已经验收。",
            }
        finally:
            connection.rollback()  # Never commit even session-local probe work.
    return result


def safe_database_error(error: psycopg.Error) -> str:
    code = error.sqlstate
    message = str(error).casefold()  # Classify locally, never emit raw driver text/DSN.
    if code == "28P01" or "password authentication failed" in message:
        return "数据库密码验证失败，请在本机重新填写；不要把密码发到聊天中。"
    if "certificate" in message or "ssl" in message or "tls" in message:
        return "TLS/证书验证未通过，请核对主机及可信根证书；不要关闭证书验证。"
    if "tenant" in message or "user not found" in message:
        return "Pooler 用户或项目未匹配，请重新从当前项目 Connect 复制连接串。"
    return "暂时无法连接数据库，请核对项目运行状态、连接串、DNS/网络与端口。原始错误已隐藏。"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "status", "check"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--replace", action="store_true", help="明确替换已有加密配置")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            result = configure(args.profile, replace=args.replace)
        elif args.command == "check":
            result = probe_profile(args.profile)
        else:
            result = {
                "project_url": project_url(),
                "profile_exists": args.profile.is_file(),
                "cloud_connected": "not_checked",
                "desktop_sync_enabled": False,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CloudConfigurationError, LocalSecretStoreError) as error:
        print(str(error))
        return 2
    except psycopg.Error as error:
        print(safe_database_error(error))
        return 3
    except (EOFError, KeyboardInterrupt):
        print("已取消，未继续操作。")
        return 2
    except Exception:
        print("配置或检查未完成，原始错误已隐藏以保护连接信息。请检查文件与运行环境。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
