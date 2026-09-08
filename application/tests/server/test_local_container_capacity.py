"""Offline tests only: synthetic SQLite/TestClient, not a 2 GiB measurement."""

from __future__ import annotations

import json
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from platform_test_support import PEPPER, SyntheticDatabase

from server.platform_app import create_app
from server.platform_config import PlatformSettings
from tools.local_platform import capacity, smoke


@pytest.fixture
def offline_lab(tmp_path, monkeypatch):
    database = SyntheticDatabase()
    settings = PlatformSettings(
        env="test", database_url="sqlite+pysqlite:///:memory:", token_pepper=PEPPER,
        allowed_hosts=["api"], require_https=True,
    )
    application = create_app(settings, database=database)
    monkeypatch.setenv("LOCAL_CONTAINER_ONLY", "1")
    monkeypatch.setattr(smoke, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(capacity.guard, "require_local_container", lambda: None)
    monkeypatch.setattr(capacity, "MAX_CYCLES_PER_CLIENT", 1)
    monkeypatch.setattr(capacity.time, "sleep", lambda _duration: None)

    # The offline fixture uses one SQLite connection, not PostgreSQL. Serialize
    # only fixture HTTP dispatch; production capacity uses unrestricted httpx.
    request_lock = threading.Lock()

    class SyntheticClient(TestClient):
        def request(self, *args, **kwargs):
            with request_lock:
                return super().request(*args, **kwargs)

    def factory():
        return SyntheticClient(
            application, base_url=smoke.API_ORIGIN, raise_server_exceptions=False,
        )

    assert smoke.run(_client_factory=factory)["status"] == "passed"
    yield database, factory
    database.close()


def test_all_phases_synthetic_business_and_baseline_survive(offline_lab):
    database, factory = offline_lab
    before = smoke.STATE_FILE.read_bytes()
    report = capacity.run(_client_factory=factory)
    assert report["status"] == "passed", json.dumps(report, indent=2)
    assert report["mode"] == "offline_contract_test"
    assert report["ai_called"] is report["cloud_connected"] is False
    assert report["production_capacity_claim"] is False
    assert [item["clients"] for item in report["stages"]] == [1, 3, 5]
    for stage in report["stages"]:
        assert stage["error_count"] == 0
        assert stage["per_client_cycles"] == [1] * stage["clients"]
        assert stage["load"]["cycles_completed"] == stage["clients"]
        assert stage["load"]["files_verified"] == stage["clients"]
        assert stage["load"]["business_checks_passed"] == 4 * stage["clients"]
        assert stage["cleanup"]["request_count"] == stage["clients"]
    assert database.scalar("SELECT COUNT(*) FROM messages") == 2 + 2 * 9
    assert database.scalar("SELECT COUNT(*) FROM platform_sessions WHERE revoked_at IS NULL") == 0
    assert smoke.STATE_FILE.read_bytes() == before
    # No fresh logins required to verify the two untouched smoke baseline conversations.
    for identifier in json.loads(before)["conversation_ids"]:
        expected = 2 if identifier == json.loads(before)["conversation_ids"][0] else 0
        assert database.scalar(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,),
        ) == expected
    output = json.dumps(report)
    for account in json.loads(before)["accounts"]:
        assert account["password"] not in output
        assert account["username"] not in output
    assert "access_token" not in output


@pytest.mark.parametrize("seconds", [0, 29, 121, True, 30.0, "30", None])
def test_invalid_duration_fails_before_guard_or_network(monkeypatch, seconds):
    monkeypatch.setattr(capacity.guard, "require_local_container", lambda: pytest.fail("guard"))
    report = capacity.run(stage_seconds=seconds, _client_factory=lambda: pytest.fail("network"))
    assert report["status"] == "failed" and report["code"] == "configuration"
    assert not report["stages"]


