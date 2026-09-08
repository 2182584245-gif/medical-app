"""Explicit, temporary loopback handoff of three Railway runtime variables.

No import, initial page, or default invocation loads a secret. Only one protected
POST may load the dedicated DPAPI runtime profile. No plaintext file is created.
Python/browser immutable strings cannot be reliably wiped from physical memory;
close/expiry removes reachable application references and clears the page UI.
This is a local user-authorized bridge, never a public HTTP deployment endpoint.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TARGET_SERVICE_ID = "afa6011e-0269-4fff-9b18-9a7b57f00093"
LIFETIME_SECONDS = 600
CLOUD_CA_PATH = "/tmp/medical-app-prod-ca.crt"
VARIABLE_NAMES = frozenset({
    "PLATFORM_DATABASE_URL", "PLATFORM_TOKEN_PEPPER", "PLATFORM_DATABASE_CA_PEM",
})


class HandoffError(RuntimeError):
    """Only fixed public codes; never include a loader, certificate or URL error."""


def prepare_runtime_variables(*, payload_loader=None, certificate_reader=None) -> dict[str, str]:
    """Load only when explicitly requested. Injectable readers support offline tests.

    The real loader validates DPAPI profile ownership, purpose, ready state,
    project, dedicated role and original local CA before returning. The duplicate
    boundary checks below also fail closed for an invalid injected reader.
    """
    try:
        from . import platform_admin, platform_entrypoint

        payload = (payload_loader or platform_admin.load_runtime_payload)()
        if (
            not isinstance(payload, dict) or set(payload) != platform_admin.PROFILE_KEYS
            or payload["purpose"] != platform_admin.PURPOSE or payload["state"] != "ready"
            or payload["project_url"] != platform_admin.project_url()
            or not isinstance(payload["provision_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", payload["provision_id"])
        ):
            raise ValueError
        uri = platform_admin.validated_url(payload["runtime_database_url"], payload["project_url"])
        project_ref = platform_admin.project_reference(payload["project_url"])
        username = "medical_app_platform_runtime"
        if (uri.host or "").endswith(".pooler.supabase.com"):
            username += "." + project_ref
        pepper = payload["token_pepper"]
        if (
            uri.username != username or not re.fullmatch(r"[a-f0-9]{64}", uri.password or "")
            or uri.query.get("sslmode") != "verify-full" or not uri.query.get("sslrootcert")
            or not isinstance(pepper, str) or not re.fullmatch(r"[a-f0-9]{64}", pepper)
            or len(set(bytes.fromhex(pepper))) < 16
        ):
            raise ValueError
        if certificate_reader is None:
            certificate = Path(platform_admin.ca_file(uri.query["sslrootcert"]))
            with certificate.open("r", encoding="ascii") as handle:
                pem = handle.read(32_001)
        else:
            pem = certificate_reader(uri.query["sslrootcert"])
        if not isinstance(pem, str) or not pem or len(pem) > 32_000:
            raise ValueError
        pem.encode("ascii")
        # The entrypoint has no side-effect-free wrapper: use the same actual
        # validator it calls, NEVER main() (which writes a file and starts HTTP).
        platform_entrypoint.ssl.create_default_context(cadata=pem)
        cloud_uri = uri.set(query={**uri.query, "sslrootcert": CLOUD_CA_PATH})
        return {
            "PLATFORM_DATABASE_URL": cloud_uri.render_as_string(hide_password=False),
            "PLATFORM_TOKEN_PEPPER": pepper,
            "PLATFORM_DATABASE_CA_PEM": pem,
        }
    except Exception:
        raise HandoffError("runtime_configuration_unavailable") from None


class HandoffState:
    def __init__(self, *, loader=prepare_runtime_variables, clock=time.monotonic):
        self._loader = loader
        self._clock = clock
        self._deadline = clock() + LIFETIME_SECONDS
        self._lock = threading.Lock()
        self._closed = False
        self._claimed = False
        self._origin = None
        self.path = "/" + secrets.token_urlsafe(32)

    def bind_origin(self, port):
        if type(port) is not int or not 1 <= port <= 65535 or self._origin is not None:
            raise HandoffError("invalid_local_binding")
        self._origin = f"http://127.0.0.1:{port}"

    @property
    def origin(self):
        return self._origin

    @property
    def host(self):
        return self._origin.removeprefix("http://") if self._origin else ""

    @property
    def url(self):
        return self._origin + self.path if self._origin else ""

    @property
    def remaining(self):
        with self._lock:
            return max(0.0, self._deadline - self._clock()) if not self._closed else 0.0

    def close(self):
        with self._lock:
            self._closed = True
            self._loader = None

    def reveal(self) -> bytes:
        with self._lock:
            if self._closed or self._clock() >= self._deadline:
                self._closed = True
                self._loader = None
                raise HandoffError("expired")
            if self._claimed:
                raise HandoffError("already_used")
            self._claimed = True  # Claim before potentially slow DPAPI/CA work.
            loader, self._loader = self._loader, None
        try:
            values = loader()
            if (not isinstance(values, dict) or set(values) != VARIABLE_NAMES
                    or any(not isinstance(value, str) or not value or len(value) > 32_000
                           for value in values.values())):
                raise ValueError
            body = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            if len(body) > 64 * 1024:
                raise ValueError
            with self._lock:
                if self._closed or self._clock() >= self._deadline:
                    raise HandoffError("expired")
            return body
        except HandoffError:
            raise
        except Exception:
            raise HandoffError("runtime_configuration_unavailable") from None
        finally:
            # Do not keep a payload/cache on state. Network/browser copies remain
            # only as long as their owners need them; this is not secure RAM erase.
            loader = None


def _page(nonce, remaining_ms):
    # No payload, password, pepper, CA, local certificate path or cloud URL here.
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Railway 本机一次性配置</title>
<style nonce="NONCE">body{font:17px system-ui;max-width:850px;margin:3rem auto;padding:1rem}
button{font:inherit;margin:.5rem;padding:.6rem}textarea{width:100%;min-height:20rem}
.warning{color:#8a3300}</style>
<h1>Railway 本机一次性配置</h1>
<p>目标服务：SERVICE_ID</p>
<p>本页只在本机开放，最多十分钟。初始页面没有凭据；点击后才读取专用运行配置。</p>
<p class="warning">仅填写到你已确认的正式服务变量中。不要截图、转发、保存网页或粘贴到聊天。
不包含管理员连接；不会更改本机加密配置，也不会自动向云端提交。</p>
<button id="reveal" type="button">一次性显示运行配置</button>
<button id="close" type="button">清除本页并关闭本机服务</button>
<p id="status" role="status">尚未读取配置。</p>
<textarea id="runtime-payload" aria-label="Railway runtime variables" readonly hidden
autocomplete="off" spellcheck="false"></textarea>
<script nonce="NONCE">
"use strict";
const reveal = document.getElementById("reveal");
const close = document.getElementById("close");
const status = document.getElementById("status");
const output = document.getElementById("runtime-payload");
let ended = false;
function clearPage() { ended = true; output.value = ""; output.hidden = true;
reveal.disabled = true; }
async function action(value) { return fetch(location.pathname, {method:"POST", credentials:"omit",
cache:"no-store", redirect:"error", headers:{"Content-Type":"application/json"},
body:JSON.stringify({action:value})}); }
reveal.addEventListener("click", async () => {
reveal.disabled = true; status.textContent = "正在本机读取，请勿刷新或截图。";
try { const response = await action("reveal"); if (!response.ok) throw new Error("denied");
const payload = await response.json(); if (ended) return;
output.value = JSON.stringify(payload, null, 2); output.hidden = false;
status.textContent = "已一次性显示；完成填写后请立即清除并关闭。";
} catch { clearPage(); status.textContent = "未能读取配置或已过期；原始错误已隐藏。"; }
});
close.addEventListener("click", async () => { clearPage(); close.disabled = true;
try { await action("close"); } catch {} finally {
status.textContent = "已清除本页内容；请关闭此页面。"; }
});
setTimeout(() => { clearPage(); status.textContent = "本页已到期，请关闭此页面。";
}, REMAINING_MS);
addEventListener("pagehide", clearPage);
</script></html>""".replace("NONCE", nonce).replace("SERVICE_ID", TARGET_SERVICE_ID).replace(
        "REMAINING_MS", str(remaining_ms)).encode("utf-8")


class HandoffHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def log_error(self, _format, *_args):
        pass

    def version_string(self):
        return "LocalHandoff"

    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def _send(self, status, body, *, content_type="application/json", nonce=None):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        csp = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        if nonce:
            csp += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'"
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _denied(self, status=403):
        self._send(status, b'{"status":"unavailable"}')

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's default may echo the request line/version.
        self._denied(code if 400 <= code <= 599 else 400)

    def _allowed(self, *, post):
        state = self.server.handoff
        if self.client_address[0] != "127.0.0.1" or self.path != state.path:
            return False
        for key in ("Host", "Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest",
                    "Content-Type", "Content-Length", "Transfer-Encoding"):
            if len(self.headers.get_all(key, [])) > 1:
                return False
        if self.headers.get("Host") != state.host or self.headers.get("Transfer-Encoding"):
            return False
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if post:
            return (origin == state.origin and site == "same-origin"
                    and self.headers.get("Sec-Fetch-Mode") in {"cors", "same-origin"}
                    and self.headers.get("Sec-Fetch-Dest") == "empty")
        return (origin in {None, state.origin} and site in {"none", "same-origin"}
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document")

    def do_GET(self):
        if not self._allowed(post=False):
            return self._denied()
        remaining = self.server.handoff.remaining
        if remaining <= 0:
            return self._denied(410)
        nonce = secrets.token_urlsafe(24)
        self._send(200, _page(nonce, int(remaining * 1000)),
                   content_type="text/html; charset=utf-8", nonce=nonce)

    def do_POST(self):
        if not self._allowed(post=True):
            return self._denied()
        if self.server.handoff.remaining <= 0:
            return self._denied(410)
        try:
            length = self.headers.get("Content-Length", "")
            if (not re.fullmatch(r"[0-9]{1,3}", length) or not 1 <= int(length) <= 256
                    or self.headers.get("Content-Type") != "application/json"):
                raise ValueError
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise ValueError
            value = json.loads(body)
            if not isinstance(value, dict) or set(value) != {"action"}:
                raise ValueError
            if value["action"] == "close":
                self.server.handoff.close()
                self._send(200, b'{"status":"closed"}')
                self.server.request_stop()
                return
            if value["action"] != "reveal":
                raise ValueError
        except Exception:
            return self._denied(400)
        try:
            payload = self.server.handoff.reveal()
        except HandoffError as error:
            return self._denied({"already_used": 409, "expired": 410}.get(str(error), 503))
        self._send(200, payload)

    def do_OPTIONS(self):
        self._denied(405)

    def do_HEAD(self):
        self._denied(405)


class LocalHandoffServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def __init__(self, state):
        self.handoff = state
        super().__init__(("127.0.0.1", 0), HandoffHandler)
        state.bind_origin(self.server_port)

    def handle_error(self, _request, _client_address):
        # ThreadingMixIn otherwise emits tracebacks, including error values.
        pass

    def request_stop(self):
        self.handoff.close()
        self.shutdown()


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Local handoff requires explicit service confirmation.\n")


def main(argv=None):
    parser = _SafeParser(description="Temporary local-only, one-time Railway runtime handoff.")
    parser.add_argument("--confirm-service", required=True)
    args = parser.parse_args(argv)
    if args.confirm_service != TARGET_SERVICE_ID:
        print(
            "Service confirmation does not match; nothing was loaded or started.",
            file=sys.stderr,
        )
        return 2
    state, server, expiry = HandoffState(), None, None
    try:
        server = LocalHandoffServer(state)
        expiry = threading.Timer(LIFETIME_SECONDS, server.request_stop)
        expiry.daemon = True
        # No secret is loaded here: output contains ONLY local navigation and
        # the explicitly authorized target service's public identifier.
        print(json.dumps({"local_ui_url": state.url, "target_service_id": TARGET_SERVICE_ID}),
              flush=True)
        expiry.start()
        server.serve_forever(poll_interval=0.25)
        return 0
    except (Exception, KeyboardInterrupt):
        print("Local handoff stopped; original details hidden.", file=sys.stderr)
        return 1
    finally:
        state.close()
        if expiry is not None:
            expiry.cancel()
        if server is not None:
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
