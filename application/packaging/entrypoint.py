"""Absolute-import launcher used only by the PyInstaller build."""

from ollama_chat_app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
