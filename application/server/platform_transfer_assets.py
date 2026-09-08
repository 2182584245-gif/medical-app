"""Explicit, content-addressed product/avatar assets; never network fetching.

File hydration for reports is handled by platform_transfer into BYTEA. These
external images are staged at a new immutable path before the DB transaction.
Failed DB transactions retain unreferenced staging artifacts for administrator
recovery; they do not remove or overwrite existing target/source assets.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

from PIL import Image

from .platform_transfer import TransferError, _file_root, _no_secrets, _safe_file, digest


def references(snapshot):
    result = {row["image_path"] for row in snapshot["rows"]["products"] if row["image_path"]}
    for row in snapshot["rows"]["user_preferences"]:
        value = json.loads(row["preferences_json"]).get("avatar_path")
        if value:
            result.add(value)
    return result


def _image(content):
    if not 0 < len(content) <= 15 * 1024 * 1024:
        raise TransferError("产品/头像原图超过 15 MiB 上限。")
    _no_secrets(content)
    try:
        with Image.open(io.BytesIO(content)) as picture:
            if picture.width * picture.height > 16_777_216:
                raise ValueError
            suffix = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}[picture.format]
            picture.verify()
        return suffix
    except Exception:
        raise TransferError("产品/头像资产不是安全支持的有效图片。") from None


def attach_assets(snapshot, asset_root: Path | None):
    needed = references(snapshot)
    if needed and asset_root is None:
        raise TransferError("存在外部产品/头像原图，必须明确指定源或目标资产目录；不会静默丢失。")
    assets = {}
    if needed:
        root = _file_root(asset_root)
        for original in sorted(needed):
            path = Path(original)
            if path.is_absolute():
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    raise TransferError("外部图片不在已确认的资产目录内。") from None
            else:
                relative = original.replace("\\", "/")
            content = _safe_file(root, relative, 15 * 1024 * 1024)
            assets[original] = {
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "format": _image(content),
            }
    snapshot["assets"] = assets
    return snapshot


def plan_assets(source):
    assets = source.get("assets", {})
    if set(assets) != references(source):
        raise TransferError("外部资产清单与 29 表中的引用不完全一致。")
    namespace = digest(source)[:32]
    result = []
    for original, asset in sorted(assets.items()):
        content = asset["content"]
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != asset["sha256"] or _image(content) != asset["format"]:
            raise TransferError("外部资产哈希或格式已改变。")
        result.append(
            {
                "source_path": original,
                "sha256": checksum,
                "bytes": len(content),
                "destination": f"transfer-assets/{namespace}/{checksum}.{asset['format']}",
            }
        )
    return result


def rewrite_asset_paths(rows, asset_plan):
    mapping = {item["source_path"]: item["destination"] for item in asset_plan}
    for row in rows["products"]:
        if row["image_path"]:
            row["image_path"] = mapping[row["image_path"]]
    for row in rows["user_preferences"]:
        preferences = json.loads(row["preferences_json"])
        if preferences.get("avatar_path"):
            preferences["avatar_path"] = mapping[preferences["avatar_path"]]
            row["preferences_json"] = json.dumps(
                preferences, ensure_ascii=False, separators=(",", ":")
            )


def stage_assets(source, plan, asset_root: Path | None):
    expected = plan_assets(source)
    if expected != plan["external_assets"]:
        raise TransferError("资产计划已变化。")
    if not expected:
        return {"staged": 0}
    if asset_root is None:
        raise TransferError("需明确设置目标资产目录，不能仅迁移图片路径。")
    root = _file_root(asset_root)
    parent = root / "transfer-assets"
    if not parent.exists():
        parent.mkdir(mode=0o700)
    _file_root(parent)
    namespace = Path(expected[0]["destination"]).parts[1]
    target = parent / namespace
    if target.exists():
        # A prior ambiguous attempt can reuse only a byte-identical immutable set.
        verify_staged_assets(plan, root)
        return {"staged": len(expected), "reused_verified_staging": True}
    temporary = Path(tempfile.mkdtemp(prefix=".transfer-stage-", dir=parent))
    for item in expected:
        output = temporary / Path(item["destination"]).name
        content = source["assets"][item["source_path"]]["content"]
        if output.exists():
            if output.read_bytes() != content:
                raise TransferError("资产内容地址碰撞。")
            continue
        with output.open("xb") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    # Rename only our newly created directory into a checked-new child. Never
    # invoke another shell, replace an existing folder, or remove old assets.
    if target.exists() or temporary.parent != parent or not target.resolve().is_relative_to(root):
        raise TransferError("资产目标发生变化；保留临时文件供检查。")
    temporary.rename(target)
    verify_staged_assets(plan, root)
    return {"staged": len(expected), "directory": str(target)}


def verify_staged_assets(plan, asset_root):
    if not plan["external_assets"]:
        return
    if asset_root is None:
        raise TransferError("缺少目标资产目录。")
    root = _file_root(asset_root)
    for item in plan["external_assets"]:
        content = _safe_file(root, item["destination"], 15 * 1024 * 1024)
        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise TransferError("已暂存资产与确认的迁移计划不一致。")
