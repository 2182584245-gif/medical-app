"""Collect upstream license artifacts for the Android release, without changing code.

Python distribution licenses are copied byte-for-byte from the supplied APK.
Other small license files are fetched from versioned or commit-pinned official
repositories. No APK, dependency, user database, or signing material is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "android/app/src/main/assets/licenses"
REQUIREMENTS = "assets/chaquopy/requirements-common.imy"
UPSTREAM = {
    "Python-3.13.9-LICENSE.txt": "https://raw.githubusercontent.com/python/cpython/v3.13.9/LICENSE",
    "Chaquopy-17.0.0-LICENSE.txt": "https://raw.githubusercontent.com/chaquo/chaquopy/17.0.0/LICENSE.txt",
    "Vosk-API-LICENSE-Apache-2.0.txt": "https://raw.githubusercontent.com/alphacep/vosk-api/05adbfcc0df27a1535913c6accd4b7fc60ffd59d/COPYING",
    "JNA-5.18.1-LICENSE-choice.txt": "https://raw.githubusercontent.com/java-native-access/jna/5.18.1/LICENSE",
    "JNA-5.18.1-Apache-2.0.txt": "https://raw.githubusercontent.com/java-native-access/jna/5.18.1/AL2.0",
    "JNA-5.18.1-LGPL-2.1.txt": "https://raw.githubusercontent.com/java-native-access/jna/5.18.1/LGPL2.1",
    "AndroidX-LICENSE-Apache-2.0.txt": "https://raw.githubusercontent.com/androidx/androidx/4e931c638cc2fca0f2326b9806a09e9f9d511175/LICENSE.txt",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(item: tuple[str, str]) -> tuple[str, bytes, str]:
    name, url = item
    request = Request(url, headers={"User-Agent": "HealthLife-License-Collector/1.0"})
    with urlopen(request, timeout=45) as response:
        data = response.read(1_000_001)
    if not 100 <= len(data) <= 1_000_000 or b"<html" in data[:200].lower():
        raise ValueError(f"Unexpected upstream license response: {url}")
    data.decode("utf-8")
    return name, data, url


def collect(apk: Path) -> tuple[dict[str, tuple[bytes, str]], list[dict]]:
    artifacts: dict[str, tuple[bytes, str]] = {}
    packages = []
    with ZipFile(apk) as outer, ZipFile(io.BytesIO(outer.read(REQUIREMENTS))) as archive:
        members = archive.namelist()
        for metadata_name in sorted(n for n in members if n.endswith(".dist-info/METADATA")):
            metadata = BytesParser().parsebytes(archive.read(metadata_name))
            directory = metadata_name.rsplit("/", 1)[0]
            license_members = [
                name
                for name in members
                if name.startswith(directory + "/")
                and not name.endswith("/")
                and any(
                    token in Path(name).name.upper()
                    for token in ("LICENSE", "COPYING", "NOTICE")
                )
            ]
            if not license_members:
                raise ValueError(f"No embedded license found for {directory}")
            filenames = []
            for member in sorted(license_members):
                relative = member[len(directory) + 1 :]
                name = (
                    re.sub(
                        r"[^A-Za-z0-9._-]",
                        "_",
                        directory.removesuffix(".dist-info") + "__" + relative,
                    )
                    + ".txt"
                )
                if name in artifacts:
                    raise ValueError(f"Duplicate flattened filename: {name}")
                artifacts[name] = (archive.read(member), f"APK!/{REQUIREMENTS}!/{member}")
                filenames.append(name)
            packages.append(
                {
                    "name": metadata["Name"],
                    "version": metadata["Version"],
                    "license_files": filenames,
                }
            )
    if len(packages) < 13:
        raise ValueError(f"Expected at least 13 Python distributions, found {len(packages)}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, data, url in pool.map(download, UPSTREAM.items()):
            artifacts[name] = (data, url)
    model_readme = ROOT / "assets/vosk-model-small-cn-0.22/README"
    artifacts["Vosk-model-small-cn-0.22-README.txt"] = (
        model_readme.read_bytes(),
        "workspace:assets/vosk-model-small-cn-0.22/README",
    )
    return artifacts, packages


def notice(packages: list[dict]) -> bytes:
    rows = "\n".join(
        f"- {p['name']} {p['version']}: {', '.join(p['license_files'])}" for p in packages
    )
    content = f"""健康生活服务平台 Android 1.0.2 — 第三方声明 / Third-party notices

本目录保存本次分发涉及的主要第三方许可原文和来源索引。各组件的版权、
商标和许可属于其各自权利人。本说明不将第三方软件重新授权，也不表示
这些机构认可本应用。具体许可原文优先于下列简要说明。

1. APK 中的 Python 依赖
下列文件从已构建 APK 的 {REQUIREMENTS} 内的 dist-info
逐字节复制，包括其原有版权声明；没有改写许可正文。
{rows}

