"""Check that the frozen application's own bytecode matches the tested sources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import CodeType

from PyInstaller.archive.readers import ZlibArchiveReader


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pyz", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "src"
    archive = ZlibArchiveReader(str(args.pyz.resolve()))
    checked = []
    mismatches = []
    for name in sorted(archive.toc):
        if name != "ollama_chat_app" and not name.startswith("ollama_chat_app."):
            continue
        path = root.joinpath(*name.split("."))
        source = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        stored = archive.extract(name)
        if not isinstance(stored, CodeType) or not source.is_file():
            mismatches.append(name)
            continue
        current = compile(source.read_bytes(), stored.co_filename, "exec", dont_inherit=True)
        checked.append(name)
        if normalized(stored) != normalized(current):
            mismatches.append(name)
    passed = bool(checked) and not mismatches
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "module_count": len(checked),
                "mismatches": mismatches,
            },
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
