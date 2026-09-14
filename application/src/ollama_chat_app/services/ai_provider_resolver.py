"""One selection path for chat and editable voice-to-life-record suggestions."""

from ..providers.deepseek_cloud import DeepSeekCloudProvider
from ..providers.platform_deepseek import PlatformDeepSeekProvider


def resolve_deepseek(secret_store, account_identity, *, session_key=None, cloud_client=None):
    """Never send a user's replacement key to the platform server.

    The Windows-user default is only a local-mode fallback. Cloud defaults are
    supplied by the authenticated server so a newly installed device needs no
    shared secret. Errors reading a personal key do not silently change payer.
    """
    personal = session_key or secret_store.get_persistent_api_key(
        account_identity, "deepseek_cloud"
    )
    if personal:
        return DeepSeekCloudProvider(personal)
    if cloud_client is not None:
        return PlatformDeepSeekProvider(cloud_client)
    default = secret_store.get_default_api_key("deepseek_cloud")
    if default:
        return DeepSeekCloudProvider(default)
    return None
