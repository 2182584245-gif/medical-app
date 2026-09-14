"""Retired runtime paths reject before credentials, stdin or network access."""

import subprocess
import sys
from pathlib import Path

import pytest

from tools.aliyun_platform import configure, handoff_runtime, receive_runtime
from tools.aliyun_platform.guard import DeploymentError


def test_retired_handoff_never_opens_profile_key_or_network(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Retired runtime path performed I/O")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(DeploymentError, match="retired"):
        handoff_runtime.handoff(
            host="synthetic.example",
            deployment_id="a" * 32,
            profile=tmp_path / "never-read",
            key=tmp_path / "never-read-key",
            known_hosts=tmp_path / "never-read-hosts",
            ssh=tmp_path / "never-start",
            confirm=True,
        )


def test_retired_receiver_does_not_parse_secret_or_read_stdin(monkeypatch, capsys):
    class Unreadable:
        @property
        def buffer(self):
            pytest.fail("Retired receiver must not consume stdin")

    monkeypatch.setattr(sys, "stdin", Unreadable())
    with pytest.raises(DeploymentError, match="retired") as error:
        receive_runtime.receive(b"synthetic-private-password", "a" * 32)
    assert "synthetic-private-password" not in str(error.value)
    assert receive_runtime.main() == 1
    assert handoff_runtime.main() == 1
    assert "synthetic-private-password" not in capsys.readouterr().out


def test_retired_config_does_not_read_url_or_ca_files(monkeypatch, tmp_path):
    monkeypatch.setattr(configure, "read_file", lambda _path: pytest.fail("No credential read"))
    with pytest.raises(DeploymentError, match="retired"):
        configure.configure_supabase(tmp_path / "private-uri", tmp_path / "private-ca")


def test_retired_configuration_preserves_historical_files(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    folder = root / "secrets/api-supabase"
    folder.mkdir(parents=True)
    previous = folder / "database-url"
    previous.write_text("synthetic-preserve-existing")
    monkeypatch.setattr(configure, "ROOT", root)
    with pytest.raises(DeploymentError, match="preserved"):
        configure.configure_supabase_values("synthetic-secret", "synthetic-ca")
    assert previous.read_text() == "synthetic-preserve-existing"
    assert sorted(path.name for path in folder.iterdir()) == ["database-url"]
