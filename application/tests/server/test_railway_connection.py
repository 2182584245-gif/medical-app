from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from server import cloud_connection
from server import railway_connection as railway

TOKEN = "synthetic-local-project-token-for-offline-tests"


def payload():
    return {
        "purpose": railway.PURPOSE,
        "project_id": railway.TARGET_PROJECT_ID,
        "environment_id": railway.TARGET_ENVIRONMENT_ID,
        "service_id": railway.TARGET_SERVICE_ID,
        "token": TOKEN,
    }


def identity():
    return {"data": {"projectToken": {
        "projectId": railway.TARGET_PROJECT_ID,
        "environmentId": railway.TARGET_ENVIRONMENT_ID,
    }}}


@pytest.fixture(autouse=True)
def never_access_real_secrets_or_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("real secrets, hidden prompts and networking are forbidden in these tests")

    monkeypatch.setattr(railway, "load_secret_payload", forbidden)
    monkeypatch.setattr(railway, "save_secret_payload", forbidden)
    monkeypatch.setattr(railway, "hidden_input", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def test_configure_uses_hidden_input_and_dpapi_only(monkeypatch, tmp_path, capsys):
    prompts, saves = [], []
    monkeypatch.setattr(railway, "hidden_input", lambda prompt: prompts.append(prompt) or TOKEN)
    monkeypatch.setattr(railway, "save_secret_payload", lambda *a, **kw: saves.append((a, kw)))
    path = tmp_path / "private" / "test.json"
    report = railway.configure(path)
    assert prompts and "不可见" in prompts[0]
    assert saves == [((path, payload()), {"replace": False})]
    assert report["status"] == "saved" and report["cloud_connected"] is False
    assert not path.exists()  # Our only writer was the mocked ciphertext store.
    assert TOKEN not in json.dumps(report) + capsys.readouterr().out


def test_existing_profile_requires_explicit_replace(monkeypatch, tmp_path):
    path = tmp_path / "encrypted.json"
    path.write_bytes(b"synthetic ciphertext")
    with pytest.raises(railway.RailwayConfigurationError, match="--replace"):
        railway.configure(path)
    saves = []
    monkeypatch.setattr(railway, "hidden_input", lambda _prompt: TOKEN)
    monkeypatch.setattr(railway, "save_secret_payload", lambda *a, **kw: saves.append((a, kw)))
    railway.configure(path, replace=True)
    assert saves == [((path, payload()), {"replace": True})]
    assert path.read_bytes() == b"synthetic ciphertext"


@pytest.mark.parametrize("token", ["x" * 19, "x" * 4097, "x" * 20 + " ", "x" * 20 + "\n",
                                   "令牌" * 20, None])
def test_invalid_tokens_never_reach_store(monkeypatch, tmp_path, token):
    monkeypatch.setattr(railway, "hidden_input", lambda _prompt: token)
    with pytest.raises(railway.RailwayConfigurationError, match="令牌格式无效"):
        railway.configure(tmp_path / "absent.json")


def test_printable_ascii_tokens_have_no_guessed_prefix():
    for token in ("!" * 20, "~" * 4096, "abc.DEF_ghi-jkl/mno+pqr=stuv"):
        candidate = payload() | {"token": token}
        assert railway._validated_payload(candidate)["token"] == token


def test_hidden_input_reuses_tty_and_getpass_warning_protection(monkeypatch, tmp_path):
    monkeypatch.setattr(railway, "hidden_input", cloud_connection.hidden_input)
    monkeypatch.setattr(cloud_connection.sys.stdin, "isatty", lambda: False)
    with pytest.raises(railway.RailwayConfigurationError, match="安全隐藏输入"):
        railway.configure(tmp_path / "absent.json")

    monkeypatch.setattr(cloud_connection.sys.stdin, "isatty", lambda: True)

    def cannot_hide(_prompt):
        raise cloud_connection.getpass.GetPassWarning(TOKEN)

    monkeypatch.setattr(cloud_connection.getpass, "getpass", cannot_hide)
    with pytest.raises(railway.RailwayConfigurationError) as caught:
        railway.configure(tmp_path / "absent.json")
    assert TOKEN not in str(caught.value)


def test_status_never_decrypts_or_connects(tmp_path):
    path = tmp_path / "encrypted.json"
    assert railway.status(path)["profile_exists"] is False
    path.write_bytes(b"synthetic ciphertext")
    report = railway.status(path)
    assert report["profile_exists"] is True
    assert report["cloud_connected"] == "not_checked"
    assert TOKEN not in json.dumps(report)


def test_loader_checks_all_scope_fields_and_exact_keys(monkeypatch, tmp_path):
    for field in ("purpose", "project_id", "environment_id", "service_id"):
        monkeypatch.setattr(
            railway, "load_secret_payload", lambda _p, f=field: payload() | {f: TOKEN}
        )
        with pytest.raises(railway.RailwayConfigurationError) as caught:
            railway.load_project_payload(tmp_path / "fake.json")
        assert TOKEN not in str(caught.value)
    for candidate in (payload() | {"extra": TOKEN}, {"token": TOKEN}, None, []):
        monkeypatch.setattr(railway, "load_secret_payload", lambda _p, p=candidate: p)
        with pytest.raises(railway.RailwayConfigurationError):
            railway.load_project_payload(tmp_path / "fake.json")


def test_loader_returns_validated_copy_and_hides_store_errors(monkeypatch, tmp_path):
    source = payload()
    monkeypatch.setattr(railway, "load_secret_payload", lambda _p: source)
    loaded = railway.load_project_payload(tmp_path / "fake.json")
    assert loaded == source and loaded is not source

    def fail(_path):
        raise OSError(TOKEN)

    monkeypatch.setattr(railway, "load_secret_payload", fail)
    with pytest.raises(railway.RailwayConfigurationError) as caught:
        railway.load_project_payload(tmp_path / "fake.json")
    assert TOKEN not in str(caught.value)


def test_check_exact_query_fixed_https_and_safe_http_settings(monkeypatch, tmp_path):
    source, calls, settings = payload(), [], []
    monkeypatch.setattr(railway, "load_secret_payload", lambda _p: source)
    actual_client = httpx.Client

    def factory(**kwargs):
        settings.append(kwargs)
        return actual_client(**kwargs)

    def respond(request):
        calls.append(request)
        assert str(request.url) == railway.API_URL
        assert request.method == "POST"
        assert request.headers["Project-Access-Token"] == TOKEN
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {"query": railway.IDENTITY_QUERY}
        return httpx.Response(200, json=identity())

    monkeypatch.setattr(railway.httpx, "Client", factory)
    report = railway.check(tmp_path / "fake.json", transport=httpx.MockTransport(respond))
    assert len(calls) == 1
    assert settings[0]["verify"] is True
    assert settings[0]["follow_redirects"] is False and settings[0]["trust_env"] is False
    assert settings[0]["timeout"] == httpx.Timeout(connect=10, read=20, write=10, pool=10)
    assert report["status"] == "connected_readonly"
    assert report["cloud_changed"] is False and report["service_access_checked"] is False
    assert TOKEN not in json.dumps(report)
    assert source == payload()  # Clearing the validated working copy does not mutate its loader.


@pytest.mark.parametrize("code", [302, 307, 401, 429, 500])
def test_check_non200_never_redirects_or_retries(monkeypatch, tmp_path, code):
    monkeypatch.setattr(railway, "load_secret_payload", lambda _p: payload())
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            code, text=TOKEN, headers={"location": "https://invalid.test/" + TOKEN}
        )

    with pytest.raises(railway.RailwayConfigurationError) as caught:
        railway.check(tmp_path / "fake.json", transport=httpx.MockTransport(respond))
    assert len(calls) == 1 and TOKEN not in str(caught.value)


