"""Host-side, local Docker-only capacity rehearsal; never opens cloud profiles.

Run from Windows using Python 3.12/3.13. Creates a new, uniquely named lab and
retains synthetic-only volumes. Stops only its own test containers at the end.
The complete host OS and image build are NOT restricted to 2 GiB by this test.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent.parent
CONTEXT = "desktop-linux"
IMAGE = "medical-app-local-backend:2gb-test"
MIB = 1024**2
EXPECTED = {"api": (1024 * MIB, 1_500_000_000), "postgres": (512 * MIB, 500_000_000)}
CGROUP_FILES = [
    "memory.current",
    "memory.peak",
    "memory.max",
    "memory.swap.max",
    "cpu.max",
    "cpuset.cpus.effective",
    "memory.events",
    "cpu.stat",
]


class TestFailure(RuntimeError):
    pass


def command(arguments, *, timeout=60, env=None, check=True):
    result = subprocess.run(
        ["docker", "--context", CONTEXT, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if result.returncode and check:
        # Never dump arbitrary docker logs, inspect configuration or build output.
        raise TestFailure(f"docker_{arguments[0]}_failed_exit_{result.returncode}")
    if not check:
        return result.stdout.strip(), result.returncode
    return result.stdout.strip()


def preflight():
    if os.name != "nt":
        raise TestFailure("windows_local_docker_desktop_required")
    for name in (
        "BUILDKIT_HOST",
        "BUILDX_CONFIG",
        "BUILDX_BAKE_FILE",
        "BUILDX_BAKE_FILE_SEPARATOR",
        "DOCKER_CONFIG",
    ):
        if os.environ.get(name):
            raise TestFailure("inherited_docker_routing_refused")
    if os.environ.get("BUILDX_BUILDER", CONTEXT) not in ("", CONTEXT):
        raise TestFailure("alternate_builder_refused")
    endpoint = command(["context", "inspect", CONTEXT, "--format", "{{.Endpoints.docker.Host}}"])
    if endpoint != "npipe:////./pipe/dockerDesktopLinuxEngine":
        raise TestFailure("nonlocal_docker_endpoint_refused")
    info = json.loads(command(["info", "--format", "{{json .}}"], timeout=30))
    if info["OSType"] != "linux" or info["Architecture"] != "x86_64" or info["NCPU"] < 3:
        raise TestFailure("linux_x64_with_three_host_cpus_required")
    builder = command(["buildx", "inspect", CONTEXT])
    for key, expected in (("Driver", "docker"), ("Endpoint", CONTEXT), ("Status", "running")):
        found = re.findall(rf"^{key}:\s*(\S+)\s*$", builder, re.MULTILINE)
        if found != [expected]:
            raise TestFailure("nonlocal_builder_refused")
    return {
        key: info[key] for key in ("OSType", "Architecture", "NCPU", "MemTotal", "ServerVersion")
    }


def parse_cgroup(raw):
    lines = raw.splitlines()
    if len(lines) < 6:
        raise TestFailure("cgroup_v2_metrics_unavailable")
    result = dict(
        zip(
            ("current_bytes", "peak_bytes", "max_bytes", "swap_max_bytes"),
            (int(value) for value in lines[:4]),
            strict=True,
        )
    )
    quota, period = lines[4].split()
    result.update(cpu_quota=int(quota), cpu_period=int(period), cpu_set=lines[5])
    for line in lines[6:]:
        key, value = line.split()
        result[key] = int(value)
    return result


def snapshot(identifier):
    raw = command(
        ["exec", identifier, "cat", *[f"/sys/fs/cgroup/{name}" for name in CGROUP_FILES]],
        timeout=10,
    )
    return parse_cgroup(raw)


def selected_inspect(identifier):
    data = json.loads(command(["inspect", identifier]))[0]
    host = data["HostConfig"]
    state = data["State"]
    return {
        "id": data["Id"],
        "image_id": data["Image"],
        "memory": host["Memory"],
        "memory_swap": host["MemorySwap"],
        "nano_cpus": host["NanoCpus"],
        "cpu_set": host["CpusetCpus"],
        "restart_policy": host["RestartPolicy"]["Name"],
        "restart_count": data["RestartCount"],
        "oom_killed": state["OOMKilled"],
        "running": state["Running"],
        "exit_code": state["ExitCode"],
        "started_at": state["StartedAt"],
        "finished_at": state["FinishedAt"],
        "health": state.get("Health", {}).get("Status"),
        "published_ports": data["NetworkSettings"]["Ports"],
        "networks": list(data["NetworkSettings"]["Networks"]),
    }


def validate_runtime(service, inspection, metric):
    memory, cpu = EXPECTED[service]
    if not (
        inspection["memory"] == inspection["memory_swap"] == metric["max_bytes"] == memory
        and inspection["nano_cpus"] == cpu
        and metric["swap_max_bytes"] == 0
        and metric["cpu_quota"] / metric["cpu_period"] == cpu / 1_000_000_000
        and inspection["cpu_set"] == metric["cpu_set"] == "0-1"
        and inspection["restart_policy"] == "no"
        and inspection["running"]
        and inspection["health"] == "healthy"
        and not inspection["oom_killed"]
        and inspection["restart_count"] == 0
        and not any(inspection["published_ports"].values())
    ):
        raise TestFailure(f"{service}_resource_or_isolation_validation_failed")


def business_report(raw):
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise TestFailure("invalid_client_report") from None
    if not isinstance(result, dict) or result.get("status") not in {"passed", "failed"}:
        raise TestFailure("invalid_client_report")
    return result


def validate_client_result(report, exit_code):
    if report.get("status") != "passed" or exit_code != 0:
        raise TestFailure("client_status_or_exit_code_failed")


def run():
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    project = "medical-app-2gb-test-" + uuid4().hex[:8]
    output = ROOT / "outputs" / "capacity-2gb" / (stamp + "-" + project[-8:])
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "failed",
        "stage": "preflight",
        "started_at_utc": stamp,
        "project": project,
        "cloud_connected": False,
        "ai_called": False,
        "real_data_used": False,
        "scope": "local_container_resource_budget_rehearsal",
        "server_memory_limit_mib": 1536,
        "server_cpu_quota_total": 2,
        "planning_os_headroom_mib": 512,
        "whole_2gib_vm_tested": False,
        "image_build_resource_limited": False,
        "caddy_included": False,
        "samples": [],
        "checks": {},
        "test_volumes_retained": True,
    }
    compose = [
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(LAB),
        "--env-file",
        str(LAB / "compose.env"),
        "-f",
        str(LAB / "compose.yaml"),
        "-f",
        str(LAB / "compose.2gb.yaml"),
    ]
    env = {**os.environ, "BUILDX_BUILDER": CONTEXT, "DOCKER_BUILDKIT": "1", "COMPOSE_BAKE": "false"}
    identifiers, baseline = {}, {}
    stop_sampling = threading.Event()
    sampler = None
    sampling_errors = []
    started = False

    def stage(name):
        report["stage"] = name
        print(json.dumps({"stage": name, "project": project}), flush=True)

    def check_client(name, arguments, timeout, *, required=True):
        raw, exit_code = command(compose + arguments, timeout=timeout, env=env, check=False)
        report["checks"][name] = business_report(raw)
        report["checks"][name]["container_exit_code"] = exit_code
        try:
            validate_client_result(report["checks"][name], exit_code)
        except TestFailure:
            if required:
                raise
            return False
        return True

    def sample_loop():
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            while not stop_sampling.is_set():
                try:
                    futures = {
                        service: pool.submit(snapshot, identifier)
                        for service, identifier in identifiers.items()
                    }
                    row = {service: future.result() for service, future in futures.items()}
                    row["elapsed_seconds"] = round(time.monotonic() - sample_start, 3)
                    row["stage"] = report["stage"]
                    report["samples"].append(row)
                except Exception:
                    sampling_errors.append("resource_sampling_failed")
                stop_sampling.wait(2)

    try:
        report["host"] = preflight()
        if command(["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]):
            raise TestFailure("existing_project_refused")
        command(compose + ["config", "--quiet"], env=env)
        stage("build_isolated_test_image")
        command(
            [
                "buildx",
                "build",
                "--builder",
                CONTEXT,
                "--file",
                str(LAB / "Dockerfile"),
                "--tag",
                IMAGE,
                "--load",
                str(ROOT),
            ],
            timeout=900,
            env=env,
        )
        stage("initialize_new_synthetic_database")
        started = True
        command(
            compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "api"],
            timeout=240,
            env=env,
        )
        for service in EXPECTED:
            identifier = command(compose + ["ps", "-q", service], env=env)
            if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                raise TestFailure("ambiguous_container_identity")
            identifiers[service] = identifier
            baseline[service] = snapshot(identifier)
            inspection = selected_inspect(identifier)
            validate_runtime(service, inspection, baseline[service])
            report.setdefault("runtime_before", {})[service] = inspection
            network = inspection["networks"]
            if network != [project + "_isolated"]:
                raise TestFailure("unexpected_container_network")
            if command(["network", "inspect", network[0], "--format", "{{.Internal}}"]) != "true":
                raise TestFailure("network_is_not_internal")
        report["baseline"] = baseline
        sample_start = time.monotonic()
        sampler = threading.Thread(target=sample_loop, daemon=True)
        sampler.start()
        stage("synthetic_functional_checks")
        check_client("initial", ["run", "--rm", "--no-deps", "smoke"], 180)
        stage("concurrent_clients_1_3_5")
        capacity_passed = check_client(
            "capacity",
            [
                "run",
                "--rm",
                "--no-deps",
                "smoke",
                "python",
                "-m",
                "tools.local_platform.capacity",
                "--stage-seconds",
                "60",
                "--observe-rate-limits",
            ],
            600,
            required=False,
        )
        report["capacity_gate_passed"] = capacity_passed
        stop_sampling.set()
        sampler.join(timeout=25)
        if sampler.is_alive() or sampling_errors or len(report["samples"]) < 10:
            raise TestFailure("incomplete_resource_measurements")
        report["before_restart"] = {
            service: snapshot(identifier) for service, identifier in identifiers.items()
        }
        for service, identifier in identifiers.items():
            inspection = selected_inspect(identifier)
            validate_runtime(service, inspection, report["before_restart"][service])
            if inspection["started_at"] != report["runtime_before"][service]["started_at"]:
                raise TestFailure("unexpected_server_restart")
            if any(
                report["before_restart"][service].get(key, 0) for key in ("oom", "oom_kill", "max")
            ):
                raise TestFailure("server_memory_limit_events_detected")
        stage("restart_and_persistence_checks")
        command(compose + ["stop", "api", "postgres"], timeout=90, env=env)
        command(compose + ["start", "postgres", "api"], timeout=60, env=env)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if all(
                selected_inspect(identifier)["health"] == "healthy"
                for identifier in identifiers.values()
            ):
                break
            time.sleep(2)
        else:
            raise TestFailure("restart_health_timeout")
        check_client(
            "persistence",
            [
                "run",
                "--rm",
                "--no-deps",
                "smoke",
                "python",
                "-m",
                "tools.local_platform.smoke",
                "--verify-persistence",
            ],
            180,
        )
        report["runtime_after_restart"] = {
            service: selected_inspect(identifier) for service, identifier in identifiers.items()
        }
        report["after_restart"] = {
            service: snapshot(identifier) for service, identifier in identifiers.items()
        }
        for service in EXPECTED:
            validate_runtime(
                service, report["runtime_after_restart"][service], report["after_restart"][service]
            )
            if any(
                report["after_restart"][service].get(key, 0) for key in ("oom", "oom_kill", "max")
            ):
                raise TestFailure("post_restart_memory_limit_events_detected")
        report.update(
            status="passed" if capacity_passed else "failed",
            stage="complete",
            test_workflow_completed=True,
        )
        if not capacity_passed:
            report["failure_code"] = "capacity_limits_or_errors_observed"
    except (TestFailure, subprocess.TimeoutExpired) as error:
        report["failure_code"] = str(error) if isinstance(error, TestFailure) else "command_timeout"
    except Exception:
        report["failure_code"] = "unexpected_failure_details_hidden"
    finally:
        stop_sampling.set()
        if sampler:
            sampler.join(timeout=25)
        report["sampling_error_count"] = len(sampling_errors)
        if started:
            try:
                command(compose + ["stop"], timeout=90, env=env)
                remaining = command(
                    ["ps", "-q", "--filter", f"label=com.docker.compose.project={project}"]
                )
                if remaining:
                    owned = remaining.splitlines()
                    if not all(re.fullmatch(r"[a-f0-9]{12,64}", value) for value in owned):
                        raise TestFailure("invalid_cleanup_container_ids")
                    command(["stop", *owned], timeout=90)
                if command(["ps", "-q", "--filter", f"label=com.docker.compose.project={project}"]):
                    raise TestFailure("test_containers_still_running")
                report["test_containers_stopped"] = True
            except Exception:
                report["test_containers_stopped"] = False
                report["status"] = "failed"
            try:
                report["retained_volumes"] = command(
                    [
                        "volume",
                        "ls",
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                        "--format",
                        "{{.Name}}",
                    ]
                ).splitlines()
            except Exception:
                report["retained_volumes"] = []
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        report_path = output / "result.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "stage": report["stage"],
                    "failure_code": report.get("failure_code"),
                    "report": str(report_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run())
