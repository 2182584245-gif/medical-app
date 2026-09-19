"""Private input preparation checks; never contact or modify a cloud database."""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from server.platform_transfer import deserialize, digest
from tools import prepare_synthetic_upgrade_v170 as prep


def test_private_new_directory_rejects_existing_without_touching(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prep._secure_new_directory(target)
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_private_new_directory_access_is_verified(tmp_path):
    target = tmp_path / "new-private"
    expected = "current-user-and-system-only-windows" if os.name == "nt" else "owner-only-posix"
    assert prep._secure_new_directory(target) == expected
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("value", ["relative", "../parent", r"\\server\share\folder"])
def test_explicit_local_paths_only(value):
    with pytest.raises(prep.PreparationError):
        prep._path(value)


def test_private_destination_must_be_outside_git(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(prep.PreparationError):
        prep._outside_git(tmp_path / "private-new")


def test_source_hardlink_rejected(tmp_path):
    source, linked = tmp_path / "file", tmp_path / "hard-link"
    source.write_bytes(b"fictional")
    try:
        os.link(source, linked)
    except OSError:
        pytest.skip("Filesystem cannot create hardlinks.")
    with pytest.raises(prep.PreparationError):
        prep._path(linked)


def test_recomputed_arbitrary_history_is_not_accepted():
    arbitrary = {"mode": "append-only", "status": "review_required", "choices": {"renames": {}}}
    arbitrary["plan_sha256"] = digest(arbitrary)
    with pytest.raises(prep.PreparationError, match="reviewed actual"):
        prep._check_payload({}, {}, arbitrary)


def test_real_reviewed_fixtures_are_readonly_and_roundtrip(tmp_path):
    names = ("V170_PLAN_BASELINE", "V170_PLAN_SOURCE", "V170_PLAN_HISTORY")
    if not all(os.environ.get(name) for name in names):
        pytest.skip("Supply all three explicit strictly reviewed fixture paths for integration.")
    baseline, source, history = (Path(os.environ[name]) for name in names)
    files = [baseline / "data/app.db", source / "data/app.db", history]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    destination = tmp_path / "reviewed-private-inputs"
    result = prep.prepare_inputs(baseline, source, history, destination)
    assert result["baseline_rebind_verified"] is True
    assert result["contains_plaintext_credentials"] is False
    assert result["contains_cloud_target_snapshot"] is False
    assert result["source_counts"]["life_records"] == 4632
    assert result["source_counts"]["users"] == 5
    assert result["baseline_counts"]["life_records"] == 156
    assert set(item.name for item in destination.iterdir()) == set(prep.FILENAMES)
    old = deserialize((destination / "baseline.json").read_bytes())
    new = deserialize((destination / "source.json").read_bytes())
    previous = deserialize((destination / "history.json").read_bytes())
    assert digest(old) == prep.BASELINE_SHA
    assert digest(new) == result["source_snapshot_sha256"]
    assert new["assets"] and old["assets"]
    assert all(isinstance(item["content"], bytes) for item in new["assets"].values())
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files} == before
    assert json.loads((destination / "manifest.json").read_bytes())["cloud_modified"] is False
    changed = copy.deepcopy(new)
    changed["rows"]["users"][0]["username"] = "different-person"
    with pytest.raises(prep.PreparationError, match="reviewed expanded"):
        prep._check_payload(old, changed, previous)
    with pytest.raises(prep.PreparationError, match="new, extension-free"):
        prep.prepare_inputs(baseline, source, history, destination)
