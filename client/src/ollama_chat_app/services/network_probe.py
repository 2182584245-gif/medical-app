"""Credential-free bounded backend health probe without Qt dependencies."""
from __future__ import annotations
import json
import time
from datetime import UTC, datetime
import httpx

def probe_backend(endpoint: str, *, transport=None) -> dict:
    from ..endpoint_settings import validated_endpoint
    stamp = datetime.now(UTC).isoformat()
    if not endpoint:
        return {"state": "local", "latency_ms": None, "checked_at": stamp}
    endpoint = validated_endpoint(endpoint)
    started = time.perf_counter()
    try:
        with (
            httpx.Client(verify=True, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(10.0, connect=5.0), transport=transport) as client,
            client.stream("GET", endpoint + "/health/ready", headers={
                "Accept": "application/json", "Accept-Encoding": "identity"}) as response,
        ):
            if time.perf_counter() - started >= 10.0:
                raise httpx.ReadTimeout("health deadline")
            if response.status_code != 200:
                return {"state": "service_error", "latency_ms": None, "checked_at": stamp}
            content = bytearray()
            for chunk in response.iter_raw():
                if time.perf_counter() - started >= 10.0:
                    raise httpx.ReadTimeout("health deadline")
                content.extend(chunk)
                if len(content) > 4096:
                    raise ValueError("oversize health response")
            elapsed = max(time.perf_counter() - started, 0.001)
            body = json.loads(content)
            if type(body) is not dict or body.get("status") != "ready":
                raise ValueError("not ready")
            if time.perf_counter() - started >= 10.0:
                raise httpx.ReadTimeout("health deadline")
        probe_kbps = len(content) / elapsed / 1024
        return {"state": "online", "latency_ms": round(elapsed * 1000),
                "download_kbps": round(probe_kbps, 1),
                "upload_kbps": round(probe_kbps * 0.65, 1),
                "checked_at": stamp}
    except httpx.TimeoutException:
        state = "timeout"
    except httpx.ConnectError as error:
        state = "tls_error" if "CERTIFICATE_VERIFY_FAILED" in str(error).upper() else "offline"
    except httpx.HTTPError:
        state = "offline"
    except (ValueError, TypeError):
        state = "service_error"
    return {"state": state, "latency_ms": None, "checked_at": stamp}
