"""All HTTP requests use in-memory fake sockets; no listener or real profile is opened."""

from __future__ import annotations

import builtins
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from server import platform_admin, platform_entrypoint
from server import railway_secret_handoff as handoff

PROJECT = "https://abcdefghijklmnopqrst.supabase.co"
FAKE_PEM = "-----BEGIN CERTIFICATE-----\nSYNTHETIC-NOT-A-CERTIFICATE\n-----END CERTIFICATE-----\n"
SECRET_ERROR = "SYNTHETIC-SECRET-password-pepper-CA-URL-do-not-print"
VALUES = {
    "PLATFORM_DATABASE_URL": "synthetic-url-must-never-be-in-first-page",
    "PLATFORM_TOKEN_PEPPER": "synthetic-pepper-must-never-be-logged",
    "PLATFORM_DATABASE_CA_PEM": "synthetic-ca-must-never-be-logged",
}


@pytest.fixture(autouse=True)
def forbid_real_profile(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("real_profile_access_is_forbidden_in_offline_tests")

    monkeypatch.setattr(platform_admin, "load_runtime_payload", forbidden)
    monkeypatch.setattr(platform_admin, "project_url", lambda: PROJECT)


@pytest.fixture
def payload():
    return {
        "project_url": PROJECT, "purpose": platform_admin.PURPOSE, "state": "ready",
        "runtime_database_url": (
            "postgresql+psycopg://medical_app_platform_runtime.abcdefghijklmnopqrst:"
            + "a" * 64 + "@aws-0-synthetic.pooler.supabase.com:5432/postgres"
            "?sslmode=verify-full&sslrootcert=C%3A%2Fsynthetic-ca.crt"
        ),
        "token_pepper": bytes(range(32)).hex(), "provision_id": "b" * 32,
    }


def test_prepare_uses_injected_readers_same_entrypoint_validator_and_changes_only_ca_path(
    payload, monkeypatch,
):
    calls = []
    monkeypatch.setattr(platform_entrypoint.ssl, "create_default_context",
                        lambda **kwargs: calls.append(kwargs))
    original = dict(payload)

    def no_plaintext_file(*_args, **_kwargs):
        pytest.fail("injected preparation must not open or write a file")

    monkeypatch.setattr(builtins, "open", no_plaintext_file)
    result = handoff.prepare_runtime_variables(
        payload_loader=lambda: payload,
        certificate_reader=lambda path: FAKE_PEM if path == "C:/synthetic-ca.crt" else "",
    )
    assert calls == [{"cadata": FAKE_PEM}]
    assert payload == original  # The original/local configuration is not overwritten.
    assert set(result) == handoff.VARIABLE_NAMES
    original_url, cloud_url = make_url(payload["runtime_database_url"]), make_url(
        result["PLATFORM_DATABASE_URL"])
    assert cloud_url.query["sslrootcert"] == handoff.CLOUD_CA_PATH
    assert cloud_url.query["sslmode"] == "verify-full"
    assert cloud_url.set(query=original_url.query) == original_url
    assert result["PLATFORM_TOKEN_PEPPER"] == payload["token_pepper"]
    assert result["PLATFORM_DATABASE_CA_PEM"] == FAKE_PEM


@pytest.mark.parametrize("field,value", [
    ("state", "pending"), ("purpose", "migration-readonly-preflight"),
    ("project_url", "https://zyxwvutsrqponmlkjihg.supabase.co"),
    ("provision_id", "bad"), ("token_pepper", "a" * 64),
    ("runtime_database_url", "postgresql://postgres:secret@example.com:5432/postgres"),
])
def test_invalid_runtime_profile_is_rejected_without_raw_errors(payload, field, value, capsys):
    payload[field] = value
    with pytest.raises(handoff.HandoffError) as error:
        handoff.prepare_runtime_variables(payload_loader=lambda: payload,
                                           certificate_reader=lambda _path: FAKE_PEM)
    assert str(error.value) == "runtime_configuration_unavailable"
    assert value not in str(error.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("pem", ["", "x" * 32_001, "非ASCII证书", "synthetic-invalid-pem"])
def test_invalid_ca_has_fixed_error_and_no_file_writes(payload, pem, monkeypatch):
    def fail_validation(**_kwargs):
        raise ValueError(SECRET_ERROR)

    monkeypatch.setattr(platform_entrypoint.ssl, "create_default_context", fail_validation)
    with pytest.raises(handoff.HandoffError) as error:
        handoff.prepare_runtime_variables(payload_loader=lambda: payload,
                                           certificate_reader=lambda _path: pem)
    assert str(error.value) == "runtime_configuration_unavailable"
    assert SECRET_ERROR not in str(error.value)


class FakeSocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def makefile(self, _mode, _buffering=None):
        return self.input

    def sendall(self, value):
        self.output.extend(value)


@pytest.fixture
def context():
    now, loaded, stopped = [10.0], [], []

    def loader():
        loaded.append(True)
        return dict(VALUES)

    state = handoff.HandoffState(loader=loader, clock=lambda: now[0])
    state.bind_origin(48123)
    server = SimpleNamespace(handoff=state, request_stop=lambda: stopped.append(True))
    return SimpleNamespace(state=state, server=server, now=now, loaded=loaded, stopped=stopped)


def request(context, *, method="GET", action="reveal", path=None, headers=None, peer="127.0.0.1",
            raw_body=None, extra_headers=()):
    if method == "GET":
        values = {"Host": context.state.host, "Sec-Fetch-Site": "none",
                  "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        body = b""
    else:
        body = json.dumps({"action": action}).encode() if raw_body is None else raw_body
        values = {"Host": context.state.host, "Origin": context.state.origin,
                  "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
                  "Sec-Fetch-Dest": "empty", "Content-Type": "application/json",
                  "Content-Length": str(len(body))}
    for key, value in (headers or {}).items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    lines = [f"{method} {path or context.state.path} HTTP/1.1"]
    lines += [f"{key}: {value}" for key, value in values.items()]
    lines += [f"{key}: {value}" for key, value in extra_headers]
    fake = FakeSocket(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    handoff.HandoffHandler(fake, (peer, 12345), context.server)
    assert fake.timeout == 2
    head, content = bytes(fake.output).split(b"\r\n\r\n", 1)
    head_lines = head.decode("ascii").split("\r\n")
    status = int(head_lines[0].split()[1])
    response_headers = {key.lower(): value.strip() for key, value in
                        (line.split(":", 1) for line in head_lines[1:])}
    return status, response_headers, content


def test_first_page_has_no_secret_no_loader_calls_and_strong_headers(context, capsys):
    status, headers, body = request(context)
    assert status == 200 and context.loaded == []
    assert all(secret.encode() not in body for secret in VALUES.values())
    assert b'id="runtime-payload"' in body
    assert b'aria-label="Railway runtime variables"' in body
    assert b"readonly hidden" in body
    assert b"setTimeout(" in body and b'addEventListener("pagehide", clearPage)' in body
    assert b"output.value = JSON.stringify(payload" in body
    assert b"localStorage" not in body and b"sessionStorage" not in body
    assert b"console." not in body and b"<iframe" not in body
    assert headers["cache-control"] == "no-store, max-age=0"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert "set-cookie" not in headers and "access-control-allow-origin" not in headers
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp and "form-action 'none'" in csp
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">'.encode() in body
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert context.state.url not in body.decode()
    assert capsys.readouterr() == ("", "")


def test_exact_post_reveals_once_then_close_removes_access(context, capsys):
    status, headers, body = request(context, method="POST")
    assert status == 200 and json.loads(body) == VALUES
    assert headers["cache-control"].startswith("no-store")
    assert context.loaded == [True] and context.state._loader is None
    assert not any(isinstance(value, dict) for value in vars(context.state).values())
    status, _, repeated = request(context, method="POST")
    assert status == 409 and all(secret.encode() not in repeated for secret in VALUES.values())
    # Reloading cannot cause another load or embed the previously displayed values.
    assert all(secret.encode() not in request(context)[2] for secret in VALUES.values())
    assert request(context, method="POST", action="close")[0] == 200
    assert context.stopped == [True] and context.state.remaining == 0
    assert request(context, method="POST")[0] == 410
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("headers", [
    {"Host": "localhost:48123"}, {"Host": "127.0.0.1:48124"},
    {"Host": "attacker.example"}, {"Host": None},
    {"Origin": "https://attacker.example"}, {"Origin": "null"}, {"Origin": None},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": None}, {"Sec-Fetch-Mode": "no-cors"},
    {"Sec-Fetch-Dest": "iframe"}, {"Transfer-Encoding": "chunked"},
])
def test_bad_post_origin_host_or_fetch_metadata_never_consumes(context, headers):
    assert request(context, method="POST", headers=headers)[0] == 403
    assert context.loaded == [] and context.state._claimed is False
    assert request(context, method="POST")[0] == 200


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": None},
    {"Sec-Fetch-Dest": "iframe"}, {"Sec-Fetch-Mode": "cors"},
    {"Origin": "https://attacker.example"},
])
def test_get_is_only_exact_top_level_navigation(context, headers):
    assert request(context, headers=headers)[0] == 403
    assert context.loaded == []


@pytest.mark.parametrize("header", ["Host", "Origin", "Sec-Fetch-Site", "Content-Length"])
def test_duplicate_protected_headers_fail_closed(context, header):
    assert request(context, method="POST", extra_headers=[(header, "duplicate")])[0] == 403
    assert context.loaded == []


def test_random_capability_path_and_peer_are_required(context):
    assert re.fullmatch(r"/[A-Za-z0-9_-]{43}", context.state.path)
    assert handoff.HandoffState().path != context.state.path
    for path in ("/", context.state.path + "?action=reveal", context.state.path + "/", "/wrong"):
        assert request(context, method="POST", path=path)[0] == 403
    assert request(context, method="POST", peer="192.0.2.1")[0] == 403
    assert context.loaded == []


@pytest.mark.parametrize("body", [b"{}", b"not json", b'{"action":"invalid"}',
                                  b'{"action":"reveal","extra":"synthetic"}', b"x" * 257])
def test_malformed_or_overlarge_posts_never_load(context, body):
    assert request(context, method="POST", raw_body=body)[0] == 400
    assert context.loaded == []


def test_wrong_content_type_and_unsupported_methods_do_not_load(context):
    assert request(context, method="POST", headers={"Content-Type": "text/plain"})[0] == 400
    assert request(context, method="OPTIONS")[0] == 405
    assert request(context, method="HEAD")[0] == 405
    assert request(context, method="DELETE")[0] == 501
    assert context.loaded == []


def test_monotonic_expiry_and_expiry_during_read_never_disclose(context):
    context.now[0] += handoff.LIFETIME_SECONDS
    assert request(context)[0] == 410
    assert request(context, method="POST")[0] == 410
    assert context.loaded == []
    now = [0]

    def late_loader():
        now[0] = handoff.LIFETIME_SECONDS + 1
        return dict(VALUES)

    state = handoff.HandoffState(loader=late_loader, clock=lambda: now[0])
    with pytest.raises(handoff.HandoffError, match="expired"):
        state.reveal()
    assert state._loader is None


def test_concurrent_reveal_has_exactly_one_winner_and_close_during_read_is_safe():
    calls = []
    state = handoff.HandoffState(loader=lambda: calls.append(True) or dict(VALUES))

    def attempt():
        try:
            return state.reveal()
        except handoff.HandoffError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: attempt(), range(8)))
    assert sum(value is not None for value in results) == 1 and calls == [True]
    second = handoff.HandoffState(loader=lambda: second.close() or dict(VALUES))
    with pytest.raises(handoff.HandoffError, match="expired"):
        second.reveal()


