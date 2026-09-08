from __future__ import annotations

from uuid import uuid4

import pytest
from platform_test_support import PEPPER, SyntheticDatabase

from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.services.service_management import ServiceManagementService
from server.platform_rpc import RpcDispatcher, RpcError
from server.platform_sync import EXCLUDED_FIELDS, PlatformSyncService


def manifest(*, records=0, conversations=0, messages=0):
    return {
        "schema_version": 1,
        "source_id": str(uuid4()),
        "source_actor_id": 1,
        "profile": None,
        "preferences": None,
        "life_records": [
            {
                "source_id": str(index + 1),
                "values": {
                    "category": "water",
                    "occurred_at": "2026-09-08T08:00:00Z",
                    "content": "x",
                    "details": {},
                    "source": "import",
                },
            }
            for index in range(records)
        ],
        "reminders": [],
        "conversations": [
            {
                "source_id": str(index + 1),
                "title": "x",
                "messages": [
                    {
                        "source_id": str(item + 1),
                        "role": "user",
                        "content": "",
                        "status": "complete",
                        "created_at": "2026-09-08T08:00:00Z",
                        "provider": None,
                        "model": None,
                    }
                    for item in range(messages if index == 0 else 0)
                ],
            }
            for index in range(conversations)
        ],
        "excluded": dict.fromkeys(EXCLUDED_FIELDS, 0),
    }


@pytest.mark.parametrize(
    "counts", [{"records": 1001}, {"conversations": 200}, {"conversations": 1, "messages": 5001}]
)
def test_import_rejects_count_that_cannot_be_completely_mirrored(counts):
    with pytest.raises(RpcError) as error:
        PlatformSyncService._validate_manifest(manifest(**counts))
    assert error.value.status == 413
    assert error.value.code == "migration_exceeds_snapshot_capacity"


@pytest.mark.parametrize(
    "counts", [{"records": 1000}, {"conversations": 199}, {"conversations": 1, "messages": 5000}]
)
def test_import_exact_capacity_including_implicit_default_is_valid(counts):
    PlatformSyncService._validate_manifest(manifest(**counts))


def test_existing_empty_conversations_count_toward_limit_and_rejection_is_atomic():
    database = SyntheticDatabase()
    try:
        actor = database.account("synthetic-capacity-target")
        services = {
            "chat": ChatService(database),
            "health": HealthService(database),
            "preferences": PreferencesService(database),
            "service_management": ServiceManagementService(database),
        }
        services["sync"] = PlatformSyncService(database, services, PEPPER)
        dispatcher = RpcDispatcher(database, services)
        # First create also creates a default, so the target now has 199 empty conversations.
        for _ in range(198):
            services["chat"].create_conversation(actor, "existing")
        assert (
            database.scalar("SELECT COUNT(*) FROM conversations WHERE user_id=?", (actor,)) == 199
        )
        source = manifest(records=1, conversations=2)
        with pytest.raises(RpcError) as error:
            dispatcher.invoke(
                actor,
                "sync",
                "import_member",
                {
                    "args": encode_rpc([actor, source]),
                    "kwargs": {},
                    "request_id": str(uuid4()),
                },
            )
        assert error.value.code == "migration_exceeds_snapshot_capacity"
        assert database.scalar("SELECT COUNT(*) FROM life_records WHERE user_id=?", (actor,)) == 0
        assert (
            database.scalar("SELECT COUNT(*) FROM conversations WHERE user_id=?", (actor,)) == 199
        )
        assert database.scalar("SELECT COUNT(*) FROM rpc_requests") == 0
    finally:
        database.close()
