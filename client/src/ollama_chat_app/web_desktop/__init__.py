"""HTML desktop bridge.

The web UI talks to :class:`~ollama_chat_app.web_desktop.bridge.WebBridgeAPI`
through pywebview.  This package deliberately contains no UI framework code;
it is safe to import while the legacy PySide6 desktop remains available.
"""

from .bridge import WebBridgeAPI

__all__ = ["WebBridgeAPI"]
