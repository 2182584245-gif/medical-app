from __future__ import annotations

import base64
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from ollama_chat_app import config
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider


def _image(format_name: str = "PNG", *, width: int = 3, height: int = 2) -> str:
    if format_name == "GIF":
        return "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    picture = QImage(width, height, QImage.Format.Format_RGB32)
    picture.fill(0xFF397BCD)
    buffer = QBuffer()
    buffer.setData(QByteArray())
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert picture.save(buffer, format_name)
    result = base64.b64encode(bytes(buffer.data())).decode("ascii")
    buffer.close()
    return result


def _provider(captured: list[httpx.Request]) -> DeepSeekCloudProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "图片中的物体"}}]})

    return DeepSeekCloudProvider("test-only-key", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("format_name", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
def test_native_images_route_to_vision_and_preserve_the_original_bytes(
    format_name: str,
    mime: str,
) -> None:
    captured: list[httpx.Request] = []
    provider = _provider(captured)
    encoded = _image(format_name)
    messages = [{"role": "user", "content": "描述图片中的物体", "images": [encoded]}]
    original = copy.deepcopy(messages)

    assert provider.resolve_model(config.DEEPSEEK_DEFAULT_MODEL, messages) == (
        config.DEEPSEEK_VISION_MODEL
    )
    assert captured == []
    assert provider.last_used_model is None
    assert provider.chat(config.DEEPSEEK_DEFAULT_MODEL, messages) == "图片中的物体"
    assert len(captured) == 1
    assert str(captured[0].url) == "https://api.deepseek.com/chat/completions"
    assert json.loads(captured[0].content) == {
        "model": config.DEEPSEEK_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述图片中的物体"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }
        ],
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    assert provider.last_used_model == config.DEEPSEEK_VISION_MODEL
    assert messages == original
    assert b"test-only-key" not in captured[0].content


def test_history_images_and_image_only_turns_are_not_dropped() -> None:
    captured: list[httpx.Request] = []
    provider = _provider(captured)
    messages = [
        {"role": "system", "content": "帮助理解图片"},
        {"role": "user", "content": "", "images": [_image()]},
        {"role": "assistant", "content": "这是图片"},
        {"role": "user", "content": "详细说明", "images": []},
    ]
    provider.chat(config.DEEPSEEK_DEFAULT_MODEL, messages)
    payload = json.loads(captured[0].content)
    assert payload["model"] == config.DEEPSEEK_VISION_MODEL
    assert len(payload["messages"]) == 4
    assert len(payload["messages"][1]["content"]) == 1
    assert payload["messages"][1]["content"][0]["type"] == "image_url"
    assert payload["messages"][-1] == {"role": "user", "content": "详细说明"}


def test_explicit_visual_model_and_text_default_are_both_supported() -> None:
    captured: list[httpx.Request] = []
    provider = _provider(captured)
    messages = [{"role": "user", "content": "你好"}]
    for model in config.DEEPSEEK_SUPPORTED_MODELS:
        assert provider.resolve_model(f" {model} ", messages) == model
        provider.chat(model, messages)
        assert provider.last_used_model == model
    assert [json.loads(request.content)["model"] for request in captured] == list(
        config.DEEPSEEK_SUPPORTED_MODELS
    )
    assert config.DEEPSEEK_DEFAULT_MODEL == "deepseek-v4-flash"


def test_data_url_has_exactly_one_mime_prefix() -> None:
    captured: list[httpx.Request] = []
    provider = _provider(captured)
    url = f"data:image/png;base64,{_image()}"
    provider.chat(
        config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "看图", "images": [url]}]
    )
    assert json.loads(captured[0].content)["messages"][0]["content"][1]["image_url"]["url"] == url


@pytest.mark.parametrize("role", ["assistant", "system"])
def test_non_user_image_roles_fail_before_network(role: str) -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(ProviderError, match="用户消息") as error:
        _provider(captured).chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": role, "content": "看图", "images": [_image()]}],
        )
    assert error.value.code == "invalid_message"
    assert captured == []


