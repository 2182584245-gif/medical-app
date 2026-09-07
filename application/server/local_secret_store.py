"""Local migration credentials, protected by Windows current-user DPAPI.

This module is an administrator-side helper, not desktop application storage.
The file is NOT a portable credential backup: decryption normally requires the
same Windows account on the same computer. Moving app databases does not move
these credentials; enter them again on the destination. Windows domain roaming
or recovery facilities can be exceptions to DPAPI's usual machine restriction.
It does not protect against code already running as the current Windows user.

Only encrypted bytes reach temporary or destination files. There is no plaintext
fallback, no machine-wide DPAPI flag, and no secret-bearing error or logging.
Use a private, trusted local directory; do not place the file in a shared folder.
References: Microsoft CryptProtectData / CryptUnprotectData documentation.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import math
import os
import stat
import sys
import tempfile
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

MAX_SECRET_BYTES = 64 * 1024
MAX_CIPHERTEXT_BYTES = MAX_SECRET_BYTES + 4096
MAX_FILE_BYTES = 100 * 1024
PROTECTION = "windows-dpapi-current-user"
_REPARSE_POINT = 0x400
_UI_FORBIDDEN = 0x1
_ENTROPY = b"HealthLife/cloud-migration/local-secret/v1"


class LocalSecretStoreError(RuntimeError):
    """A safe user-facing failure without paths, credentials, or OS details."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise LocalSecretStoreError("此凭据存储仅支持 Windows 当前用户加密，不提供明文保存。")


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Call noninteractive DPAPI and free its allocated output on every path."""
    _require_windows()
    input_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(_ENTROPY)
    source = _DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _DataBlob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    kernel = None
    try:
        # Search only Windows System32, never a DLL in the working directory.
        crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
        kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR if protect else ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        succeeded = function(
            ctypes.byref(source),
            "HealthLife cloud credentials" if protect else None,
            ctypes.byref(entropy),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(output),
        )
        limit = MAX_CIPHERTEXT_BYTES if protect else MAX_SECRET_BYTES
        if not succeeded or not output.pbData or not 0 < output.cbData <= limit:
            raise LocalSecretStoreError(
                "Windows 凭据加密失败，未保存明文。"
                if protect
                else "凭据无法解密：文件可能已损坏，或当前 Windows 用户、设备不匹配。"
            )
        return ctypes.string_at(output.pbData, output.cbData)
    except LocalSecretStoreError:
        raise
    except Exception:
        raise LocalSecretStoreError("Windows 凭据保护不可用，未使用明文后备方式。") from None
    finally:
        ctypes.memset(input_buffer, 0, ctypes.sizeof(input_buffer))
        if output.pbData and kernel is not None:
            # Best effort for native buffers; Python caller-owned strings cannot
            # be reliably erased from process memory by this helper.
            if not protect and output.cbData:
                ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def _validate_json_value(value: Any, *, depth: int = 0, budget: list[int]) -> None:
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise LocalSecretStoreError("凭据 JSON 结构过于复杂。")
    value_type = type(value)
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise LocalSecretStoreError("凭据 JSON 的字段名称必须是字符串。")
            _validate_json_value(key, depth=depth + 1, budget=budget)
            _validate_json_value(item, depth=depth + 1, budget=budget)
    elif value_type is list:
        for item in value:
            _validate_json_value(item, depth=depth + 1, budget=budget)
    elif value_type is str:
        if len(value) > MAX_SECRET_BYTES:
            raise LocalSecretStoreError("凭据数据不得超过 64 KiB。")
    elif value_type is float:
        if not math.isfinite(value):
            raise LocalSecretStoreError("凭据 JSON 不允许无穷大或非数值。")
    elif value is not None and value_type not in (bool, int):
        raise LocalSecretStoreError("凭据包含不受支持的 JSON 数据类型。")


def _serialize_payload(payload: dict) -> bytes:
    if type(payload) is not dict:
        raise LocalSecretStoreError("凭据必须是 JSON 对象。")
    try:
        _validate_json_value(payload, budget=[10000])
        result = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except LocalSecretStoreError:
        raise
    except Exception:
        raise LocalSecretStoreError("凭据不是有效的 UTF-8 JSON 对象。") from None
    if not 0 < len(result) <= MAX_SECRET_BYTES:
        raise LocalSecretStoreError("凭据数据不得超过 64 KiB。")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LocalSecretStoreError("凭据 JSON 包含重复字段。")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise LocalSecretStoreError("凭据 JSON 包含不合法的数值。")


def _parse_object(data: bytes) -> dict:
    try:
        result = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if type(result) is not dict:
            raise LocalSecretStoreError("凭据文件必须包含 JSON 对象。")
        return result
    except LocalSecretStoreError:
        raise
    except Exception:
        raise LocalSecretStoreError("凭据文件不是有效的 UTF-8 JSON 对象。") from None


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _validate_path(path: Path, *, must_exist: bool) -> tuple[Path, bool]:
    """Inspect each lexical parent, without resolving and following links first."""
    try:
        if not isinstance(path, Path) or ".." in path.parts:
            raise LocalSecretStoreError("请使用不含上级跳转的本地凭据文件路径。")
        target = path.absolute()
        if (
            target.drive.startswith("\\")
            or not target.drive
            or any(":" in part or os.path.isreserved(part) for part in target.parts[1:])
        ):
            raise LocalSecretStoreError("凭据只能保存到普通本地磁盘文件，不能使用网络或设备路径。")
        for parent in reversed(target.parents):
            info = parent.lstat()
            if _is_reparse(info):
                raise LocalSecretStoreError("凭据路径不能包含符号链接、目录联接或重解析点。")
            if not stat.S_ISDIR(info.st_mode):
                raise LocalSecretStoreError("凭据的父路径必须是已存在的普通目录。")
        try:
            info = target.lstat()
        except FileNotFoundError:
            if must_exist:
                raise LocalSecretStoreError("未找到本机加密凭据，请先完成安全配置。") from None
            return target, False
        if _is_reparse(info):
            raise LocalSecretStoreError("凭据文件不能是符号链接或重解析点。")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LocalSecretStoreError("凭据目标必须是独立的普通文件。")
        return target, True
    except LocalSecretStoreError:
        raise
    except Exception:
        raise LocalSecretStoreError(
            "无法安全检查凭据路径；请确认本地目录已存在且可访问。"
        ) from None


def save_secret_payload(path: Path, payload: dict, *, replace: bool = False) -> None:
    """Encrypt JSON using current-user DPAPI, then atomically save ciphertext.

    The parent directory must already exist. Existing files are refused unless
    ``replace=True`` is explicit. The default uses Windows' atomic no-clobber
    rename; explicit replacement uses ``os.replace``. Encryption, validation, or
    write failure before the rename leaves the previous configuration untouched.
    """
    _require_windows()
    if type(replace) is not bool:
        raise LocalSecretStoreError("是否覆盖必须明确设置为 True 或 False。")
    target, exists = _validate_path(path, must_exist=False)
    if exists and not replace:
        raise LocalSecretStoreError("加密凭据文件已存在；如需更新，请明确选择覆盖。")
    ciphertext = _dpapi(_serialize_payload(payload), protect=True)
    envelope = json.dumps(
        {
            "version": 1,
            "protection": PROTECTION,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".health-cloud-secret-", dir=target.parent)
        temporary = Path(filename)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(envelope)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_path(target, must_exist=False)
        if replace:
            os.replace(temporary, target)
        else:
            # Unlike os.replace, os.rename on Windows never overwrites a file
            # created by another process between validation and this operation.
            os.rename(temporary, target)
        temporary = None
    except LocalSecretStoreError:
        raise
    except FileExistsError:
        raise LocalSecretStoreError("加密凭据文件已存在；未覆盖已有配置。") from None
    except Exception:
        raise LocalSecretStoreError("无法原子保存加密凭据；已有配置未被主动删除。") from None
    finally:
        if temporary is not None:
            # A failed cleanup can leave ciphertext only, never plaintext.
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def load_secret_payload(path: Path) -> dict:
    """Load and decrypt credentials locally; never print or log returned data."""
    _require_windows()
    target, _ = _validate_path(path, must_exist=True)
    try:
        before = target.lstat()
        if not 0 < before.st_size <= MAX_FILE_BYTES:
            raise LocalSecretStoreError("加密凭据文件为空或超过允许大小。")
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _is_reparse(opened) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise LocalSecretStoreError("凭据文件在读取时发生变化，已拒绝读取。")
            data = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        _validate_path(target, must_exist=True)
        if len(data) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise LocalSecretStoreError("凭据文件在读取时发生变化，已拒绝读取。")
        envelope = _parse_object(data)
        if (
            set(envelope) != {"version", "protection", "ciphertext"}
            or type(envelope["version"]) is not int
            or envelope["version"] != 1
            or envelope["protection"] != PROTECTION
            or type(envelope["ciphertext"]) is not str
        ):
            raise LocalSecretStoreError("加密凭据文件的版本或保护格式不受支持。")
        try:
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        except (ValueError, binascii.Error):
            raise LocalSecretStoreError("加密凭据文件中的密文格式无效。") from None
        if not 0 < len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
            raise LocalSecretStoreError("加密凭据文件中的密文大小无效。")
        payload = _parse_object(_dpapi(ciphertext, protect=False))
        _serialize_payload(payload)
        return payload
    except LocalSecretStoreError:
        raise
    except Exception:
        raise LocalSecretStoreError("无法安全读取本机加密凭据，请检查文件或重新配置。") from None
