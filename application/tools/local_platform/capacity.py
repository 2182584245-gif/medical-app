"""Bounded synthetic load against the fixed, isolated local HTTPS laboratory.

This does not invoke an AI model, read a cloud profile, change the smoke baseline,
or establish a production capacity claim. Test records are intentionally retained.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import random
import re
import ssl
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from uuid import uuid4

import httpx
from PIL import Image

from ollama_chat_app.services.cloud_rpc_codec import decode_rpc

from . import guard, smoke

CLIENT_COUNTS = (1, 3, 5)
INTERVAL_SECONDS = 0.3
MAX_CYCLES_PER_CLIENT = 400
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
_USER_TEXT = "本地容量测试合成消息，不调用 AI。"
_ANSWER_TEXT = "本地容量测试固定合成回答，不是 AI 生成结果。"
_RPC_METHODS = frozenset({
    ("chat", "create_conversation"), ("chat", "list_conversations"),
    ("chat", "begin_message"), ("chat", "complete_message"),
    ("chat", "list_messages"), ("files", "upload_bytes"), ("files", "get_file"),
})
_ERROR_CODES = frozenset({
    "configuration", "http_status", "timeout", "network", "tls", "response_invalid",
    "business_validation", "worker_failure", "cleanup_failure", "interrupted",
    "upload_capacity_limited",
})


class CapacityError(RuntimeError):
    def __init__(self, code, *, status_code=None):
        self.code = code if code in _ERROR_CODES else "worker_failure"
        self.counted = False
        self.status_code = (
            status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        )
        super().__init__(self.code)


def _require(condition, code="business_validation"):
    if not condition:
        raise CapacityError(code)


def _percentile(values, fraction):
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 3)


class Metrics:
    """Bounded statistics; error_count counts events, not failed HTTP requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.samples = []
        self.statuses = Counter()
        self.operation_statuses = Counter()
        self.errors = Counter()
        self.checks = 0
        self.cycles = 0
        self.uploads = 0

    def request(self, elapsed, status, error=None, *, operation=None):
        with self.lock:
            self.samples.append(max(0.0, elapsed) * 1000)
            if type(status) is int and 100 <= status <= 599:
                self.statuses[str(status)] += 1
                if operation is not None:
                    self.operation_statuses[(operation, str(status))] += 1
            if error is not None:
                self.errors[error if error in _ERROR_CODES else "worker_failure"] += 1

    def failure(self, code):
        with self.lock:
            self.errors[code if code in _ERROR_CODES else "worker_failure"] += 1

    def checked(self, condition):
        _require(condition)
        with self.lock:
            self.checks += 1

    def completed(self, *, uploaded=False):
        with self.lock:
            self.cycles += 1
            self.uploads += int(uploaded)

    def summary(self):
        with self.lock:
            values = list(self.samples)
            return {
                "request_count": len(values), "error_count": sum(self.errors.values()),
                "http_status_counts": dict(sorted(self.statuses.items())),
                "operation_http_status_counts": {
                    f"{operation} {status}": count
                    for (operation, status), count in sorted(self.operation_statuses.items())
                },
                "error_codes": dict(sorted(self.errors.items())),
                "latency_ms": {
                    "p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95),
                    "max": round(max(values, default=0.0), 3),
                },
                "business_checks_passed": self.checks,
                "cycles_completed": self.cycles, "files_verified": self.uploads,
            }


def _create_client():
    return httpx.Client(
        base_url=smoke.API_ORIGIN,
        verify=ssl.create_default_context(cafile=str(smoke.CA_FILE)),
        trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=5, pool=5),
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    )


class Transport:
    def __init__(self, client, metrics):
        self.client, self.metrics = client, metrics

    def request(self, method, path, *, status=200, **kwargs):
        allowed = {("POST", "/v1/auth/login"), ("POST", "/v1/auth/logout")}
        allowed.update(("POST", f"/v1/rpc/{service}/{operation}")
                       for service, operation in _RPC_METHODS)
        _require((method, path) in allowed, "configuration")
        start, actual_status, error = time.monotonic(), None, None
        try:
            response = self.client.request(method, path, **kwargs)
            actual_status = response.status_code
            if actual_status != status:
                category = "http_status"
                if actual_status == 429 and len(response.content) <= MAX_RESPONSE_BYTES:
                    try:
                        detail = response.json()
                        if (
                            isinstance(detail, dict)
                            and detail.get("detail") == "Upload capacity limited"
                        ):
                            category = "upload_capacity_limited"
                    except Exception:
                        pass
                raise CapacityError(category, status_code=actual_status)
            _require(len(response.content) <= MAX_RESPONSE_BYTES, "response_invalid")
            if status == 204:
                _require(not response.content, "response_invalid")
                return None
            result = response.json()
            _require(isinstance(result, dict), "response_invalid")
            return result
        except CapacityError as caught:
            error = caught.code
            caught.counted = True
            raise
        except httpx.TimeoutException:
            error = "timeout"
            caught = CapacityError(error)
            caught.counted = True
            raise caught from None
        except (ssl.SSLError, ssl.CertificateError):
            error = "tls"
            caught = CapacityError(error)
            caught.counted = True
            raise caught from None
        except httpx.HTTPError:
            error = "network"
            caught = CapacityError(error)
            caught.counted = True
            raise caught from None
        except Exception:
            error = "response_invalid"
            caught = CapacityError(error)
            caught.counted = True
            raise caught from None
        finally:
            self.metrics.request(time.monotonic() - start, actual_status, error, operation=path)

    def rpc(self, service, method, *args, **kwargs):
        _require((service, method) in _RPC_METHODS, "configuration")
        body = smoke._payload(args, kwargs)
        # The platform's total request-body limit is 1 MiB, including base64/JSON.
        _require(len(json.dumps(body).encode("utf-8")) <= 1024 * 1024, "configuration")
        result = self.request("POST", f"/v1/rpc/{service}/{method}", json=body)
        _require(set(result) == {"result"}, "response_invalid")
        try:
            return decode_rpc(result["result"])
        except Exception:
            raise CapacityError("response_invalid") from None


