"""Public desktop endpoint configuration. Never store credentials or user data here."""

from __future__ import annotations

import os

from .services.cloud_client import CloudAPIError, validate_base_url

# Filled only after the application's real public service URL is verified.
# An empty default keeps development builds local-first and makes no requests.
DEFAULT_CLOUD_BASE_URL = ""
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
