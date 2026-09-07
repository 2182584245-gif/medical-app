"""Offline layout checks, not Docker builds or Railway deployment validation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

PROJECT = Path(__file__).resolve().parents[2]
DEPLOYMENT = PROJECT / "tools" / "railway_platform"


def _rules():
    return [line.strip() for line in (DEPLOYMENT / "Dockerfile.dockerignore").read_text(
        encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def _allowed(relative: str) -> bool:
    """Model this deliberately small subset of Docker patterns, including parents.

    We do not claim to replace Docker's patternmatcher. The paired directory
    allow + subtree deny rules also keep unlisted children denied when parent
    matches propagate, instead of accidentally reopening a whole source tree.
    """
    candidate = PurePosixPath(relative)
    candidates = [str(candidate), *(str(parent) for parent in candidate.parents
                                   if str(parent) != ".")]
    included = True
    for rule in _rules():
        admit = rule.startswith("!")
        pattern = rule.removeprefix("!").strip("/")
        assert "?" not in pattern and "[" not in pattern and "\\" not in pattern
        expression = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if any(re.fullmatch(expression, value) for value in candidates):
            included = admit
    return included


def test_dockerfile_pins_python_and_only_installs_server_requirements():
    text = (DEPLOYMENT / "Dockerfile").read_text(encoding="utf-8")
    commands = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "FROM python:3.13.15-slim-bookworm" in commands
    assert "python -m pip install --no-cache-dir --only-binary=:all:" in commands
    assert "-r /app/requirements-server.txt" in commands
    assert "python -m pip check" in commands
    assert "USER 10001:10001" in commands
    assert "PYTHONPATH=/app/src" in commands and "PORT=8080" in commands
    assert 'CMD ["python", "-m", "server.platform_entrypoint"]' in commands
    assert re.findall(r"^COPY (.+)$", commands, re.MULTILINE) == [
        "requirements-server.txt /app/requirements-server.txt", "server/ /app/server/",
        "src/ /app/src/",
    ]
    assert not re.search(r"^\s*(ARG|ADD)\s", commands, re.MULTILINE)
    assert "pip install ." not in commands and "requirements.txt" not in commands
    assert "PLATFORM_DATABASE_URL" not in commands and "TOKEN_PEPPER" not in commands
    assert "--proxy-headers" not in commands and "alembic upgrade" not in commands


def test_server_dependencies_are_version_pinned_and_headless():
    lines = [line.strip() for line in (PROJECT / "requirements-server.txt").read_text(
        encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+(?:\[[a-z]+\])?==\d+(?:\.\d+)+", line)
               for line in lines)
    names = {line.split("==")[0].casefold() for line in lines}
    assert {"fastapi", "uvicorn", "sqlalchemy", "psycopg[binary]", "pillow"} <= names
    assert not names & {"pyside6", "rapidocr", "onnxruntime", "vosk", "ollama", "torch"}


def test_public_settings_use_application_root_and_require_edge_review():
    settings = json.loads((DEPLOYMENT / "public-settings.json").read_text(encoding="utf-8"))
    assert settings["root_directory"] == "/application"
    assert settings["builder"] == "DOCKERFILE"
    assert settings["dockerfile_path"] == "tools/railway_platform/Dockerfile"
    assert settings["start_command"] == "python -m server.platform_entrypoint"
    assert settings["healthcheck_path"] == "/health/ready"
    assert settings["replicas"] == 1
    assert settings["runtime_variables"]["PORT"] == "8080"
    assert settings["runtime_variables"]["PLATFORM_REQUIRE_HTTPS"] == "true"
    assert settings["runtime_variables"]["PLATFORM_RENDER_PROXY"] == "false"
    assert settings["after_edge_isolation_review_only"] == {
        "PLATFORM_RAILWAY_PROXY": "true", "PLATFORM_RAILWAY_EDGE_ONLY": "true",
    }
    assert set(settings["required_sensitive_runtime_variable_names"]) == {
        "PLATFORM_DATABASE_URL", "PLATFORM_TOKEN_PEPPER", "PLATFORM_DATABASE_CA_PEM",
    }
    assert not set(settings["required_sensitive_runtime_variable_names"]) & set(
        settings["runtime_variables"])
    assert not (DEPLOYMENT / "railway.json").exists()
    assert not (DEPLOYMENT / "railway.toml").exists()


@pytest.mark.parametrize("path", [
    "requirements.txt", "requirements-lock.txt", "pyproject.toml", ".git/config",
    ".local/supabase-connection.json", ".local/certs/prod-ca-2021.crt",
    "data/app.db", "data/logs/application.log", "assets/voice/model.bin",
    "packaging/private-android-signing/example.jks", "tests/server/test_platform_app.py",
    "build/module.pyc", "dist/app.exe", ".venv/Lib/site-packages/PySide6/QtCore.pyd",
    "server/__pycache__/platform_app.cpython-313.pyc", "server/local-pilot.db",
    "server/.local/secret.py", "server/nested/test.py", "server/migrations/env.py",
    "src/ollama_chat_app/__pycache__/config.cpython-313.pyc",
    "src/ollama_chat_app/main.py", "src/ollama_chat_app/ui/main_window.py",
    "src/ollama_chat_app/workers/cloud_bridge.py", "src/ollama_chat_app/data/private.db",
    "src/ollama_chat_app/providers/deepseek.py", "src/ollama_chat_app/services/voice.py",
    "src/ollama_chat_app/security/private.key", "src/ollama_chat_app/services/new_unknown.py",
])
def test_context_allowlist_denies_sensitive_desktop_or_unknown_files(path):
    assert not _allowed(path)


@pytest.mark.parametrize("path", [
    "requirements-server.txt", "server/platform_entrypoint.py", "server/platform_app.py",
    "src/ollama_chat_app/__init__.py", "src/ollama_chat_app/config.py",
    "src/ollama_chat_app/data/database.py", "src/ollama_chat_app/providers/base.py",
    "src/ollama_chat_app/security/passwords.py", "src/ollama_chat_app/services/auth.py",
    "src/ollama_chat_app/services/cloud_rpc_codec.py",
])
def test_context_allowlist_contains_required_public_runtime_files(path):
    assert _allowed(path) and (PROJECT / path).is_file()


def test_no_broad_directory_exception_reopens_private_children():
    rules = _rules()
    assert rules[0] == "**"
    for index, rule in enumerate(rules):
        if rule.startswith("!") and rule.endswith("/"):
            assert rules[index + 1] == rule[1:] + "**"


def test_minimal_allowed_python_layout_imports_without_ui_database_or_network(tmp_path):
    # Copy only explicitly public .py code, never enumerate .local or any user DB.
    paths = list((PROJECT / "server").glob("*.py"))
    paths += [PROJECT / rule[1:] for rule in _rules()
              if rule.startswith("!src/") and rule.endswith(".py")]
    for source in paths:
        relative = source.relative_to(PROJECT)
        assert _allowed(relative.as_posix())
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # Isolated Python ignores user PYTHONPATH. The staged tree is the ONLY
    # application source; imported functions are not invoked or constructed.
    code = """
