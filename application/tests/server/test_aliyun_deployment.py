"""Offline deployment contracts. No cloud connection, secret lookup or real account access."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import platform_experience_schema as schema
from tools.aliyun_platform import api_entrypoint, bootstrap, configure, guard, maintenance, prepare

SOURCE = Path(__file__).resolve().parents[2]
TOOLS = SOURCE / "tools/aliyun_platform"


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    root = tmp_path / "deployment"
    monkeypatch.setattr(guard, "ROOT", root)
    monkeypatch.setattr(configure, "ROOT", root)
    monkeypatch.setattr(maintenance, "ROOT", root)
    monkeypatch.setattr(
        prepare.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="", returncode=0)
    )

    def openssl(*args):
        for flag in ("-keyout", "-out"):
            if flag in args:
                path = Path(args[args.index(flag) + 1])
                path.write_text("SYNTHETIC CERTIFICATE ONLY; NOT REAL TLS MATERIAL")

    monkeypatch.setattr(prepare, "_openssl", openssl)
    return root


def test_prepare_new_has_separate_secrets_no_data_and_staging(isolated_root):
    root = isolated_root
    result = prepare.prepare(root, "39.106.166.15", confirm_new=True)
    assert result["historical_data_imported"] is False
    assert result["certificate_environment"] == "staging"
    values = [
        guard.secret(root / name)
        for name in (
            "secrets/postgres/bootstrap-password",
            "secrets/api-aliyun/database-password",
            "secrets/api-aliyun/token-pepper",
        )
    ]
    assert len(set(values)) == 3
    assert not (root / "secrets/api-supabase").exists()
    assert list((root / "secrets/ai-envelope").iterdir()) == []
    assert list((root / "secrets/ai-key").iterdir()) == []
    assert not list(root.rglob("*.db"))
    assert prepare.STAGING in (root / "config/Caddyfile").read_text()
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(guard.DeploymentError, match="preserved"):
        prepare.prepare(root, "39.106.166.15", confirm_new=True)
    assert all(path.read_bytes() == value for path, value in before.items())


def test_prepare_retained_volume_refused_before_any_write(isolated_root, monkeypatch):
    monkeypatch.setattr(
        prepare.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="medical-app-aliyun-pg-data\n"),
    )
    with pytest.raises(guard.DeploymentError, match="volume already exists"):
        prepare.prepare(isolated_root, "39.106.166.15", confirm_new=True)
    assert not isolated_root.exists()


@pytest.mark.parametrize(
    "value",
    ["localhost", "127.0.0.1", "10.0.0.1", "0.0.0.0", "example.com\nfoo", "../other", "abc..com"],
)
def test_public_host_rejects_private_and_injection(value):
    with pytest.raises(guard.DeploymentError):
        guard.public_host(value)


def test_prepare_requires_confirmation_and_exact_root(isolated_root):
    with pytest.raises(guard.DeploymentError, match="confirmed"):
        prepare.prepare(isolated_root, "39.106.166.15")
    with pytest.raises(guard.DeploymentError, match="exact"):
        guard.host_root(isolated_root.parent)


def test_caddy_public_ip_profile_and_verified_upstream():
    value = prepare.caddyfile("39.106.166.15")
    assert "profile shortlived" in value
    assert "default_sni 39.106.166.15" in value
    assert prepare.STAGING in value and prepare.PRODUCTION not in value
    assert "handle_path /aliyun/*" in value
    assert "reverse_proxy https://api-aliyun:8443" in value
    assert "tls_server_name api-aliyun" in value
    assert "@retired_supabase path /supabase /supabase/*" in value
    assert 'respond @retired_supabase "This runtime route has been retired." 410' in value
    assert "api-supabase" not in value and "redir /supabase" not in value
    assert value.count("header_up Host {hostport}") == 1
    assert value.count("tls_trust_pool file /run/ca.crt") == 1
    assert "tls_insecure_skip_verify" not in value
    assert prepare.PRODUCTION in prepare.caddyfile("39.106.166.15", production=True)


def test_production_promotion_is_explicit_and_preserves_secrets(isolated_root):
    prepare.prepare(isolated_root, "39.106.166.15", confirm_new=True)
    previous = (isolated_root / "secrets/api-aliyun/token-pepper").read_bytes()
    with pytest.raises(guard.DeploymentError, match="explicit"):
        configure.promote(confirm_production=False)
    configure.promote(confirm_production=True)
    assert prepare.PRODUCTION in (isolated_root / "config/Caddyfile").read_text()
    assert list((isolated_root / "config").glob("Caddyfile.staging-*"))
    assert previous == (isolated_root / "secrets/api-aliyun/token-pepper").read_bytes()
    with pytest.raises(guard.DeploymentError, match="differs"):
        configure.promote(confirm_production=True)


def test_repair_only_recognized_missing_ip_sni_config(isolated_root):
    root = isolated_root
    prepare.prepare(root, "39.106.166.15", confirm_new=True)
    path = root / "config/Caddyfile"
    for production in (False, True):
        desired = prepare.caddyfile("39.106.166.15", production=production)
        path.write_text(desired.replace("    default_sni 39.106.166.15\n", ""))
        configure.repair_ip_sni()
        assert path.read_text() == desired
    path.write_text("different-config")
    with pytest.raises(guard.DeploymentError, match="Unrecognized"):
        configure.repair_ip_sni()
    assert path.read_text() == "different-config"


@pytest.mark.parametrize("target", ["supabase", "", "unknown", "Aliyun"])
def test_retired_or_unknown_runtime_never_reads_secret(target, monkeypatch):
    monkeypatch.setattr(api_entrypoint, "secret", lambda _path: pytest.fail("No secret read"))
    with pytest.raises(guard.DeploymentError, match="Only the explicit Aliyun"):
        api_entrypoint.runtime_url(target)


def test_only_aliyun_runtime_uses_fixed_target_verify_full_and_restricted_identity(monkeypatch):
    calls = []

    def read(path):
        calls.append(path.name)
        return "synthetic-runtime-password-only"

    monkeypatch.setattr(api_entrypoint, "secret", read)
    url = api_entrypoint.runtime_url("aliyun")
    assert url.query["sslmode"] == "verify-full"
    assert url.query["sslrootcert"].replace("\\", "/") == "/run/api-secrets/ca.crt"
    assert url.username == guard.RUNTIME
    assert (url.host, url.port, url.database) == ("postgres", 5432, guard.DATABASE)
    assert calls == ["database-password"]


def test_schema_full_v2_and_strong_bootstrap_source():
    assert len(schema.PLATFORM_TABLES) == 32
    statements = "\n".join(bootstrap.ddl())
    assert statements.count("CREATE TABLE ") == 32
    assert statements.count("FORCE ROW LEVEL SECURITY") == 32
    assert "AS RESTRICTIVE" in statements
    for table in schema.NEW_COLUMNS:
        assert "GRANT " in statements and f"{schema.PLATFORM_SCHEMA}.{table}" in statements
    source = (TOOLS / "bootstrap.py").read_text()
    assert "verify-full" in source
    assert "catalog_digest" in source
    assert "CONNECTION LIMIT 5" in source
    assert "NOBYPASSRLS NOINHERIT" in source
    assert "ALTER ROLE" not in source
    assert "local_platform" not in source


def test_maintenance_never_prunes_archives_or_restores_over_live_database():
    source = (TOOLS / "maintenance.py").read_text()
    assert "--format=custom" in source
    assert "--exit-on-error" in source and "--single-transaction" in source
    assert "medical_app_verify_" in source
    assert "actual != ownership" in source
    assert "DROP DATABASE {verification_db}" in source
    assert "PGSSLMODE=verify-full" in source
    assert "unlink(" not in source and "rmtree(" not in source
    assert "password=" not in source


def test_compose_config_and_network_contract():
    if not shutil.which("docker"):
        pytest.skip("Docker CLI unavailable; source contracts still tested separately")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(TOOLS / "compose.yaml"),
            "--profile",
            "maintenance",
            "config",
            "--format",
            "json",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    config = json.loads(result.stdout)
    services = config["services"]
    assert all("ports" not in service for name, service in services.items() if name != "caddy")
    assert {port["published"] for port in services["caddy"]["ports"]} == {"80", "443"}
    assert config["networks"]["database"]["internal"] is True
    assert set(services["postgres"]["networks"]) == {"database"}
    assert set(services) == {"postgres", "bootstrap", "api-aliyun", "caddy"}
    for name in ("api-aliyun",):
        assert services[name]["read_only"] is True
        assert services[name]["user"] == "10001:10001"
        sources = [mount["source"] for mount in services[name]["volumes"]]
        assert any(name in source for source in sources)
        assert not any("postgres" in source for source in sources)
    api = services["api-aliyun"]
    assert api["environment"]["API_TARGET"] == "aliyun"
    assert (
        api["environment"]["HEALTHLIFE_AI_SECRET_ENVELOPE"] == "/run/ai-secrets/deepseek-key.aesgcm"
    )
    assert api["environment"]["HEALTHLIFE_AI_SECRET_KEY_FILE"] == "/run/ai-key/default-ai-kek.bin"
    assert api["environment"]["HEALTHLIFE_AI_GLOBAL_DAY_LIMIT"] == "100"
    mounts = {item["target"]: item for item in api["volumes"]}
    assert mounts["/run/ai-secrets"]["read_only"] and mounts["/run/ai-key"]["read_only"]
    assert mounts["/run/ai-secrets"]["source"] != mounts["/run/ai-key"]["source"]
    for name in ("postgres", "bootstrap", "caddy"):
        assert not {"/run/ai-key", "/run/ai-secrets"} & {
            item["target"] for item in services[name]["volumes"]
        }


def test_api_direct_tls_and_source_allowlist():
    entrypoint = (TOOLS / "api_entrypoint.py").read_text()
    assert "proxy_headers=False" in entrypoint
    assert 'env="production"' in entrypoint
    assert "check_ready()" in entrypoint
    included = [
        line[1:]
        for line in (TOOLS / "Dockerfile.dockerignore").read_text().splitlines()
        if line.startswith("!")
    ]
    assert "server/platform_experience_schema.py" in included
    assert "server/platform_sync.py" in included
    assert not any("/ui/" in path or "voice" in path or "ocr" in path for path in included)
    assert not any(path.endswith((".db", ".env", ".pem", ".key")) for path in included)
