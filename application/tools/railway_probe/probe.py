"""Temporary, credential-free Railway ingress/egress probe; not the application.

Only main() runs the one fixed DNS/TCP check. HTTP requests read its cached
enumerated result, never initiate outbound traffic, and never return raw network
metadata. A successful TCP connection is NOT a database/TLS/login test.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

CHECK_HOST = "aws-0-ap-southeast-1.pooler.supabase.com"
CHECK_PORT = 5432
CHECK_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 3.0
SENTINEL = ipaddress.ip_address("198.51.100.27")


def check_connectivity() -> dict[str, str]:
    """One DNS lookup and at most one TCP connect, within one 5-second budget.

    Python's OS DNS resolver has no portable timeout parameter. A daemon thread
    performs only DNS; if it outlives the deadline, no late TCP attempt occurs.
    No PostgreSQL bytes, certificate, password, or other data are transmitted.
    """
    deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS
    completed = queue.Queue(maxsize=1)

    def resolve():
        try:
            addresses = socket.getaddrinfo(
                CHECK_HOST, CHECK_PORT, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            completed.put(("ok", addresses))
        except Exception:
            completed.put(("failed", ()))

    threading.Thread(target=resolve, daemon=True, name="fixed-dns-probe").start()
    try:
        dns, addresses = completed.get(timeout=max(0.001, deadline - time.monotonic()))
    except queue.Empty:
        return {"dns": "timeout", "tcp": "not_attempted"}
    if dns != "ok":
        return {"dns": "failed", "tcp": "not_attempted"}

    candidates = []
    for family, kind, _protocol, _name, address in addresses:
        try:
            parsed = ipaddress.ip_address(address[0])
            if (
                family not in {socket.AF_INET, socket.AF_INET6}
                or kind != socket.SOCK_STREAM
                or not parsed.is_global
                or parsed.version != (4 if family == socket.AF_INET else 6)
            ):
                continue
            candidates.append((family, str(parsed)))
        except (ValueError, TypeError, IndexError):
            continue
    if not candidates:
        return {"dns": "no_address", "tcp": "not_attempted"}

    # The Session pooler supports IPv4. Prefer it, but never retry another IP.
    family, address = min(candidates, key=lambda item: item[0] != socket.AF_INET)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"dns": "ok", "tcp": "timeout"}
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(CHECK_TIMEOUT_SECONDS, remaining))
            endpoint = (address, CHECK_PORT) if family == socket.AF_INET else (
                address, CHECK_PORT, 0, 0
            )
            connection.connect(endpoint)
        tcp = "connected"
    except TimeoutError:
        tcp = "timeout"
    except ConnectionRefusedError:
        tcp = "refused"
    except Exception:
        tcp = "failed"
    return {"dns": "ok", "tcp": tcp}


def peer_summary(peer: str) -> dict[str, str]:
    try:
        address = ipaddress.ip_address(peer)
        return {
            "family": "ipv4" if address.version == 4 else "ipv6",
            "private": "yes" if address.is_private else "no",
        }
    except (TypeError, ValueError):
        return {"family": "invalid", "private": "unknown"}


def ip_header_summary(headers, name: str) -> dict[str, str]:
    values = headers.get_all(name, [])
    if len(values) != 1:
        return {
            "presence": "absent" if not values else "duplicate",
            "validity": "not_checked", "sentinel": "not_checked",
        }
    try:
        # No strip/split: a comma-separated chain is not a single client IP.
        address = ipaddress.ip_address(values[0])
        return {
            "presence": "present", "validity": "valid",
            "sentinel": "yes" if address == SENTINEL else "no",
        }
    except (TypeError, ValueError):
        return {"presence": "present", "validity": "invalid", "sentinel": "not_checked"}


def proto_summary(headers) -> str:
    values = headers.get_all("X-Forwarded-Proto", [])
    if not values:
        return "absent"
    if len(values) != 1:
        return "duplicate"
    return values[0] if values[0] in {"https", "http"} else "invalid"


def request_summary(cached, peer, headers) -> dict:
    """Fixed schema and enums only; no arbitrary value/Host/header reflection."""
    return {
        "dns": cached["dns"], "tcp": cached["tcp"],
        "peer": peer_summary(peer),
        "x_real_ip": ip_header_summary(headers, "X-Real-IP"),
        "x_forwarded_for": ip_header_summary(headers, "X-Forwarded-For"),
        "cf_connecting_ip": ip_header_summary(headers, "CF-Connecting-IP"),
        "x_forwarded_proto": proto_summary(headers),
    }


class ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
        super().setup()

    def log_message(self, _format, *args):
        """Disable all standard access/error logs, including request contents."""

    def _reply(self, status: int, payload: dict):
        content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_error(self, code, message=None, explain=None):
        # The base implementation interpolates invalid methods/versions into HTML.
        self._reply(code if code in {400, 405, 414, 431} else 400, {"status": "rejected"})

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"status": "ok"})
        elif self.path == "/probe":
            self._reply(200, request_summary(
                self.server.cached_result, self.client_address[0], self.headers
            ))
        else:
            self._reply(404, {"status": "not_found"})

    def do_POST(self):
        # Never read a request body and never interpret any supplied target URL.
        self._reply(405, {"status": "method_not_allowed"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_HEAD = do_POST
    do_CONNECT = do_POST
    do_TRACE = do_POST


class ProbeServer(HTTPServer):
    """One request at a time, bounded socket timeout; no per-request thread growth."""

    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address, cached_result):
        self.cached_result = dict(cached_result)
        super().__init__(address, ProbeHandler)

    def handle_error(self, request, client_address):
        """Never print traceback, peer IP, or exception text."""


def main() -> int:
    try:
        port = int(os.environ.get("PORT", "8080"))
        if not 1024 <= port <= 65535:
            raise ValueError
        cached = check_connectivity()
        print(json.dumps({"event": "railway_probe_start", **cached}), flush=True)
        with ProbeServer(("0.0.0.0", port), cached) as application:
            application.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('{"event":"railway_probe_failed"}', flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
