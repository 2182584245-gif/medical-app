"""Local Docker-only synthetic smoke. Creates new uniquely labelled lab resources, no cloud."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from . import guard, maintenance

SOURCE = Path(__file__).resolve().parents[2]
IMAGE = "medical-app-aliyun-api:v2"
LABEL = "medical-app.offline-review"


def docker(*args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["docker", *args], text=True, capture_output=True, check=True, timeout=timeout
    )
    return result.stdout.strip()


def smoke(*, confirm_local: bool) -> dict:
    if not confirm_local:
        raise RuntimeError("Explicit local synthetic Docker confirmation is required.")
    # Never run this lab through a remote Docker context.
    endpoint = json.loads(docker("context", "inspect"))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("npipe://", "unix://")):
        raise RuntimeError("Only the local Docker daemon is allowed.")
    nonce = uuid.uuid4().hex
    prefix = "medical-app-aliyun-review-" + nonce
    config, data, network = prefix + "-config", prefix + "-data", prefix + "-net"
    pg, prep, api = prefix + "-pg", prefix + "-prep", prefix + "-api"
    created_containers, created_volumes = [], []
    network_created = False
    with tempfile.TemporaryDirectory(prefix="medical-app-offline-") as temporary:
        marker = Path(temporary) / "deployment.json"
        try:
            for volume in (config, data):
                docker("volume", "create", "--label", LABEL + "=" + nonce, volume)
                created_volumes.append(volume)
            docker("network", "create", "--internal", "--label", LABEL + "=" + nonce, network)
            network_created = True
            code = """from pathlib import Path
from types import SimpleNamespace
from tools.aliyun_platform import prepare
original = prepare.subprocess.run
def local_run(args, **kwargs):
    if args[:3] == ['docker', 'volume', 'ls']:
        return SimpleNamespace(stdout='', returncode=0)
    return original(args, **kwargs)
prepare.subprocess.run = local_run
print(prepare.prepare(Path('/opt/medical-app'), '39.106.166.15', confirm_new=True))
"""
            created_containers.append(prep)
            docker(
                "run",
                "--name",
                prep,
                "--network",
                "none",
                "--label",
                LABEL + "=" + nonce,
                "-u",
                "0:0",
                "-w",
                "/review",
                "-e",
                "PYTHONPATH=/review:/review/src",
                "-v",
                str(SOURCE) + ":/review:ro",
                "-v",
                config + ":/opt/medical-app",
                IMAGE,
                "python",
                "-c",
                code,
            )
            docker("cp", prep + ":/opt/medical-app/deployment.json", str(marker))
            mounts = [
                "--mount",
                f"type=volume,source={config},target=/run/db-secrets,volume-subpath=secrets/postgres,readonly",
                "--mount",
                f"type=volume,source={config},target=/etc/postgresql,volume-subpath=config,readonly",
                "--mount",
                f"type=volume,source={config},target=/docker-entrypoint-initdb.d,volume-subpath=config,readonly",
                "--mount",
                f"type=volume,source={config},target=/backups,volume-subpath=backups",
            ]
            created_containers.append(pg)
            docker(
                "run",
                "-d",
                "--name",
                pg,
                "--label",
                LABEL + "=" + nonce,
                "--network",
                network,
                "--network-alias",
                "postgres",
                *mounts,
                "-v",
                data + ":/var/lib/postgresql/data",
                "-e",
                "POSTGRES_USER=aliyun_bootstrap",
                "-e",
                "POSTGRES_DB=medical_app_aliyun",
                "-e",
                "POSTGRES_PASSWORD_FILE=/run/db-secrets/bootstrap-password",
                "-e",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256",
                "postgres:17-bookworm",
                "postgres",
                "-c",
                "config_file=/etc/postgresql/postgresql.conf",
            )
            for _attempt in range(45):
                try:
                    docker(
                        "exec",
                        pg,
                        "pg_isready",
                        "-h",
                        "postgres",
                        "-U",
                        "aliyun_bootstrap",
                        "-d",
                        "medical_app_aliyun",
                        timeout=5,
                    )
                    break
                except subprocess.CalledProcessError:
                    time.sleep(1)
            else:
                raise RuntimeError("Isolated PostgreSQL did not become ready.")
            common = [
                "--network",
                network,
                "-e",
                "ALIYUN_DEPLOYMENT=medical-app:aliyun:v1",
                "-v",
                str(marker) + ":/run/deployment.json:ro",
                "--mount",
                f"type=volume,source={config},target=/run/api-secrets,volume-subpath=secrets/api-aliyun,readonly",
            ]
            bootstrap = [
                "run",
                "--rm",
                "-u",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges:true",
                *common,
                "--mount",
                f"type=volume,source={config},target=/run/db-secrets,volume-subpath=secrets/postgres,readonly",
                IMAGE,
                "python",
                "-c",
                "import json; from tools.aliyun_platform.bootstrap import bootstrap; "
                "print(json.dumps(bootstrap()))",
            ]
            first = json.loads(docker(*bootstrap))
            second = json.loads(docker(*bootstrap))
            assert first["created"] and not second["created"] and first["tables"] == 32
            created_containers.append(api)
            docker(
                "run",
                "-d",
                "--name",
                api,
                "--network-alias",
                "api-aliyun",
                "--label",
                LABEL + "=" + nonce,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                *common,
                "-e",
                "API_TARGET=aliyun",
                IMAGE,
            )
            # Production app readiness checks actual forced RLS and shared auth buckets.
            ready = docker(
                "run",
                "--rm",
                *common,
                "-e",
                "API_TARGET=aliyun",
                IMAGE,
                "python",
                "-c",
                "from tools.aliyun_platform.api_entrypoint import create_cloud_app; "
                "print(create_cloud_app().state.platform_auth.check_ready())",
            )
            assert "production_verified" in ready and "True" in ready
            probe = """import ssl, httpx, time