@pytest.mark.parametrize(
    "images",
    [
        "",
        "encoded-image",
        {},
        [None],
        [""],
        ["%%%"],
        ["图片"],
        [base64.b64encode(b"not an image").decode()],
        [base64.b64encode(b"%PDF-1.7").decode()],
        ["data:image/png;base64,%%%"],
        ["https://example.com/image.png"],
    ],
)
def test_invalid_attachments_cannot_be_silently_sent_as_text(images: object) -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(ProviderError) as error:
        _provider(captured).chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": "看图", "images": images}],
        )
    assert error.value.code == "invalid_image"
    assert captured == []


def test_mime_mismatch_and_corrupted_png_fail_before_network() -> None:
    captured: list[httpx.Request] = []
    valid_png = _image()
    broken_png = base64.b64encode(base64.b64decode(valid_png)[:33]).decode()
    for encoded in (f"data:image/jpeg;base64,{valid_png}", broken_png):
        with pytest.raises(ProviderError) as error:
            _provider(captured).chat(
                config.DEEPSEEK_DEFAULT_MODEL,
                [{"role": "user", "content": "看图", "images": [encoded]}],
            )
        assert error.value.code == "invalid_image"
    assert captured == []


@pytest.mark.parametrize(
    ("limit_name", "limit", "expected_code"),
    [
        ("DEEPSEEK_MAX_IMAGE_BYTES", 10, "image_too_large"),
        ("DEEPSEEK_MAX_TOTAL_IMAGE_BYTES", 100, "images_too_large"),
        ("DEEPSEEK_MAX_REQUEST_BYTES", 100, "request_too_large"),
        ("DEEPSEEK_MAX_IMAGES", 1, "too_many_images"),
    ],
)
def test_oversized_requests_are_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    expected_code: str,
) -> None:
    monkeypatch.setattr(config, limit_name, limit)
    captured: list[httpx.Request] = []
    with pytest.raises(ProviderError) as error:
        _provider(captured).chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": "看图", "images": [_image(), _image()]}],
        )
    assert error.value.code == expected_code
    assert captured == []


def test_inline_body_limit_uses_exact_utf8_request_size(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []
    provider = _provider(captured)
    messages = [{"role": "user", "content": "中文字符", "images": [_image()]}]
    provider.chat(config.DEEPSEEK_DEFAULT_MODEL, messages)
    size = len(captured[0].content)
    monkeypatch.setattr(config, "DEEPSEEK_MAX_REQUEST_BYTES", size)
    provider.chat(config.DEEPSEEK_DEFAULT_MODEL, messages)
    monkeypatch.setattr(config, "DEEPSEEK_MAX_REQUEST_BYTES", size - 1)
    with pytest.raises(ProviderError) as error:
        provider.chat(config.DEEPSEEK_DEFAULT_MODEL, messages)
    assert error.value.code == "request_too_large"
    assert len(captured) == 2
    assert provider.last_used_model is None


@pytest.mark.parametrize(("width", "count"), [(8193, 1), (4097, 15)])
def test_dimensions_include_stricter_limit_for_fifteen_images(width: int, count: int) -> None:
    captured: list[httpx.Request] = []
    images = [_image(width=width, height=1)] + [_image()] * (count - 1)
    with pytest.raises(ProviderError) as error:
        _provider(captured).chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": "看图", "images": images}],
        )
    assert error.value.code == "image_dimensions_exceeded"
    assert captured == []


def test_fourteen_images_keep_the_larger_dimension_limit() -> None:
    captured: list[httpx.Request] = []
    _provider(captured).chat(
        config.DEEPSEEK_DEFAULT_MODEL,
        [{"role": "user", "content": "看图", "images": [_image(width=4097, height=1)] * 14}],
    )
    assert len(captured) == 1


