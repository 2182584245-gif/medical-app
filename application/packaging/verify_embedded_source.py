"""Verify required app modules and source parity in a final EXE or standalone PYZ.

This reads archives without running the executable or importing application code.
A standalone PYZ remains useful during development, but only the EXE mode binds
the result to the actual distributable. Neither mode is an antivirus/runtime test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import CodeType

from PyInstaller.archive.readers import PKG_ITEM_PYZ, CArchiveReader, ZlibArchiveReader

REQUIRED_CLOUD_MODULES = frozenset({
    "ollama_chat_app.cloud_config",
    "ollama_chat_app.services.cloud_client",
    "ollama_chat_app.services.cloud_rpc_codec",
    "ollama_chat_app.services.remote_services",
    "ollama_chat_app.workers.cloud_bridge",
})
REQUIRED_APP_MODULES = REQUIRED_CLOUD_MODULES | {
    "ollama_chat_app", "ollama_chat_app.main", "ollama_chat_app.ui.main_window",
    "ollama_chat_app.ui.auth_pages",
}


def normalized(value):
    if isinstance(value, CodeType):
        return (
            value.co_code,
            normalized(value.co_consts),
            value.co_names,
            value.co_varnames,
            value.co_freevars,
            value.co_cellvars,
            value.co_argcount,
            value.co_posonlyargcount,
            value.co_kwonlyargcount,
            value.co_flags,
            value.co_exceptiontable,
            value.co_linetable,
            value.co_firstlineno,
        )
    if isinstance(value, tuple):
        return tuple(normalized(item) for item in value)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_archive(path: Path):
    if path.suffix.casefold() == ".exe":
        executable = CArchiveReader(str(path))
        names = [name for name, item in executable.toc.items() if item[-1] == PKG_ITEM_PYZ]
        if len(names) != 1:
            raise ValueError("Expected exactly one embedded PYZ")
        return executable.open_embedded_archive(names[0]), "exe_embedded_pyz", names[0]
    if path.suffix.casefold() != ".pyz":
        raise ValueError("Expected EXE or PYZ")
    return ZlibArchiveReader(str(path)), "standalone_pyz", path.name


def verify(path: Path, *, source_root: Path | None = None) -> dict:
    path = path.resolve()
    root = source_root or Path(__file__).resolve().parents[1] / "src"
    report = {"status": "failed", "artifact": str(path), "module_count": 0,
              "missing_modules": [], "mismatches": [], "errors": []}
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError
        digest_before = _sha256(path)
        archive, kind, archive_name = _open_archive(path)
    except Exception:
        report["errors"] = ["archive_unreadable_or_ambiguous"]
        return report
    checked = []
    mismatches = []
    missing = sorted(REQUIRED_APP_MODULES - set(archive.toc))
    for name in sorted(archive.toc):
        if name != "ollama_chat_app" and not name.startswith("ollama_chat_app."):
            continue
        module_path = root.joinpath(*name.split("."))
        source = (module_path / "__init__.py" if module_path.is_dir()
                  else module_path.with_suffix(".py"))
        try:
            stored = archive.extract(name)
            if not isinstance(stored, CodeType) or not source.is_file():
                raise ValueError
            current = compile(source.read_bytes(), stored.co_filename, "exec", dont_inherit=True,
                              optimize=0)
        except Exception:
            mismatches.append(name)
            continue
        checked.append(name)
        if normalized(stored) != normalized(current):
            mismatches.append(name)
    try:
        if _sha256(path) != digest_before:
            report["errors"].append("artifact_changed_during_verification")
    except OSError:
        report["errors"].append("artifact_changed_during_verification")
    passed = bool(checked) and not missing and not mismatches and not report["errors"]
    report.update(status="passed" if passed else "failed", kind=kind, archive_name=archive_name,
                  artifact_sha256=digest_before, module_count=len(checked),
                  checked_modules=checked, required_cloud_modules=sorted(REQUIRED_CLOUD_MODULES),
                  missing_modules=missing, mismatches=mismatches)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Final .exe (preferred), or development .pyz")
    args = parser.parse_args(argv)
    result = verify(args.artifact)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