import secrets
context = ssl.create_default_context(cafile='/run/api-secrets/ca.crt')
for attempt in range(20):
    try:
        with httpx.Client(verify=context, trust_env=False) as client:
            response = client.get('https://api-aliyun:8443/health/ready',
                                  headers={'Host': '39.106.166.15'})
            assert response.status_code == 200, response.status_code
            registered = client.post('https://api-aliyun:8443/v1/auth/register',
                headers={'Host': '39.106.166.15'},
                json={'username': 'synthetic-restore-member',
                      'password': secrets.token_urlsafe(32)})
            assert registered.status_code == 201, registered.status_code
            print('verified_https_ready')
            break
    except httpx.ConnectError:
        time.sleep(1)
else:
    raise RuntimeError('API HTTPS did not become ready')
"""
            result = docker("run", "--rm", *common, IMAGE, "python", "-c", probe)
            assert "verified_https_ready" in result
            # Exercise the actual maintenance function against the new lab container.
            # Only filesystem/Compose routing is redirected, not backup/restore logic.
            review_root = Path(temporary)
            (review_root / "backups").mkdir()

            def lab_compose(*args, stdin=None, timeout=1800):
                assert args[0] == "exec"
                command = list(args)
                command.remove("-T")
                command.insert(1, "-i")
                command[command.index("postgres")] = pg
                completed = subprocess.run(
                    ["docker", *command],
                    input=stdin,
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=timeout,
                )
                if "pg_dump" in command:
                    filename = next(
                        value.split("/backups/", 1)[1]
                        for value in command
                        if value.startswith("--file=/backups/")
                    )
                    docker(
                        "cp", pg + ":/backups/" + filename, str(review_root / "backups" / filename)
                    )
                return completed.stdout.strip()

            with (
                patch.object(guard, "ROOT", review_root),
                patch.object(maintenance, "ROOT", review_root),
                patch.object(maintenance, "_compose", lab_compose),
            ):
                restored = maintenance.backup()
            assert restored["restored_row_counts"]["users"] == 1
            assert restored["restore_database_removed"] is True
            caddy_image = (
                "public.ecr.aws/docker/library/caddy:2-alpine@sha256:"
                "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
            )
            # Validation loads the private trust pool but never starts ACME/network listeners.
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,source={config},target=/etc/caddy,volume-subpath=config,readonly",
                "--mount",
                f"type=volume,source={config},target=/run,volume-subpath=config,readonly",
                caddy_image,
                "caddy",
                "validate",
                "--config",
                "/etc/caddy/Caddyfile",
            )
            return dict(
                offline_only=True,
                schema_tables=first["tables"],
                bootstrap_idempotent=True,
                production_readiness_verified=True,
                direct_https_verified=True,
                caddy_config_validated=True,
                custom_backup_restore_verified=True,
                restored_synthetic_users=restored["restored_row_counts"]["users"],
                cloud_connected=False,
                resources=prefix,
            )
        finally:
            # Delete only exact resources generated by this run after verifying labels.
            for name in reversed(created_containers):
                if (
                    docker("inspect", "-f", '{{ index .Config.Labels "' + LABEL + '" }}', name)
                    == nonce
                ):
                    docker("rm", "-f", name)
            if (
                network_created
                and docker(
                    "network", "inspect", "-f", '{{ index .Labels "' + LABEL + '" }}', network
                )
                == nonce
            ):
                docker("network", "rm", network)
            for name in created_volumes:
                if (
                    docker("volume", "inspect", "-f", '{{ index .Labels "' + LABEL + '" }}', name)
                    == nonce
                ):
                    docker("volume", "rm", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-local", action="store_true")
    arguments = parser.parse_args()
    print(json.dumps(smoke(confirm_local=arguments.confirm_local)))
