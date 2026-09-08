"""Version-two bounded pages, deterministic resource hashes and assembly.

Budgets apply per response and independently to the encrypted local account
mirror. The 8 MiB HTTP envelope is not a whole-account database limit.
"""
from __future__ import annotations

import hashlib
import json

from .cloud_rpc_codec import decode_rpc, encode_rpc

PAGE_BYTES = 512 * 1024
PAGE_ITEMS = 100
MAX_MIRROR_BYTES = 250 * 1024 * 1024
RESOURCE_KINDS = {
    "profile": "dict", "preferences": "dict", "life_records": "list",
    "reminders": "list", "conversations": "list", "messages": "messages",
    "service_summary": "dict", "appointments": "list", "members": "list",
    "advisors": "list", "work_statistics": "dict", "user_files": "list",
    "reports": "list", "chat_attachments": "list", "visit_records": "list",
}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode()


def resource_records(name, value):
    kind = RESOURCE_KINDS[name]
    if kind == "list":
        yield from value
    elif kind == "dict":
        yield from sorted(value.items())
    else:
        for key in sorted(value, key=int):
            # The empty marker preserves conversations with no messages.
            yield [key, None]
            for item in value[key]:
                yield [key, item]


def resource_digest(name, value):
    digest, count = hashlib.sha256(), 0
    for record in resource_records(name, value):
        encoded = json_bytes(encode_rpc(record))
        if len(encoded) > PAGE_BYTES:
            raise ValueError("A sync record exceeds the per-record safety budget")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return {"kind": RESOURCE_KINDS[name], "count": count, "version": digest.hexdigest()}


def assemble_resource(name, records):
    kind = RESOURCE_KINDS[name]
    if kind == "list":
        return records
    if kind == "dict":
        result = {}
        for key, value in records:
            if type(key) is not str or key in result:
                raise ValueError("Duplicate resource key")
            result[key] = value
        return result
    result = {}
    for key, value in records:
        if type(key) is not str or not key.isdecimal():
            raise ValueError("Invalid conversation identity")
        if value is None:
            if key in result:
                raise ValueError("Duplicate conversation marker")
            result[key] = []
        else:
            result[key].append(value)
    return result


def encode_large_snapshot(snapshot):
    encoded = {key: value for key, value in snapshot.items() if key != "resources"}
    encoded["resources"] = {
        name: [encode_rpc(item) for item in resource_records(name, value)]
        for name, value in snapshot["resources"].items()
    }
    raw = json_bytes(encoded)
    if len(raw) > MAX_MIRROR_BYTES:
        raise ValueError("Encrypted account mirror exceeds its 250 MiB device safety budget")
    return encoded


def decode_large_snapshot(encoded):
    result = {key: value for key, value in encoded.items() if key != "resources"}
    result["resources"] = {
        name: assemble_resource(name, [decode_rpc(item) for item in records])
        for name, records in encoded["resources"].items()
    }
    return result


def fetch_paged_snapshot(client, actor_id):
    def rpc(method, *args):
        return decode_rpc(client.rpc("sync", method, encode_rpc([actor_id, *args]), {}))

    manifest = rpc("get_sync_manifest")
    if (type(manifest) is not dict or manifest.get("schema_version") != 2
            or manifest.get("actor_id") != actor_id
            or set(manifest.get("resources", {})) != set(RESOURCE_KINDS)):
        raise ValueError("Invalid synchronization manifest")
    resources, used = {}, 0
    for name, description in manifest["resources"].items():
        records, offset = [], 0
        if (type(description) is not dict or description.get("kind") != RESOURCE_KINDS[name]
                or type(description.get("count")) is not int or description["count"] < 0):
            raise ValueError("Invalid resource description")
        while offset < description["count"]:
            page = rpc("get_sync_page", name, description["version"], offset)
            if (set(page) != {"resource", "version", "offset", "next_offset", "items"}
                    or page["resource"] != name or page["version"] != description["version"]
                    or page["offset"] != offset or type(page["items"]) is not list
                    or not 1 <= len(page["items"]) <= PAGE_ITEMS
                    or page["next_offset"] != offset + len(page["items"])
                    or page["next_offset"] > description["count"]):
                raise ValueError("Invalid sync page sequence")
            used += sum(len(json_bytes(encode_rpc(item))) for item in page["items"])
            if used > MAX_MIRROR_BYTES:
                raise ValueError("Device mirror safety budget exceeded")
            records.extend(page["items"])
            offset = page["next_offset"]
        resources[name] = assemble_resource(name, records)
        if resource_digest(name, resources[name]) != description:
            raise ValueError("Synchronization resource integrity mismatch")
    # Detect changes across resources/pages, including permission-scope changes.
    if rpc("get_sync_manifest") != manifest:
        from .cloud_client import CloudAPIError

        raise CloudAPIError("conflict")
    return {"schema_version": 2, "actor_id": actor_id,
            "server_instance_id": manifest["server_instance_id"], "revision": manifest["revision"],
            "complete": True, "resources": resources, "excluded": {}}
