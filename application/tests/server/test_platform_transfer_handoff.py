"""Synthetic key handoff only; subprocess and deployment boundaries are mocked."""

import json
import os
from types import SimpleNamespace

import pytest

from tools.aliyun_platform import receive_transfer_key as receiver


def test_receiver_is_bounded_exclusive_and_does_not_change_key(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "private_directory", lambda _expected: tmp_path)
    raw = bytes(range(32))
    result = receiver.receive(raw, "a" * 32, "b" * 64)
    assert result["database_credentials_received"] is False
    path = tmp_path / ("b" * 64 + ".key")
    assert path.read_bytes() == raw
    with pytest.raises(FileExistsError):
        receiver.receive(bytes(range(1, 33)), "a" * 32, "b" * 64)
    assert path.read_bytes() == raw
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("payload", [b"", bytes(range(33)), b"x" * 32])
def test_receiver_rejects_before_any_directory_access(payload, monkeypatch):
    monkeypatch.setattr(receiver, "private_directory", lambda _expected: pytest.fail("gate bypass"))
    with pytest.raises(receiver.DeploymentError):
        receiver.receive(payload, "a" * 32, "b" * 64)


@pytest.mark.skipif(os.name != "nt", reason="the sender is Windows-only")
def test_sender_exclusive_host_policy_key_only_stdin_and_output_allowlist(tmp_path, monkeypatch):
    from tools.aliyun_platform import handoff_transfer_key as sender

    files = [tmp_path / item for item in ("transfer.key", "ssh-key", "known-hosts", "ssh.exe")]
    for path in files:
        path.write_bytes(bytes(range(32)))

    def run(command, **options):
        assert options["input"] == bytes(range(32))
        assert command[1:3] == ["-F", "none"]
        assert "StrictHostKeyChecking=yes" in command
        assert "GlobalKnownHostsFile=NUL" in command
        assert "ProxyCommand=none" in command and "ClearAllForwardings=yes" in command
        assert str(files[0]) not in command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "transfer_key_installed",
                    "deployment_id": "a" * 32,
                    "export_sha256": "b" * 64,
                    "untrusted_extra": "MUST NOT RETURN",
                }
            ).encode(),
        )

    monkeypatch.setattr(sender.subprocess, "run", run)
    result = sender.handoff(
        host="39.106.166.15",
        deployment_id="a" * 32,
        export_sha256="b" * 64,
        key_file=files[0],
        ssh_key=files[1],
        known_hosts=files[2],
        ssh=files[3],
        confirm=True,
    )
    assert "untrusted_extra" not in result and result["database_credentials_sent"] is False
