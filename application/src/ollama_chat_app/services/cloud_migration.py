"""Reviewable first migration into an empty cloud member account, preserving the local source."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from .cloud_client import CloudAPIError
from .cloud_rpc_codec import decode_rpc, encode_rpc
from .cloud_sync import SyncIdentity
from .health import PROFILE_FIELDS

MAX_MANIFEST_BYTES = 900 * 1024
MAX_MIRRORED_LIFE_RECORDS = 1000
MAX_MIRRORED_CONVERSATIONS = 200
MAX_MIRRORED_MESSAGES = 5000
PREFERENCE_FIELDS = frozenset(
    {
        "nickname",
        "preferred_name",
        "font_size",
        "brightness",
        "theme_color",
        "timezone",
        "city",
        "latitude",
        "longitude",
    }
)
SELECTIONS = frozenset({"profile", "preferences", "life_records", "reminders", "conversations"})


class MigrationError(RuntimeError):
    pass


def _canonical(manifest):
    return json.dumps(
        encode_rpc(manifest),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_object(value, label):
    if not value:
        return {}
    result = json.loads(value)
    if type(result) is not dict:
        raise MigrationError(f"{label}不是合法对象，源文件未作任何修改")
    return result


def _capacity_error(manifest, existing_conversations=0):
    # ChatService.create_conversation creates a default conversation first when
    # the target has none. Reserve that real row, not just the imported rows.
    existing_conversations = max(existing_conversations, int(bool(manifest["conversations"])))
    limits = (
        ("生活记录", len(manifest["life_records"]), MAX_MIRRORED_LIFE_RECORDS),
        (
            "聊天对话（含目标已有空对话）",
            len(manifest["conversations"]) + existing_conversations,
            MAX_MIRRORED_CONVERSATIONS,
        ),
        (
            "聊天消息",
            sum(len(item["messages"]) for item in manifest["conversations"]),
            MAX_MIRRORED_MESSAGES,
        ),
    )
    for label, count, limit in limits:
        if count > limit:
            return (
                f"{label}共 {count} 项，超过完整镜像上限 {limit} 项。"
                "本次未上传、未截断，源数据完整保留。"
            )
    return None


@dataclass(frozen=True)
class MigrationPlan:
    manifest: dict
    manifest_hash: str
    payload_bytes: int
    counts: dict
    excluded: dict
    source_username: str
    blocked_reason: str | None = None
    resumed: bool = False


class MigrationJournal:
    """Persist filtered migration plans and stable retries; never account secrets."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS migration_sources "
                "(source_key TEXT PRIMARY KEY,source_uuid TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS migration_jobs "
                "(target_key TEXT NOT NULL,manifest_hash TEXT NOT NULL,"
                "request_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,"
                "result_json TEXT,plan_json TEXT,PRIMARY KEY(target_key,manifest_hash))"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(migration_jobs)")}
            if "plan_json" not in columns:
                connection.execute("ALTER TABLE migration_jobs ADD COLUMN plan_json TEXT")

    def source_id(self, source_path, actor_id, identity_fingerprint=""):
        source_key = hashlib.sha256(
            f"{Path(source_path).resolve()}\0{actor_id}\0{identity_fingerprint}".encode()
        ).hexdigest()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO migration_sources(source_key,source_uuid) VALUES(?,?) "
                "ON CONFLICT(source_key) DO NOTHING",
                (source_key, str(uuid4())),
            )
            return connection.execute(
                "SELECT source_uuid FROM migration_sources WHERE source_key=?", (source_key,)
            ).fetchone()[0]

    def job(self, target: SyncIdentity, plan: MigrationPlan):
        frozen_plan = json.dumps(
            encode_rpc(
                {
                    "manifest": plan.manifest,
                    "manifest_hash": plan.manifest_hash,
                    "payload_bytes": plan.payload_bytes,
                    "counts": plan.counts,
                    "excluded": plan.excluded,
                    "source_username": plan.source_username,
                }
            ),
            ensure_ascii=False,
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO migration_jobs(target_key,manifest_hash,request_id,state,plan_json) "
                "VALUES(?,?,?,'prepared',?) ON CONFLICT(target_key,manifest_hash) DO NOTHING",
                (target.cache_key, plan.manifest_hash, str(uuid4()), frozen_plan),
            )
            row = connection.execute(
                "SELECT request_id,state,result_json FROM migration_jobs "
                "WHERE target_key=? AND manifest_hash=?",
                (target.cache_key, plan.manifest_hash),
            ).fetchone()
        return {
            "request_id": row[0],
            "state": row[1],
            "result": decode_rpc(json.loads(row[2])) if row[2] else None,
        }

    def resume_pending(self, target: SyncIdentity, source_id: str):
        """Restore the exact uncertain request payload, even if the source has changed."""
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT plan_json FROM migration_jobs WHERE target_key=? "
                "AND state IN ('sending','uncertain') ORDER BY rowid DESC",
                (target.cache_key,),
            ).fetchall()
        for row in rows:
            if not row[0]:
                continue
            saved = decode_rpc(json.loads(row[0]))
            if saved["manifest"]["source_id"] != source_id:
                continue
            raw = _canonical(saved["manifest"])
            if hashlib.sha256(raw).hexdigest() != saved["manifest_hash"]:
                raise MigrationError("未完成迁移清单校验失败，请保留本机数据并联系维护人员")
            return MigrationPlan(**saved)
        return None

    def set_state(self, target, plan, state, result=None):
        if state not in {"prepared", "sending", "uncertain", "rejected", "completed"}:
            raise MigrationError("迁移任务状态无效")
        encoded = json.dumps(encode_rpc(result), ensure_ascii=False) if result is not None else None
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE migration_jobs SET state=?,result_json=? "
                "WHERE target_key=? AND manifest_hash=?",
                (state, encoded, target.cache_key, plan.manifest_hash),
            )


