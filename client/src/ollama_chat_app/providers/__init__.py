"""AI provider adapters."""

from .base import ProviderCapabilities


def __getattr__(name: str):
    if name == "DeepSeekCloudProvider":
        from .deepseek_cloud import DeepSeekCloudProvider
        return DeepSeekCloudProvider
    raise AttributeError(name)


__all__ = ["DeepSeekCloudProvider", "ProviderCapabilities"]