@pytest.mark.parametrize(
    ("status", "code", "fragment"),
    [
        (400, "bad_request", "图片"),
        (403, "permission_denied", "视觉实验模型"),
        (404, "model_unavailable", "视觉实验模型"),
        (413, "request_too_large", "大小限制"),
        (422, "invalid_parameters", "图片"),
    ],
)
def test_vision_http_errors_are_actionable_and_do_not_leak_image_or_secret(
    status: int,
    code: str,
    fragment: str,
) -> None:
    encoded = _image()
    provider = DeepSeekCloudProvider(
        "private-test-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                json={"error": {"message": "private-test-key " + encoded}},
            )
        ),
    )
    with pytest.raises(ProviderError) as error:
        provider.chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": "看图", "images": [encoded]}],
        )
    assert error.value.code == code
    assert fragment in str(error.value)
    assert encoded not in str(error.value)
    assert "private-test-key" not in str(error.value)
    assert provider.last_used_model is None


def test_audit_model_is_reset_after_a_later_invalid_call() -> None:
    provider = _provider([])
    provider.chat(config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "你好"}])
    assert provider.last_used_model == config.DEEPSEEK_DEFAULT_MODEL
    with pytest.raises(ProviderError):
        provider.chat("wrong-model", [{"role": "user", "content": "你好"}])
    assert provider.last_used_model is None


@pytest.mark.parametrize("value_type", [bytes, bytearray])
def test_raw_image_bytes_are_encoded_once(value_type) -> None:
    captured: list[httpx.Request] = []
    encoded = _image()
    raw = value_type(base64.b64decode(encoded))
    provider = _provider(captured)
    provider.chat(
        config.DEEPSEEK_DEFAULT_MODEL,
        [{"role": "user", "content": "看图", "images": [raw]}],
    )
    url = json.loads(captured[0].content)["messages"][0]["content"][1]["image_url"]["url"]
    assert url == f"data:image/png;base64,{encoded}"
    assert base64.b64decode(url.split(",", 1)[1]) == bytes(raw)


@pytest.mark.parametrize("image", [b"", bytearray(), b"not-an-image", b"%%%"])
def test_invalid_raw_bytes_are_not_interpreted_as_base64(image: bytes | bytearray) -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(ProviderError) as error:
        _provider(captured).chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": "看图", "images": [image]}],
        )
    assert error.value.code == "invalid_image"
    assert captured == []


@pytest.mark.parametrize("use_prepared_attachments", [False, True])
def test_ui_attachment_bytes_reach_native_vision_payload(use_prepared_attachments: bool) -> None:
    from ollama_chat_app.ui.chat_page import ChatPage

    raw = base64.b64decode(_image())
    files = SimpleNamespace(
        get_file=lambda user_id, file_id: {"content": raw, "media_type": "image/png"}
    )
    # Exercise the UI's real byte-loading method without launching a window.
    page = SimpleNamespace(
        _pending_image_ids=[1],
        current_user=SimpleNamespace(id=1),
        file_management_service=files,
        _pending_attachments=(
            [SimpleNamespace(content=raw, is_image=True)] if use_prepared_attachments else []
        ),
    )
    images = ChatPage._pending_image_bytes(page)
    assert images == [raw]
    captured: list[httpx.Request] = []
    _provider(captured).chat(
        config.DEEPSEEK_DEFAULT_MODEL,
        [{"role": "user", "content": "描述物体", "images": images}],
    )
    payload = json.loads(captured[0].content)
    assert payload["model"] == config.DEEPSEEK_VISION_MODEL
    url = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == raw