class MemberMigrationPlanner:
    def __init__(self, database, journal: MigrationJournal):
        self.source_path = Path(getattr(database, "path", database)).resolve()
        self.journal = journal

    def prepare_for_target(self, actor_id: int, target: SyncIdentity | None = None):
        if target is not None:
            # Reusing a path and integer id after a database replacement must not
            # resurrect another local account's unfinished migration payload.
            with sqlite3.connect(self.source_path.as_uri() + "?mode=ro", uri=True) as connection:
                actor = connection.execute(
                    "SELECT username,created_at,role_code,account_status FROM users WHERE id=?",
                    (actor_id,),
                ).fetchone()
            if actor is None or actor[2] != "member" or actor[3] != "active":
                raise MigrationError("本机来源账户未通过验证")
            fingerprint = f"{actor[0]}\0{actor[1]}"
            source_id = self.journal.source_id(self.source_path, actor_id, fingerprint)
            previous = self.journal.resume_pending(target, source_id)
            if previous is not None:
                return replace(previous, resumed=True)
        return self.build_plan(actor_id)

    def build_plan(self, actor_id: int, selection=None):
        if type(actor_id) is not int or actor_id <= 0:
            raise MigrationError("本地账户编号无效")
        selected = SELECTIONS if selection is None else frozenset(selection)
        if selected - SELECTIONS:
            raise MigrationError("选择中包含不支持迁移的数据类型")
        if not self.source_path.is_file():
            raise MigrationError("本地源数据库不存在")
        # URI mode=ro guarantees planning cannot initialize/migrate/change the source database.
        with sqlite3.connect(self.source_path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            actor = connection.execute(
                "SELECT id,username,created_at,role_code,account_status FROM users WHERE id=?",
                (actor_id,),
            ).fetchone()
            if (
                actor is None
                or actor["role_code"] != "member"
                or actor["account_status"] != "active"
            ):
                raise MigrationError("当前只支持本人有效会员账户迁入空云账户")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }
            profile = None
            if "profile" in selected:
                row = connection.execute(
                    "SELECT * FROM member_profiles WHERE user_id=?", (actor_id,)
                ).fetchone()
                if row:
                    row_values = dict(row)
                    profile = {
                        key: row[key]
                        for key in PROFILE_FIELDS
                        if key in row_values and row[key] not in (None, "")
                    }
            preferences = None
            if "preferences" in selected and "user_preferences" in tables:
                row = connection.execute(
                    "SELECT preferences_json FROM user_preferences WHERE user_id=?", (actor_id,)
                ).fetchone()
                if row:
                    preferences = {
                        key: value
                        for key, value in _json_object(row[0], "偏好设置").items()
                        if key in PREFERENCE_FIELDS
                    }
            records, reminders, conversations = [], [], []
            if "life_records" in selected:
                for row in connection.execute(
                    "SELECT id,category,occurred_at,content,details_json "
                    "FROM life_records WHERE user_id=? ORDER BY id",
                    (actor_id,),
                ):
                    records.append(
                        {
                            "source_id": str(row["id"]),
                            "values": {
                                "category": row["category"],
                                "occurred_at": row["occurred_at"],
                                "content": row["content"],
                                "details": _json_object(row["details_json"], "记录详情"),
                                "source": "import",
                            },
                        }
                    )
            if "reminders" in selected:
                for row in connection.execute(
                    "SELECT id,title,scheduled_at,reminder_type,status "
                    "FROM reminders WHERE user_id=? ORDER BY id",
                    (actor_id,),
                ):
                    reminders.append(
                        {
                            "source_id": str(row["id"]),
                            "values": {
                                "title": row["title"],
                                "scheduled_at": row["scheduled_at"],
                                "reminder_type": row["reminder_type"],
                                "enabled": row["status"] == "active",
                            },
                        }
                    )
            excluded = {
                "chat_attachments": 0,
                "user_files": 0,
                "pending_messages": 0,
                "system_messages": 0,
                "orders": 0,
                "service_assignments": 0,
            }
            if "chat_attachments" in tables:
                excluded["chat_attachments"] = connection.execute(
                    "SELECT COUNT(*) FROM chat_attachments a JOIN messages m ON a.message_id=m.id "
                    "JOIN conversations c ON m.conversation_id=c.id WHERE c.user_id=?",
                    (actor_id,),
                ).fetchone()[0]
            if "user_files" in tables:
                excluded["user_files"] = connection.execute(
                    "SELECT COUNT(*) FROM user_files WHERE user_id=?",
                    (actor_id,),
                ).fetchone()[0]
            if "orders" in tables:
                excluded["orders"] = connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE member_user_id=?",
                    (actor_id,),
                ).fetchone()[0]
            if "visit_tasks" in tables:
                excluded["service_assignments"] = connection.execute(
                    "SELECT COUNT(*) FROM visit_tasks WHERE member_user_id=?",
                    (actor_id,),
                ).fetchone()[0]
            if "conversations" in selected:
                for conversation in connection.execute(
                    "SELECT id,title FROM conversations WHERE user_id=? ORDER BY id",
                    (actor_id,),
                ).fetchall():
                    messages = []
                    for message in connection.execute(
                        "SELECT id,role,content,status,created_at,provider,model FROM messages "
                        "WHERE conversation_id=? ORDER BY sequence_no,id",
                        (conversation["id"],),
                    ):
                        if message["role"] not in {"user", "assistant"}:
                            excluded["system_messages"] += 1
                            continue
                        if message["status"] not in {"complete", "error"}:
                            excluded["pending_messages"] += 1
                            continue
                        messages.append(
                            {
                                "source_id": str(message["id"]),
                                **{
                                    key: message[key]
                                    for key in (
                                        "role",
                                        "content",
                                        "status",
                                        "created_at",
                                        "provider",
                                        "model",
                                    )
                                },
                            }
                        )
                    conversations.append(
                        {
                            "source_id": str(conversation["id"]),
                            "title": conversation["title"],
                            "messages": messages,
                        }
                    )
            manifest = {
                "schema_version": 1,
                "source_id": self.journal.source_id(
                    self.source_path, actor_id, f"{actor['username']}\0{actor['created_at']}"
                ),
                "source_actor_id": actor_id,
                "profile": profile,
                "preferences": preferences,
                "life_records": records,
                "reminders": reminders,
                "conversations": conversations,
                "excluded": excluded,
            }
            counts = {
                "profile": int(bool(profile)),
                "preferences": int(bool(preferences)),
                "life_records": len(records),
                "reminders": len(reminders),
                "conversations": len(conversations),
                "messages": sum(len(item["messages"]) for item in conversations),
            }
        raw = _canonical(manifest)
        blocked = (
            "所选记录超过 900 KiB，暂不能一次迁移；请缩小选择范围。本次未上传、未截断。"
            if len(raw) > MAX_MANIFEST_BYTES
            else None
        )
        blocked = blocked or _capacity_error(manifest)
        return MigrationPlan(
            manifest,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            counts,
            excluded,
            str(actor["username"]),
            blocked,
        )


