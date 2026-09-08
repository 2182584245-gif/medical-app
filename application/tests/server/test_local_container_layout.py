"""Local deployment boundaries; these tests do not claim a real Docker run."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from tools.local_platform import api_entrypoint, guard, prepare

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "tools" / "local_platform"


def allowed(relative):
    candidates = [str(PurePosixPath(relative))]
    candidates += [str(p) for p in PurePosixPath(relative).parents if str(p) != "."]
    included = True
    for line in (LAB / "Dockerfile.dockerignore").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        admit = line.startswith("!")
        pattern = line.removeprefix("!").strip("/")
        regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            included = admit
    return included


@pytest.mark.parametrize(
    "path",
    [
        ".local/supabase-connection.json",
        "data/app.db",
        ".env",
        "server/cloud_connection.py",
        "server/platform_admin.py",
        "server/local_secret_store.py",
        "server/railway_connection.py",
        "server/supabase-project.json",
        "server/new_unknown.py",
        "assets/model.onnx",
        "src/ollama_chat_app/main.py",
        "src/ollama_chat_app/providers/deepseek.py",
        "src/ollama_chat_app/ui/login.py",
        "tools/local_platform/secret.py",
        "tools/local_platform/ca.key",
        "tools/local_platform/private.env",
        "tests/test_private.py",
        "requirements.txt",
        "requirements-lock.txt",
        "requirements-server.txt",
        "pyproject.toml",
        ".git/config",
        "tools/local_platform/requirements-private.txt",
    ],
)
def test_build_context_denies_private_desktop_and_unlisted_files(path):
    assert not allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "tools/local_platform/requirements-linux-lock.txt",
        "server/platform_app.py",
        "server/schema.py",
        "server/platform_schema.py",
        "src/ollama_chat_app/services/chat.py",
        "tools/local_platform/prepare.py",
        "tools/local_platform/bootstrap.py",
        "tools/local_platform/smoke.py",
    ],
)
def test_build_context_includes_only_needed_public_runtime_files(path):
    assert allowed(path)


def test_docker_definition_is_headless_pinned_and_nonroot():
    content = (LAB / "Dockerfile").read_text()
    assert "FROM python:3.13.15-slim-bookworm@sha256:" in content
    assert "USER 10001:10001" in content
    assert (
        'RUN python -c "import server.platform_app; import tools.local_platform.smoke"' in content
    )
    assert "pip check" in content
    assert "--require-hashes" in content and "--only-binary=:all:" in content
    assert "-r /app/requirements-linux-lock.txt" in content
    assert "requirements-server.txt" not in content
    assert "COPY . " not in content
    assert "pip install ." not in content
    assert "PLATFORM_DATABASE_URL" not in content


def _linux_lock():
    content = (LAB / "requirements-linux-lock.txt").read_text()
    entries = [
        line.strip()
        for line in content.replace("\\\n", " ").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    pattern = r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s+--hash=sha256:([a-f0-9]{64})"
    result = {}
    for entry in entries:
        match = re.fullmatch(pattern, entry)
        assert match is not None, "Every Linux dependency must have an exact version and SHA256"
        name, version, digest = match.groups()
        name = re.sub(r"[-_.]+", "-", name).lower()
        assert name not in result, "Duplicate normalized dependency"
        result[name] = (version, digest)
    return content, result


def test_linux_lock_pins_all_35_runtime_and_test_dependencies_with_hashes():
    content, entries = _linux_lock()
    assert "Linux x86_64" in content and "CPython 3.13.15" in content
    assert len(entries) == 35
    assert {"fastapi", "psycopg", "psycopg-binary", "pydantic-core", "greenlet"} <= entries.keys()
    assert not ({"pyside6", "onnxruntime", "vosk", "rapidocr", "ollama"} & entries.keys())


def test_linux_lock_retains_current_server_direct_versions():
    _content, locked = _linux_lock()
    for line in (ROOT / "requirements-server.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        name = re.sub(r"[-_.]+", "-", name.split("[", 1)[0]).lower()
        assert locked[name][0] == version


def test_compose_fixed_local_network_and_separate_secret_volumes():
    content = (LAB / "compose.yaml").read_text()
    assert "name: medical-app-local-lab" in content
    assert "internal: true" in content
    assert "ports:" not in content
    assert "18443" not in content
    assert "${" not in content
    assert "env_file:" not in content and "privileged:" not in content
    assert "network_mode: host" not in content
    api = content.split("\n  api:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    assert "db-secrets" not in api and "ca-private" not in api
    assert "api-secrets:/run/api-secrets:ro" in api
    assert "ssl=on" in content
    hba = (LAB / "pg_hba.conf").read_text()
    assert "hostnossl all all all reject" in hba
    assert "host all all all reject" in hba


@pytest.mark.parametrize(
    "key",
    [
        "PLATFORM_DATABASE_URL",
        "SERVER_DATABASE_URL",
        "SUPABASE_KEY",
        "RAILWAY_PROJECT_ID",
        "RENDER_SERVICE_ID",
        "PGHOSTADDR",
        "PGSERVICE",
        "PGOPTIONS",
    ],
)
def test_guard_rejects_inherited_cloud_or_libpq_configuration(monkeypatch, key):
    monkeypatch.setenv("LOCAL_CONTAINER_ONLY", "1")
    monkeypatch.setattr(guard.sys, "platform", "linux")
    monkeypatch.setattr(guard.Path, "is_file", lambda _p: True)
    monkeypatch.setenv(key, "synthetic-do-not-use")
    with pytest.raises(RuntimeError, match="isolated"):
        guard.require_local_container()


def test_guard_requires_container_and_explicit_gate(monkeypatch):
    monkeypatch.delenv("LOCAL_CONTAINER_ONLY", raising=False)
    with pytest.raises(RuntimeError):
        guard.require_local_container()
    monkeypatch.setenv("LOCAL_CONTAINER_ONLY", "1")
    monkeypatch.setattr(guard.sys, "platform", "linux")
    monkeypatch.setattr(guard.Path, "is_file", lambda _p: False)
    with pytest.raises(RuntimeError):
        guard.require_local_container()


def test_runtime_url_is_fixed_and_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "require_local_container", lambda: None)
    monkeypatch.setattr(guard, "API_SECRETS", tmp_path)
    (tmp_path / "database-password").write_text("0123456789abcdef" * 4)
    url = guard.runtime_url()
    assert (url.host, url.port, url.database) == ("postgres", 5432, "medical_app_local")
    assert url.username == "medical_app_platform_runtime"
    assert url.query["sslmode"] == "verify-full"
    assert url.query["sslrootcert"] == str(tmp_path / "ca.crt")


@pytest.mark.parametrize("value", ["", "short", "z" * 64, "a" * 129])
def test_secret_parser_rejects_invalid_values_without_echo(tmp_path, value):
    path = tmp_path / "secret"
    path.write_text(value)
    with pytest.raises(RuntimeError) as error:
        guard.read_secret(path)
    assert "secret is missing or invalid" in str(error.value)
    if value:
        assert value not in str(error.value)


def test_partial_prepare_volumes_are_never_reset(monkeypatch, tmp_path):
    roots = [tmp_path / name for name in ("ca", "db", "api")]
    for root in roots:
        root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    preserved = roots[1] / "postgres-password"
    preserved.write_text("synthetic-existing-value")
    monkeypatch.setattr(prepare, "FILES", {root: {"bundle.json"} for root in roots})
    monkeypatch.setattr(prepare, "SMOKE_STATE", state)
    with pytest.raises(RuntimeError, match="Partial"):
        prepare.existing_bundle()
    assert preserved.read_text() == "synthetic-existing-value"


def test_api_entrypoint_keeps_production_security_unchanged():
    content = (LAB / "api_entrypoint.py").read_text()
    assert "proxy_headers=False" in content
    assert "require_https=True" in content
    assert "platform_auth.check_ready()" in content
    assert 'host="0.0.0.0", port=8443' in content
    assert "ssl_certfile=" in content and "ssl_keyfile=" in content
    assert "platform_admin" not in content
    assert api_entrypoint.MARKER == guard.MARKER


def test_source_sync_admits_exact_deployment_paths_not_all_yaml():
    script = (ROOT / "tools" / "sync_repository_source.ps1").read_text()
    for path in ("Dockerfile", "Dockerfile.dockerignore", "compose.yaml", "compose.env"):
        assert f"'tools/local_platform/{path}'" in script
    extension_block = script.split("$script:TextExtensions = @(", 1)[1].split(")", 1)[0]
    assert "'.yaml'" not in extension_block and "'.env'" not in extension_block


def test_wrapper_pins_local_engine_and_builder_without_global_changes():
    script = (LAB / "local_lab.ps1").read_text()
    assert "$contextName = 'desktop-linux'" in script
    assert "$builderName = 'desktop-linux'" in script
    assert "npipe:////./pipe/dockerDesktopLinuxEngine" in script
    assert "BUILDKIT_HOST" in script and "BUILDX_CONFIG" in script
    assert "'buildx', 'build'" in script and "'--builder', $builderName" in script
    assert "'--load'" in script and "'--no-build'" in script
    assert "finally {" in script and "$savedBuildEnvironment.GetEnumerator()" in script
    assert "Set-ExecutionPolicy" not in script
    assert "buildx use" not in script and "'--volumes'" not in script
