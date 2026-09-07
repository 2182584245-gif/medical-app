from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..paths import database_path

SCHEMA_VERSION = 5
DEFAULT_CONVERSATION_TITLE = "默认对话"


class DatabaseError(RuntimeError):
    """Base error raised by the local persistence layer."""


class UnsupportedSchemaVersionError(DatabaseError):
    """Raised when the database was created by an unsupported app version."""


@dataclass(frozen=True, slots=True)
class User:
    """A safe user view; deliberately excludes the password hash."""

    id: int
    username: str
    username_normalized: str
    created_at: datetime
    last_login_at: datetime | None
    role_code: str = "member"
    account_status: str = "active"
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    user_id: int
    title: str
    provider: str | None
    model: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    conversation_id: int
    sequence_no: int
    role: str
    content: str
    status: str
    error_message: str | None
    provider: str | None
    model: str | None
    created_at: datetime
    updated_at: datetime


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def timestamp_to_db(value: datetime) -> str:
    """Serialize a timestamp in an unambiguous, lexicographically sortable format."""

    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone information")
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp_from_db(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def user_from_row(row: sqlite3.Row) -> User:
    columns = set(row.keys())
    created_at = _required_timestamp(row["created_at"])
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        username_normalized=str(row["username_normalized"]),
        created_at=created_at,
        last_login_at=timestamp_from_db(row["last_login_at"]),
        role_code=str(row["role_code"]) if "role_code" in columns else "member",
        account_status=(str(row["account_status"]) if "account_status" in columns else "active"),
        updated_at=(
            _required_timestamp(row["updated_at"])
            if "updated_at" in columns and row["updated_at"] is not None
            else created_at
        ),
    )


def conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        title=str(row["title"]),
        provider=_optional_text(row["provider"]),
        model=_optional_text(row["model"]),
        created_at=_required_timestamp(row["created_at"]),
        updated_at=_required_timestamp(row["updated_at"]),
    )


def message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        sequence_no=int(row["sequence_no"]),
        role=str(row["role"]),
        content=str(row["content"]),
        status=str(row["status"]),
        error_message=_optional_text(row["error_message"]),
        provider=_optional_text(row["provider"]),
        model=_optional_text(row["model"]),
        created_at=_required_timestamp(row["created_at"]),
        updated_at=_required_timestamp(row["updated_at"]),
    )


def _required_timestamp(value: Any) -> datetime:
    parsed = timestamp_from_db(str(value))
    if parsed is None:  # pragma: no cover - protected by NOT NULL constraints
        raise DatabaseError("database contains a missing required timestamp")
    return parsed


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


