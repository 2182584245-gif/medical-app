"""Offline tests: all DNS/socket traffic is replaced by in-memory doubles."""

import importlib.util
import io
import json
import socket
import threading
import time
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.railway_probe import probe

PUBLIC_ADDRESS = "8.8.8.8"


class FakeConnection:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.calls.append(("closed",))

    def settimeout(self, value):
        self.calls.append(("timeout", value))

    def connect(self, address):
        self.calls.append(("connect", address))
        if self.error:
            raise self.error


def address_info(address=PUBLIC_ADDRESS, family=socket.AF_INET):
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 5432))]


def fake_network(monkeypatch, *, addresses=None, error=None):
    looked_up, created = [], []
    connection = FakeConnection(error)

    def resolve(*args):
        looked_up.append(args)
        return address_info() if addresses is None else addresses

    def factory(*args):
        created.append(args)
        return connection

    monkeypatch.setattr(probe.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(probe.socket, "socket", factory)
    return looked_up, created, connection


def test_import_is_network_free(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Import attempted networking")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    spec = importlib.util.spec_from_file_location("independent_probe", probe.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_connects_once_fixed_host_port_no_protocol_bytes(monkeypatch):
    looked_up, created, connection = fake_network(monkeypatch)
    assert probe.check_connectivity() == {"dns": "ok", "tcp": "connected"}
    assert looked_up == [(probe.CHECK_HOST, 5432, socket.AF_UNSPEC, socket.SOCK_STREAM)]
    assert created == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert connection.calls[1] == ("connect", (PUBLIC_ADDRESS, 5432))
    assert 0 < connection.calls[0][1] <= 5.0
    assert connection.calls[2] == ("closed",)


@pytest.mark.parametrize("error,expected", [
    (TimeoutError("private text"), "timeout"),
    (ConnectionRefusedError("private text"), "refused"),
    (OSError("private text"), "failed"),
])
def test_socket_errors_are_fixed_enums_no_retry(monkeypatch, capsys, error, expected):
    addresses = address_info() + address_info("1.1.1.1")
    _looked_up, created, connection = fake_network(monkeypatch, addresses=addresses, error=error)
    assert probe.check_connectivity() == {"dns": "ok", "tcp": expected}
    assert len(created) == 1
    assert sum(call[0] == "connect" for call in connection.calls) == 1
    assert capsys.readouterr() == ("", "")


def test_dns_failure_never_connects_or_leaks(monkeypatch, capsys):
    def fail(*_args):
        raise socket.gaierror("private DNS details")

    _looked_up, created, _connection = fake_network(monkeypatch)
    monkeypatch.setattr(probe.socket, "getaddrinfo", fail)
    assert probe.check_connectivity() == {"dns": "failed", "tcp": "not_attempted"}
    assert created == []
    assert capsys.readouterr() == ("", "")


def test_dns_timeout_is_bounded_and_no_late_tcp(monkeypatch):
    release, finished = threading.Event(), threading.Event()

    def slow(*_args):
        release.wait(1)
        finished.set()
        return address_info()

    _looked_up, created, _connection = fake_network(monkeypatch)
    monkeypatch.setattr(probe.socket, "getaddrinfo", slow)
    monkeypatch.setattr(probe, "CHECK_TIMEOUT_SECONDS", 0.02)
    start = time.monotonic()
    try:
        assert probe.check_connectivity() == {"dns": "timeout", "tcp": "not_attempted"}
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        assert finished.wait(1)
    assert created == []


@pytest.mark.parametrize("addresses", [
    [], address_info("127.0.0.1"), address_info("10.0.0.1"), address_info("invalid"),
    address_info("::1", socket.AF_INET6), address_info("8.8.8.8", socket.AF_INET6),
])
def test_nonpublic_or_invalid_dns_results_not_connected(monkeypatch, addresses):
    _looked_up, created, _connection = fake_network(monkeypatch, addresses=addresses)
    assert probe.check_connectivity() == {"dns": "no_address", "tcp": "not_attempted"}
    assert created == []


def test_ipv4_is_preferred_without_second_attempt(monkeypatch):
    addresses = address_info("2606:4700:4700::1111", socket.AF_INET6) + address_info()
    _looked_up, created, connection = fake_network(monkeypatch, addresses=addresses)
    assert probe.check_connectivity()["tcp"] == "connected"
    assert created == [(socket.AF_INET, socket.SOCK_STREAM)]
    assert connection.calls[1] == ("connect", (PUBLIC_ADDRESS, 5432))


def test_ipv6_if_only_candidate(monkeypatch):
    address = "2606:4700:4700::1111"
    _looked_up, created, connection = fake_network(
        monkeypatch, addresses=address_info(address, socket.AF_INET6)
    )
    assert probe.check_connectivity()["tcp"] == "connected"
    assert created == [(socket.AF_INET6, socket.SOCK_STREAM)]
    assert connection.calls[1] == ("connect", (address, 5432, 0, 0))


def test_tcp_uses_remaining_shared_budget(monkeypatch):
    _looked_up, _created, connection = fake_network(monkeypatch)
    moments = iter([100.0, 100.0, 103.0])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(moments))
    assert probe.check_connectivity()["tcp"] == "connected"
    assert connection.calls[0] == ("timeout", 2.0)


@pytest.mark.parametrize("peer,expected", [
    ("10.0.0.1", {"family": "ipv4", "private": "yes"}),
    ("8.8.8.8", {"family": "ipv4", "private": "no"}),
    ("::1", {"family": "ipv6", "private": "yes"}),
    ("2606:4700:4700::1111", {"family": "ipv6", "private": "no"}),
    ("secret-text", {"family": "invalid", "private": "unknown"}),
])
def test_peer_is_redacted(peer, expected):
    assert probe.peer_summary(peer) == expected
    assert peer not in json.dumps(expected)


@pytest.mark.parametrize("values,presence,validity,sentinel", [
    ([], "absent", "not_checked", "not_checked"),
    (["198.51.100.27"], "present", "valid", "yes"),
    (["8.8.8.8"], "present", "valid", "no"),
    (["::1"], "present", "valid", "no"),
    (["198.51.100.27, 8.8.8.8"], "present", "invalid", "not_checked"),
    (["secret-value"], "present", "invalid", "not_checked"),
    (["8.8.8.8:1000"], "present", "invalid", "not_checked"),
    (["198.51.100.27", "1.1.1.1"], "duplicate", "not_checked", "not_checked"),
])
def test_ip_header_is_enum_only(values, presence, validity, sentinel):
    headers = Message()
    for value in values:
        headers["X-Real-IP"] = value
    assert probe.ip_header_summary(headers, "x-real-ip") == {
        "presence": presence, "validity": validity, "sentinel": sentinel,
    }


@pytest.mark.parametrize("values,expected", [
    ([], "absent"), (["https"], "https"), (["http"], "http"),
    (["https,http"], "invalid"), (["private-value"], "invalid"),
    (["https", "http"], "duplicate"),
])
def test_forwarded_proto_is_enum_only(values, expected):
    headers = Message()
    for value in values:
        headers["X-Forwarded-Proto"] = value
    assert probe.proto_summary(headers) == expected


class FakeSocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def makefile(self, _mode, _buffering):
        return self.input

    def sendall(self, value):
        self.output.extend(value)


def http_request(request, *, peer="8.8.8.8"):
    connection = FakeSocket(request)
    server = SimpleNamespace(cached_result={"dns": "ok", "tcp": "connected"})
    probe.ProbeHandler(connection, (peer, 5000), server)
    headers, body = bytes(connection.output).split(b"\r\n\r\n", 1)
    status = int(headers.split(b" ", 2)[1])
    assert b"Cache-Control: no-store" in headers
    assert b"Connection: close" in headers
    assert connection.timeout == 3.0
    return status, body


def test_health_generic_works_for_platform_healthcheck_host(capsys):
    assert http_request(b"GET /health HTTP/1.1\r\nHost: healthcheck.railway.app\r\n\r\n") == (
        200, b'{"status":"ok"}'
    )
    assert capsys.readouterr() == ("", "")


def test_http_probe_cached_no_network_no_metadata_reflection(monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Request initiated outbound traffic")

    monkeypatch.setattr(probe, "check_connectivity", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    request = (
        b"GET /probe HTTP/1.1\r\nHost: private.example\r\n"
        b"Authorization: private-token\r\nX-Real-IP: 198.51.100.27\r\n"
        b"X-Forwarded-For: 198.51.100.27, 1.1.1.1\r\n"
        b"CF-Connecting-IP: private-error\r\nX-Forwarded-Proto: https\r\n\r\n"
    )
    for _ in range(2):
        status, body = http_request(request)
        assert status == 200
        data = json.loads(body)
        assert data["tcp"] == "connected"
        assert data["x_real_ip"]["sentinel"] == "yes"
        assert data["x_forwarded_for"]["validity"] == "invalid"
        assert data["cf_connecting_ip"]["validity"] == "invalid"
        assert data["x_forwarded_proto"] == "https"
        for secret in (b"private", b"198.51", b"1.1.1.1", b"8.8.8.8"):
            # 'private' is a fixed peer field name, never a raw user value.
            if secret != b"private":
                assert secret not in body
        assert b"private.example" not in body and b"private-token" not in body
        assert b"private-error" not in body
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("path", [
    "/", "/probe?target=https://private.example", "/health?secret=value",
    "http://private.example/probe", "/.local/supabase-platform-runtime.json",
])
def test_unknown_or_parameterized_paths_are_rejected_without_echo(path, capsys):
    status, body = http_request(f"GET {path} HTTP/1.1\r\nHost: ignored\r\n\r\n".encode())
    assert status == 404 and body == b'{"status":"not_found"}'
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"])
def test_write_methods_rejected_without_reading_body(method, capsys):
    request = (
        f"{method} /probe HTTP/1.1\r\nHost: ignored\r\nContent-Length: 999999\r\n\r\n"
    ).encode()
    status, body = http_request(request)
    assert status == 405 and body == b'{"status":"method_not_allowed"}'
    assert capsys.readouterr() == ("", "")


def test_invalid_request_and_server_exception_never_echo(capsys):
    status, body = http_request(b"PRIVATE-METHOD /private-path HTTP/1.1\r\n\r\n")
    assert status == 400 and body == b'{"status":"rejected"}'
    probe.ProbeServer.handle_error(None, None, ("private-address", 80))
    assert capsys.readouterr() == ("", "")


def test_main_checks_once_and_reads_no_secret_variables(monkeypatch, capsys):
    calls = []
    cached = {"dns": "ok", "tcp": "refused"}

    def check():
        calls.append("check")
        return cached

    class Server:
        def __init__(self, address, result):
            calls.append((address, result))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def serve_forever(self, *, poll_interval):
            calls.append(("serve", poll_interval))

    monkeypatch.setattr(probe, "check_connectivity", check)
    monkeypatch.setattr(probe, "ProbeServer", Server)
    monkeypatch.setattr(probe.os, "environ", {"PORT": "23456", "PRIVATE": "private-value"})
    assert probe.main() == 0
    assert calls == ["check", (("0.0.0.0", 23456), cached), ("serve", 0.2)]
    output = capsys.readouterr()
    assert json.loads(output.out) == {"event": "railway_probe_start", **cached}
    assert output.err == "" and output.out.isascii()


@pytest.mark.parametrize("port", ["private-value", "0", "443", "65536"])
def test_invalid_port_fails_safely_before_network(monkeypatch, capsys, port):
    def forbidden():
        raise AssertionError("Network must not run for invalid port")

    monkeypatch.setattr(probe, "check_connectivity", forbidden)
    monkeypatch.setattr(probe.os, "environ", {"PORT": port})
    assert probe.main() == 1
    assert capsys.readouterr() == ('{"event":"railway_probe_failed"}\n', "")


def test_deployment_bundle_contains_no_dependencies_or_legacy_railway_config():
    bundle = Path(probe.__file__).parent
    assert (bundle / ".python-version").read_text().strip() == "3.13.15"
    assert all(not line.strip() or line.startswith("#")
               for line in (bundle / "requirements.txt").read_text().splitlines())
    config = json.loads((bundle / "railpack.json").read_text())
    assert config["provider"] == "python"
    assert config["deploy"]["startCommand"] == "python -B probe.py"
    assert not (bundle / "railway.json").exists()
    assert not (bundle / "Dockerfile").exists()