def test_guard_is_required_before_state_or_client(monkeypatch):
    def refuse():
        raise RuntimeError("SECRET cloud connection details")

    monkeypatch.setattr(capacity.guard, "require_local_container", refuse)
    monkeypatch.setattr(smoke, "_load_state", lambda: pytest.fail("state must not be read"))
    result = capacity.run(_client_factory=lambda: pytest.fail("network"))
    assert result["status"] == "failed"
    assert "SECRET" not in json.dumps(result)


def test_invalid_saved_synthetic_state_fails_before_client(monkeypatch):
    monkeypatch.setattr(capacity.guard, "require_local_container", lambda: None)

    def invalid():
        raise smoke.LocalSmokeError("state_invalid")

    monkeypatch.setattr(smoke, "_load_state", invalid)
    assert capacity.run(_client_factory=lambda: pytest.fail("network"))["code"] == "configuration"


def test_client_only_uses_fixed_https_verified_ca(monkeypatch):
    values = {}
    context = ssl.create_default_context()

    def ssl_context(*, cafile):
        values["cafile"] = cafile
        return context

    def client(**kwargs):
        values.update(kwargs)
        return object()

    monkeypatch.setattr(capacity.ssl, "create_default_context", ssl_context)
    monkeypatch.setattr(capacity.httpx, "Client", client)
    capacity._create_client()
    assert values["base_url"] == "https://api:8443"
    assert values["cafile"] == str(smoke.CA_FILE)
    assert values["verify"] is context and context.check_hostname
    assert values["trust_env"] is values["follow_redirects"] is False
    assert values["timeout"].read == 10
    assert values["limits"].max_connections == 1


def test_latency_and_status_statistics():
    metrics = capacity.Metrics()
    metrics.request(0.010, 200)
    metrics.request(0.020, 503, "http_status")
    metrics.request(0.030, None, "timeout")
    summary = metrics.summary()
    assert summary["request_count"] == 3 and summary["error_count"] == 2
    assert summary["latency_ms"] == {"p50": 20.0, "p95": 29.0, "max": 30.0}
    assert summary["http_status_counts"] == {"200": 1, "503": 1}


def test_workers_really_overlap_in_five_distinct_threads(monkeypatch):
    barrier = threading.Barrier(5, timeout=5)
    names, lock = set(), threading.Lock()
    metrics = capacity.Metrics()

    def concurrent_cycle(*_args):
        with lock:
            names.add(threading.current_thread().name)
        barrier.wait()
        metrics.completed()

    monkeypatch.setattr(capacity, "_cycle", concurrent_cycle)
    monkeypatch.setattr(capacity, "MAX_CYCLES_PER_CLIENT", 1)
    monkeypatch.setattr(capacity.time, "sleep", lambda _duration: None)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(
            capacity._worker, None, index, index, b"", time.monotonic() + 10, metrics,
        ) for index in range(5)]
        for future in futures:
            assert future.result() == 1
    assert len(names) == 5
    assert metrics.summary()["cycles_completed"] == 5
    assert metrics.summary()["error_count"] == 0


def test_one_idle_worker_fails_even_if_other_clients_complete_enough(offline_lab, monkeypatch):
    _database, factory = offline_lab
    outcomes, lock = iter((0, 2, 2)), threading.Lock()

    def uneven_worker(_transport, _actor, _conversation, _image, _deadline, metrics,
                      _observe_rate_limits=False):
        with lock:
            completed = next(outcomes)
        for _index in range(completed):
            metrics.completed()
        return completed

    monkeypatch.setattr(capacity, "_worker", uneven_worker)
    state = smoke._load_state()
    stage = capacity._stage(
        state["accounts"], set(state["conversation_ids"]), 3, 30, capacity._picture(), factory,
    )
    assert stage["load"]["cycles_completed"] == 4
    assert sorted(stage["per_client_cycles"]) == [0, 2, 2]
    assert stage["status"] == "failed"
    assert stage["load"]["error_codes"] == {"business_validation": 1}


