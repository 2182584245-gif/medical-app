"""Safe offline metadata, bounded by the authenticated session and staff term."""

import hashlib
import hmac
from datetime import timedelta
from uuid import UUID

from ollama_chat_app.data.database import timestamp_from_db, timestamp_to_db


def server_instance_id(pepper: str) -> str:
    digest = hmac.new(bytes.fromhex(pepper), b"medical-app-sync-instance-v1", hashlib.sha256)
    return str(UUID(digest.hexdigest()[:32]))


def make_offline_lease(connection, user_id, role_code, now, session_expiry, pepper):
    expiry = min(now + timedelta(hours=12), session_expiry)
    if role_code == "advisor":
        term = connection.execute(
            "SELECT ends_at FROM staff_account_terms WHERE user_id = ?", (user_id,)
        ).fetchone()
        if term is not None:
            expiry = min(expiry, timestamp_from_db(term["ends_at"]))
    return {"version": 1, "actor_id": user_id, "role_code": role_code,
            "server_instance_id": server_instance_id(pepper),
            "issued_at": timestamp_to_db(now), "expires_at": timestamp_to_db(expiry)}
