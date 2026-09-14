"""Prove the no-EXE-change local companion-image path using synthetic pixels."""

from PySide6.QtGui import QColor, QPixmap

from ollama_chat_app.ui import member_commerce


def test_cloud_relative_asset_can_be_read_from_companion_app_folder(qapp, tmp_path, monkeypatch):
    relative = "transfer-assets/synthetic-namespace/synthetic-image.png"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    image = QPixmap(32, 32)
    image.fill(QColor("#ab3579"))
    assert image.save(str(path))
    monkeypatch.setattr(member_commerce, "application_dir", lambda: tmp_path)
    shown = member_commerce.product_illustration({"image_path": relative}, size=32).toImage()
    assert shown.pixelColor(16, 16) == QColor("#ab3579")


def test_missing_cloud_url_or_unsafe_path_falls_back_without_network(qapp, tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Product illustration must not fetch a network image")

    monkeypatch.setattr("httpx.Client.send", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr(member_commerce, "application_dir", lambda: tmp_path)
    fallback = member_commerce.product_illustration({"name": "合成杯"}, size=82).toImage()
    for path in ("https://39.106.166.15/aliyun/image.png", "transfer-assets/missing.png",
                 "../outside.png"):
        actual = member_commerce.product_illustration(
            {"name": "合成杯", "image_path": path}, size=82
        ).toImage()
        assert actual == fallback