@pytest.mark.parametrize("failure", [
    capacity.CapacityError("business_validation"), RuntimeError("SECRET"),
])
def test_worker_error_returns_already_completed_cycles(monkeypatch, failure):
    metrics = capacity.Metrics()
    calls = 0

    def cycle(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        metrics.completed()

    monkeypatch.setattr(capacity, "_cycle", cycle)
    monkeypatch.setattr(capacity.time, "sleep", lambda _duration: None)
    completed = capacity._worker(None, 1, 1, b"", time.monotonic() + 10, metrics)
    assert completed == 1
    assert metrics.summary()["cycles_completed"] == 1
    assert metrics.summary()["error_count"] == 1
    assert "SECRET" not in json.dumps(metrics.summary())


def test_observation_retains_failed_429_stages_and_exercises_all_levels(offline_lab, monkeypatch):
    _database, factory = offline_lab
    original_cycle = capacity._cycle
    monkeypatch.setattr(capacity, "MAX_CYCLES_PER_CLIENT", 2)

    def one_limit_then_business(transport, *args):
        if not getattr(transport.client, "synthetic_limit_observed", False):
            transport.client.synthetic_limit_observed = True
            transport.metrics.request(
                0.001, 429, "upload_capacity_limited",
                operation="/v1/rpc/chat/begin_message",
            )
            error = capacity.CapacityError("upload_capacity_limited", status_code=429)
            error.counted = True
            raise error
        return original_cycle(transport, *args)

    monkeypatch.setattr(capacity, "_cycle", one_limit_then_business)
    report = capacity.run(observe_rate_limits=True, _client_factory=factory)
    assert report["status"] == "failed" and report["code"] == "rate_limits_observed"
    assert [stage["clients"] for stage in report["stages"]] == [1, 3, 5]
    for stage in report["stages"]:
        assert stage["status"] == "failed"
        assert stage["rate_limit_only_failure"] is True
        assert stage["per_client_cycles"] == [1] * stage["clients"]
        assert stage["load"]["http_status_counts"]["429"] == stage["clients"]
        assert stage["load"]["error_codes"]["upload_capacity_limited"] == stage["clients"]
    assert report["error_count"] == 9


def test_observation_does_not_continue_after_non429(offline_lab, monkeypatch):
    _database, factory = offline_lab

    def failed(*_args):
        raise capacity.CapacityError("http_status", status_code=503)

    monkeypatch.setattr(capacity, "_cycle", failed)
    report = capacity.run(observe_rate_limits=True, _client_factory=factory)
    assert report["status"] == "failed" and len(report["stages"]) == 1
    assert report["stages"][0]["rate_limit_only_failure"] is False


@pytest.mark.parametrize("detail,code", [
    ("Upload capacity limited", "upload_capacity_limited"),
    ("SECRET database connection", "http_status"),
])
def test_429_detail_only_maps_exact_known_classification(detail, code):
    metrics = capacity.Metrics()
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(429, json={"detail": detail}),
    ), base_url=smoke.API_ORIGIN) as client:
        transport = capacity.Transport(client, metrics)
        with pytest.raises(capacity.CapacityError) as caught:
            transport.request("POST", "/v1/rpc/files/upload_bytes")
    assert caught.value.status_code == 429 and str(caught.value) == code
    assert "SECRET" not in json.dumps(metrics.summary())
    assert "SECRET" not in str(caught.value)
    assert metrics.summary()["operation_http_status_counts"] == {
        "/v1/rpc/files/upload_bytes 429": 1,
    }


def test_observation_waits_half_second_and_starts_new_cycle(monkeypatch):
    metrics, indices, sleeps = capacity.Metrics(), [], []

    def cycle(_transport, _actor, _conversation, _image, index, _metrics):
        indices.append(index)
        if index == 0:
            raise capacity.CapacityError("http_status", status_code=429)
        metrics.completed()

    monkeypatch.setattr(capacity, "MAX_CYCLES_PER_CLIENT", 2)
    monkeypatch.setattr(capacity, "_cycle", cycle)
    monkeypatch.setattr(capacity.time, "sleep", sleeps.append)
    assert capacity._worker(None, 1, 1, b"", time.monotonic() + 10, metrics, True) == 1
    assert indices == [0, 1] and 0.5 in sleeps
    assert metrics.summary()["error_count"] == 1


