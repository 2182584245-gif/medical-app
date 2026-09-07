from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import local_secret_store as store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI tests")


@pytest.fixture
def synthetic_secret():
    return {
        "password": "SYNTHETIC-only-private-password-20260906",
        "host": "test.invalid",
        "中文": "只用于本机临时测试",
        "options": [True, False, None, 123, 0.75],
    }


def _write_envelope(path, ciphertext_bytes, **changes):
    envelope = {
        "version": 1,
        "protection": store.PROTECTION,
        "ciphertext": base64.b64encode(ciphertext_bytes).decode("ascii"),
    }
    envelope.update(changes)
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_real_dpapi_round_trip_is_ciphertext_only(tmp_path, synthetic_secret, capsys):
    path = tmp_path / "credentials.json"
    assert store.save_secret_payload(path, synthetic_secret) is None
    on_disk = path.read_bytes()
    envelope = json.loads(on_disk)
    assert set(envelope) == {"version", "protection", "ciphertext"}
    assert envelope["protection"] == "windows-dpapi-current-user"
    assert synthetic_secret["password"].encode() not in on_disk
    assert b"password" not in on_disk
    assert b"test.invalid" not in on_disk
    assert store.load_secret_payload(path) == synthetic_secret
    assert list(tmp_path.iterdir()) == [path]
    assert capsys.readouterr() == ("", "")


def test_default_overwrite_refused_and_explicit_replace_works(tmp_path, synthetic_secret):
    path = tmp_path / "credentials.json"
    store.save_secret_payload(path, synthetic_secret)
    original = path.read_bytes()
    with pytest.raises(store.LocalSecretStoreError, match="明确选择覆盖"):
        store.save_secret_payload(path, {"password": "changed-synthetic-secret"})
    assert path.read_bytes() == original
    replacement = {"password": "changed-synthetic-secret"}
    store.save_secret_payload(path, replacement, replace=True)
    assert store.load_secret_payload(path) == replacement


def test_exact_64_kib_plaintext_is_accepted_and_larger_rejected(tmp_path):
    path = tmp_path / "credentials.json"
    # Compact JSON framing for {"x":""} adds exactly eight UTF-8 bytes.
    maximum = {"x": "a" * (store.MAX_SECRET_BYTES - 8)}
    store.save_secret_payload(path, maximum)
    assert store.load_secret_payload(path) == maximum
    before = path.read_bytes()
    with pytest.raises(store.LocalSecretStoreError, match="64 KiB"):
        store.save_secret_payload(path, {"x": maximum["x"] + "a"}, replace=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        "text",
        {1: "bad-key"},
        {"x": object()},
        {"x": (1, 2)},
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": "\ud800"},
    ],
)
def test_strict_json_rejects_invalid_values_before_writing(tmp_path, payload):
    path = tmp_path / "credentials.json"
    with pytest.raises(store.LocalSecretStoreError):
        store.save_secret_payload(path, payload)
    assert not list(tmp_path.iterdir())


def test_recursive_and_excessively_deep_json_is_rejected(tmp_path):
    recursive = {}
    recursive["again"] = recursive
    with pytest.raises(store.LocalSecretStoreError, match="复杂"):
        store.save_secret_payload(tmp_path / "credentials.json", recursive)


def test_large_multibyte_payload_uses_utf8_bytes_not_character_count(tmp_path):
    with pytest.raises(store.LocalSecretStoreError, match="64 KiB"):
        store.save_secret_payload(tmp_path / "credentials.json", {"x": "中" * 23000})


@pytest.mark.parametrize("replace", [1, "yes", None])
def test_replace_must_be_an_explicit_boolean(tmp_path, replace):
    with pytest.raises(store.LocalSecretStoreError, match="明确"):
        store.save_secret_payload(tmp_path / "credentials.json", {}, replace=replace)