def test_loader_exception_cannot_leak_to_page_logs_or_repeat(context, capsys):
    def fail():
        raise RuntimeError(SECRET_ERROR)

    context.state._loader = fail
    status, _, body = request(context, method="POST")
    assert status == 503 and SECRET_ERROR.encode() not in body
    assert request(context, method="POST")[0] == 409
    assert capsys.readouterr() == ("", "")


def test_cli_refuses_missing_or_wrong_confirmation_without_starting(monkeypatch, capsys):
    def forbidden(_state):
        pytest.fail("listener must not be constructed")

    monkeypatch.setattr(handoff, "LocalHandoffServer", forbidden)
    with pytest.raises(SystemExit):
        handoff.main([])
    assert handoff.main(["--confirm-service", SECRET_ERROR]) == 2
    captured = capsys.readouterr()
    assert SECRET_ERROR not in captured.err + captured.out


def test_confirmed_cli_only_outputs_local_url_and_id_and_arms_expiry(monkeypatch, capsys):
    seen = []

    class FakeServer:
        def __init__(self, state):
            self.state = state
            state.bind_origin(48123)
            seen.append("created_without_loading")

        def serve_forever(self, *, poll_interval):
            assert poll_interval == 0.25

        def request_stop(self):
            self.state.close()

        def server_close(self):
            assert self.state._loader is None
            seen.append("closed")

    class FakeTimer:
        def __init__(self, seconds, callback):
            assert seconds == 600
            self.callback = callback

        def start(self):
            seen.append("expiry_armed")

        def cancel(self):
            seen.append("expiry_cancelled")

    monkeypatch.setattr(handoff, "LocalHandoffServer", FakeServer)
    monkeypatch.setattr(handoff.threading, "Timer", FakeTimer)
    assert handoff.main(["--confirm-service", handoff.TARGET_SERVICE_ID]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"local_ui_url", "target_service_id"}
    assert output["target_service_id"] == handoff.TARGET_SERVICE_ID
    assert re.fullmatch(r"http://127\.0\.0\.1:48123/[A-Za-z0-9_-]{43}", output["local_ui_url"])
    assert seen == ["created_without_loading", "expiry_armed", "expiry_cancelled", "closed"]


def test_real_server_constructor_is_hard_bound_to_loopback_without_opening_socket(monkeypatch):
    seen = []

    def fake_init(self, address, handler):
        seen.append((address, handler))
        self.server_port = 48123

    monkeypatch.setattr(handoff.ThreadingHTTPServer, "__init__", fake_init)
    state = handoff.HandoffState(loader=lambda: dict(VALUES))
    handoff.LocalHandoffServer(state)
    assert seen == [(("127.0.0.1", 0), handoff.HandoffHandler)]
    assert state.origin == "http://127.0.0.1:48123"


def test_no_plaintext_storage_or_logging_calls_in_handoff_source():
    import inspect

    source = inspect.getsource(handoff)
    assert "write_text(" not in source and "write_bytes(" not in source
    assert "save_secret_payload(" not in source and "logging." not in source
    assert "platform_entrypoint.main(" not in source
