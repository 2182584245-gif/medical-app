"""Real desktop planner -> authenticated dispatcher -> real mirror, synthetic databases only."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_test_support import PEPPER, SyntheticDatabase

pytest.importorskip(
    "PySide6", reason="Desktop-to-server integration requires the desktop test runtime"
)

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.cloud_migration import (
    MemberMigrationPlanner,
    MemberMigrationService,
    MigrationJournal,
)
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.cloud_sync import CloudMirrorStore, SyncIdentity
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.services.service_management import ServiceManagementService
from server.platform_rpc import RpcDispatcher
from server.platform_sync import PlatformSyncService


def test_real_planner_dispatcher_migration_id_map_and_snapshot_roundtrip(tmp_path):
    local = Database(tmp_path / "source-synthetic.db")
    member = AuthService(local).register("source-person", "synthetic-password-123")
    health = HealthService(local)
    source_record = health.add_life_record(
        member.id, "water", "2026-09-08T09:30:00+08:00", "本人合成饮水", details={"amount_ml": 300}
    )
    health.add_reminder(member.id, "合成生活提醒", "2026-09-09T18:30:00+08:00")
    chat = ChatService(local)
    exchange = chat.begin_message(member.id, "本机合成提问", "synthetic", "test-model")
    chat.complete_message(member.id, exchange.assistant_message.id, "本机合成回答")
    source_bytes = local.path.read_bytes()
    journal = MigrationJournal(tmp_path / "migration-journal.sqlite")
    plan = MemberMigrationPlanner(local, journal).build_plan(member.id)
    cloud = SyntheticDatabase()
    try:
        other = cloud.account("cloud-other")
        target_actor = cloud.account("cloud-target")
        services = {
            "chat": ChatService(cloud),
            "health": HealthService(cloud),
            "preferences": PreferencesService(cloud),
            "service_management": ServiceManagementService(cloud),
        }
        services["health"].add_life_record(other, "water", "2026-09-08T09:00:00+08:00", "其他人")
        sync = PlatformSyncService(cloud, services, PEPPER)
        services["sync"] = sync
        dispatcher = RpcDispatcher(cloud, services)

        class Client:
            base_url = "https://example.com/aliyun"
            is_authenticated = True

            def rpc(self, service, method, args, kwargs, request_id=None):
                return dispatcher.invoke(
                    target_actor,
                    service,
                    method,
                    {
                        "args": args,
                        "kwargs": kwargs,
                        "request_id": request_id or str(uuid4()),
                    },
                )["result"]

        client = Client()
        target = SyncIdentity(client.base_url, sync.instance_id, target_actor)
        migration = MemberMigrationService(client, journal)
        result = migration.execute(plan, target)
        assert result["source_preserved"] is True
        assert migration.execute(plan, target) == result
        assert local.path.read_bytes() == source_bytes
        mapped = result["id_mapping"]["life_records"][str(source_record)]
        assert mapped != source_record
        assert (
            cloud.scalar("SELECT user_id FROM life_records WHERE id=?", (mapped,)) == target_actor
        )
        assert cloud.scalar("SELECT COUNT(*) FROM messages") == 2
        snapshot = decode_rpc(
            client.rpc("sync", "get_snapshot", encode_rpc([target_actor]), encode_rpc({}))
        )
        mirror = CloudMirrorStore(tmp_path / "mirror")
        mirror.activate(target)
        mirror.apply_snapshot(snapshot)
        assert mirror.read_resource("life_records")[0]["content"] == "本人合成饮水"
        assert mirror.snapshot()["excluded"]["chat_attachments"] == 0
    finally:
        cloud.close()