def test_ciphertext_tampering_is_rejected_without_secret_output(tmp_path, synthetic_secret, capsys):
    path = tmp_path / "credentials.json"
    store.save_secret_payload(path, synthetic_secret)
    envelope = json.loads(path.read_bytes())
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[len(ciphertext) // 2] ^= 0x55
    _write_envelope(path, ciphertext)
    with pytest.raises(store.LocalSecretStoreError, match="无法解密") as caught:
        store.load_secret_payload(path)
    assert synthetic_secret["password"] not in str(caught.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"protection": "plaintext"},
        {"ciphertext": "not base64!"},
        {"ciphertext": ""},
        {"ciphertext": 123},
        {"password": "must-not-be-read-as-plaintext"},
    ],
)
def test_unrecognized_envelope_is_rejected(tmp_path, changes):
    path = tmp_path / "credentials.json"
    _write_envelope(path, b"synthetic", **changes)
    with pytest.raises(store.LocalSecretStoreError):
        store.load_secret_payload(path)


@pytest.mark.parametrize("content", [b"", b"[1]", b"\xff", b'{"version":1,"version":1}'])
def test_bad_json_file_is_rejected(tmp_path, content):
    path = tmp_path / "credentials.json"
    path.write_bytes(content)
    with pytest.raises(store.LocalSecretStoreError):
        store.load_secret_payload(path)


@pytest.mark.parametrize("plaintext", [b"[]", b'{"x":1,"x":2}', b'{"x":NaN}'])
def test_invalid_json_inside_authentic_dpapi_ciphertext_is_rejected(tmp_path, plaintext):
    path = tmp_path / "credentials.json"
    _write_envelope(path, store._dpapi(plaintext, protect=True))
    with pytest.raises(store.LocalSecretStoreError):
        store.load_secret_payload(path)


