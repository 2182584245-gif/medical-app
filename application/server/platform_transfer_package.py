"""Authenticated administrator packages. No plaintext or credential fallback.

AES-256-GCM requires a separately generated 32-byte key. On Linux its directory
and file must be root-owned/private. Windows can instead use current-user DPAPI.
Neither envelope contains its encryption key, DB connection settings or API keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from pathlib import Path

from .platform_transfer import MAX_BYTES, TransferError, deserialize, digest, serialize

MAGIC = b"HEALTHLIFE-TRANSFER-1\n"
CHUNK = 8 * 1024 * 1024


def safe_path(path: Path, *, exists: bool) -> Path:
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise TransferError("迁移文件及父目录不允许链接或重解析点。")
    if path.exists() != exists or (exists and not path.is_file()):
        raise TransferError("迁移文件不存在或目标已存在；禁止覆盖。")
    if not path.parent.is_dir():
        raise TransferError("请先明确创建专用管理员工作目录。")
    return path


def _private_linux(path: Path):
    if os.name == "posix":
        for item in (path, path.parent):
            info = item.stat()
            if os.geteuid() != 0 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
                raise TransferError("Linux 加密密钥及其目录必须归 root 所有且禁止组/其他用户访问。")


def generate_key(path: Path) -> dict:
    path = safe_path(path, exists=False)
    if os.name == "posix":
        info = path.parent.stat()
        if os.geteuid() != 0 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise TransferError("Linux 加密密钥目录必须归 root 所有且仅 root 可访问。")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    return {"status": "key_created", "bytes": 32, "key_content_printed": False}


def read_key(path: Path) -> bytes:
    path = safe_path(path, exists=True)
    _private_linux(path)
    if path.stat().st_size != 32:
        raise TransferError(
            "迁移加密 key 必须为独立生成的 32 字节文件，不能使用数据库密码或 pepper。"
        )
    value = path.read_bytes()
    if len(value) != 32 or len(set(value)) < 16:
        raise TransferError("迁移加密 key 的随机性检查失败。")
    return value


def _context(context):
    if set(context) != {"purpose", "plan_sha256", "source_sha256", "target_sha256"}:
        raise TransferError("加密包缺少计划身份绑定。")
    if context["purpose"] not in {"source", "target-backup", "transfer-state"}:
        raise TransferError("未知迁移包用途。")
    if any(
        len(context[name]) != 64 or any(c not in "0123456789abcdef" for c in context[name])
        for name in ("plan_sha256", "source_sha256", "target_sha256")
    ):
        raise TransferError("迁移计划内容指纹无效。")
    return {"format": 1, "business_schema": "sqlite6-platform2", **context}


def package_context(plan, purpose):
    return {
        "purpose": purpose,
        **{
            name: plan[name]
            for name in (
                "plan_sha256",
                "source_sha256",
                "target_sha256",
            )
        },
    }


def save_package(value, destination: Path, *, context: dict, key: bytes | None = None):
    target = safe_path(destination, exists=False)
    header = _context(context)
    header["cipher"] = "aes-256-gcm" if key is not None else "windows-dpapi"
    if key is not None and (len(key) != 32 or len(set(key)) < 16):
        raise TransferError("AES-GCM 需要独立随机的 32 字节密钥。")
    plaintext = serialize(value)
    header["payload_sha256"] = hashlib.sha256(plaintext).hexdigest()
    header["chunks"] = max(1, (len(plaintext) + CHUNK - 1) // CHUNK)
    encoded_header = serialize(header)
    if key is not None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        cipher = AESGCM(key)
    with target.open("xb") as stream:
        if os.name == "posix":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(MAGIC + struct.pack(">I", len(encoded_header)) + encoded_header)
        for index in range(header["chunks"]):
            part = plaintext[index * CHUNK : (index + 1) * CHUNK]
            associated = encoded_header + struct.pack(">I", index)
            if key is not None:
                nonce = os.urandom(12)
                encrypted = nonce + cipher.encrypt(nonce, part, associated)
            else:
                from .platform_backup import _protect

                encrypted = _protect(hashlib.sha256(associated).digest() + part)
            stream.write(struct.pack(">I", len(encrypted)) + encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    # Mandatory authentication/readback before treating this file as a backup.
    if digest(load_package(target, context=context, key=key)) != digest(value):
        raise TransferError("加密迁移包回读验证失败。")


def load_package(source: Path, *, context: dict, key: bytes | None = None):
    path = safe_path(source, exists=True)
    expected = _context(context)
    if path.stat().st_size > MAX_BYTES + 1_000_000:
        raise TransferError("加密包超过上限。")
    try:
        with path.open("rb") as stream:
            if stream.read(len(MAGIC)) != MAGIC:
                raise ValueError
            length = struct.unpack(">I", stream.read(4))[0]
            if not 1 <= length <= 4096:
                raise ValueError
            encoded_header = stream.read(length)
            header = json.loads(encoded_header)
            if any(header.get(name) != value for name, value in expected.items()):
                raise ValueError
            if header.get("cipher") != ("aes-256-gcm" if key is not None else "windows-dpapi"):
                raise ValueError
            count = header["chunks"]
            if type(count) is not int or not 1 <= count <= MAX_BYTES // CHUNK + 1:
                raise ValueError
            if key is not None:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                cipher = AESGCM(key)
            pieces = []
            total = 0
            for index in range(count):
                size = struct.unpack(">I", stream.read(4))[0]
                if not 16 <= size <= CHUNK + 32768:
                    raise ValueError
                encrypted = stream.read(size)
                associated = encoded_header + struct.pack(">I", index)
                if key is not None:
                    part = cipher.decrypt(encrypted[:12], encrypted[12:], associated)
                else:
                    from .platform_backup import _protect

                    clear = _protect(encrypted, decrypt=True)
                    if clear[:32] != hashlib.sha256(associated).digest():
                        raise ValueError
                    part = clear[32:]
                total += len(part)
                if total > MAX_BYTES:
                    raise ValueError
                pieces.append(part)
            if stream.read(1):
                raise ValueError
        plaintext = b"".join(pieces)
        if hashlib.sha256(plaintext).hexdigest() != header["payload_sha256"]:
            raise ValueError
        return deserialize(plaintext)
    except Exception:
        raise TransferError("加密包认证、计划身份或完整性检查失败，未解密输出数据。") from None
