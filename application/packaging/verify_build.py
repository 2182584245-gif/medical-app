from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BANNED_MODULE_PREFIXES = (
    "_distutils_hack",
    "_multiprocessing",
    "distutils",
    "keyring",
    "multiprocessing",
    "pip",
    "pkg_resources",
    "setuptools",
    "wheel",
)
BANNED_SOURCE_MARKERS = (
    "/.cache/codex-runtimes/",
    "/.codex/",
    "/appdata/local/openai/codex/",
)
VOICE_MODEL_HASHES = {
    "_internal/assets/vosk-model-small-cn-0.22/am/final.mdl": (
        "91EDA2C04C4F599361CB92B0E5298CCDF6B3C7A1FA52BFCEFCD2C4E07AA1C131"
    ),
    "_internal/assets/vosk-model-small-cn-0.22/graph/Gr.fst": (
        "46D41A9B1723ABCCDCB354D731A5B58D87C060100D88ADA1A3E31220F52B0434"
    ),
    "_internal/assets/vosk-model-small-cn-0.22/graph/HCLr.fst": (
        "798AF0D9BA6F2D727747F58E1E7B2305F8618BC8EA7F6C51BEECD8A2C965B965"
    ),
    "_internal/assets/vosk-model-small-cn-0.22/ivector/final.ie": (
        "C39D46FBFF04EFB7BAA9660AE7A728B0E2F35A4A1250B56ED3A599E26956DF86"
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _name(entry: object) -> str:
    if isinstance(entry, (tuple, list)) and entry:
        return str(entry[0])
    return str(entry)


def _is_banned_module(name: str) -> bool:
    folded = name.casefold()
    return any(
        folded == prefix or folded.startswith(f"{prefix}.") or folded.startswith(f"{prefix}-")
        for prefix in BANNED_MODULE_PREFIXES
    )


def _source_path(entry: object) -> Path | None:
    if not isinstance(entry, (tuple, list)) or len(entry) < 2:
        return None
    candidate = Path(str(entry[1]))
    return candidate.resolve() if candidate.is_absolute() else None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def verify(toc_path: Path, dist_dir: Path) -> dict[str, object]:
    toc = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    if not isinstance(toc, tuple) or len(toc) < 20:
        raise RuntimeError("Analysis-00.toc 格式与预期不符")

    runtime_hooks = toc[13]
    pure_modules = toc[14]
    binaries = toc[15]
    datas = toc[18]
    base_modules = toc[19]

    errors: list[str] = []
    hook_names = [_name(item) for item in runtime_hooks]
    bad_hooks = [
        name
        for name in hook_names
        if "multiprocessing" in name.casefold() or "setuptools" in name.casefold()
    ]
    if bad_hooks:
        errors.append(f"收集了不需要的运行时 Hook：{bad_hooks}")

    collected_module_names = [_name(item) for item in (*pure_modules, *base_modules)]
    bad_modules = sorted({name for name in collected_module_names if _is_banned_module(name)})
    if bad_modules:
        errors.append(f"收集了禁用的 Python 模块：{bad_modules}")

    source_entries = [*binaries, *datas]
    source_paths = [path for item in source_entries if (path := _source_path(item)) is not None]
    normalized_sources = [f"/{path.as_posix().casefold().strip('/')}/" for path in source_paths]
    bad_marker_sources = sorted(
        {
            str(path)
            for path, normalized in zip(source_paths, normalized_sources, strict=True)
            if any(marker in normalized for marker in BANNED_SOURCE_MARKERS)
        }
    )
    if bad_marker_sources:
        errors.append(f"发现宿主工具缓存来源：{bad_marker_sources}")

    allowed_roots = tuple(
        path.resolve()
        for path in (
            PROJECT_ROOT / "src",
            PROJECT_ROOT / "packaging",
            PROJECT_ROOT / "assets",
            toc_path.parent,
            Path(sys.prefix),
            Path(sys.base_prefix),
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows")),
        )
    )
    unexpected_sources = sorted(
        {
            str(path)
            for path in source_paths
            if not any(_is_relative_to(path, root) for root in allowed_roots)
        }
    )
    if unexpected_sources:
        errors.append(f"发现允许范围外的依赖来源：{unexpected_sources}")

    required_files = (
        "健康生活服务平台.exe",
        "_internal/base_library.zip",
        "_internal/python313.dll",
        "_internal/_socket.pyd",
        "_internal/_sqlite3.pyd",
        "_internal/_ssl.pyd",
        "_internal/sqlite3.dll",
        "_internal/certifi/cacert.pem",
        "_internal/PySide6/QtCore.pyd",
        "_internal/PySide6/QtGui.pyd",
        "_internal/PySide6/QtWidgets.pyd",
        "_internal/PySide6/QtMultimedia.pyd",
        "_internal/PySide6/QtPdf.pyd",
        "_internal/PySide6/QtPdfWidgets.pyd",
        "_internal/PySide6/Qt6Core.dll",
        "_internal/PySide6/Qt6Gui.dll",
        "_internal/PySide6/Qt6Widgets.dll",
        "_internal/PySide6/Qt6Multimedia.dll",
        "_internal/PySide6/Qt6Pdf.dll",
        "_internal/PySide6/Qt6PdfWidgets.dll",
        "_internal/PySide6/plugins/platforms/qwindows.dll",
        "_internal/ollama-0.6.2.dist-info/METADATA",
        "_internal/rapidocr/config.yaml",
        "_internal/rapidocr/default_models.yaml",
        "_internal/rapidocr/models/PP-OCRv6_det_small.onnx",
        "_internal/rapidocr/models/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        "_internal/rapidocr/models/PP-OCRv6_rec_small.onnx",
        "_internal/onnxruntime/capi/onnxruntime.dll",
        "_internal/onnxruntime/capi/onnxruntime_providers_shared.dll",
        "_internal/onnxruntime/capi/onnxruntime_pybind11_state.pyd",
        "_internal/vosk/libgcc_s_seh-1.dll",
        "_internal/vosk/libstdc++-6.dll",
        "_internal/vosk/libvosk.dll",
        "_internal/vosk/libwinpthread-1.dll",
        "_internal/assets/vosk-model-small-cn-0.22/am/final.mdl",
        "_internal/assets/vosk-model-small-cn-0.22/conf/mfcc.conf",
        "_internal/assets/vosk-model-small-cn-0.22/conf/model.conf",
        "_internal/assets/vosk-model-small-cn-0.22/graph/disambig_tid.int",
        "_internal/assets/vosk-model-small-cn-0.22/graph/Gr.fst",
        "_internal/assets/vosk-model-small-cn-0.22/graph/HCLr.fst",
        "_internal/assets/vosk-model-small-cn-0.22/graph/phones/word_boundary.int",
        "_internal/assets/vosk-model-small-cn-0.22/ivector/final.dubm",
        "_internal/assets/vosk-model-small-cn-0.22/ivector/final.ie",
        "_internal/assets/vosk-model-small-cn-0.22/ivector/final.mat",
        "_internal/assets/vosk-model-small-cn-0.22/ivector/global_cmvn.stats",
        "_internal/assets/vosk-model-small-cn-0.22/ivector/online_cmvn.conf",
        "_internal/assets/vosk-model-small-cn-0.22/ivector/splice.conf",
        "_internal/assets/vosk-model-small-cn-0.22/README",
    )
    missing_files = [name for name in required_files if not (dist_dir / name).is_file()]
    if not list(dist_dir.glob("_internal/_argon2_cffi_bindings/_ffi*.pyd")):
        missing_files.append("_internal/_argon2_cffi_bindings/_ffi*.pyd")
    if not list(dist_dir.glob("_internal/cv2/cv2*.pyd")):
        missing_files.append("_internal/cv2/cv2*.pyd")
    if not list(dist_dir.glob("_internal/numpy/_core/_multiarray_umath*.pyd")):
        missing_files.append("_internal/numpy/_core/_multiarray_umath*.pyd")

    required_modules = {
        "ollama_chat_app.cloud_config",
        "ollama_chat_app.services.cloud_client",
        "ollama_chat_app.services.cloud_rpc_codec",
        "ollama_chat_app.services.remote_services",
        "ollama_chat_app.workers.cloud_bridge",
        "ollama_chat_app.time_utils",
        "ollama_chat_app.ui.time_fields",
        "onnxruntime",
        "pypdf",
        "rapidocr.main",
        "vosk",
    }
    missing_modules = sorted(required_modules - set(collected_module_names))
    if missing_modules:
        errors.append(f"缺少必要 Python 模块：{missing_modules}")
    if missing_files:
        errors.append(f"缺少必要运行文件：{missing_files}")

    bad_voice_hashes = [
        name
        for name, expected in VOICE_MODEL_HASHES.items()
        if (dist_dir / name).is_file() and _sha256(dist_dir / name) != expected
    ]
    if bad_voice_hashes:
        errors.append(f"离线语音模型哈希不正确：{bad_voice_hashes}")

    final_files = [path for path in dist_dir.rglob("*") if path.is_file()]
    banned_final_files: list[str] = []
    for path in final_files:
        relative = path.relative_to(dist_dir).as_posix()
        parts = tuple(part.casefold() for part in Path(relative).parts)
        package_part = parts[1] if len(parts) > 1 and parts[0] == "_internal" else parts[0]
        if _is_banned_module(package_part):
            banned_final_files.append(relative)
    if banned_final_files:
        errors.append(f"成品包含禁用文件：{sorted(banned_final_files)}")

    return {
        "status": "passed" if not errors else "failed",
        "runtime_hooks": hook_names,
        "pure_module_count": len(pure_modules),
        "base_module_count": len(base_modules),
        "binary_count": len(binaries),
        "data_count": len(datas),
        "final_file_count": len(final_files),
        "final_size_bytes": sum(path.stat().st_size for path in final_files),
        "errors": errors,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("用法：verify_build.py <Analysis-00.toc> <dist 目录>")
        return 2
    result = verify(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
