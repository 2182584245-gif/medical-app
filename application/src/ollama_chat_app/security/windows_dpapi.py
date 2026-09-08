"""Small current-Windows-user DPAPI adapter; never falls back to plain text."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class SecretProtectionError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect_secret(value: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise SecretProtectionError("加密保存仅支持 Windows 当前用户，不会保存明文密钥。")
    buffer = ctypes.create_string_buffer(value)
    entropy_bytes = b"HealthLife/desktop-api-key/v1"
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    kernel = None
    try:
        crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
        kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_Blob),
            ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_Blob),
        ]
        function.restype = wintypes.BOOL
        if (
            not function(
                ctypes.byref(source),
                None if decrypt else "HealthLife desktop API key",
                ctypes.byref(entropy),
                None,
                None,
                1,
                ctypes.byref(output),
            )
            or not output.data
            or not 0 < output.size <= 32_768
        ):
            raise SecretProtectionError("密钥无法加密或解密，请确认当前 Windows 账户及凭据文件。")
        return ctypes.string_at(output.data, output.size)
    except SecretProtectionError:
        raise
    except Exception:
        raise SecretProtectionError("Windows 密钥保护不可用，未保存明文密钥。") from None
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if output.data and kernel is not None:
            if decrypt:
                ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))