def test_graphql_errors_and_wrong_scopes_are_never_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(railway, "load_secret_payload", lambda _p: payload())
    bad_results = [
        identity() | {"errors": [{"message": TOKEN}]},
        {"data": {"projectToken": None}},
        {"data": {"projectToken": {"projectId": TOKEN,
                                    "environmentId": railway.TARGET_ENVIRONMENT_ID}}},
        {"data": {"projectToken": {"projectId": railway.TARGET_PROJECT_ID,
                                    "environmentId": TOKEN}}},
        [], None,
    ]
    for result in bad_results:
        transport = httpx.MockTransport(lambda _r, item=result: httpx.Response(200, json=item))
        with pytest.raises(railway.RailwayConfigurationError) as caught:
            railway.check(tmp_path / "fake.json", transport=transport)
        assert TOKEN not in str(caught.value)


def test_oversized_stream_stops_reading_and_closes(monkeypatch, tmp_path):
    monkeypatch.setattr(railway, "load_secret_payload", lambda _p: payload())

    class LargeStream(httpx.SyncByteStream):
        count = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                self.count += 1
                yield b"x" * 8192

        def close(self):
            self.closed = True

    stream = LargeStream()
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, stream=stream))
    with pytest.raises(railway.RailwayConfigurationError, match="安全大小"):
        railway.check(tmp_path / "fake.json", transport=transport)
    assert stream.count == 9 and stream.closed


