"""Explicit two-request check, synthetic content only; key never printed or saved here."""

from __future__ import annotations

import argparse
import base64
import io
import json

from PIL import Image

from ollama_chat_app.config import DEEPSEEK_DEFAULT_MODEL
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider
from ollama_chat_app.security.secret_store import SecretStore


def verify() -> dict:
    key = SecretStore().get_default_api_key()
    if not key:
        return {"status": "not_run", "reason": "no_encrypted_default_key"}
    provider = DeepSeekCloudProvider(key)
    results = []
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (220, 25, 25)).save(buffer, format="PNG")
    checks = (
        ("text", {"role": "user", "content": "这是连通测试，请仅回复 OK。"}, 8),
        (
            "vision",
            {
                "role": "user",
                "content": "这是纯色合成测试图片。请只用一个中文词回答图片主要是什么颜色。",
                "images": [base64.b64encode(buffer.getvalue()).decode("ascii")],
            },
            24,
        ),
    )
    for kind, message, limit in checks:
        try:
            answer = provider.chat(DEEPSEEK_DEFAULT_MODEL, [message], max_tokens=limit)
            results.append(
                {
                    "kind": kind,
                    "status": "responded",
                    "requested_model": provider.last_used_model,
                    "reported_model": provider.last_response_model,
                    "synthetic_answer": answer[:80],
                    "semantic_check": "OK" in answer.upper() if kind == "text" else "红" in answer,
                }
            )
        except ProviderError as error:
            results.append({"kind": kind, "status": "failed", "safe_code": error.code})
        except Exception:
            results.append({"kind": kind, "status": "failed", "safe_code": "unclassified"})
    return {
        "status": "checked",
        "requests_attempted": 2,
        "personal_data_sent": False,
        "checks": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-billable-synthetic-check", action="store_true")
    arguments = parser.parse_args()
    if not arguments.confirm_billable_synthetic_check:
        parser.error("Explicit billable synthetic check confirmation required")
    result = verify()
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if result["status"] == "checked"
        and all(item.get("semantic_check", False) for item in result["checks"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
