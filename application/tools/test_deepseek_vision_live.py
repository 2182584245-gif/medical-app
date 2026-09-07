"""Optional one-request live check using only a generated geometric PNG.

No arguments: no key is read and no request is sent.
--run: send once using DEEPSEEK_API_KEY or a hidden interactive key prompt.
The request uses the paid official API, disables thinking, and caps output at
128 tokens. No health data, local images, database, or credential files are read.
"""

from __future__ import annotations

import argparse
import getpass
import os
import struct
import sys
import warnings
import zlib
from pathlib import Path

OUTPUT_TOKEN_BUDGET = 128


def synthetic_image() -> bytes:
    """Make a 256 x 128 PNG: a red square on the left and a blue circle on the right."""

    width, height = 256, 128
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)  # PNG row filter: none.
        for x in range(width):
            if 24 <= x < 88 and 32 <= y < 96:
                color = (235, 35, 35)
            elif (x - 184) ** 2 + (y - 64) ** 2 <= 32**2:
                color = (25, 65, 235)
            else:
                color = (255, 255, 255)
            scanlines.extend(color)

    def chunk(kind: bytes, content: bytes) -> bytes:
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + chunk(b"IEND", b"")
    )


def _read_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key or not sys.stdin.isatty():
        return key
    try:
        # Refuse getpass's echoed-input fallback on terminals without hiding.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass("DeepSeek API Key (hidden, empty to skip): ").strip()
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Send one paid vision request for a generated image, with max_tokens=128.",
    )
    args = parser.parse_args(argv)
    if not args.run:
        print("SKIPPED: no request sent and no API key read. Add --run to opt in.")
        print("Only a synthetic red/blue geometric image is used; output is capped at 128 tokens.")
        return 0

    key = _read_key()
    if not key:
        print("SKIPPED: no API key supplied; no request sent.")
        return 0

    # Resolve the checked-out app so this script also works before installation.
    source_dir = str(Path(__file__).resolve().parents[1] / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    from ollama_chat_app import config
    from ollama_chat_app.providers.base import ProviderError
    from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider

    print(
        "Sending one paid request for a synthetic 256 x 128 PNG; max_tokens=128, thinking disabled."
    )
    provider = DeepSeekCloudProvider(key)
    key = ""
    try:
        answer = provider.chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [
                {
                    "role": "user",
                    "content": "Describe the visible colors, shapes, and positions. "
                    "Identify what is on the left and right in one short English sentence.",
                    "images": [synthetic_image()],
                }
            ],
            max_tokens=OUTPUT_TOKEN_BUDGET,
        )
    except ProviderError as error:
        # The adapter's stable code is safe; raw HTTP details are never logged.
        print(f"FAILED: request did not complete ({error.code}). No retry was sent.")
        return 1
    except Exception:
        print("FAILED: local validation or runtime error. No automatic retry was sent.")
        return 1

    expected_model = config.DEEPSEEK_VISION_MODEL
    if provider.last_used_model != expected_model:
        print("FAILED: request did not use the expected vision model.")
        return 1
    reported_model = (provider.last_response_model or "").casefold()
    if not (
        reported_model == expected_model.casefold()
        or reported_model.startswith(expected_model.casefold() + "-")
    ):
        print(
            "FAILED: the API did not report the expected vision model; "
            "review the official model ID."
        )
        return 1
    if not answer.strip():
        print("FAILED: the API returned an empty answer.")
        return 1

    # Print only bounded acceptance facts, never the key or raw server response.
    # This also prevents an unexpected reflected credential from reaching stdout.
    lowered = answer.casefold()
    color_shape_check = all(term in lowered for term in ("red", "blue", "square", "circle"))
    print("PASS: one request completed, the API reported the vision model, and answer is nonempty.")
    if color_shape_check:
        print("PASS: the answer contains the expected red/blue and square/circle terms.")
    else:
        print(
            "REVIEW: color/shape keywords did not all match; visual accuracy remains unconfirmed."
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