import pathlib, socket, sqlite3, sys
import psycopg, sqlalchemy
staged = pathlib.Path(sys.argv[1]).resolve()
sys.path[:0] = [str(staged), str(staged / 'src')]
def forbidden(*args, **kwargs):
    raise AssertionError('offline_layout_import_must_not_connect')
socket.create_connection = forbidden
socket.socket.connect = forbidden
sqlite3.connect = forbidden
psycopg.connect = forbidden
sqlalchemy.create_engine = forbidden
import server.platform_app
import server.platform_entrypoint
assert not any(name.startswith(('PySide6', 'rapidocr', 'onnxruntime', 'vosk', 'ollama.'))
               for name in sys.modules)
for name, module in tuple(sys.modules.items()):
    if name == 'server' or name.startswith(('server.', 'ollama_chat_app')):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(staged)
print('headless_public_layout_ok')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(tmp_path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "headless_public_layout_ok"


def test_readme_describes_builder_choice_and_does_not_claim_deployment_success():
    readme = (DEPLOYMENT / "README.md").read_text(encoding="utf-8")
    assert "不是自动识别桌面项目的 Railpack" in readme
    assert "不代表镜像已构建" in readme
    assert "RAILPACK_CONFIG_FILE" in readme and "steps.install.commands" in readme
    assert "不能仅改 Start Command" in readme
    assert "PLATFORM_RAILWAY_EDGE_ONLY=true" in readme
    assert "无 TCP Proxy" in readme
    assert "https://railpack.com/config/file/" in readme
    assert "https://docs.docker.com/build/concepts/context/" in readme
