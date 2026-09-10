from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_demo_outputs_utf8_on_non_chinese_console():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tools/demo_life_agent.py")],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=30,
        check=True,
    )
    report = json.loads(result.stdout.decode("utf-8"))
    assert report["synthetic_data_only"] is True
    assert report["saved_life_records"] == report["saved_reminders"] == 1
    assert report["confirmed_preferences"]["dietary_preferences"] == "我不吃香菜"