def test_default_worker_stops_on_first_429(monkeypatch):
    def limited(*_args):
        raise capacity.CapacityError("http_status", status_code=429)

    monkeypatch.setattr(capacity, "_cycle", limited)
    metrics = capacity.Metrics()
    assert capacity._worker(None, 1, 1, b"", time.monotonic() + 10, metrics) == 0
    assert metrics.summary()["error_count"] == 1


@pytest.mark.parametrize("status", [302, 401, 429, 500, 503])
def test_unexpected_status_never_echoes_response(status):
    metrics = capacity.Metrics()
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="SECRET token and database URI"),
    ), base_url=smoke.API_ORIGIN) as client:
        transport = capacity.Transport(client, metrics)
        with pytest.raises(capacity.CapacityError, match="http_status"):
            transport.request("POST", "/v1/auth/logout", status=204)
    assert metrics.summary()["error_count"] == 1
    assert "SECRET" not in json.dumps(metrics.summary())


@pytest.mark.parametrize("failure,code", [
    (httpx.ReadTimeout("SECRET"), "timeout"),
    (httpx.ConnectError("SECRET"), "network"),
    (ssl.SSLError("SECRET"), "tls"),
    (RuntimeError("SECRET"), "response_invalid"),
])
def test_transport_failure_sanitization(failure, code):
    class Broken:
        def request(self, *_args, **_kwargs):
            raise failure

    metrics = capacity.Metrics()
    with pytest.raises(capacity.CapacityError) as error:
        capacity.Transport(Broken(), metrics).request("POST", "/v1/auth/login")
    assert str(error.value) == code
    assert metrics.summary()["error_codes"] == {code: 1}


def test_arbitrary_target_or_ai_operation_is_refused():
    transport = capacity.Transport(None, capacity.Metrics())
    with pytest.raises(capacity.CapacityError, match="configuration"):
        transport.request("POST", "https://cloud.example.invalid/v1/auth/login")
    with pytest.raises(capacity.CapacityError, match="configuration"):
        transport.rpc("ai", "prepare_member_draft", 1)


def test_business_mismatch_fails_and_sessions_still_logout(offline_lab, monkeypatch):
    database, factory = offline_lab

    def invalid(*_args):
        raise capacity.CapacityError("business_validation")

    monkeypatch.setattr(capacity, "_cycle", invalid)
    report = capacity.run(_client_factory=factory)
    assert report["status"] == "failed" and report["code"] == "stage_failed"
    assert len(report["stages"]) == 1
    assert report["error_count"] > 0
    assert database.scalar("SELECT COUNT(*) FROM platform_sessions WHERE revoked_at IS NULL") == 0


def test_synthetic_png_is_small_and_request_fits_body_limit():
    content = capacity._picture()
    from server.platform_uploads import validate_image

    validate_image(content, "image/png")
    assert 40 * 1024 < len(content) <= 64 * 1024
    payload = smoke._payload((1, "synthetic.png", content), {})
    assert len(json.dumps(payload).encode()) < 1024 * 1024


@pytest.mark.parametrize("args", [
    ["--base-url", "https://SECRET.invalid"], ["--password", "SECRET"],
    ["--stage-seconds", "SECRET"], ["--help"],
])
def test_cli_only_prints_one_safe_json_for_invalid_args(args, capsys):
    assert capacity.main(args) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "SECRET" not in captured.out
    assert json.loads(captured.out)["code"] == "configuration"


@pytest.mark.parametrize("status,exit_code", [("passed", 0), ("failed", 1)])
def test_cli_exit_code_tracks_result(monkeypatch, capsys, status, exit_code):
    monkeypatch.setattr(capacity, "run", lambda **_kwargs: {"status": status})
    assert capacity.main(["--stage-seconds", "30"]) == exit_code
    assert json.loads(capsys.readouterr().out) == {"status": status}