def test_network_certificate_json_failures_are_sanitized_without_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(railway, "load_secret_payload", lambda _p: payload())
    for exception_type in (httpx.ConnectError, httpx.ReadTimeout, ValueError):
        calls = []

        def fail(request, seen=calls, error_type=exception_type):
            seen.append(request)
            raise error_type(TOKEN)

        with pytest.raises(railway.RailwayConfigurationError) as caught:
            railway.check(tmp_path / "fake.json", transport=httpx.MockTransport(fail))
        assert TOKEN not in str(caught.value) and len(calls) == 1
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=TOKEN))
    with pytest.raises(railway.RailwayConfigurationError) as caught:
        railway.check(tmp_path / "fake.json", transport=transport)
    assert TOKEN not in str(caught.value)


def test_save_failures_and_cancelled_prompts_never_echo(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(railway, "hidden_input", lambda _p: TOKEN)

    def failed_save(*_args, **_kwargs):
        raise OSError(TOKEN)

    monkeypatch.setattr(railway, "save_secret_payload", failed_save)
    assert railway.main(["configure", "--profile", str(tmp_path / "fake.json")]) == 2
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err

    def cancelled(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(railway, "hidden_input", cancelled)
    with pytest.raises(railway.RailwayConfigurationError, match="已取消"):
        railway.configure(tmp_path / "fake.json")


def test_cli_parser_does_not_echo_accidental_secret(monkeypatch, capsys):
    for args in ([TOKEN], ["check", "--token", TOKEN], ["check", "--endpoint", TOKEN], []):
        with pytest.raises(SystemExit) as caught:
            railway.main(args)
        assert caught.value.code == 2
        output = capsys.readouterr()
        assert TOKEN not in output.out + output.err
    called = []
    monkeypatch.setattr(railway, "status", lambda path: called.append(path) or {"status": "safe"})
    assert railway.main(["status"]) == 0
    assert called == [railway.DEFAULT_PROFILE]
    assert railway.DEFAULT_PROFILE.name == "railway-project-token.json"
    assert railway.DEFAULT_PROFILE.parent.name == ".local"


def test_cli_status_and_replace_restrictions(tmp_path, capsys):
    path = str(tmp_path / "not-present.json")
    assert railway.main(["status", "--profile", path]) == 0
    assert json.loads(capsys.readouterr().out)["profile_exists"] is False
    assert railway.main(["check", "--replace", "--profile", path]) == 2
    assert "仅能用于 configure" in capsys.readouterr().err
    assert isinstance(railway.DEFAULT_PROFILE, Path)
