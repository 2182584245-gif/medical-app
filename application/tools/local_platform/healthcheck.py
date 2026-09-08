"""Certificate-verified health check; never calls the internet."""

import json
import ssl
import urllib.request

from .guard import API_SECRETS, require_local_container


def main():
    require_local_container()
    context = ssl.create_default_context(cafile=str(API_SECRETS / "ca.crt"))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
    )
    with opener.open("https://localhost:8443/health/ready", timeout=8) as response:
        if response.status != 200 or json.load(response).get("status") != "ready":
            raise RuntimeError


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Local API is not ready.") from None