def _picture():
    # Deterministic synthetic pixels: approximately 49 KiB, valid static PNG.
    picture = Image.frombytes("RGB", (128, 128), random.Random(260907).randbytes(128 * 128 * 3))
    output = io.BytesIO()
    picture.save(output, format="PNG")
    content = output.getvalue()
    _require(0 < len(content) <= 64 * 1024, "configuration")
    return content


def _login(transport, account):
    result = transport.request("POST", "/v1/auth/login", json={
        "username": account["username"], "password": account["password"],
    })
    token = result.get("access_token")
    _require(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token),
             "response_invalid")
    # Retain a validly-shaped returned token for finally/logout even if identity is wrong.
    transport.client.headers["Authorization"] = "Bearer " + token
    user = result.get("user", {})
    transport.metrics.checked(user.get("id") == account["id"] and user.get("role_code") == "member")


def _cycle(transport, actor, conversation, image, index, metrics):
    listed = transport.rpc("chat", "list_conversations", actor)
    metrics.checked(conversation in {item.id for item in listed})
    exchange = transport.rpc(
        "chat", "begin_message", actor, _USER_TEXT, "deepseek_cloud", "synthetic-no-ai-call",
        conversation_id=conversation,
    )
    metrics.checked(exchange.conversation.id == conversation)
    transport.rpc("chat", "complete_message", actor, exchange.assistant_message.id, _ANSWER_TEXT)
    messages = transport.rpc("chat", "list_messages", actor, conversation_id=conversation, limit=2)
    metrics.checked(
        [item.id for item in messages] == [exchange.user_message.id, exchange.assistant_message.id]
        and [item.content for item in messages] == [_USER_TEXT, _ANSWER_TEXT]
        and all(item.status == "complete" for item in messages)
    )
    uploaded = index % 10 == 0
    if uploaded:
        identifier = transport.rpc("files", "upload_bytes", actor, "capacity-synthetic.png", image)
        file = transport.rpc("files", "get_file", actor, identifier)
        metrics.checked(hashlib.sha256(file["content"]).digest() == hashlib.sha256(image).digest())
    metrics.completed(uploaded=uploaded)


def _worker(transport, actor, conversation, image, deadline, metrics, observe_rate_limits=False):
    completed_cycles = 0
    for index in range(MAX_CYCLES_PER_CLIENT):
        if time.monotonic() >= deadline:
            break
        try:
            _cycle(transport, actor, conversation, image, index, metrics)
            completed_cycles += 1
        except CapacityError as error:
            # Transport already counts wire errors; semantic errors are counted here.
            if not error.counted:
                metrics.failure(error.code)
            if observe_rate_limits and error.status_code == 429:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.5, remaining))
                # A new cycle uses new request IDs. Never replay a failed write silently.
                continue
            return completed_cycles
        except Exception:
            metrics.failure("worker_failure")
            return completed_cycles
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(INTERVAL_SECONDS, remaining))
    return completed_cycles