class Database:
    """Creates short-lived SQLite connections suitable for use from Qt workers.

    A connection is never shared between threads. Callers should use ``connect``
    for reads and ``transaction`` for a small unit of writes.
    """

    def __init__(self, path: str | Path | None = None, *, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path) if path is not None else database_path()
        self.timeout_seconds = timeout_seconds

    def initialize(self) -> None:
        """Create or transactionally migrate the local database to the current schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            # SQLite ignores this pragma inside a transaction. The v5 table
            # rebuild must not cascade-delete its existing message children.
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 0 or version > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"unsupported database schema version {version}; "
                        f"this app supports version {SCHEMA_VERSION}"
                    )
                if version == 0:
                    self._create_schema_v1(connection)
                    version = 1
                if version == 1:
                    self._migrate_v1_to_v2(connection)
                    version = 2
                if version == 2:
                    self._migrate_v2_to_v3(connection)
                    version = 3
                if version == 3:
                    self._migrate_v3_to_v4(connection)
                    version = 4
                if version == 4:
                    self._migrate_v4_to_v5(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DatabaseError("数据库升级外键检查失败，已保留升级前数据")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured, short-lived connection and always close it."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        foreign_keys_enabled = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys_enabled != 1:
            connection.close()
            raise DatabaseError("SQLite foreign-key enforcement could not be enabled")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield one explicit transaction and commit or roll it back.

        ``BEGIN IMMEDIATE`` is the default so message sequence allocation and
        uniqueness checks remain deterministic when two UI actions race.
        """

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _create_schema_v1(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                username_normalized TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT,
                CHECK (length(username_normalized) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'error')),
                error_message TEXT,
                provider TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id) ON DELETE CASCADE,
                UNIQUE (conversation_id, sequence_no),
                CHECK (status != 'pending' OR role = 'assistant')
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Add the local-only product, recommendation and simulated-order records.

        The migration is intentionally additive. ``initialize`` executes it in
        the same immediate transaction as the version change, so an interrupted
        upgrade cannot leave a database reporting v3 with only some v3 tables.
        Monetary values are integer CNY cents throughout; no payment or shipping
        credentials are represented by this schema.
        """

        statements = (
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                brand TEXT,
                specification TEXT,
                unit TEXT,
                image_path TEXT,
                description TEXT,
                price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                source_type TEXT NOT NULL DEFAULT 'self_operated'
                    CHECK (source_type IN ('self_operated', 'third_party')),
                is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (length(trim(sku)) > 0),
                CHECK (length(trim(name)) > 0),
                CHECK (length(trim(category)) > 0),
                CHECK (image_path IS NULL OR (
                    length(trim(image_path)) > 0
                    AND substr(image_path, 1, 1) NOT IN ('/', '\\')
                    AND instr(image_path, '..') = 0
                ))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS product_recommendations (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL,
                member_user_id INTEGER NOT NULL,
                advisor_user_id INTEGER,
                reason TEXT NOT NULL,
                recommendation_source TEXT NOT NULL DEFAULT 'advisor'
                    CHECK (recommendation_source IN ('advisor', 'catalogue')),
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'viewed', 'interested')),
                viewed_at TEXT,
                interested_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT,
                FOREIGN KEY (member_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (length(trim(reason)) > 0),
                CHECK (
                    (recommendation_source = 'advisor' AND advisor_user_id IS NOT NULL)
                    OR (recommendation_source = 'catalogue' AND advisor_user_id IS NULL)
                ),
                CHECK (status != 'viewed' OR viewed_at IS NOT NULL),
                CHECK (status != 'interested' OR interested_at IS NOT NULL)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                order_no TEXT NOT NULL UNIQUE,
                member_user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                recommendation_id INTEGER,
                reordered_from_order_id INTEGER,
                product_name_snapshot TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
                total_amount_cents INTEGER NOT NULL CHECK (
                    total_amount_cents >= 0
                    AND total_amount_cents = unit_price_cents * quantity
                ),
                currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
                status TEXT NOT NULL DEFAULT 'created'
                    CHECK (status IN ('created', 'delivered')),
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES users(id) ON DELETE RESTRICT,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT,
                FOREIGN KEY (recommendation_id)
                    REFERENCES product_recommendations(id) ON DELETE SET NULL,
                FOREIGN KEY (reordered_from_order_id) REFERENCES orders(id) ON DELETE SET NULL,
                CHECK (length(trim(order_no)) > 0),
                CHECK (length(trim(product_name_snapshot)) > 0),
                CHECK (status != 'delivered' OR delivered_at IS NOT NULL)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_products_active_category
            ON products(is_active, category, name)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_recommendations_member_status
            ON product_recommendations(member_user_id, status, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_recommendations_advisor_date
            ON product_recommendations(advisor_user_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_recommendations_product
            ON product_recommendations(product_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_catalogue_interest_member_product
            ON product_recommendations(member_user_id, product_id)
            WHERE recommendation_source = 'catalogue'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_orders_member_date
            ON orders(member_user_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_orders_status_date
            ON orders(status, created_at)
            """,
            "CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product_id)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Store user documents inside SQLite and enrich local reminder/report state.

        Keeping the binary payload in a child table means a normal SQLite snapshot
        and the existing data-only ZIP automatically carry every uploaded image and
        PDF to another device.  The migration is additive and therefore preserves
        all v1-v3 account, chat, health and commerce rows.
        """

        reminder_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(reminders)")
        }
        if "repeat_rule" not in reminder_columns:
            connection.execute(
                """
                ALTER TABLE reminders ADD COLUMN repeat_rule TEXT NOT NULL DEFAULT 'none'
                CHECK (repeat_rule IN ('none', 'daily', 'weekly', 'monthly'))
                """
            )
        if "source" not in reminder_columns:
            connection.execute(
                """
                ALTER TABLE reminders ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual', 'ai_confirmed', 'advisor'))
                """
            )
        if "paused_until" not in reminder_columns:
            connection.execute("ALTER TABLE reminders ADD COLUMN paused_until TEXT")

        report_item_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(report_items)")
        }
        if "status" not in report_item_columns:
            connection.execute(
                """
                ALTER TABLE report_items ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'
                CHECK (status IN ('draft', 'confirmed'))
                """
            )
        if "updated_at" not in report_item_columns:
            connection.execute(
                """
                ALTER TABLE report_items ADD COLUMN updated_at TEXT NOT NULL
                DEFAULT '1970-01-01T00:00:00.000000Z'
                """
            )
            connection.execute("UPDATE report_items SET updated_at = created_at")

        statements = (
            """
            CREATE TABLE IF NOT EXISTS user_file_contents (
                file_id INTEGER PRIMARY KEY,
                content BLOB NOT NULL,
                mime_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES user_files(id) ON DELETE CASCADE,
                CHECK (length(content) > 0 AND length(content) <= 15728640),
                CHECK (length(trim(mime_type)) > 0),
                CHECK (length(sha256) = 64 AND sha256 = lower(sha256))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_user_file_contents_sha256
            ON user_file_contents(sha256)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Keep conversation IDs and children while removing the one-chat limit."""

        connection.execute(
            """
            CREATE TABLE conversations_v5 (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("INSERT INTO conversations_v5 SELECT * FROM conversations")
        connection.execute("DROP TABLE conversations")
        connection.execute("ALTER TABLE conversations_v5 RENAME TO conversations")
        connection.execute(
            "CREATE INDEX idx_conversations_user_updated "
            "ON conversations(user_id, updated_at DESC, id DESC)"
        )
        connection.execute(
            """
            CREATE TABLE chat_attachments (
                id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                content BLOB NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                extraction_method TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                CHECK (length(content) > 0 AND length(content) <= 10485760),
                CHECK (length(original_name) BETWEEN 1 AND 255),
                CHECK (length(extracted_text) <= 50000),
                CHECK (length(sha256) = 64)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_chat_attachments_message ON chat_attachments(message_id, id)"
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Upgrade schema v1 without rebuilding or deleting existing account/chat rows.

        SQLite supports the DDL below transactionally. ``initialize`` wraps this
        method in ``BEGIN IMMEDIATE``, so a failed migration leaves the original
        v1 database usable rather than partially upgraded.
        """

        user_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "role_code" not in user_columns:
            connection.execute(
                """
                ALTER TABLE users ADD COLUMN role_code TEXT NOT NULL DEFAULT 'member'
                CHECK (role_code IN ('member', 'advisor', 'operator'))
                """
            )
        if "account_status" not in user_columns:
            connection.execute(
                """
                ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'
                CHECK (account_status IN ('active', 'disabled', 'locked'))
                """
            )
        if "updated_at" not in user_columns:
            connection.execute(
                """
                ALTER TABLE users ADD COLUMN updated_at TEXT NOT NULL
                DEFAULT '1970-01-01T00:00:00.000000Z'
                """
            )
            connection.execute("UPDATE users SET updated_at = created_at")

        statements = (
            """
            CREATE TABLE IF NOT EXISTS member_profiles (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT,
                birth_date TEXT,
                gender TEXT CHECK (
                    gender IS NULL OR gender IN ('female', 'male', 'other', 'unspecified')
                ),
                phone TEXT,
                living_situation TEXT,
                emergency_contact_name TEXT,
                emergency_contact_phone TEXT,
                ai_preferred_name TEXT,
                reminder_frequency TEXT DEFAULT 'normal' CHECK (
                    reminder_frequency IS NULL
                    OR reminder_frequency IN ('normal', 'low', 'off')
                ),
                height_cm REAL CHECK (height_cm IS NULL OR (height_cm >= 30 AND height_cm <= 300)),
                health_goals TEXT,
                dietary_preferences TEXT,
                medical_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS profile_facts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                fact_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('draft', 'confirmed', 'superseded')),
                effective_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (length(trim(fact_key)) > 0),
                CHECK (length(trim(value_json)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS life_records (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('diet', 'water', 'activity', 'sleep', 'environment')),
                occurred_at TEXT NOT NULL,
                local_date TEXT NOT NULL,
                timezone_offset_minutes INTEGER NOT NULL
                    CHECK (timezone_offset_minutes BETWEEN -840 AND 840),
                content TEXT NOT NULL,
                details_json TEXT,
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual', 'import', 'device', 'ai_confirmed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (length(trim(content)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                reminder_type TEXT NOT NULL DEFAULT 'custom',
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'completed', 'disabled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (length(trim(title)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS advisor_profiles (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                organization TEXT,
                specialty TEXT,
                bio TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS advisor_bindings (
                id INTEGER PRIMARY KEY,
                member_user_id INTEGER NOT NULL,
                advisor_user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('pending', 'active', 'ended')),
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (member_user_id != advisor_user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memberships (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('pending', 'active', 'expired', 'cancelled')),
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                benefits_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (length(trim(plan_code)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS visit_tasks (
                id INTEGER PRIMARY KEY,
                member_user_id INTEGER NOT NULL,
                advisor_user_id INTEGER,
                title TEXT NOT NULL,
                scheduled_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (length(trim(title)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS visit_records (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                member_user_id INTEGER NOT NULL,
                advisor_user_id INTEGER,
                visited_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES visit_tasks(id) ON DELETE SET NULL,
                FOREIGN KEY (member_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (length(trim(summary)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_files (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                relative_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                media_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
                sha256 TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (length(trim(relative_path)) > 0),
                CHECK (substr(relative_path, 1, 1) NOT IN ('/', '\\')),
                CHECK (instr(relative_path, '..') = 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS medical_reports (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                file_id INTEGER,
                report_type TEXT NOT NULL DEFAULT 'other',
                report_date TEXT,
                institution TEXT,
                summary TEXT,
                status TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK (status IN ('draft', 'confirmed', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES user_files(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS report_items (
                id INTEGER PRIMARY KEY,
                report_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                result_value TEXT,
                unit TEXT,
                reference_range TEXT,
                flag TEXT CHECK (flag IS NULL OR flag IN ('low', 'normal', 'high', 'unknown')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES medical_reports(id) ON DELETE CASCADE,
                CHECK (length(trim(item_name)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ai_insights (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                insight_type TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_json TEXT,
                provider TEXT,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'shown', 'accepted', 'dismissed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                CHECK (length(trim(content)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS advisor_summaries (
                id INTEGER PRIMARY KEY,
                member_user_id INTEGER NOT NULL,
                advisor_user_id INTEGER,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('manual', 'ai_draft', 'ai_confirmed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (member_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (advisor_user_id) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (period_start <= period_end),
                CHECK (length(trim(content)) > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                setting_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY,
                actor_user_id INTEGER,
                action TEXT NOT NULL,
                entity_type TEXT,
                entity_id INTEGER,
                details_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (length(trim(action)) > 0)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_users_role_status ON users(role_code, account_status)",
            """
            CREATE INDEX IF NOT EXISTS idx_profile_facts_user_status
            ON profile_facts(user_id, status, fact_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_life_records_user_date
            ON life_records(user_id, local_date DESC, occurred_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_life_records_user_category
            ON life_records(user_id, category, occurred_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_reminders_user_status_time
            ON reminders(user_id, status, scheduled_at)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_advisor_binding
            ON advisor_bindings(member_user_id) WHERE status = 'active'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_advisor_bindings_advisor_status
            ON advisor_bindings(advisor_user_id, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_memberships_user_status
            ON memberships(user_id, status, ends_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_visit_tasks_member_status
            ON visit_tasks(member_user_id, status, scheduled_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_visit_tasks_advisor_status
            ON visit_tasks(advisor_user_id, status, scheduled_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_visit_records_member_date
            ON visit_records(member_user_id, visited_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_user_files_user_date
            ON user_files(user_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_medical_reports_user_date
            ON medical_reports(user_id, report_date DESC)
            """,
            "CREATE INDEX IF NOT EXISTS idx_report_items_report ON report_items(report_id)",
            """
            CREATE INDEX IF NOT EXISTS idx_ai_insights_user_status
            ON ai_insights(user_id, status, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_advisor_summaries_member_period
            ON advisor_summaries(member_user_id, period_end DESC)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_app_settings_scope_key
            ON app_settings(COALESCE(user_id, 0), setting_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_date
            ON audit_logs(actor_user_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
            ON messages(conversation_id, created_at)
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _create_schema_v2(connection: sqlite3.Connection) -> None:
        """Create the historical v2 schema in an empty connection.

        Kept for backup compatibility and migration tests. New empty databases
        should use :meth:`_create_schema_v3`.
        """

        Database._create_schema_v1(connection)
        Database._migrate_v1_to_v2(connection)
        connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _create_schema_v3(connection: sqlite3.Connection) -> None:
        """Create the historical v3 schema in an empty connection."""

        Database._create_schema_v2(connection)
        Database._migrate_v2_to_v3(connection)
        connection.execute("PRAGMA user_version = 3")

    @staticmethod
    def _create_schema_v4(connection: sqlite3.Connection) -> None:
        """Create the historical v4 schema in an empty connection."""

        Database._create_schema_v3(connection)
        Database._migrate_v3_to_v4(connection)
        connection.execute("PRAGMA user_version = 4")

    @staticmethod
    def _create_schema_v5(connection: sqlite3.Connection) -> None:
        """Create the current schema in an empty connection."""

        Database._create_schema_v4(connection)
        Database._migrate_v4_to_v5(connection)
        connection.execute("PRAGMA user_version = 5")


__all__ = [
    "Conversation",
    "DEFAULT_CONVERSATION_TITLE",
    "Database",
    "DatabaseError",
    "Message",
    "SCHEMA_VERSION",
    "UnsupportedSchemaVersionError",
    "User",
    "conversation_from_row",
    "message_from_row",
    "timestamp_from_db",
    "timestamp_to_db",
    "user_from_row",
    "utc_now",
]
