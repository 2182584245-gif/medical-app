"""Version and explicit dependency contracts; never runs a frozen executable."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from ollama_chat_app.config import APP_VERSION
from ollama_chat_app.data.database import SCHEMA_VERSION

PROJECT = Path(__file__).resolve().parents[1]


def test_desktop_release_identity_matches_all_active_packaging_metadata():
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    resource = (PROJECT / "packaging/version_info.txt").read_text(encoding="utf-8")
    installer = (PROJECT / "packaging/installer.iss").read_text(encoding="utf-8")
    version = tuple(map(int, APP_VERSION.split("."))) + (0,)
    assert APP_VERSION == project["project"]["version"] == "1.7.0"
    assert SCHEMA_VERSION == 7  # The AI additions must not downgrade local databases.
    for field in ("filevers", "prodvers"):
        match = re.search(rf"{field}=\(([^)]+)\)", resource)
        assert match and ast.literal_eval("(" + match.group(1) + ")") == version
    assert f"StringStruct('FileVersion', '{APP_VERSION}.0')" in resource
    assert f"StringStruct('ProductVersion', '{APP_VERSION}')" in resource
    assert f'#define MyAppVersion "{APP_VERSION}"' in installer


def test_agent_camera_and_local_speech_are_explicit_packaging_dependencies():
    specification = ast.parse((PROJECT / "packaging/app.spec").read_text(encoding="utf-8"))
    analysis = next(
        node for node in ast.walk(specification)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    )
    hidden = ast.literal_eval(next(
        keyword.value for keyword in analysis.keywords if keyword.arg == "hiddenimports"
    ))
    assert {
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtTextToSpeech",
        "ollama_chat_app.services.life_agent", "ollama_chat_app.services.agent_tools",
        "ollama_chat_app.services.device_observations", "ollama_chat_app.providers.demo_life",
        "ollama_chat_app.ui.meal_capture_dialog",
    } <= set(hidden)
    text = (PROJECT / "packaging/verify_build.py").read_text(encoding="utf-8")
    for component in (
        "QtTextToSpeech.pyd", "Qt6TextToSpeech.dll", "qtexttospeech_sapi.dll",
        "QtMultimediaWidgets.pyd", "Qt6MultimediaWidgets.dll",
    ):
        assert component in text


def test_packaging_keeps_no_upx_no_elevation_and_no_private_key_collection():
    specification = ast.parse((PROJECT / "packaging/app.spec").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(specification)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"EXE", "COLLECT"}
    ]
    assert len(calls) == 2
    for call in calls:
        fields = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.literal_eval(fields["upx"]) is False
        if call.func.id == "EXE":
            assert ast.literal_eval(fields["uac_admin"]) is False
            assert ast.literal_eval(fields["uac_uiaccess"]) is False
    text = (PROJECT / "packaging/app.spec").read_text(encoding="utf-8")
    assert "runtime_hooks=[]" in text and '"keyring"' in text
    assert "api-key.dpapi" not in text and "deepseek-key.aesgcm" not in text
