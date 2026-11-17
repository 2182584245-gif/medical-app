"""Prepare a static APK release; default dry-run. No SSH, Docker, API or DB changes.

Run on the Linux server with the verified static update route and mount.
Explicit --apply creates an immutable versioned APK, verifies it over strict
HTTPS, then atomically replaces latest.json. All state/backups stay private.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import tempfile
import urllib.request
from urllib.parse import urlsplit

APPLICATION_ID = "com.fawnsee.mobile"
APK_NAME = re.compile(r"FAWNSEE-MOBILE-[1-9][0-9]*\.apk\Z")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_real_path(value, directory=False):
    absolute = Path(os.path.abspath(value))
    if absolute.resolve(strict=True) != absolute:
        raise ValueError("Symlink/alias paths are not permitted: " + str(absolute))
    if directory and not absolute.is_dir():
        raise ValueError("Expected an existing directory")
    if not directory and not absolute.is_file():
        raise ValueError("Expected a regular existing file")
    return absolute


def require(condition, message):
    if not condition:
        raise ValueError(message)


def update_base(value):
    """Require an explicit HTTPS directory without user information or query fields."""
    try:
        parsed = urlsplit(value)
        valid = (
            value.startswith("https://")
            and not re.search(r"[\s\\]", value)
            and parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and "?" not in value
            and "#" not in value
            and parsed.path.endswith("/")
            and "//" not in parsed.path
            and all(part not in (".", "..") for part in parsed.path.split("/"))
            and "%" not in parsed.path
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("Expected an HTTPS directory URL ending with /")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        raise ValueError("Release verification refuses redirects")


def download_verified(url, size, expected_sha, immutable=False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity", "User-Agent": "FAWNSEE-release-verifier/1"})
    with opener.open(request, timeout=60) as response:
        require(response.status == 200 and response.geturl() == url, "Unexpected release response")
        require(response.headers.get("Content-Encoding", "identity").lower() == "identity", "Native updater requires an identity-encoded response")
        cache = response.headers.get("Cache-Control", "").lower()
        if immutable:
            require("immutable" in cache and re.search(r"max-age\s*=\s*31536000(?:\D|$)", cache), "APK immutable cache headers missing")
        else:
            require("no-store" in cache, "latest.json must be no-store")
        digest, count = hashlib.sha256(), 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            require(count <= size, "HTTPS body exceeds expected size")
            digest.update(chunk)
    require(count == size and digest.hexdigest() == expected_sha, "Strict HTTPS size/hash readback failed")
    return {"url": url, "bytes": count, "sha256": digest.hexdigest(), "cache_control": cache, "tls_verified": True, "redirects": False}


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_temp(state, data=None, copy_from=None):
    descriptor, name = tempfile.mkstemp(prefix="publish-", dir=state)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            if copy_from:
                with copy_from.open("rb") as source:
                    shutil.copyfileobj(source, target, 1024 * 1024)
            else:
                target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(path, 0o444)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", required=True, help="Verified dedicated Caddy public directory, already existing")
    parser.add_argument("--state-dir", required=True, help="Existing private directory on the SAME filesystem, outside public root")
    parser.add_argument("--staged-apk", required=True)
    parser.add_argument("--audit-json", required=True, help="Independent final APK audit copied to private staging")
    parser.add_argument("--base-url", required=True, type=update_base, help="Exact HTTPS update directory used by the Android client")
    parser.add_argument("--version-code", required=True, type=int)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--release-notes", required=True, help="UTF-8 JSON array of short release-note strings")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    release = exact_real_path(args.release_root, directory=True)
    state = exact_real_path(args.state_dir, directory=True)
    apk = exact_real_path(args.staged_apk)
    audit_path = exact_real_path(args.audit_json)
    notes_path = exact_real_path(args.release_notes)
    for private in (state, apk, audit_path, notes_path):
        require(not private.is_relative_to(release), "Private staging/state cannot be inside public root")
    require(not release.is_relative_to(state), "State directory cannot contain public root")
    if os.name != "nt":
        require(state.stat().st_mode & 0o077 == 0, "State directory must be private to its owner (mode 700)")
    require(release.stat().st_dev == state.stat().st_dev, "Atomic staging requires state and public root on the same filesystem")
    require(1 <= args.version_code <= 2147483647, "Native version_code must fit a positive signed Android integer")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", args.version_name), "Invalid Android version name")
    for entry in release.iterdir():
        require(entry.is_file() and not entry.is_symlink() and (entry.name == "latest.json" or APK_NAME.fullmatch(entry.name)), "Public root contains a directory, link or non-release file; stop")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    expected_identity = {"application_id": APPLICATION_ID, "version_code": args.version_code, "version_name": args.version_name}
    require(audit.get("passed") is True and audit.get("checks") and all(value is True for value in audit["checks"].values()), "Independent APK audit must fully pass")
    require(audit.get("apk", {}).get("identity") == expected_identity, "Audit identity differs from requested release")
    size, apk_sha = apk.stat().st_size, sha(apk)
    require(1 <= size <= 300 * 1024 * 1024, "Native APK size limit is 300 MiB")
    require(size == audit["apk"].get("bytes") and apk_sha == audit["apk"].get("sha256"), "Staged APK differs from independently audited bytes")
    min_sdk = audit["apk"].get("min_sdk")
    if min_sdk is None:
        badging = audit.get("native_tool_results", {}).get("new_badging", {}).get("stdout", "")
        matches = re.findall(r"^sdkVersion:'([1-9][0-9]*)'", badging, re.M)
        min_sdk = int(matches[0]) if len(matches) == 1 else None
    require(type(min_sdk) is int and 1 <= min_sdk <= 1000, "Independent audit must identify the APK's actual minimum SDK")
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    require(isinstance(notes, list) and 1 <= len(notes) <= 12 and all(isinstance(n, str) and 1 <= len(n) <= 500 and n == n.strip() and "\0" not in n for n in notes), "Expected 1..12 bounded, trimmed release-note strings without NUL")
    name = f"FAWNSEE-MOBILE-{args.version_code}.apk"
    apk_url = args.base_url + name
    require(len(apk_url) <= 300, "APK URL exceeds the Android manifest limit")
    target, latest = release / name, release / "latest.json"
    previous = latest.read_bytes() if latest.exists() else None
    if previous:
        prior = json.loads(previous)
        require(prior.get("schema_version") == 1 and prior.get("application_id") == APPLICATION_ID and type(prior.get("version_code")) is int, "Existing latest.json has unknown schema/identity")
        require(prior["version_code"] <= args.version_code, "Refusing to downgrade latest.json")
    else:
        prior = None
    release_notes = "\n".join(notes)
    require(len(release_notes.encode("utf-16-le")) // 2 <= 8000, "Native release_notes contract is a bounded plain-text string")
    manifest = {"schema_version": 1, **expected_identity, "min_sdk": min_sdk, "size_bytes": size, "sha256": apk_sha, "apk_url": apk_url,
                "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "release_notes": release_notes}
    if prior and prior["version_code"] == args.version_code:
        require(all(prior.get(k) == value for k, value in manifest.items() if k != "published_at"), "Existing same-version manifest differs; immutable release cannot be republished with different metadata")
        manifest = prior
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    require(len(raw) <= 32 * 1024, "Native manifest limit is 32 KiB")
    if target.exists():
        require(target.stat().st_size == size and sha(target) == apk_sha, "Versioned APK already exists with different bytes; never overwrite")
    plan = {"mode": "apply" if args.apply else "dry-run", "public_root": str(release), "immutable_apk": str(target), "latest": str(latest), "manifest": manifest,
            "sequence": ["create immutable APK if absent", "strict HTTPS download and exact size/hash + cache verification", "atomic latest.json replacement", "strict HTTPS latest readback"], "no_api_db_or_caddy_changes": True}
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    lock = state / ("publish-" + hashlib.sha256(str(release).encode()).hexdigest()[:20] + ".lock")
    lock.mkdir(mode=0o700)
    changed_latest, temp = False, None
    try:
        require((latest.read_bytes() if latest.exists() else None) == previous, "latest.json changed after preflight; rerun without overwriting another publisher")
        if previous is not None:
            backup = state / ("latest-before-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
            with backup.open("xb") as output:
                output.write(previous)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(backup, 0o600)
        if not target.exists():
            temp = private_temp(state, copy_from=apk)
            require(temp.stat().st_size == size and sha(temp) == apk_sha, "Staging copy changed")
            try:
                os.link(temp, target)
            except FileExistsError:
                require(target.stat().st_size == size and sha(target) == apk_sha, "Concurrent immutable APK differs")
            temp.unlink()
            temp = None
            fsync_directory(release)
        apk_verification = download_verified(apk_url, size, apk_sha, immutable=True)
        if previous != raw:
            temp = private_temp(state, data=raw)
            os.replace(temp, latest)
            temp = None
            fsync_directory(release)
            changed_latest = True
        latest_verification = download_verified(args.base_url + "latest.json", len(raw), hashlib.sha256(raw).hexdigest())
        result = {**plan, "passed": True, "apk_https": apk_verification, "latest_https": latest_verification}
        (state / f"publication-{args.version_code}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "version_code": args.version_code, "sha256": apk_sha, "latest_url": args.base_url + "latest.json"}, ensure_ascii=False))
    except BaseException:
        if changed_latest:
            require(latest.is_file() and not latest.is_symlink() and latest.read_bytes() == raw,
                    "Another publisher changed latest.json; refusing to overwrite its result during rollback")
            if previous is None:
                latest.unlink(missing_ok=True)
            else:
                temp = private_temp(state, data=previous)
                os.replace(temp, latest)
                temp = None
            fsync_directory(release)
        raise
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        lock.rmdir()


if __name__ == "__main__":
    main()
