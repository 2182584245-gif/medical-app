"""AI provider adapters."""

from .base import ProviderCapabilities


def __getattr__(name: str):
    """Load desktop SDK adapters only when used, including on Android."""
    if name == "DeepSeekCloudProvider":
        from .deepseek_cloud import DeepSeekCloudProvider

        return DeepSeekCloudProvider
    if name == "LocalOllamaProvider":
        from .local_ollama import LocalOllamaProvider

        return LocalOllamaProvider
    if name == "OllamaCloudProvider":
        from .ollama_cloud import OllamaCloudProvider

        return OllamaCloudProvider
    raise AttributeError(name)


__all__ = [
    "DeepSeekCloudProvider",
    "LocalOllamaProvider",
    "OllamaCloudProvider",
    "ProviderCapabilities",
]
