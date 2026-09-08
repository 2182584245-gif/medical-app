"""Static/safe-negative harness checks only. Never starts a desktop executable."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/check_windows_release_startup.ps1"


def test_harness_has_isolated_user_dirs_exact_hash_and_owned_process_only():
    text = SCRIPT.read_text(encoding="utf-8")
    for required in (
        "ExpectedSha256",
        "ConfirmRun",
        "LOCALAPPDATA",
        "APPDATA",
        "OLLAMA_DUAL_CHAT_DATA_DIR",
        "FindOwnedWindows($testProcess.Id)",
        "-WindowStyle Hidden",
        "owner == processId",
        "WaitForExit(10000)",
        "Stop-Process -Id $testProcess.Id",
        "startup-report.json",
        "packet_capture_performed=$false",
    ):
        assert required in text
    assert text.index("if (-not $ConfirmRun)") < text.index(
        "New-Item -ItemType Directory -Path $testTarget"
    )
    assert "Remove-Item" not in text
    assert "netsh" not in text and "Set-MpPreference" not in text
    assert "real_user_data_opened = $false" not in text  # No unsupported telemetry claim.
    assert "ai_requests = 0" not in text


def test_powershell_syntax_and_negative_hash_guard_never_launch(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell syntax checker unavailable")
    quoted_script = str(SCRIPT).replace("'", "''")
    parser = (
        "$errors=$null; $tokens=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{quoted_script}',[ref]$tokens,[ref]$errors) | Out-Null; "
        "if($errors.Count){$errors | ForEach-Object Message;exit 1}"
    )
    # The static parser does not execute the harness or the source file it examines.
    result = subprocess.run(
        [shell, "-NoProfile", "-Command", parser],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    inert = tmp_path / "never-executed.txt"
    inert.write_text("synthetic input, never an executable")
    target = tmp_path / "must-not-be-created"
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-ExecutablePath",
            str(inert),
            "-ExpectedSha256",
            "0" * 64,
            "-PythonPath",
            sys.executable,
            "-TestRoot",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode != 0
    assert "nothing started" in result.stdout + result.stderr
    assert not target.exists()
