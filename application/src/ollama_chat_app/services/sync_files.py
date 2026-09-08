"""Identity-bound, encrypted attachment cache. No original-name filesystem paths."""
from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path

from ..security.private_payload import read_private_json, write_private_json
from .cloud_rpc_codec import decode_rpc, encode_rpc
from .sync_protocol import MAX_MIRROR_BYTES

CHUNK_BYTES = 64 * 1024
MAX_FILE_BYTES = 15 * 1024 * 1024


class SyncFileCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _key(identity, kind, item):
        if (kind not in {"user_files", "chat_attachments"}
                or type(item.get("id")) is not int or item["id"] <= 0
                or type(item.get("size_bytes")) is not int
                or not 0 < item["size_bytes"] <= MAX_FILE_BYTES
                or type(item.get("sha256")) is not str or len(item["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in item["sha256"])):
            raise ValueError("Invalid attachment metadata")
        return hashlib.sha256((identity.cache_key + "/" + kind + "/" + str(item["id"])
                               + "/" + item["sha256"]).encode()).hexdigest()

    def read(self, identity, kind, item):
        key = self._key(identity, kind, item)
        value = read_private_json(self.root / identity.cache_key / (key + ".attachment"),
                                  "attachment/" + key)
        if value is None:
            return None
        content = base64.b64decode(value["content"], validate=True)
        if (len(content) != item["size_bytes"]
                or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), item["sha256"])):
            raise ValueError("Encrypted attachment integrity mismatch")
        return content

    def fetch(self, client, identity, resources):
        total = sum(item["size_bytes"] for kind in ("user_files", "chat_attachments")
                    for item in resources[kind])
        if total > MAX_MIRROR_BYTES:
            raise ValueError("Attachment cache exceeds its 250 MiB device safety budget")
        for kind in ("user_files", "chat_attachments"):
            for item in resources[kind]:
                key = self._key(identity, kind, item)
                if self.read(identity, kind, item) is not None:
                    continue
                buffer, offset = bytearray(), 0
                while offset < item["size_bytes"]:
                    response = decode_rpc(client.rpc(
                        "sync", "get_file_chunk", encode_rpc([
                            identity.actor_id, kind, item["id"], item["sha256"], offset]), {},
                    ))
                    block = response.get("content")
                    if (set(response) != {"kind", "id", "sha256", "size_bytes", "offset",
                                          "next_offset", "content"}
                            or response["kind"] != kind or response["id"] != item["id"]
                            or response["sha256"] != item["sha256"]
                            or response["size_bytes"] != item["size_bytes"]
                            or response["offset"] != offset or type(block) is not bytes
                            or len(block) != min(CHUNK_BYTES, item["size_bytes"] - offset)
                            or response["next_offset"] != offset + len(block)):
                        raise ValueError("Attachment chunk sequence invalid")
                    buffer.extend(block)
                    offset += len(block)
                if not hmac.compare_digest(hashlib.sha256(buffer).hexdigest(), item["sha256"]):
                    raise ValueError("Attachment content hash invalid")
                write_private_json(self.root / identity.cache_key / (key + ".attachment"),
                                   "attachment/" + key,
                                   {"content": base64.b64encode(buffer).decode("ascii")})
                buffer[:] = b"\0" * len(buffer)
