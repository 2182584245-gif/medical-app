"""Launch the new HTML/CSS/JavaScript desktop shell with WebView2.

The existing PySide6 launcher remains untouched.  This module is deliberately a
separate entry point so the old release can be run and compared while the new
web shell is being validated page by page.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import webview

from ..cloud_config import configured_cloud_base_url
from ..security.secret_store import SecretStore
from ..services.cloud_client import CloudAPIClient
from .bridge import WebBridgeAPI


def web_dist_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        candidate = Path(frozen_root) / "web"
    else:
        candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
    index = candidate / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"网页资源未构建：{index}")
    return candidate


def main() -> int:
    directory = web_dist_dir()
    # WebView2 cannot reliably create its default profile beside a packaged
    # executable (especially when the release is launched from Desktop or a
    # read-only location). Keep the browser profile in the user's writable
    # local application data directory instead.
    storage_path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "FAWNSEE" / "WebView2"
    storage_path.mkdir(parents=True, exist_ok=True)
    cloud_base_url = configured_cloud_base_url()
    recovery_store = SecretStore(namespace=f"cloud:{cloud_base_url or 'default'}")
    bridge = WebBridgeAPI(
        base_url=cloud_base_url,
        client_factory=lambda url: CloudAPIClient(url, recovery_store=recovery_store),
    )
    webview.create_window(
        "FAWNSEE · 健康生活",
        url=directory.joinpath("index.html").as_uri(),
        js_api=bridge,
        width=1440,
        height=900,
        min_size=(1120, 720),
        background_color="#f5f0e5",
        text_select=True,
    )
    # EdgeChromium is selected explicitly; Windows WebView2 is checked by the
    # release validator and remains a target-machine prerequisite.
    webview.start(
        gui="edgechromium",
        debug=os.environ.get("HEALTHLIFE_WEB_DEBUG") == "1",
        private_mode=False,
        storage_path=str(storage_path),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
