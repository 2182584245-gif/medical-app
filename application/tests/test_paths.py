from __future__ import annotations

from pathlib import Path

from ollama_chat_app import paths


def test_frozen_app_uses_portable_data_folder(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "Ollama 双模聊天.exe"
    monkeypatch.delenv("OLLAMA_DUAL_CHAT_DATA_DIR", raising=False)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(executable))

    assert paths.is_frozen_app()
    assert paths.application_dir() == tmp_path
    assert paths.user_data_dir() == tmp_path / "data"
    assert paths.database_path() == tmp_path / "data" / "app.db"
    assert paths.log_path() == tmp_path / "data" / "logs" / "app.log"


def test_explicit_data_override_wins_in_frozen_app(monkeypatch, tmp_path: Path) -> None:
    executable_dir = tmp_path / "app"
    override_dir = tmp_path / "override"
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        paths.sys,
        "executable",
        str(executable_dir / "Ollama 双模聊天.exe"),
    )
    monkeypatch.setenv("OLLAMA_DUAL_CHAT_DATA_DIR", str(override_dir))

    assert paths.user_data_dir() == override_dir.resolve()
    assert paths.database_path() == override_dir.resolve() / "app.db"


def test_source_run_keeps_per_user_data_location(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.delattr(paths.sys, "frozen", raising=False)
    monkeypatch.delenv("OLLAMA_DUAL_CHAT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert not paths.is_frozen_app()
    assert paths.user_data_dir() == local_app_data / "ollama-dual-chat"
