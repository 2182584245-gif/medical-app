"""Verify only a separately labelled synthetic release; does not relax verify_release.py."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def _common():
    spec = importlib.util.spec_from_file_location(
        "medical_demo_common", Path(__file__).with_name("_demo_release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _common()
base = common.sibling("verify_release")
ROOT_ENTRIES = base.EXPECTED_ROOT_ENTRIES | {"assets", "demo_manifest.json", "DEMO_ACCOUNTS.md"}


def verify_directory(root: Path, *, expected_executable_sha256: str | None = None) -> dict:
    if root.is_symlink():
        raise RuntimeError("不允许链接形式的示例发布目录。")
    root = root.resolve()
    files = common.ordinary_tree(root)
    if {path.name for path in root.iterdir()} != ROOT_ENTRIES:
        raise RuntimeError("示例版根目录不符合精确白名单。")
    manifest = {}
    for line in (root / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or not re.fullmatch("[A-F0-9]{64}", digest)
            or name in manifest
            or name not in files
        ):
            raise RuntimeError("示例版哈希清单格式、路径或重复项异常。")
        manifest[name] = digest
    if set(manifest) != files - {"SHA256SUMS.txt"}:
        raise RuntimeError("示例版哈希清单必须涵盖除清单自身之外的每个文件。")
    if any(base._sha256(root / name) != digest for name, digest in manifest.items()):
        raise RuntimeError("示例版文件哈希不匹配。")
    metadata = json.loads((root / "portable.json").read_text(encoding="utf-8"))
    digest = base._sha256(root / base.EXECUTABLE_NAME)
    if (
        metadata.get("application") != common.APP_NAME
        or metadata.get("version") != common.APP_VERSION
        or metadata.get("synthetic") is not True
        or metadata.get("database_state") != "synthetic_demo"
        or metadata.get("database_schema_version") != 6
        or metadata.get("database_included") is not True
        or metadata.get("api_key_persisted") is not False
        or metadata.get("dpapi_included") is not False
        or metadata.get("executable_sha256") != digest
        or metadata.get("source_executable_sha256") != digest
        or metadata.get("synthetic_counts") != common.COUNTS
    ):
        raise RuntimeError("示例版元数据没有正确声明虚构资料、版本和密钥边界。")
    if expected_executable_sha256 and digest != expected_executable_sha256.upper():
        raise RuntimeError("示例版 EXE 与同轮纯净版不相同。")
    instructions = (root / "使用说明.txt").read_text(encoding="utf-8-sig")
    for text in (
        "完整解压",
        "虚构",
        "DEMO_ACCOUNTS.md",
        "不覆盖",
        "DPAPI",
        "不会自动上传",
        "本地模式",
    ):
        if text not in instructions:
            raise RuntimeError("示例说明缺少必须的安全提示。")
    result = common.verify_payload(root)
    return dict(
        kind="synthetic_directory",
        path=str(root),
        file_count=len(files),
        size_bytes=sum((root / name).stat().st_size for name in files),
        executable_sha256=digest,
        database_sha256=base._sha256(root / "data/app.db"),
        database=result,
    )


def verify_archive(path: Path, *, expected_executable_sha256: str | None = None) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("示例 ZIP 不是普通文件。")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len({name.casefold() for name in names}) != len(names):
            raise RuntimeError("示例 ZIP 有重复或大小写冲突成员。")
        for info in infos:
            member = base._validate_member(info, common.APP_NAME)
            if ":" in info.filename or any(part.endswith((" ", ".")) for part in member.parts):
                raise RuntimeError("示例 ZIP 路径不兼容 Windows 安全规则。")
            if any(
                re.fullmatch(
                    r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, flags=re.IGNORECASE
                )
                for part in member.parts
            ):
                raise RuntimeError("示例 ZIP 包含 Windows 保留设备路径。")
        if sum(info.file_size for info in infos) > base.MAX_ARCHIVE_BYTES or archive.testzip():
            raise RuntimeError("示例 ZIP 体积或 CRC 校验失败。")
        with tempfile.TemporaryDirectory(prefix="medical-synthetic-release-") as temporary:
            temporary_root = Path(temporary)
            for info in infos:
                target = temporary_root.joinpath(*PurePosixPath(info.filename).parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
            contents = verify_directory(
                temporary_root / common.APP_NAME,
                expected_executable_sha256=expected_executable_sha256,
            )
    return dict(
        kind="synthetic_zip",
        path=str(path.resolve()),
        sha256=base._sha256(path),
        compressed_size_bytes=path.stat().st_size,
        contents=contents,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--expected-exe-sha256")
    args = parser.parse_args()
    verifier = verify_archive if args.target.suffix.casefold() == ".zip" else verify_directory
    print(
        json.dumps(
            verifier(args.target, expected_executable_sha256=args.expected_exe_sha256),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
