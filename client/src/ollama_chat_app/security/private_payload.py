"""Current-Windows-user DPAPI for private offline state; no plaintext fallback."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

MAX_PRIVATE_BYTES = 256 * 1024 * 1024


class PrivateStateError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect_payload(data: bytes, purpose: str, *, decrypt=False) -> bytes:
    if sys.platform != "win32" or not 0 < len(data) <= MAX_PRIVATE_BYTES:
        raise PrivateStateError("离线私有数据需要 Windows 当前用户加密保护。")
    entropy_bytes = hashlib.sha256(("HealthLife/offline/v1/" + purpose).encode()).digest()
    buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    kernel = None
    try:
        crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
        kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob),
                             ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                             ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(_Blob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(source), None if decrypt else "HealthLife offline state",
                        ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
            raise PrivateStateError("离线数据无法解密或保存，请重新在线登录。")
        if not output.data or not 0 < output.size <= MAX_PRIVATE_BYTES:
            raise PrivateStateError("离线数据大小无效。")
        return ctypes.string_at(output.data, output.size)
    except PrivateStateError:
        raise
    except Exception:
        raise PrivateStateError("Windows 数据保护不可用；没有使用明文后备。") from None
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if output.data and kernel is not None:
            if decrypt:
                ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def _safe_path(path: Path) -> Path:
    path = Path(path).absolute()
    if ".." in path.parts or str(path).startswith("\\\\"):
        raise PrivateStateError("离线状态只能保存在本机普通目录。")
    for part in path.parts[1:]:
        if ":" in part or part.endswith((".", " ")):
            raise PrivateStateError("离线状态路径不能使用设备流或歧义名称。")
    for item in (*reversed(path.parents), path):
        if not item.exists():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PrivateStateError("离线状态路径不能包含重解析点。")
        if item == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise PrivateStateError("离线状态文件不能使用硬链接。")
    return path


def read_private_json(path: Path, purpose: str):
    path = _safe_path(path)
    if not path.exists():
        return None
    try:
        if not 0 < path.stat().st_size <= MAX_PRIVATE_BYTES:
            raise ValueError
        raw = path.read_bytes()
        return json.loads(protect_payload(raw, purpose, decrypt=True))
    except Exception:
        raise PrivateStateError("离线状态不可用或已损坏，请重新在线登录。") from None


def write_private_json(path: Path, purpose: str, value) -> None:
    path = _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(path)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    ciphertext = protect_payload(raw, purpose)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".encrypted-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)  # Ciphertext only, our exact temporary file.
