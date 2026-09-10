"""Synthetic secrets only; no profile/SSH/database is contacted."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from tools.aliyun_platform import configure, handoff_runtime, receive_runtime
from tools.aliyun_platform.guard import DeploymentError


def test_receiver_checks_identity_and_exact_payload(monkeypatch):
    monkeypatch.setattr(receive_runtime, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    marker = "a" * 32
    monkeypatch.setattr(receive_runtime, "marker", lambda _: {"id": marker})
    calls = []
    monkeypatch.setattr(
        receive_runtime, "configure_supabase_values", lambda *args: calls.append(args)
    )
    payload = {"version": 1, "deployment_id": marker, "runtime_url": "synthetic", "ca": "synthetic"}
    result = receive_runtime.receive(json.dumps(payload).encode(), marker)
    assert result["status"] == "runtime_installed"
    assert calls == [("synthetic", "synthetic")]
    for bad in ({**payload, "administrator_password": "synthetic"}, {**payload, "version": True}):
        with pytest.raises(DeploymentError):
            receive_runtime.receive(json.dumps(bad).encode(), marker)
    with pytest.raises(DeploymentError):
        receive_runtime.receive(json.dumps(payload).encode(), "b" * 32)
    with pytest.raises(DeploymentError):
        receive_runtime.receive(b"x" * 32769, marker)
    assert len(calls) == 1


def test_handoff_secret_only_in_stdin_pinned_host(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff_runtime, "os", SimpleNamespace(name="nt"))
    import server.cloud_connection as connection
    import server.platform_admin as admin
    private = "c" * 64
    url = make_url("postgresql+psycopg://medical_app_platform_runtime:" + private
                   + "@db.abcdefghijklmnopqrst.supabase.co:5432/postgres?sslmode=verify-full")
    monkeypatch.setattr(admin, "load_runtime_url", lambda _: url)
    ca = tmp_path / "public-ca.crt"
    ca.write_text("SYNTHETIC-CA")
    monkeypatch.setattr(connection, "ca_file", lambda _: str(ca))
    key, known, ssh = [tmp_path / name for name in ("synthetic-key", "known-hosts", "ssh.exe")]
    for path in (key, known, ssh):
        path.write_text("synthetic")
    captured = []
    def run(command, **kwargs):
        captured.append((command, kwargs))
        assert private not in " ".join(command)
        assert "env" not in kwargs
        assert json.loads(kwargs["input"])["runtime_url"].find(private) > 0
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "runtime_installed",
                               "deployment_id": "a" * 32, "unexpected": private}).encode())
    monkeypatch.setattr(handoff_runtime.subprocess, "run", run)
    result = handoff_runtime.handoff(host="39.106.166.15", deployment_id="a" * 32,
             profile=tmp_path / "DO-NOT-READ", key=key, known_hosts=known, ssh=ssh, confirm=True)
    assert result["status"] == "runtime_installed"
    assert private not in json.dumps(result) and "unexpected" not in result
    command, kwargs = captured[0]
    assert "StrictHostKeyChecking=yes" in command and "IdentitiesOnly=yes" in command
    assert "BatchMode=yes" in command and kwargs["capture_output"]
    assert "-F" in command and "none" in command and "ProxyCommand=none" in command
    assert "GlobalKnownHostsFile=NUL" in command and "ClearAllForwardings=yes" in command
    assert "runtime_installed" not in str(kwargs["input"])


def test_receiver_error_does_not_echo_sensitive_exception(monkeypatch):
    monkeypatch.setattr(receive_runtime, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(receive_runtime, "marker", lambda _: {"id": "a" * 32})
    def fail(*_):
        raise ValueError("synthetic-private-password")
    monkeypatch.setattr(receive_runtime, "configure_supabase_values", fail)
    with pytest.raises(Exception) as error:
        receive_runtime.receive(json.dumps({"version": 1, "deployment_id": "a" * 32,
                    "runtime_url": "synthetic", "ca": "synthetic"}).encode(), "a" * 32)
    assert "synthetic-private-password" not in str(error.value)


def test_runtime_initializer_preserves_existing(monkeypatch, tmp_path):
    root = tmp_path / "owned"
    folder = root / "secrets/api-supabase"
    folder.mkdir(parents=True)
    previous = folder / "database-url"
    previous.write_text("preserve-existing")
    monkeypatch.setattr(configure, "ROOT", root)
    monkeypatch.setattr(configure, "host_root", lambda: root)
    monkeypatch.setattr(configure, "marker", lambda _: {"id": "a" * 32})
    with pytest.raises(Exception, match="preserved"):
        configure.configure_supabase_values("unvalidated", "unvalidated")
    assert previous.read_text() == "preserve-existing"
