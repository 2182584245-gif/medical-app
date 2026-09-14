"""Only the six frozen synthetic PNGs may have reviewed cloud-path aliases.

This is not a file downloader or a general cloud-product image feature. The
namespace comes from an explicitly reviewed transfer plan. No EXE, source DB,
original image, existing release directory, or arbitrary file is rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

MAPPING_FILE = "cloud-assets-plan.json"
ROOT_DIRECTORY = "transfer-assets"
MAX_PLAN_BYTES = 16 * 1024
# Read-only verified files from outputs/synthetic-demo-20260909. Images 3 and 4
# have equal bytes, therefore six source mappings produce five unique aliases.
APPROVED_IMAGES = {
    "assets/products/demo-1.png": (
        "28c997abb182380a4cc200f953a5912cc3975c25cd3bc3d0061ad529a73ef1cb",
        8372,
    ),
    "assets/products/demo-2.png": (
        "264455605562f6a43ca5bf3f3055001782db19e248078c15559d46bae0b8c3ba",
        10468,
    ),
    "assets/products/demo-3.png": (
        "8fdf6a5c4cdf9e7226fbb5cf1de2fa98565833fd7255f054f349b29346956421",
        11446,
    ),
    "assets/products/demo-4.png": (
        "8fdf6a5c4cdf9e7226fbb5cf1de2fa98565833fd7255f054f349b29346956421",
        11446,
    ),
    "assets/products/demo-5.png": (
        "31fa8f3f152d7e3f7aa28c28acefbdbadc3c17dc5cdf5edc68b6926c1d873da8",
        7362,
    ),
    "assets/products/demo-6.png": (
        "d183f5b3b4d30b925913643f70e21f55341936d44efc67e9092cb4c342717104",
        15047,
    ),
}


def ordinary_path(path: Path, *, exists: bool = True) -> Path:
    path = Path(path).absolute()
    for entry in (path, *path.parents):
        if entry.is_symlink() or entry.is_junction():
            raise RuntimeError("示例图片别名不允许链接、junction 或重解析路径。")
        if entry.exists() and getattr(entry.stat(), "st_file_attributes", 0) & 0x400:
            raise RuntimeError("示例图片别名不允许重解析路径。")
    if exists and not path.exists():
        raise RuntimeError("示例图片别名输入不存在。")
    return path


def read_file(path: Path, *, max_bytes: int) -> bytes:
    path = ordinary_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= max_bytes:
        raise RuntimeError("示例图片别名输入不是受限的独立普通文件。")
    with path.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise RuntimeError("示例图片别名输入超过读取上限。")
    return content


def _unique_fields(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RuntimeError("示例图片映射存在重复 JSON 字段。")
        result[key] = value
    return result


def normalize(value) -> list[dict]:
    if type(value) is not list or len(value) != 6:
        raise RuntimeError("仅接受恰好六项的非敏感示例图片映射列表，不接受完整迁移计划。")
    result, sources, namespaces = [], set(), set()
    for item in value:
        if type(item) is not dict or set(item) != {"source_path", "destination", "sha256", "bytes"}:
            raise RuntimeError("示例图片映射只能包含四个固定非敏感字段。")
        name = item["source_path"]
        if type(name) is not str or name not in APPROVED_IMAGES or name in sources:
            raise RuntimeError("示例图片映射必须精确覆盖原六张 PNG，不能重复或替换来源。")
        checksum, size = APPROVED_IMAGES[name]
        if item["sha256"] != checksum or type(item["bytes"]) is not int or item["bytes"] != size:
            raise RuntimeError("示例图片 SHA256 或字节数不属于已审核的原六图。")
        destination = item["destination"]
        match = (
            re.fullmatch(r"transfer-assets/([a-f0-9]{32})/([a-f0-9]{64})\.png", destination)
            if type(destination) is str
            else None
        )
        if not match or match[2] != checksum:
            raise RuntimeError("别名只允许单层命名空间内与图片 SHA256 对应的相对 PNG 路径。")
        sources.add(name)
        namespaces.add(match[1])
        result.append({key: item[key] for key in ("source_path", "destination", "sha256", "bytes")})
    if sources != set(APPROVED_IMAGES) or len(namespaces) != 1:
        raise RuntimeError("六张示例图片必须使用同一个已审阅的命名空间。")
    return sorted(result, key=lambda item: item["source_path"])


def load(path: Path) -> list[dict]:
    try:
        raw = read_file(path, max_bytes=MAX_PLAN_BYTES)
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_fields)
        return normalize(value)
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("无法安全读取六图映射；未输出原始内容。") from None


def load_external(path: Path, *excluded_roots: Path) -> list[dict]:
    reviewed = ordinary_path(path).resolve()
    for root in excluded_roots:
        root = ordinary_path(root, exists=False).resolve()
        if reviewed == root or reviewed.is_relative_to(root):
            raise RuntimeError("必须使用发行包与来源包之外独立审阅的映射，禁止包内计划自证。")
    return load(reviewed)


def encoded(plan) -> bytes:
    return (
        json.dumps(normalize(plan), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def metadata(plan) -> dict:
    plan = normalize(plan)
    return {
        "mapping_file": MAPPING_FILE,
        "mapping_sha256": hashlib.sha256(encoded(plan)).hexdigest(),
        "namespace": plan[0]["destination"].split("/")[1],
        "mapping_count": 6,
        "file_count": len({item["destination"] for item in plan}),
        "scope": "bundled_synthetic_aliases_only",
    }


def validate_originals(root: Path, plan) -> None:
    for item in normalize(plan):
        raw = read_file(root / item["source_path"], max_bytes=item["bytes"])
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise RuntimeError("原示例图片已改变，不能按云映射生成别名。")


def stage(root: Path, plan) -> dict:
    """Caller has made a NEW release directory; fail rather than reuse aliases."""
    root, plan = ordinary_path(root), normalize(plan)
    validate_originals(root, plan)
    branch, manifest = root / ROOT_DIRECTORY, root / MAPPING_FILE
    if branch.exists() or manifest.exists():
        raise RuntimeError("别名或映射文件已存在；只允许组装全新发行目录。")
    branch.mkdir()
    namespace = branch / metadata(plan)["namespace"]
    namespace.mkdir()
    written = set()
    for item in plan:
        target = root / item["destination"]
        if item["destination"] in written:
            continue  # Only approved byte-identical images 3/4 share their hash.
        raw = read_file(root / item["source_path"], max_bytes=item["bytes"])
        with ordinary_path(target, exists=False).open("xb") as stream:
            stream.write(raw)
        written.add(item["destination"])
    with ordinary_path(manifest, exists=False).open("xb") as stream:
        stream.write(encoded(plan))
    return verify(root, plan)


def verify(root: Path, plan) -> dict:
    root, plan = ordinary_path(root), normalize(plan)
    validate_originals(root, plan)
    if load(root / MAPPING_FILE) != plan or read_file(
        root / MAPPING_FILE, max_bytes=MAX_PLAN_BYTES
    ) != encoded(plan):
        raise RuntimeError("包内示例映射与明确审阅的外部计划不一致。")
    info = metadata(plan)
    branch, namespace = root / ROOT_DIRECTORY, root / ROOT_DIRECTORY / info["namespace"]
    if not ordinary_path(branch).is_dir() or not ordinary_path(namespace).is_dir():
        raise RuntimeError("示例图片别名必须位于普通目录。")
    if {entry.name for entry in branch.iterdir()} != {info["namespace"]}:
        raise RuntimeError("别名目录含有额外命名空间或文件。")
    expected = {item["destination"]: item for item in plan}
    if {entry.name for entry in namespace.iterdir()} != {Path(name).name for name in expected}:
        raise RuntimeError("别名目录只能包含明确映射的五个去重 PNG。")
    for name, item in expected.items():
        raw = read_file(root / name, max_bytes=item["bytes"])
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise RuntimeError("示例图片别名的实际字节与已审核 SHA256 不一致。")
    return info