def _stage(accounts, baseline_conversations, count, seconds, image, factory,
           observe_rate_limits=False):
    setup, load, cleanup = Metrics(), Metrics(), Metrics()
    clients, workers = [], []
    per_client_cycles = []
    started, elapsed, fatal = time.monotonic(), 0.0, None
    try:
        with ExitStack() as stack:
            try:
                for index in range(count):
                    client = stack.enter_context(factory())
                    clients.append(client)
                    transport = Transport(client, setup)
                    account = accounts[index % len(accounts)]
                    _login(transport, account)
                    conversation = transport.rpc(
                        "chat", "create_conversation", account["id"],
                        "本地容量测试独立会话 " + uuid4().hex[:12],
                    )
                    setup.checked(conversation.id not in baseline_conversations)
                    setup.checked(conversation.id not in {item[2] for item in workers})
                    workers.append((client, account["id"], conversation.id))
                started = time.monotonic()
                deadline = started + seconds
                with ThreadPoolExecutor(
                    max_workers=count, thread_name_prefix="local-capacity",
                ) as pool:
                    futures = [pool.submit(
                        _worker, Transport(client, load), actor, conversation,
                        image, deadline, load, observe_rate_limits,
                    ) for client, actor, conversation in workers]
                    for future in futures:
                        per_client_cycles.append(future.result())
                elapsed = time.monotonic() - started
                if len(per_client_cycles) != count or not all(
                    type(cycles) is int and cycles >= 1 for cycles in per_client_cycles
                ):
                    load.failure("business_validation")
            finally:
                for client in clients:
                    if "Authorization" in client.headers:
                        try:
                            Transport(client, cleanup).request(
                                "POST", "/v1/auth/logout", status=204,
                            )
                        except Exception:
                            cleanup.failure("cleanup_failure")
                        finally:
                            client.headers.pop("Authorization", None)
    except CapacityError as error:
        fatal = error.code
        if not error.counted:
            setup.failure(error.code)
    except Exception:
        fatal = "worker_failure"
    summaries = {"setup": setup.summary(), "load": load.summary(), "cleanup": cleanup.summary()}
    error_count = sum(item["error_count"] for item in summaries.values())
    rate_limit_only = (
        fatal is None and error_count > 0
        and summaries["setup"]["error_count"] == summaries["cleanup"]["error_count"] == 0
        and set(summaries["load"]["error_codes"]) <= {"http_status", "upload_capacity_limited"}
        and set(summaries["load"]["http_status_counts"]) <= {"200", "429"}
        and int(summaries["load"]["http_status_counts"].get("429", 0)) > 0
    )
    return {
        "clients": count, "requested_load_seconds": seconds,
        "actual_load_seconds": round(elapsed, 3),
        "per_client_cycles": per_client_cycles,
        "rate_limit_only_failure": rate_limit_only,
        "status": "passed" if fatal is None and error_count == 0 else "failed",
        "failure_code": fatal, "request_count": sum(v["request_count"] for v in summaries.values()),
        "error_count": error_count + int(fatal is not None and error_count == 0),
        **summaries,
    }


def run(*, stage_seconds=30, observe_rate_limits=False, _client_factory=None):
    report = {
        "status": "failed", "code": "configuration",
        "mode": "offline_contract_test" if _client_factory else "isolated_local_https_capacity",
        "cloud_connected": False, "ai_called": False, "synthetic_answers_only": True,
        "production_capacity_claim": False, "synthetic_data_retained": False,
        "client_process_is_external_load_generator": True,
        "scope": "short synthetic API and PostgreSQL exercise; not long-term or cloud capacity",
        "interval_seconds": INTERVAL_SECONDS, "stages": [],
        "duration_note": "New cycles stop at the stage deadline; in-flight cycles drain.",
        "error_count_note": "Counts error events, not failed HTTP requests.",
        "observe_rate_limits": observe_rate_limits is True,
        "rate_limit_policy": (
            "Observation only: retain 429 failures, wait 0.5s, begin a new cycle; "
            "higher concurrency includes backoff and is not an all-success capacity claim."
            if observe_rate_limits is True else "Stop a worker on any unexpected request failure."
        ),
    }
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        _require(type(stage_seconds) is int and 30 <= stage_seconds <= 120, "configuration")
        _require(type(observe_rate_limits) is bool, "configuration")
        guard.require_local_container()
        state = smoke._load_state()
        image = _picture()
        report["file_bytes"] = len(image)
        report["synthetic_data_retained"] = True
        for count in CLIENT_COUNTS:
            stage = _stage(state["accounts"], set(state["conversation_ids"]), count,
                           stage_seconds, image, _client_factory or _create_client,
                           observe_rate_limits)
            report["stages"].append(stage)
            if stage["status"] != "passed" and not (
                observe_rate_limits and stage["rate_limit_only_failure"]
            ):
                report["code"] = "stage_failed"
                break
        else:
            if any(stage["status"] != "passed" for stage in report["stages"]):
                report.update(status="failed", code="rate_limits_observed")
            else:
                report.update(status="passed", code="ok")
    except (CapacityError, smoke.LocalSmokeError):
        # State validation errors deliberately do not echo state fields or paths.
        report["code"] = "configuration"
    except KeyboardInterrupt:
        report["code"] = "interrupted"
    except Exception:
        report["code"] = "configuration"
    report["request_count"] = sum(stage["request_count"] for stage in report["stages"])
    report["error_count"] = sum(stage["error_count"] for stage in report["stages"])
    return report


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CapacityError("configuration")


def main(argv=None):
    parser = _SafeParser(description="Fixed local synthetic capacity checks", add_help=False)
    parser.add_argument("--stage-seconds", type=int, default=30)
    parser.add_argument("--observe-rate-limits", action="store_true")
    try:
        args = parser.parse_args(argv)
        report = run(stage_seconds=args.stage_seconds, observe_rate_limits=args.observe_rate_limits)
    except CapacityError:
        report = {"status": "failed", "code": "configuration", "cloud_connected": False,
                  "ai_called": False}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
