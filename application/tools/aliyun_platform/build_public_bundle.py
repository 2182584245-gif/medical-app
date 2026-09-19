"""Validate or create a frozen public-source bundle; never deploy or read live data.

Run from any directory with --output-dir D:\\a-new-directory. Validation is the
default; --create is required to write three new artifacts. Source selection is
literal-only: two reviewed Docker lists plus explicit administrator additions.
Missing local imports are reported, never recursively included or executed.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

SOURCE_ROOT = Path(__file__).absolute().parents[2]
OUTPUT_DRIVE = "D:"
STEM = "source-v170"
ALLOWLISTS = (
    "tools/aliyun_platform/Dockerfile.dockerignore",
    "tools/aliyun_platform/Transfer.Dockerfile.dockerignore",
)
EXTRA = (
    *ALLOWLISTS,
    "tools/aliyun_platform/Dockerfile",
    "tools/aliyun_platform/Transfer.Dockerfile",
    "tools/aliyun_platform/compose.yaml",
    "tools/aliyun_platform/prepare.py",
    "tools/aliyun_platform/configure.py",
    "tools/aliyun_platform/maintenance.py",
    "tools/aliyun_platform/upgrade_developer.py",
    "tools/aliyun_platform/upgrade_medical.py",
    "tools/aliyun_platform/handoff_default_ai.py",
    "tools/aliyun_platform/receive_default_ai.py",
    "tools/aliyun_platform/handoff_transfer_key.py",
    "tools/aliyun_platform/receive_transfer_key.py",
    "tools/aliyun_platform/handoff_runtime.py",
    "tools/aliyun_platform/receive_runtime.py",
    "tools/aliyun_platform/pg_hba.conf",
    "tools/aliyun_platform/medical-app-backup.service",
    "tools/aliyun_platform/medical-app-backup.timer",
    "tools/aliyun_platform/medical-app-capacity.service",
    "tools/aliyun_platform/medical-app-capacity.timer",
    "tools/aliyun_platform/README.md",
    "tools/aliyun_platform/TRANSFER.md",
    "tools/aliyun_platform/build_public_bundle.py",
    "server/developer_admin.py",
    "server/DEVELOPER_V4_UPGRADE.md",
    "server/DEFAULT_AI_AND_REMINDERS.md",
    "src/ollama_chat_app/data/member_schema.py",
    "src/ollama_chat_app/providers/deepseek_cloud.py",
    "src/ollama_chat_app/providers/local_ollama.py",
    "src/ollama_chat_app/providers/ollama_cloud.py",
    "src/ollama_chat_app/security/secret_store.py",
    "src/ollama_chat_app/security/windows_dpapi.py",
    "src/ollama_chat_app/workers/__init__.py",
    "src/ollama_chat_app/workers/cloud_bridge.py",
    "src/ollama_chat_app/workers/task.py",
    "src/ollama_chat_app/workers/gui_gc.py",
)
# Separately reviewed pure compatibility source, not executable deployment steps.
# Required by existing Windows transfer/DPAPI branches; importing these modules
# does not read profiles. Do NOT use their old management/migration entrypoints
# on a v4 production database. No profiles, project JSON, Alembic env or keys ship.
COMPATIBILITY_SOURCES = {
    "server/cloud_connection.py": "Windows transfer CA/validated management URI helpers only",
    "server/local_secret_store.py": "Windows current-user DPAPI helper dependency only",
    "server/platform_admin.py": "Windows transfer management_url helper; not v4 migration entry",
    "server/platform_backup.py": "Legacy transfer-package Windows DPAPI _protect helper only",
    "server/migrations/__init__.py": "Package initializer for imported ownership guards only",
    "server/migrations/guards.py": "Pure ownership/marker definitions; no migration execution",
}
FORBIDDEN_PARTS = frozenset({
    ".git", ".env", ".local", "outputs", "tests", "__pycache__", "credentials",
    "secrets", "backups", "models", "assets", "build", "dist", "node_modules",
})
PUBLIC_SUFFIXES = frozenset({
    ".py", ".mako", ".ini", ".txt", ".md", ".yaml", ".conf", ".service", ".timer",
    ".dockerignore", ".dockerfile",
})
SECRET_PATTERNS = (
    rb"sk-[A-Za-z0-9_-]{16,}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    rb"(?:postgres(?:ql)?|mysql)(?:\+[a-z]+)?://[^\s/:]+:[^\s/@]+@",
    rb"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}",
    rb"(?:sb_secret_|AKIA|ASIA)[A-Za-z0-9_-]{16,}",
)
MAX_FILE_BYTES = 4 * 1024**2
MAX_SOURCE_BYTES = 20 * 1024**2


class BundleError(ValueError):
    """Only reviewed relative filenames/fixed text; never source/secret contents."""


def _no_links(path: Path, *, must_exist=True) -> None:
    for item in (path, *path.parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            if must_exist:
                raise BundleError("Required path is missing") from None
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise BundleError("Links and reparse points are forbidden")


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or str(path) != name or "\\" in name
            or ":" in name or any(ord(c) < 32 for c in name)
            or any(symbol in name for symbol in "*?[]!#")
            or any(part.startswith(".") for part in path.parts)
            or set(part.casefold() for part in path.parts) & FORBIDDEN_PARTS):
        raise BundleError("Unreviewed source filename")
    return path


def safe_source(root: Path, name: str) -> Path:
    relative = _relative(name)
    if (("data" in relative.parts and not name.startswith("src/ollama_chat_app/data/"))
            or (relative.suffix.casefold() not in PUBLIC_SUFFIXES
                and relative.name != "Dockerfile")):
        raise BundleError("Unreviewed source file type: " + name)
    candidate = root.joinpath(*relative.parts)
    _no_links(candidate)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise BundleError("Source escapes repository: " + name)
    metadata = candidate.stat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_size > MAX_FILE_BYTES):
        raise BundleError("Invalid public source file: " + name)
    return candidate


def read_public(root: Path, name: str) -> bytes:
    path = safe_source(root, name)
    before = path.stat()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise BundleError("Source changed while opening: " + name)
        raw = stream.read(MAX_FILE_BYTES + 1)
    after = safe_source(root, name).stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise BundleError("Source changed while reading: " + name)
    if len(raw) > MAX_FILE_BYTES or b"\0" in raw:
        raise BundleError("Binary or oversized source: " + name)
    try:
        raw.decode("utf-8-sig")
    except UnicodeError:
        raise BundleError("Non-UTF8 public source: " + name) from None
    if any(re.search(pattern, raw, re.I) for pattern in SECRET_PATTERNS):
        raise BundleError("Potential secret; contents withheld: " + name)
    return raw


def literal_allowlist(root: Path) -> tuple[list[str], dict[str, list[str]], dict[str, bytes]]:
    selected = set(EXTRA) | set(COMPATIBILITY_SOURCES)
    contexts = {}
    policies = {}
    for allowlist in ALLOWLISTS:
        policies[allowlist] = read_public(root, allowlist)
        rules = [line.strip() for line in policies[allowlist].decode("utf-8-sig")
                 .splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not rules or rules[0] != "**":
            raise BundleError("Docker list must begin with deny-all: " + allowlist)
        permitted = set()
        for line in rules[1:]:
            if not line.startswith("!"):
                # A deny rule cannot grant access. Only understood literal-tree
                # deny forms are accepted, so future policy changes get reviewed.
                _relative(line.removesuffix("/**"))
                continue
            name = line[1:]
            if name.endswith("/"):
                _relative(name[:-1])
                continue  # parent traversal permission, never recursive selection
            _relative(name)
            permitted.add(name)
        contexts[allowlist] = sorted(permitted)
        selected.update(permitted)
    for name in selected:
        safe_source(root, name)
    return sorted(selected), contexts, policies


def _module_files(root: Path, module: str) -> set[str]:
    parts = module.split(".")
    if not parts or parts[0] not in {"server", "tools", "ollama_chat_app"}:
        return set()
    if any(not part.isidentifier() for part in parts):
        return set()
    prefix = ["src"] if parts[0] == "ollama_chat_app" else []
    stem = "/".join(prefix + parts)
    result = {name for name in (stem + ".py", stem + "/__init__.py")
              if (root / name).is_file()}
    if result:
        for length in range(1, len(parts)):
            init = "/".join(prefix + parts[:length]) + "/__init__.py"
            if (root / init).is_file():
                result.add(init)
    return result


def dependency_check(root: Path, blobs: dict[str, bytes]) -> list[dict[str, str]]:
    missing = set()
    for name, raw in blobs.items():
        if not name.endswith(".py"):
            continue
        parts = list(PurePosixPath(name).with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        package = parts[:-1]
        try:
            tree = ast.parse(raw.decode("utf-8-sig"), filename=name)
        except SyntaxError:
            raise BundleError("Python syntax is invalid: " + name) from None
        for node in ast.walk(tree):
            modules = []
            required_modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                required_modules = modules
            elif isinstance(node, ast.ImportFrom):
                if node.level > len(package):
                    raise BundleError("Invalid relative local import: " + name)
                head = package[:len(package) - node.level + 1] if node.level else []
                base = ".".join(head + (node.module.split(".") if node.module else []))
                modules = [base] + [base + "." + alias.name for alias in node.names]
                required_modules = [base]
            elif (isinstance(node, ast.Call) and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)
                  and ((isinstance(node.func, ast.Name) and node.func.id == "__import__")
                       or (isinstance(node.func, ast.Attribute)
                           and node.func.attr == "import_module"))):
                modules = [node.args[0].value]
                required_modules = modules
            for module in required_modules:
                parts = module.split(".")
                if parts[0] in {"server", "tools", "ollama_chat_app"}:
                    prefix = "src/" if parts[0] == "ollama_chat_app" else ""
                    stem = prefix + "/".join(parts)
                    if not (root / (stem + ".py")).is_file() and not (root / stem).is_dir():
                        missing.add((name, stem + ".py"))
            for module in modules:
                for dependency in _module_files(root, module):
                    if dependency not in blobs:
                        missing.add((name, dependency))
    return [{"source": name, "required": dependency} for name, dependency in sorted(missing)]


def validate_destination(output: Path, root: Path) -> Path:
    if (not output.is_absolute() or output.drive.casefold() != OUTPUT_DRIVE.casefold()
            or len(output.parts) < 2 or ".." in output.parts):
        raise BundleError("Output must be a new absolute external D-drive directory")
    _no_links(output, must_exist=False)
    if output.exists() or not output.parent.is_dir():
        raise BundleError("Output must not exist and its parent must already be a directory")
    resolved = output.resolve()
    if resolved.is_relative_to(root.resolve()) or root.resolve().is_relative_to(resolved):
        raise BundleError("Output must be outside the source repository")
    return output


def _archive(blobs: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(filename="", fileobj=buffer, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive,
    ):
        for name in sorted(blobs):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(blobs[name]), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(blobs[name]))
    return buffer.getvalue()


def verify_archive(raw: bytes, blobs: dict[str, bytes]) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = archive.getmembers()
        if ([member.name for member in members] != sorted(blobs)
                or any(not member.isfile() or member.linkname or member.mode != 0o644
                       or member.uid or member.gid or member.mtime for member in members)):
            raise BundleError("Archive membership or metadata differs")
        for member in members:
            with archive.extractfile(member) as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            if content != blobs[member.name]:
                raise BundleError("Archive readback differs: " + member.name)


def _freeze(root: Path, blobs: dict[str, bytes]) -> None:
    if any(read_public(root, name) != raw for name, raw in blobs.items()):
        raise BundleError("Source changed during bundle preparation; start again")


def build_bundle(root: Path, output: Path, *, create=False, expected_source_sha256=None) -> dict:
    root = root.absolute()
    _no_links(root)
    if not root.is_dir():
        raise BundleError("Source root must be a directory")
    output = validate_destination(output, root)
    names, contexts, policies = literal_allowlist(root)
    blobs = {
        name: policies[name] if name in policies else read_public(root, name) for name in names
    }
    missing = dependency_check(root, blobs)
    if missing:
        return {"status": "missing_explicit_dependencies", "missing": missing,
                "created": False}
    rows = [{"path": name, "bytes": len(blobs[name]),
             "sha256": hashlib.sha256(blobs[name]).hexdigest()} for name in names]
    total = sum(row["bytes"] for row in rows)
    if total > MAX_SOURCE_BYTES:
        raise BundleError("Unexpected public-source size")
    source_sha = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    if expected_source_sha256 is not None and (
        not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256)
        or expected_source_sha256 != source_sha
    ):
        raise BundleError("Source no longer matches the explicitly reviewed SHA256")
    raw = _archive(blobs)
    verify_archive(raw, blobs)
    _freeze(root, blobs)
    archive_name = STEM + ".tar.gz"
    manifest = {
        "format_version": 1, "application_version": "1.7.0", "archive": archive_name,
        "archive_sha256": hashlib.sha256(raw).hexdigest(), "source_sha256": source_sha,
        "archive_bytes": len(raw), "source_bytes": total, "source_file_count": len(rows),
        "docker_allowlist_files": contexts, "explicit_additions": sorted(set(EXTRA)),
        "reviewed_compatibility_sources": COMPATIBILITY_SOURCES,
        "dependency_check": "all_resolvable_static_local_imports_present",
        "secret_scan": "bounded_source_only_no_matches",
        "source_freeze": "source_bytes_rechecked_before_and_after_disk_write",
        "files": rows,
    }
    result = {key: manifest[key] for key in (
        "archive", "source_file_count", "source_bytes", "archive_bytes",
        "archive_sha256", "source_sha256", "dependency_check")}
    if not create:
        return {"status": "validated_only", "created": False, **result}
    # Repeat the destination gate immediately before exclusive creation. A
    # concurrent creator is never overwritten, merged, removed or followed.
    validate_destination(output, root)
    output.mkdir(mode=0o700)
    manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    outputs = {
        archive_name: raw,
        STEM + "-manifest.json": manifest_raw,
        STEM + ".sha256": (manifest["archive_sha256"] + "  " + archive_name + "\n"
                           + hashlib.sha256(manifest_raw).hexdigest() + "  "
                           + STEM + "-manifest.json\n").encode(),
    }
    for name, content in outputs.items():
        with (output / (name + ".pending")).open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    for name, content in outputs.items():
        pending = output / (name + ".pending")
        _no_links(pending)
        if pending.read_bytes() != content:
            raise BundleError("Written artifact readback differs")
    verify_archive((output / (archive_name + ".pending")).read_bytes(), blobs)
    _freeze(root, blobs)
    # The manifest is the final completion marker. Failed freeze/readback keeps
    # only explicitly pending files; never mistake those for a deployable set.
    for name in (archive_name, STEM + ".sha256", STEM + "-manifest.json"):
        target = output / name
        pending = output / (name + ".pending")
        # link() is atomic and refuses an existing destination on Windows and
        # POSIX; rename() could overwrite a concurrent destination on POSIX.
        os.link(pending, target)
        pending.unlink()
    return {"status": "created_and_verified", "created": True, **result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--expected-source-sha256")
    args = parser.parse_args(argv)
    try:
        result = build_bundle(SOURCE_ROOT, args.output_dir, create=args.create,
                              expected_source_sha256=args.expected_source_sha256)
    except (BundleError, OSError, tarfile.TarError) as error:
        message = (str(error) if isinstance(error, BundleError)
                   else "Filesystem/archive check failed; do not deploy incomplete artifacts")
        print(json.dumps({"status": "blocked", "message": message}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "missing_explicit_dependencies" else 0


if __name__ == "__main__":
    raise SystemExit(main())