def test_oversized_file_is_rejected_before_decryption(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_bytes(b"x" * (store.MAX_FILE_BYTES + 1))

    def should_not_decrypt(*_args, **_kwargs):
        pytest.fail("Oversized file reached DPAPI")

    monkeypatch.setattr(store, "_dpapi", should_not_decrypt)
    with pytest.raises(store.LocalSecretStoreError, match="大小"):
        store.load_secret_payload(path)


def test_missing_and_directory_targets_are_rejected(tmp_path):
    with pytest.raises(store.LocalSecretStoreError, match="未找到"):
        store.load_secret_payload(tmp_path / "missing.json")
    with pytest.raises(store.LocalSecretStoreError, match="普通文件"):
        store.save_secret_payload(tmp_path, {}, replace=True)
    with pytest.raises(store.LocalSecretStoreError, match="路径"):
        store.save_secret_payload(tmp_path / "nonexistent" / "credentials.json", {})


def test_parent_traversal_network_devices_and_alternate_streams_are_rejected(tmp_path):
    bad_paths = [
        tmp_path / ".." / "credentials.json",
        Path(r"\\test.invalid\share\credentials.json"),
        tmp_path / "file.json:stream",
        tmp_path / "CON",
    ]
    for path in bad_paths:
        with pytest.raises(store.LocalSecretStoreError):
            store.save_secret_payload(path, {})


def test_real_directory_junction_is_rejected_without_touching_target(tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    junction = tmp_path / "junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(destination)],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    try:
        with pytest.raises(store.LocalSecretStoreError, match="重解析点"):
            store.save_secret_payload(junction / "credentials.json", {})
        assert list(destination.iterdir()) == []
    finally:
        # Both targets are fresh children of this test's temporary directory;
        # rmdir removes the junction itself, never recurses into its destination.
        junction.rmdir()
    assert destination.is_dir()


def test_existing_file_reparse_attribute_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_text("test", encoding="utf-8")
    real_lstat = Path.lstat

    def fake_lstat(current):
        result = real_lstat(current)
        if current == path:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=store._REPARSE_POINT)
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(store.LocalSecretStoreError, match="重解析点"):
        store.load_secret_payload(path)
    with pytest.raises(store.LocalSecretStoreError, match="重解析点"):
        store.save_secret_payload(path, {}, replace=True)


def test_hardlinked_credential_file_is_rejected(tmp_path, synthetic_secret):
    original = tmp_path / "original.json"
    linked = tmp_path / "linked.json"
    store.save_secret_payload(original, synthetic_secret)
    os.link(original, linked)
    with pytest.raises(store.LocalSecretStoreError, match="独立"):
        store.load_secret_payload(linked)
    with pytest.raises(store.LocalSecretStoreError, match="独立"):
        store.save_secret_payload(linked, {}, replace=True)


def test_replace_failure_preserves_old_ciphertext_and_cleans_temp(
    tmp_path, synthetic_secret, monkeypatch
):
    path = tmp_path / "credentials.json"
    store.save_secret_payload(path, synthetic_secret)
    original = path.read_bytes()

    def failed_replace(*_args):
        raise OSError("SYNTHETIC-sensitive-OS-error")

    monkeypatch.setattr(store.os, "replace", failed_replace)
    with pytest.raises(store.LocalSecretStoreError, match="原子保存") as caught:
        store.save_secret_payload(path, {"password": "replacement"}, replace=True)
    assert "SYNTHETIC-sensitive-OS-error" not in str(caught.value)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    assert store.load_secret_payload(path) == synthetic_secret


def test_default_write_does_not_overwrite_a_racing_creator(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    real_rename = store.os.rename

    def race_before_rename(source, target):
        path.write_bytes(b"other-writer-ciphertext-placeholder")
        real_rename(source, target)

    monkeypatch.setattr(store.os, "rename", race_before_rename)
    with pytest.raises(store.LocalSecretStoreError, match="未覆盖"):
        store.save_secret_payload(path, {})
    assert path.read_bytes() == b"other-writer-ciphertext-placeholder"
    assert list(tmp_path.iterdir()) == [path]


def test_dpapi_unavailable_never_falls_back_to_plaintext(tmp_path, monkeypatch, capsys):
    def fail_library_load(*_args, **_kwargs):
        raise OSError("SYNTHETIC-sensitive-library-error")

    monkeypatch.setattr(store.ctypes, "WinDLL", fail_library_load)
    with pytest.raises(store.LocalSecretStoreError, match="未使用明文") as caught:
        store.save_secret_payload(tmp_path / "credentials.json", {"password": "synthetic"})
    assert "SYNTHETIC-sensitive-library-error" not in str(caught.value)
    assert not list(tmp_path.iterdir())
    assert capsys.readouterr() == ("", "")


def test_dpapi_flags_are_current_user_and_noninteractive(tmp_path, monkeypatch):
    used_flags = []

    class FailedFunction:
        def __call__(self, *_args):
            used_flags.append(_args[5])
            return False

    crypt = SimpleNamespace(CryptProtectData=FailedFunction())
    kernel = SimpleNamespace(LocalFree=lambda *_args: None)

    def fake_library(name, **_kwargs):
        return crypt if name == "crypt32.dll" else kernel

    monkeypatch.setattr(store.ctypes, "WinDLL", fake_library)
    with pytest.raises(store.LocalSecretStoreError, match="加密失败"):
        store.save_secret_payload(tmp_path / "credentials.json", {"password": "synthetic"})
    assert used_flags == [1]
    assert not (used_flags[0] & 4)  # CRYPTPROTECT_LOCAL_MACHINE is forbidden.
    assert not list(tmp_path.iterdir())


def test_non_windows_is_explicitly_rejected_before_file_access(tmp_path, monkeypatch):
    monkeypatch.setattr(store.sys, "platform", "linux")
    path = tmp_path / "credentials.json"
    with pytest.raises(store.LocalSecretStoreError, match="仅支持 Windows"):
        store.save_secret_payload(path, {})
    with pytest.raises(store.LocalSecretStoreError, match="仅支持 Windows"):
        store.load_secret_payload(path)
    assert not list(tmp_path.iterdir())
