"""Public desktop endpoint configuration. Never store credentials or user data here."""

from __future__ import annotations

import os

from .config import DEFAULT_ALIYUN_ENDPOINT
from .services.cloud_client import CloudAPIError, validate_base_url

# Public release gate: both routes must pass real HTTPS verification before packaging.
# Constructing a window still makes no network request.
DEFAULT_CLOUD_BASE_URL = DEFAULT_ALIYUN_ENDPOINT
ENVIRONMENT_VARIABLE = "HEALTHLIFE_CLOUD_BASE_URL"


def configured_cloud_base_url() -> str:
    value = os.environ.get(ENVIRONMENT_VARIABLE, DEFAULT_CLOUD_BASE_URL)
    if not value:
        return ""
    try:
        return validate_base_url(value)
    except CloudAPIError:
        # A malformed optional cloud setting must not break local startup, nor
        # accidentally display a pasted credential-bearing URI in the UI/log.
        return ""
