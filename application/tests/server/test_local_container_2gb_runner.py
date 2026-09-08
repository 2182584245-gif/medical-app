"""Offline checks for measurement gates, not evidence of a live 2 GiB host."""

from __future__ import annotations

import copy
import json

import pytest

from tools.local_platform import run_2gb_test as runner


def metric_text():
    return "\n".join(
        [
            "100000000",
            "200000000",
            str(1024**3),
            "0",
            "150000 100000",
            "0-1",
            "low 0",
            "high 0",
            "max 0",
            "oom 0",
            "oom_kill 0",
            "oom_group_kill 0",
            "usage_usec 100",
            "user_usec 50",
            "system_usec 50",
            "nr_periods 30",
            "nr_throttled 10",
            "throttled_usec 5",
        ]
    )


def valid_inspection():
    return {
        "memory": 1024**3,
        "memory_swap": 1024**3,
        "nano_cpus": 1_500_000_000,
        "cpu_set": "0-1",
        "restart_policy": "no",
        "running": True,
        "health": "healthy",
        "oom_killed": False,
        "restart_count": 0,
        "published_ports": {"8443/tcp": None},
    }


def test_cgroup_metrics_keep_kernel_peak_and_oom_counters():
    metric = runner.parse_cgroup(metric_text())
    assert metric["peak_bytes"] == 200000000
    assert metric["max_bytes"] == 1024**3
    assert metric["swap_max_bytes"] == 0
    assert metric["cpu_quota"] / metric["cpu_period"] == 1.5
    assert metric["oom"] == metric["oom_kill"] == 0
    assert metric["nr_throttled"] == 10
    runner.validate_runtime("api", valid_inspection(), metric)


@pytest.mark.parametrize(
    "key,value",
    [
        ("memory", 2 * 1024**3),
        ("memory_swap", 2 * 1024**3),
        ("nano_cpus", 2_000_000_000),
        ("cpu_set", "0-27"),
        ("restart_policy", "unless-stopped"),
        ("running", False),
        ("health", "unhealthy"),
        ("oom_killed", True),
        ("restart_count", 1),
        ("published_ports", {"8443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8443"}]}),
    ],
)
def test_runtime_mismatch_is_not_a_pass(key, value):
    inspection = valid_inspection()
    inspection[key] = value
    with pytest.raises(runner.TestFailure):
        runner.validate_runtime("api", inspection, runner.parse_cgroup(metric_text()))


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_bytes", 2 * 1024**3),
        ("swap_max_bytes", 1024**3),
        ("cpu_quota", 200000),
        ("cpu_set", "0-27"),
    ],
)
def test_actual_cgroup_not_just_compose_text_is_checked(key, value):
    metric = runner.parse_cgroup(metric_text())
    metric[key] = value
    with pytest.raises(runner.TestFailure):
        runner.validate_runtime("api", valid_inspection(), metric)


def test_api_and_database_budget_leaves_os_headroom():
    assert sum(item[0] for item in runner.EXPECTED.values()) == 1536 * runner.MIB
    assert sum(item[1] for item in runner.EXPECTED.values()) == 2_000_000_000
    data = (runner.LAB / "compose.2gb.yaml").read_text()
    assert 'cpuset: "2"' in data
    assert data.count('restart: "no"') == 2
    assert "mem_limit: 1024m" in data and "memswap_limit: 1024m" in data
    assert "mem_limit: 512m" in data and "memswap_limit: 512m" in data
    assert "ports:" not in data and "external:" not in data


def test_safe_failed_client_report_is_retained_for_diagnosis():
    failed = {"status": "failed", "code": "timeout", "requests": 10}
    assert runner.business_report(json.dumps(failed)) == failed
    with pytest.raises(runner.TestFailure):
        runner.business_report("not a client report")


@pytest.mark.parametrize("status,exit_code", [("passed", 1), ("failed", 0), ("failed", 1)])
def test_passed_json_cannot_hide_nonzero_container_exit(status, exit_code):
    with pytest.raises(runner.TestFailure):
        runner.validate_client_result({"status": status}, exit_code)


def test_command_keeps_nonzero_exit_for_safe_report_collection(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=7, stdout='{"status":"passed"}'),
    )
    raw, exit_code = runner.command(["compose"], check=False)
    assert exit_code == 7
    assert json.loads(raw)["status"] == "passed"
    with pytest.raises(runner.TestFailure):
        runner.command(["compose"])


def test_inspection_filters_out_environment_and_credentials(monkeypatch):
    document = {
        "Id": "abc",
        "Image": "sha256:abc",
        "Config": {"Env": ["SECRET=must-not-appear"]},
        "HostConfig": {
            "Memory": 1024**3,
            "MemorySwap": 1024**3,
            "NanoCpus": 1_500_000_000,
            "CpusetCpus": "0-1",
            "RestartPolicy": {"Name": "no"},
        },
        "State": {
            "OOMKilled": False,
            "Running": True,
            "ExitCode": 0,
            "StartedAt": "start",
            "FinishedAt": "finish",
            "Health": {"Status": "healthy"},
        },
        "RestartCount": 0,
        "NetworkSettings": {"Ports": {}, "Networks": {"isolated": {}}},
    }
    monkeypatch.setattr(
        runner, "command", lambda *args, **kwargs: json.dumps([copy.deepcopy(document)])
    )
    data = runner.selected_inspect("abc")
    assert "SECRET" not in json.dumps(data)
    assert data["memory"] == 1024**3
    assert data["health"] == "healthy"
