from __future__ import annotations

import json
import sqlite3

from ..data.database import (
    DEFAULT_CONVERSATION_TITLE,
    Database,
    User,
    timestamp_to_db,
    user_from_row,
    utc_now,
)
from ..security.passwords import (
    MAX_PASSWORD_LENGTH,
    PasswordPolicyError,
    clean_username,
    hash_password,
    normalize_username,
    perform_dummy_verification,
    verify_and_rehash,
)

LOGIN_ERROR_MESSAGE = "用户名或密码错误"
MAX_USERNAME_LENGTH = 64
STAFF_ROLE_CODES = frozenset({"advisor", "operator"})

_USER_COLUMNS = (
    "id, username, username_normalized, created_at, last_login_at, "
    "role_code, account_status, updated_at"
)


class AuthError(RuntimeError):
    """Base error for authentication business rules."""


class RegistrationError(AuthError):
    """A registration error safe to display in the UI."""


class AuthenticationError(AuthError):
    """A deliberately uniform login error safe to display in the UI."""

    def __init__(self) -> None:
        super().__init__(LOGIN_ERROR_MESSAGE)


class AuthorizationError(AuthError):
    """A safe authorization error for privileged account operations."""


class AuthService:
    """Registration and login operations for the local-account app."""

    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()
        self.database.initialize()

    def register(self, username: str, password: str) -> User:
        """Create one user and that user's single default conversation atomically."""

        display_username, normalized_username, password_hash = self._prepare_new_account(
            username, password
        )

        now = timestamp_to_db(utc_now())
        try:
            with self.database.transaction() as connection:
                row = self._insert_account(
                    connection,
                    display_username=display_username,
                    normalized_username=normalized_username,
                    password_hash=password_hash,
                    role_code="member",
                    now=now,
                )
        except sqlite3.IntegrityError as error:
            raise RegistrationError("用户名已存在") from error

        if row is None:  # pragma: no cover - insert and select share one transaction
            raise AuthError("注册成功后无法读取用户记录")
        return user_from_row(row)

    def has_operator(self) -> bool:
        """Return whether the database already contains any operator account."""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM users WHERE role_code = 'operator')"
            ).fetchone()
        return bool(row[0])

    def bootstrap_operator(self, username: str, password: str) -> User:
        """Atomically create the one permitted initial operator account.

        The existence check intentionally runs inside the default ``BEGIN IMMEDIATE``
        transaction. Concurrent callers therefore serialize before checking and at
        most one of them can create an operator.
        """

        display_username, normalized_username, password_hash = self._prepare_new_account(
            username, password
        )
        now = timestamp_to_db(utc_now())
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT 1 FROM users WHERE role_code = 'operator' LIMIT 1"
                ).fetchone()
                if existing is not None:
                    raise RegistrationError("运营账号已经初始化，不能重复创建")

                row = self._insert_account(
                    connection,
                    display_username=display_username,
                    normalized_username=normalized_username,
                    password_hash=password_hash,
                    role_code="operator",
                    now=now,
                )
                user_id = int(row["id"])
                self._add_account_audit_log(
                    connection,
                    actor_user_id=user_id,
                    action="operator.bootstrapped",
                    entity_id=user_id,
                    role_code="operator",
                    now=now,
                )
        except RegistrationError:
            raise
        except sqlite3.IntegrityError as error:
            raise RegistrationError("用户名已存在，无法初始化运营账号") from error
        except sqlite3.OperationalError as error:
            raise RegistrationError("运营账号初始化失败，请稍后重试") from error

        return user_from_row(row)

    def create_staff(
        self,
        actor_user_id: int,
        username: str,
        password: str,
        role_code: str,
    ) -> User:
        """Create an advisor or operator account as an active operator."""

        if role_code not in STAFF_ROLE_CODES:
            raise RegistrationError("员工角色只能是 advisor 或 operator")
        display_username, normalized_username, password_hash = self._prepare_new_account(
            username, password
        )
        now = timestamp_to_db(utc_now())
        try:
            with self.database.transaction() as connection:
                actor = connection.execute(
                    """
                    SELECT role_code, account_status
                    FROM users
                    WHERE id = ?
                    """,
                    (actor_user_id,),
                ).fetchone()
                if (
                    actor is None
                    or str(actor["role_code"]) != "operator"
                    or str(actor["account_status"]) != "active"
                ):
                    raise AuthorizationError("只有启用中的运营账号可以创建员工账号")

                row = self._insert_account(
                    connection,
                    display_username=display_username,
                    normalized_username=normalized_username,
                    password_hash=password_hash,
                    role_code=role_code,
                    now=now,
                )
                user_id = int(row["id"])
                self._add_account_audit_log(
                    connection,
                    actor_user_id=actor_user_id,
                    action="staff.created",
                    entity_id=user_id,
                    role_code=role_code,
                    now=now,
                )
        except (AuthorizationError, RegistrationError):
            raise
        except sqlite3.IntegrityError as error:
            raise RegistrationError("用户名已存在") from error
        except sqlite3.OperationalError as error:
            raise RegistrationError("员工账号创建失败，请稍后重试") from error

        return user_from_row(row)

    def authenticate(self, username: str, password: str) -> User:
        """Verify credentials or raise the same error for every login failure."""

        if not isinstance(username, str) or not isinstance(password, str):
            raise AuthenticationError

        try:
            normalized_username = normalize_username(username)
        except TypeError:
            raise AuthenticationError from None

        invalid_shape = (
            not normalized_username
            or len(normalized_username) > MAX_USERNAME_LENGTH
            or len(password) > MAX_PASSWORD_LENGTH
        )
        if invalid_shape:
            perform_dummy_verification(password[:MAX_PASSWORD_LENGTH])
            raise AuthenticationError

        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT {_USER_COLUMNS}, password_hash
                FROM users
                WHERE username_normalized = ?
                """,
                (normalized_username,),
            ).fetchone()

        if row is None:
            perform_dummy_verification(password)
            raise AuthenticationError

        if str(row["account_status"]) != "active":
            perform_dummy_verification(password)
            raise AuthenticationError

        verification = verify_and_rehash(str(row["password_hash"]), password)
        if not verification.valid:
            raise AuthenticationError

        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            # Recheck state under the write lock. An expired advisor must not
            # receive a session even if a password was valid a moment earlier.
            current = connection.execute(
                "SELECT role_code, account_status FROM users WHERE id = ?", (int(row["id"]),)
            ).fetchone()
            if current is None or current["account_status"] != "active":
                raise AuthenticationError
            if current["role_code"] == "advisor":
                term = connection.execute(
                    "SELECT starts_at, ends_at FROM staff_account_terms WHERE user_id = ?",
                    (int(row["id"]),),
                ).fetchone()
                if term is not None and not term["starts_at"] <= now < term["ends_at"]:
                    raise AuthenticationError
            if verification.replacement_hash is None:
                cursor = connection.execute(
                    "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, int(row["id"])),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE users
                    SET last_login_at = ?, password_hash = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, verification.replacement_hash, now, int(row["id"])),
                )
            if cursor.rowcount != 1:
                raise AuthenticationError
            updated = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",
                (int(row["id"]),),
            ).fetchone()

        if updated is None:  # pragma: no cover - guarded by rowcount
            raise AuthenticationError
        return user_from_row(updated)

    def login(self, username: str, password: str) -> User:
        """UI-friendly alias for :meth:`authenticate`."""

        return self.authenticate(username, password)

    def get_user(self, user_id: int) -> User | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return None if row is None else user_from_row(row)

    @staticmethod
    def _prepare_new_account(username: str, password: str) -> tuple[str, str, str]:
        try:
            display_username = clean_username(username)
            normalized_username = normalize_username(username)
        except TypeError as error:
            raise RegistrationError("用户名格式无效") from error

        if not normalized_username:
            raise RegistrationError("用户名不能为空")
        if (
            len(display_username) > MAX_USERNAME_LENGTH
            or len(normalized_username) > MAX_USERNAME_LENGTH
        ):
            raise RegistrationError(f"用户名不能超过 {MAX_USERNAME_LENGTH} 个字符")

        try:
            password_hash = hash_password(password)
        except PasswordPolicyError as error:
            raise RegistrationError(str(error)) from error
        return display_username, normalized_username, password_hash

    @staticmethod
    def _insert_account(
        connection: sqlite3.Connection,
        *,
        display_username: str,
        normalized_username: str,
        password_hash: str,
        role_code: str,
        now: str,
    ) -> sqlite3.Row:
        cursor = connection.execute(
            """
            INSERT INTO users (
                username, username_normalized, password_hash, created_at,
                last_login_at, role_code, account_status, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?, 'active', ?)
            """,
            (display_username, normalized_username, password_hash, now, role_code, now),
        )
        user_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO conversations (user_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, DEFAULT_CONVERSATION_TITLE, now, now),
        )
        row = connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - insert and select share one transaction
            raise AuthError("账号创建成功后无法读取用户记录")
        return row

    @staticmethod
    def _add_account_audit_log(
        connection: sqlite3.Connection,
        *,
        actor_user_id: int,
        action: str,
        entity_id: int,
        role_code: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (?, ?, 'user', ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                entity_id,
                json.dumps({"role_code": role_code}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )


__all__ = [
    "AuthenticationError",
    "AuthError",
    "AuthService",
    "AuthorizationError",
    "LOGIN_ERROR_MESSAGE",
    "MAX_USERNAME_LENGTH",
    "RegistrationError",
    "STAFF_ROLE_CODES",
]