def test_ai_assistant_service_preserves_raw_images_to_provider(tmp_path: Path) -> None:
    from ollama_chat_app.data.database import Database
    from ollama_chat_app.services.ai_assistant import AiAssistantService
    from ollama_chat_app.services.auth import AuthService

    database = Database(tmp_path / "test.db")
    member = AuthService(database).register("synthetic-test-user", "only-test-password-123")
    assistant = AiAssistantService(database)
    raw = base64.b64decode(_image())
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        content = json.dumps({"answer": "图片中有一个彩色区域。", "proposals": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    provider = DeepSeekCloudProvider("mock-only", transport=httpx.MockTransport(handler))
    result = assistant.create_member_draft(
        member.id,
        provider,
        "DeepSeek",
        config.DEEPSEEK_DEFAULT_MODEL,
        "描述图片中的可见颜色",
        images=[raw],
    )
    assert result["answer"] == "图片中有一个彩色区域。"
    payload = json.loads(captured[0].content)
    assert payload["model"] == config.DEEPSEEK_VISION_MODEL
    url = payload["messages"][-1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == raw


def test_output_budget_and_server_model_are_available_for_live_acceptance() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "model": config.DEEPSEEK_VISION_MODEL,
                "choices": [{"message": {"content": "A red square and blue circle."}}],
            },
        )

    provider = DeepSeekCloudProvider("mock-only", transport=httpx.MockTransport(handler))
    provider.chat(
        config.DEEPSEEK_DEFAULT_MODEL,
        [{"role": "user", "content": "Describe", "images": [base64.b64decode(_image())]}],
        max_tokens=128,
    )
    assert json.loads(captured[0].content)["max_tokens"] == 128
    assert provider.last_response_model == config.DEEPSEEK_VISION_MODEL
    with pytest.raises(ProviderError):
        provider.chat("invalid", [])
    assert provider.last_response_model is None


@pytest.mark.parametrize("budget", [0, -1, True, "128"])
def test_invalid_output_budget_cannot_trigger_network(budget: object) -> None:
    captured: list[httpx.Request] = []
    with pytest.raises(ProviderError) as error:
        _provider(captured).chat(
            config.DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": "Test"}],
            max_tokens=budget,
        )
    assert error.value.code == "invalid_parameters"
    assert captured == []


def _live_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "test_deepseek_vision_live.py"
    spec = importlib.util.spec_from_file_location("deepseek_live_tool_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_tool_default_never_reads_key_or_sends(monkeypatch, capsys) -> None:
    module = _live_tool()

    def unexpected_key_read():
        raise AssertionError("The default path must not read any key")

    monkeypatch.setattr(module, "_read_key", unexpected_key_read)
    assert module.main([]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_live_tool_run_without_key_skips_without_constructing_provider(monkeypatch, capsys) -> None:
    module = _live_tool()
    monkeypatch.setattr(module, "_read_key", lambda: "")

    def unexpected_provider(*args, **kwargs):
        raise AssertionError("No provider may be constructed without a key")

    monkeypatch.setattr(
        "ollama_chat_app.providers.deepseek_cloud.DeepSeekCloudProvider", unexpected_provider
    )
    assert module.main(["--run"]) == 0
    assert "no API key supplied" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("reported_model", "answer", "status", "expected_exit"),
    [
        (
            config.DEEPSEEK_VISION_MODEL,
            "Red square on the left and blue circle on the right.",
            200,
            0,
        ),
        (config.DEEPSEEK_DEFAULT_MODEL, "A red square and blue circle.", 200, 1),
        (None, "A red square and blue circle.", 200, 1),
        (config.DEEPSEEK_VISION_MODEL, "", 200, 1),
        (config.DEEPSEEK_VISION_MODEL, "A colored object.", 200, 2),
        (None, "private-test-only-key", 401, 1),
    ],
)
def test_live_tool_mock_has_exactly_one_budgeted_synthetic_request(
    monkeypatch,
    capsys,
    reported_model: str | None,
    answer: str,
    status: int,
    expected_exit: int,
) -> None:
    module = _live_tool()
    captured: list[httpx.Request] = []
    monkeypatch.setattr(module, "_read_key", lambda: "private-test-only-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            status,
            json={
                "model": reported_model,
                "choices": [{"message": {"content": answer}}],
            },
        )

    def factory(key: str) -> DeepSeekCloudProvider:
        assert key == "private-test-only-key"
        return DeepSeekCloudProvider(key, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("ollama_chat_app.providers.deepseek_cloud.DeepSeekCloudProvider", factory)
    assert module.main(["--run"]) == expected_exit
    output = capsys.readouterr().out
    assert "private-test-only-key" not in output
    assert len(captured) == 1
    payload = json.loads(captured[0].content)
    assert payload["model"] == config.DEEPSEEK_VISION_MODEL
    assert payload["max_tokens"] == 128
    assert payload["thinking"] == {"type": "disabled"}
    assert len(payload["messages"]) == 1
    url = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == module.synthetic_image()