2. Python 与 Android 嵌入运行时
- CPython 3.13.9: Python-3.13.9-LICENSE.txt，包含 PSF 与历史许可原文。
  官方源码：https://github.com/python/cpython/tree/v3.13.9
  Android 运行时由 Chaquopy 提供；本应用未自行修改 CPython 源码。
  Chaquopy 的 Android 构建及适配来源：https://github.com/chaquo/chaquopy/tree/17.0.0/target
- Chaquopy 17.0.0: MIT，见 Chaquopy-17.0.0-LICENSE.txt。
  官方源码：https://github.com/chaquo/chaquopy/tree/17.0.0

3. Android 原生与语音组件
- AndroidX Activity、WebKit、ExifInterface 及相关 AndroidX 组件采用
  Apache License 2.0；通用原文见 AndroidX-LICENSE-Apache-2.0.txt。
  官方源码：https://github.com/androidx/androidx
  本说明不把 Google ML Kit 归入 AndroidX，也不把 ML Kit 声明为 Apache 开源。
- Vosk Android 0.3.75: Apache License 2.0，见 Vosk-API-LICENSE-Apache-2.0.txt。
  官方源码：https://github.com/alphacep/vosk-api
  此副本来自上游固定提交；准确来源及摘要见 SOURCES.json。
- vosk-model-small-cn-0.22: 官方模型清单将本模型列为 Apache 2.0。
  模型清单：https://alphacephei.com/vosk/models
  模型文件：https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
  原 README 见 Vosk-model-small-cn-0.22-README.txt；Apache 2.0 全文同本目录。
- Java Native Access 5.18.1: 上游允许选择 Apache-2.0 或 LGPL-2.1-or-later。
  本次分发采用 Apache-2.0 许可选项；保留 JNA 上游选择说明、Apache 全文及
  LGPL 全文供查阅，并非要求同时采用两种许可。
  官方源码：https://github.com/java-native-access/jna/tree/5.18.1

4. Google ML Kit — 独立使用条款及隐私披露
本应用使用随包内置中文模型的 com.google.mlkit:text-recognition-chinese:16.0.1。
ML Kit 及其 Google 组件应依适用的 Google 条款使用，不在此声明为开源软件，
也不适用本目录中 AndroidX 的 Apache 许可替代其专有条款。
官方条款：https://developers.google.com/ml-kit/terms
Google APIs 条款：https://developers.google.com/terms
隐私说明：https://developers.google.com/ml-kit/terms#privacy
Android 数据披露：https://developers.google.com/ml-kit/android-data-disclosure

根据 Google 的公开说明，OCR 输入图片/文字及识别结果在本机处理，不发送到
Google 服务器；SDK 仍可能向 Google 发送设备与应用信息、每安装标识、
性能/调用统计、输入格式/尺寸和错误等诊断信息，并可能取得兼容性信息。
因此“离线识别”不等于“SDK 完全不联网”。本应用没有将未公开或内部接口
作为关闭所有 ML Kit 遥测的保证。WebView.MetricsOptOut 仅针对 WebView
使用统计，不是 ML Kit 的遥测关闭开关。

5. 文件来源与重新收集
SOURCES.json 记录每份许可的官方 URL 或原始 APK 内路径、SHA-256 和字节数。
extract_source_apk_sha256 标识用于抽取依赖许可的先前构建，不是包含本目录
后的最终 APK 摘要；最终交付 APK 的校验值由发布流程另行提供。
可使用 tools/collect_android_licenses.py --apk <已构建APK> 重新收集，
或以 --check 检查已生成材料的完整性。未删除或替换依赖自带的原始许可。
"""
    return content.encode("utf-8")


def validate() -> None:
    manifest = json.loads((OUTPUT / "SOURCES.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        name = entry["file"]
        if Path(name).name != name:
            raise ValueError("Non-flat filename in license manifest")
        data = (OUTPUT / name).read_bytes()
        if len(data) != entry["bytes"] or sha256(data) != entry["sha256"]:
            raise ValueError(f"License artifact mismatch: {name}")
    print(
        f"Verified {len(manifest['files'])} license/notice artifacts; "
        f"{len(manifest['python_distributions'])} Python distributions."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        validate()
        return
    if args.apk is None or not args.apk.is_file():
        parser.error("--apk must identify an existing Android release APK")
    artifacts, packages = collect(args.apk)
    artifacts["NOTICE.txt"] = (notice(packages), "generated:tools/collect_android_licenses.py")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, (data, source) in sorted(artifacts.items()):
        if Path(name).name != name:
            raise ValueError("Refusing to write a non-flat artifact filename")
        (OUTPUT / name).write_bytes(data)
        entries.append({"file": name, "source": source, "bytes": len(data), "sha256": sha256(data)})
    with args.apk.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "format": 1,
        "extract_source_apk_sha256": source_hash,
        "python_distributions": packages,
        "files": entries,
    }
    (OUTPUT / "SOURCES.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    validate()


if __name__ == "__main__":
    main()
