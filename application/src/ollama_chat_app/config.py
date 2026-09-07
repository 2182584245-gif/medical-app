from __future__ import annotations

# APP_ID deliberately remains unchanged so existing PyCharm/local data is found
# and migrated instead of appearing to disappear after the product rename.
APP_NAME = "健康生活服务平台"
APP_ID = "ollama-dual-chat"
APP_VERSION = "1.2.0"

LOCAL_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_LOCAL_MODEL = "qwen3:4b"

OLLAMA_CLOUD_HOST = "https://ollama.com"
OLLAMA_API_KEY_URL = "https://ollama.com/settings/keys"

# Ollama does not currently publish a stable, machine-readable Starter-model list.
# The app therefore asks the authenticated API for the account's current model list
# and selects its first returned model, without claiming permanent free availability.
CLOUD_MODEL_PLACEHOLDER = "自动选择（免费 Starter 额度优先）"

DEEPSEEK_API_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
DEEPSEEK_VISION_MODEL = "deepseek-v4-flash-vision-exp"
DEEPSEEK_SUPPORTED_MODELS = (DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_VISION_MODEL)
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
MAX_MESSAGE_LENGTH = 20_000
REQUEST_TIMEOUT_SECONDS = 120.0
