"""TEST ONLY: loopback bridge for real mobile UI / Python integration tests.

Never packaged. Run with an explicitly disposable test data directory.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "android/app/src/main/python"))
import mobile_api  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        name = self.path.partition("?")[0].removeprefix("/") or "index.html"
        if name not in {"index.html", "app.css", "app.js"}:
            self.send_error(404)
            return
        body = (ROOT / "android/app/src/main/assets/www" / name).read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/rpc" or self.headers.get("X-HealthLife-Test") != "disposable-local-ui":
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 256000:
            self.send_error(413)
            return
        request = self.rfile.read(length).decode("utf-8")
        body = mobile_api.dispatch(request).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    # Prevent accidentally pointing this unauthenticated development tool at user data.
    destination = Path(args.data_dir).resolve()
    if "android-ui-test" not in destination.name or (destination / "app.db").exists():
        raise SystemExit("Use a NEW directory whose name contains android-ui-test.")
    result = json.loads(mobile_api.initialize(str(destination)))
    if not result["ok"]:
        raise SystemExit(result["error"])
    print(f"Disposable UI test server: http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
