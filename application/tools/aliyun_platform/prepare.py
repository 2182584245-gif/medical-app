"""Create secrets/configuration once; never adopt volumes or import SQLite data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path

from .guard import ADMIN, DATABASE, MARKER, PROJECT, ROOT, DeploymentError, host_root, public_host

STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"


def _new(path: Path, value: str | bytes, mode: int = 0o600, uid: int | None = None) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value.encode() if isinstance(value, str) else value)
    path.chmod(mode)
    if uid is not None and os.name == "posix":
        os.chown(path, uid, uid)


def _openssl(*args: str) -> None:
    subprocess.run(
        ["openssl", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def _certificate(root: Path, folder: Path, name: str, uid: int) -> None:
    ca = root / "secrets/ca"
    _openssl(
        "req",
        "-new",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-subj",
        "/CN=" + name,
        "-keyout",
        str(folder / "server.key"),
        "-out",
        str(folder / "server.csr"),
    )
    extension = folder / "server.ext"
    _new(
        extension,
        f"subjectAltName=DNS:{name}\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n",
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(folder / "server.csr"),
        "-CA",
        str(ca / "ca.crt"),
        "-CAkey",
        str(ca / "ca.key"),
        "-set_serial",
        str(secrets.randbits(128)),
        "-days",
        "365",
        "-sha256",
        "-extfile",
        str(extension),
        "-out",
        str(folder / "server.crt"),
    )
    for filename in ("server.key", "server.crt"):
        (folder / filename).chmod(0o400)
        if os.name == "posix":
            os.chown(folder / filename, uid, uid)
    _new(folder / "ca.crt", (ca / "ca.crt").read_bytes(), 0o444)


def caddyfile(host: str, *, production: bool = False) -> str:
    public_host(host)
    directory = PRODUCTION if production else STAGING
    return f"""{{
    admin off
    persist_config off
    default_sni {host}
    log {{
        level WARN
    }}
}}
https://{host} {{
    tls {{
        issuer acme {{
            dir {directory}
            profile shortlived
        }}
    }}
    redir /aliyun /aliyun/ 308
    redir /supabase /supabase/ 308
    handle_path /aliyun/* {{
        reverse_proxy https://api-aliyun:8443 {{
            header_up Host {{hostport}}
            header_up -Forwarded
            transport http {{
                tls_trust_pool file /run/ca.crt
                tls_server_name api-aliyun
                response_header_timeout 180s
            }}
        }}
    }}
    handle_path /supabase/* {{
        reverse_proxy https://api-supabase:8443 {{
            header_up Host {{hostport}}
            header_up -Forwarded
            transport http {{
                tls_trust_pool file /run/ca.crt
                tls_server_name api-supabase
                response_header_timeout 180s
            }}
        }}
    }}
    respond "Medical platform gateway" 200
}}
"""


def prepare(root: Path, host: str, *, confirm_new: bool = False) -> dict:
    host_root(root)
    public_host(host)
    if not confirm_new:
        raise DeploymentError("New deployment must be explicitly confirmed.")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise DeploymentError("Existing deployment content is preserved; preparation refused.")
    # Fail closed if Docker is unreachable; never regenerate secrets against retained data.
    for suffix in ("pg-data", "caddy-data", "caddy-config"):
        result = subprocess.run(
            [
                "docker",
                "volume",
                "ls",
                "--format",
                "{{.Name}}",
                "--filter",
                f"name=^{PROJECT}-{suffix}$",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if result.stdout.strip():
            raise DeploymentError("A reserved data volume already exists; nothing changed.")
    subprocess.run(["openssl", "version"], check=True, capture_output=True, timeout=10)
    root.mkdir(mode=0o755, exist_ok=True)
    for name in (
        "secrets",
        "secrets/ca",
        "secrets/postgres",
        "secrets/api-aliyun",
        "secrets/api-supabase",
        "config",
        "backups",
        "state",
    ):
        folder = root / name
        folder.mkdir(mode=0o700)
    deployment = dict(kind=MARKER, id=uuid.uuid4().hex, public_host=host)
    _new(root / "deployment.json", json.dumps(deployment) + "\n", 0o444)
    ca = root / "secrets/ca"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        str(ca / "ca.key"),
        "-out",
        str(ca / "ca.crt"),
        "-days",
        "3650",
        "-subj",
        "/CN=Medical private deployment " + deployment["id"],
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    (ca / "ca.key").chmod(0o400)
    (ca / "ca.crt").chmod(0o444)
    admin_password, runtime_password = secrets.token_hex(32), secrets.token_hex(32)
    _new(root / "secrets/postgres/bootstrap-password", admin_password, 0o400, 999)
    _new(root / "secrets/postgres/pgpass", f"postgres:5432:*:{ADMIN}:{admin_password}\n", 0o400)
    _new(root / "secrets/api-aliyun/database-password", runtime_password, 0o400, 10001)
    for target in ("postgres", "api-aliyun", "api-supabase"):
        folder = root / "secrets" / target
        uid = 999 if target == "postgres" else 10001
        _certificate(root, folder, target, uid)
        folder.chmod(0o750)
        if os.name == "posix":
            os.chown(folder, 0, uid)
        if target != "postgres":
            _new(folder / "token-pepper", secrets.token_hex(32), 0o400, uid)
    _new(root / "config/ca.crt", (ca / "ca.crt").read_bytes(), 0o444)
    _new(root / "config/Caddyfile", caddyfile(host), 0o644)
    # Generated SQL only interpolates a generated hex id and fixed identifiers.
    _new(
        root / "config/init.sql",
        f"COMMENT ON DATABASE {DATABASE} IS '{MARKER}:{deployment['id']}';\n"
        f"REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;\n"
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n",
        0o644,
    )
    _new(
        root / "config/postgresql.conf",
        "listen_addresses='*'\nssl=on\nssl_min_protocol_version='TLSv1.2'\n"
        "ssl_cert_file='/run/db-secrets/server.crt'\n"
        "ssl_key_file='/run/db-secrets/server.key'\n"
        "hba_file='/etc/postgresql/pg_hba.conf'\npassword_encryption='scram-sha-256'\n"
        "max_connections=30\nshared_buffers='128MB'\nwork_mem='2MB'\n"
        "idle_in_transaction_session_timeout='30s'\nidle_session_timeout='10min'\n"
        f"medical_app.deployment_id='{deployment['id']}'\n",
        0o644,
    )
    source = Path(__file__).parent
    for name in ("compose.yaml", "pg_hba.conf"):
        _new(
            root / ("compose.yaml" if name == "compose.yaml" else "config/pg_hba.conf"),
            (source / name).read_bytes(),
            0o644,
        )
    _new(root / ".env", f"PUBLIC_HOST={host}\nSOURCE_ROOT=/opt/medical-app/source\n", 0o600)
    (root / "config").chmod(0o755)
    return {
        "prepared": True,
        "certificate_environment": "staging",
        "deployment_id": deployment["id"],
        "historical_data_imported": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--confirm-new", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(ROOT, args.public_host, confirm_new=args.confirm_new)))
        return 0
    except Exception:
        print("Preparation refused or incomplete; preserved files require operator review.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
