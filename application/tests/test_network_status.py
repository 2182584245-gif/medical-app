"""Credential-free network status probes use synthetic transports only."""

import httpx
import pytest

from ollama_chat_app.config import DEFAULT_ALIYUN_ENDPOINT
from ollama_chat_app.services import network_status as module
from ollama_chat_app.services.network_status import NetworkStatusMonitor, probe_backend


def test_probe_is_explicit_https_without_proxy_auth_or_redirects(monkeypatch):
    requests, options = [], []
    original = httpx.Client

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"status":"ready"}'))

    monkeypatch.setattr(module.httpx, "Client", client)
    result = probe_backend(DEFAULT_ALIYUN_ENDPOINT, transport=httpx.MockTransport(handler))
    assert result["state"] == "online"
    assert isinstance(result["latency_ms"], int)
    assert requests[0].url == DEFAULT_ALIYUN_ENDPOINT + "/health/ready"
    assert requests[0].method == "GET" and not requests[0].content
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers
    assert requests[0].headers["accept-encoding"] == "identity"
    assert options[0]["verify"] is True
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["timeout"].read == 5
    assert options[0]["timeout"].connect == 3


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (503, b'{"status":"ready"}'),
        (302, b'{"status":"ready"}'),
        (200, b'{"status":"starting"}'),
        (200, b"[]"),
        (200, b"not json"),
        (200, b" " * 5000 + b'{"status":"ready"}'),
    ],
)
def test_probe_rejects_nonready_redirect_malformed_and_oversized_bodies(status, payload):
    seen = []

    def response(request):
        seen.append(request.url)
        return httpx.Response(
            status, stream=httpx.ByteStream(payload), headers={"Location": "https://other.test"}
        )

    result = probe_backend(DEFAULT_ALIYUN_ENDPOINT, transport=httpx.MockTransport(response))
    assert result["state"] == "service_error"
    assert result["latency_ms"] is None
    assert len(seen) == 1
    assert "other.test" not in str(result)


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (httpx.ConnectTimeout("synthetic secret should not escape"), "timeout"),
        (httpx.ConnectError("CERTIFICATE_VERIFY_FAILED: synthetic secret"), "tls_error"),
        (httpx.ConnectError("synthetic private network name"), "offline"),
        (httpx.RemoteProtocolError("synthetic internals"), "offline"),
    ],
)
def test_probe_failures_are_fixed_state_codes(error, state):
    def fail(_):
        raise error

    result = probe_backend(DEFAULT_ALIYUN_ENDPOINT, transport=httpx.MockTransport(fail))
    assert result["state"] == state
    assert "synthetic" not in str(result)


def test_local_mode_does_not_request_any_endpoint():
    def forbidden(_):
        raise AssertionError("local mode must not connect")

    assert probe_backend("", transport=httpx.MockTransport(forbidden))["state"] == "local"
    with pytest.raises(ValueError):
        probe_backend("http://unverified.example", transport=httpx.MockTransport(forbidden))
    with pytest.raises(ValueError):
        probe_backend("https://user:secret@example.test", transport=httpx.MockTransport(forbidden))


def test_monitor_is_opt_in_single_flight_and_discards_stopped_generation(qtbot, monkeypatch):
    queued, changed = [], []

    class Pool:
        def start(self, task):
            queued.append(task)

    class PoolFactory:
        @staticmethod
        def globalInstance():
            return Pool()

    monkeypatch.setattr(module, "QThreadPool", PoolFactory)
    monkeypatch.setattr(module, "local_network_description", lambda: "本机合成适配器")
    monitor = NetworkStatusMonitor(DEFAULT_ALIYUN_ENDPOINT)
    monitor.changed.connect(changed.append)
    assert not queued and not monitor.timer.isActive()
    monitor.start()
    monitor.check_now()
    monitor.start()
    assert len(queued) == 1
    generation = monitor._generation
    result = {"state": "online", "latency_ms": 20, "checked_at": "synthetic"}
    monitor._accept(generation, result)
    assert changed[-1]["service_location"] == "中国·北京（阿里云服务节点）"
    assert changed[-1]["network"] == "本机合成适配器"
    monitor.stop()
    monitor._accept(generation, result)
    assert len(changed) == 1 and not monitor.timer.isActive()
    monitor._finished(queued[0])
    monitor.start()
    assert len(queued) == 2
    monitor.stop()


def test_custom_endpoint_never_claims_user_or_server_geolocation(qtbot):
    monitor = NetworkStatusMonitor("https://synthetic.example.test/service")
    changed = []
    monitor.changed.connect(changed.append)
    monitor._running = True
    monitor._accept(monitor._generation, {"state": "online", "latency_ms": 10})
    assert changed[0]["service_location"] == "自定义服务（属地未验证）"
    assert changed[0]["host"] == "synthetic.example.test"
    monitor.stop()


def test_slow_trickle_cannot_keep_probe_alive_or_report_late_ready(monkeypatch):
    elapsed, yielded, closed = [0.0], [], []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"{", b'"status":', b'"ready"}'):
                elapsed[0] += 3.0
                yielded.append(chunk)
                yield chunk

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module.time, "perf_counter", lambda: elapsed[0])
    result = probe_backend(
        DEFAULT_ALIYUN_ENDPOINT,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
    )
    assert result["state"] == "timeout" and result["latency_ms"] is None
    assert len(yielded) == 2 and closed


def test_late_headers_are_not_accepted_as_online(monkeypatch):
    elapsed = [0.0]

    def handler(_):
        elapsed[0] = 6.0
        return httpx.Response(200, stream=httpx.ByteStream(b'{"status":"ready"}'))

    monkeypatch.setattr(module.time, "perf_counter", lambda: elapsed[0])
    assert (
        probe_backend(DEFAULT_ALIYUN_ENDPOINT, transport=httpx.MockTransport(handler))["state"]
        == "timeout"
    )
