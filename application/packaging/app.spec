# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller configuration for the Windows onedir build."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


PROJECT_ROOT = Path(SPECPATH).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
MAIN_SCRIPT = PROJECT_ROOT / "packaging" / "entrypoint.py"
VERSION_INFO = PROJECT_ROOT / "packaging" / "version_info.txt"
VOICE_MODEL = PROJECT_ROOT / "assets" / "vosk-model-small-cn-0.22"

# Ollama reads its installed version with importlib.metadata to construct the
# User-Agent header. It needs only its own metadata; keyring is intentionally not
# collected; explicitly saved keys use Windows DPAPI outside the release tree.
data_files = (
    copy_metadata("ollama")
    + copy_metadata("pypdf")
    + copy_metadata("rapidocr")
    + copy_metadata("onnxruntime")
    + copy_metadata("vosk")
    + collect_data_files("tzdata")
    + collect_data_files("rapidocr", includes=["*.yaml", "models/*"])
    + [(str(VOICE_MODEL), "assets/vosk-model-small-cn-0.22")]
)

# Vosk loads its native library through CFFI at runtime, and ONNX Runtime
# exposes its execution engine through binaries in onnxruntime/capi. Explicit
# collection keeps those runtime assets present even when ModuleGraph cannot
# infer the dynamic import path.
native_binaries = collect_dynamic_libs("vosk") + collect_dynamic_libs("onnxruntime")

# These packages are either build-time tooling, unsupported-platform backends,
# or process-spawning infrastructure unused by this desktop application. Keeping
# them out reduces both the attack surface and the amount of unrelated code an
# antivirus scanner needs to evaluate.
excluded_modules = [
    "_distutils_hack",
    "_multiprocessing",
    "distutils",
    "keyring",
    "multiprocessing",
    "pip",
    "pkg_resources",
    "setuptools",
    "wheel",
]


analysis = Analysis(
    [str(MAIN_SCRIPT)],
    pathex=[str(SRC_DIR)],
    binaries=native_binaries,
    datas=data_files,
    # Role workspaces are intentionally loaded lazily so a missing optional UI
    # cannot route staff into the member screen. Include the complete release
    # modules explicitly because static analysis cannot see import_module().
    hiddenimports=[
        "ollama_chat_app.ui.advisor_workspace",
        "ollama_chat_app.ui.operator_workspace",
        "PySide6.QtMultimedia",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "rapidocr.main",
        "rapidocr.inference_engine.onnxruntime",
        "rapidocr.inference_engine.onnxruntime.main",
        "onnxruntime",
        "vosk",
        "vosk.vosk_cffi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)

# A developer host can expose Codex's bundled Poppler/OpenSSL/MSVC runtime through
# PATH. PyInstaller may then mistake those host binaries for application
# dependencies. Filter by the resolved *source path* so every current and future
# binary from a .cache/codex-runtimes tree is rejected, not just a short filename
# denylist.
def _comes_from_codex_runtime_cache(entry):
    source_path = Path(entry[1]).resolve()
    source_parts = tuple(part.casefold() for part in source_path.parts)
    return any(
        source_parts[index : index + 2] == (".cache", "codex-runtimes")
        for index in range(len(source_parts) - 1)
    )


analysis.binaries = [
    entry
    for entry in analysis.binaries
    if not _comes_from_codex_runtime_cache(entry)
]

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="健康生活服务平台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    version=str(VERSION_INFO),
    # PyInstaller's standard Windows manifest maps these two False values to
    # requestedExecutionLevel="asInvoker" and uiAccess="false".
    uac_admin=False,
    uac_uiaccess=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="健康生活服务平台",
)
