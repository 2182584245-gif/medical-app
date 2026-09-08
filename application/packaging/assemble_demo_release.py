"""Assemble optional clean + synthetic packages from one freshly verified final EXE."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def _common():
    spec = importlib.util.spec_from_file_location(
        "medical_demo_assembly_common", Path(__file__).with_name("_demo_release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _common()
clean = common.sibling("assemble_release")
verify = common.sibling("verify_demo_release")


def _hash_manifest(destination: Path) -> None:
    paths = sorted(
        (
            path
            for path in destination.rglob("*")
            if path.is_file() and path != destination / "SHA256SUMS.txt"
        ),
        key=lambda path: path.relative_to(destination).as_posix().casefold(),
    )
    lines = [f"{clean._sha256(path)}  {path.relative_to(destination).as_posix()}" for path in paths]
    (destination / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _zip(root: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, common.APP_NAME + "/" + path.relative_to(root).as_posix())


def assemble(
    source: Path,
    demo: Path,
    destination: Path,
    *,
    clean_destination: Path | None = None,
    demo_zip: Path | None = None,
    clean_zip: Path | None = None,
) -> dict:
    supplied = [
        path for path in (source, demo, destination, clean_destination, demo_zip, clean_zip) if path
    ]
    if any(
        path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
        for path in supplied
    ):
        raise RuntimeError("输入与输出不允许符号链接。")
    source, demo, destination = source.resolve(), demo.resolve(), destination.resolve()
    targets = [
        destination,
        *(path.resolve() for path in (clean_destination, demo_zip, clean_zip) if path),
    ]
    if clean_zip and not clean_destination:
        raise RuntimeError("纯净 ZIP 必须有独立纯净目录。")
    if len(set(targets)) != len(targets) or any(path.exists() for path in targets):
        raise RuntimeError("发布目标重复或已存在，拒绝覆盖。")
    for index, target in enumerate(targets):
        if any(
            target.is_relative_to(other) or other.is_relative_to(target)
            for other in [source, demo, *targets[:index]]
        ):
            raise RuntimeError("输入输出目录或 ZIP 路径不能互相包含。")
    common.verify_fresh_build(source)
    source_hashes = {name: clean._sha256(source / name) for name in common.ordinary_tree(source)}
    payload = common.verify_payload(demo)
    before = {name: clean._sha256(demo / name) for name in common.PAYLOAD_FILES}
    executable_hash = clean._sha256(source / clean.EXECUTABLE_NAME)
    report = {"version": common.APP_VERSION, "same_executable_sha256": executable_hash}
    if clean_destination:
        clean.assemble(source, clean_destination)
        report["clean"] = common.sibling("verify_release").verify_directory(clean_destination)
    shutil.copytree(source, destination)
    for name in sorted(common.PAYLOAD_FILES):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(demo / name, target)
    if any(
        clean._sha256(demo / name) != digest or clean._sha256(destination / name) != digest
        for name, digest in before.items()
    ):
        raise RuntimeError("示例源或复制后的文件在组装期间发生变化。")
    metadata = dict(
        application=common.APP_NAME,
        version=common.APP_VERSION,
        build_kind="windows-x64-portable-onedir",
        database_included=True,
        database_state="synthetic_demo",
        database_schema_version=6,
        synthetic=True,
        api_key_persisted=False,
        dpapi_included=False,
        executable_sha256=executable_hash,
        source_executable_sha256=executable_hash,
        assembled_at_utc=datetime.now(UTC).isoformat(),
        synthetic_counts=payload["counts"],
    )
    (destination / "portable.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    instructions = clean._instructions().replace(
        "本包已包含空数据库 data\\app.db。", "本示例包包含仅有虚构资料的 data\\app.db。"
    )
    instructions += """

示例版专用提示：
所有账号、地址、健康记录、预约和商品均为虚构预设示例，不是实测值或 AI 医疗结论。
请先完整解压到新的独立目录，不覆盖纯净版或原有 data 目录。
程序首次启动默认云端模式；使用演示账号前，请主动选择“本地模式”并应用，再登录。
初始账号和独立随机密码见 DEMO_ACCOUNTS.md。它们只适用于本示例库，不是云端账号。
示例库不会自动上传到云端；不要把示例数据作为个人历史进行首次导入。
本包不包含 API Key、DPAPI 私有文件、数据库连接密码或云端登录令牌。
商品和订单只是虚拟流程，无支付、发货或真实医疗服务。若希望录入真实资料，请改用同轮纯净包。
"""
    (destination / "使用说明.txt").write_text(instructions, encoding="utf-8-sig")
    _hash_manifest(destination)
    report["demo"] = verify.verify_directory(
        destination, expected_executable_sha256=executable_hash
    )
    if clean._sha256(source / clean.EXECUTABLE_NAME) != executable_hash:
        raise RuntimeError("构建源 EXE 在组装期间发生变化。")
    if (
        clean_destination
        and clean._sha256(clean_destination / clean.EXECUTABLE_NAME) != executable_hash
    ):
        raise RuntimeError("两版 EXE 不一致。")
    for name, digest in source_hashes.items():
        if clean._sha256(source / name) != digest or clean._sha256(destination / name) != digest:
            raise RuntimeError("构建源或示例包运行文件在组装期间发生变化。")
        if clean_destination and clean._sha256(clean_destination / name) != digest:
            raise RuntimeError("纯净包和示例包运行文件不一致。")
    if demo_zip:
        _zip(destination, demo_zip)
        report["demo_zip"] = verify.verify_archive(
            demo_zip, expected_executable_sha256=executable_hash
        )
    if clean_zip:
        _zip(clean_destination, clean_zip)
        report["clean_zip"] = common.sibling("verify_release").verify_archive(clean_zip)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--demo", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--clean-destination", type=Path)
    parser.add_argument("--zip", type=Path, dest="demo_zip")
    parser.add_argument("--clean-zip", type=Path)
    args = parser.parse_args()
    result = assemble(
        args.source,
        args.demo,
        args.destination,
        clean_destination=args.clean_destination,
        demo_zip=args.demo_zip,
        clean_zip=args.clean_zip,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
