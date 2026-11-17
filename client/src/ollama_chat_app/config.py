from __future__ import annotations

# This release has a separate identity and never discovers legacy user data.
APP_NAME = "FAWNSEE-MOBILEv1.0.0"
APP_ID = "fawnsee-mobile-v1.0.0"
APP_VERSION = "1.0.0"

DEEPSEEK_API_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_VISION_MODEL = "deepseek-flash"
# Both legacy Flash aliases route to the same official V4.1 Flash service.
# Accept saved settings from earlier versions, but never switch model families.
DEEPSEEK_SUPPORTED_MODELS = (DEEPSEEK_DEFAULT_MODEL, "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
DEEPSEEK_VISION_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
# Official inline-image limits, checked before sending any image bytes.
DEEPSEEK_MAX_IMAGE_BYTES = 32 * 1024 * 1024
DEEPSEEK_MAX_REQUEST_BYTES = 48 * 1024 * 1024
DEEPSEEK_MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
DEEPSEEK_MAX_IMAGES = 600
DEEPSEEK_MAX_IMAGE_EDGE = 8192
DEEPSEEK_MANY_IMAGES_THRESHOLD = 15
DEEPSEEK_MANY_IMAGES_MAX_EDGE = 4096
DEEPSEEK_API_KEY_URL = "https://platform.deepseek.com/api_keys"
DEEPSEEK_USAGE_URL = "https://platform.deepseek.com/usage"
DEEPSEEK_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/"

MAX_CONTEXT_MESSAGES = 20
MAX_MESSAGE_LENGTH = 2_000_000
REQUEST_TIMEOUT_SECONDS = 120.0

# Public application API routes, never database URLs or credentials. Shipping a
# build with these defaults still requires the deployment's strict-TLS acceptance.
DEFAULT_ALIYUN_ENDPOINT = "https://39.106.166.15/aliyun"