class MemberMigrationService:
    def __init__(self, client, journal: MigrationJournal, *, sync_service=None):
        self.client, self.journal = client, journal
        if sync_service is not None and sync_service.client is not client:
            raise MigrationError("迁移与镜像必须使用同一个云端连接")
        self.sync_service = sync_service

    def _invalidate_target(self, target):
        sync = self.sync_service
        if sync is not None and sync.identity == target and sync.actor_id == target.actor_id:
            sync.invalidate_after_write()

    def execute(self, plan: MigrationPlan, target: SyncIdentity):
        if plan.blocked_reason:
            raise MigrationError(plan.blocked_reason)
        raw = _canonical(plan.manifest)
        if len(raw) > MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != plan.manifest_hash:
            raise MigrationError("迁移清单已改变，请重新生成并核对预览")
        UUID(plan.manifest["source_id"])
        if self.client.base_url.rstrip("/") != target.endpoint.rstrip("/"):
            raise MigrationError("云端目标与已确认的迁移目标不一致")
        if not self.client.is_authenticated:
            raise MigrationError("请先在线登录目标云端账户")
        if capacity_error := _capacity_error(plan.manifest):
            raise MigrationError(capacity_error)
        job = self.journal.job(target, plan)
        # Re-check both actor and server instance directly before the write.
        snapshot = decode_rpc(
            self.client.rpc("sync", "get_snapshot", encode_rpc([target.actor_id]), encode_rpc({}))
        )
        if (
            snapshot.get("actor_id") != target.actor_id
            or snapshot.get("server_instance_id") != target.server_instance_id
        ):
            raise MigrationError("云端身份或实例已经变化，请重新预览迁移目标")
        if job["state"] == "completed":
            return job["result"]
        if capacity_error := _capacity_error(
            plan.manifest, len(snapshot.get("resources", {}).get("conversations", []))
        ):
            raise MigrationError(capacity_error)
        self.journal.set_state(target, plan, "sending")
        try:
            result = decode_rpc(
                self.client.rpc(
                    "sync",
                    "import_member",
                    encode_rpc([target.actor_id, plan.manifest]),
                    encode_rpc({}),
                    request_id=job["request_id"],
                )
            )
        except Exception as error:
            uncertain = not isinstance(error, CloudAPIError) or error.outcome_uncertain
            if uncertain:
                self._invalidate_target(target)
            self.journal.set_state(target, plan, "uncertain" if uncertain else "rejected")
            raise
        # A raw import RPC bypasses SyncedRemoteService. Invalidate before the
        # local journal write too: its failure cannot make old data current.
        self._invalidate_target(target)
        self.journal.set_state(target, plan, "completed", result)
        return result
