"""ANIM-013 CLI fixtures: archive-create / archive-restore / archive-status
on the local backend (real local files, no OAuth, no network).
"""
import json

import pytest

from anim_013_kit import make_png

from engine import cli


def _png_dir(path, count=3):
    path.mkdir()
    for i in range(count):
        (path / f"frame_{i:03d}").with_suffix(".png").write_bytes(
            make_png(i, 16, 16))
    return path


def test_archive_create_restore_status_roundtrip(tmp_path, capsys):
    source = _png_dir(tmp_path / "frames")
    root = tmp_path / "archive-root"
    assert cli.main(["archive-create", str(source),
                     "--root", str(root)]) == 0
    created = json.loads(capsys.readouterr().out)
    manifest = created["manifest"]
    assert created["achieved_level"] == "UPLOAD_HASH_MATCHED"
    facets = created["qualification"]
    # Local/fake evidence never promotes real connectivity or release.
    assert facets == {"node_state": "IN_PROGRESS",
                      "qualification_state": "UNQUALIFIED",
                      "acceptance_state": "PENDING",
                      "release_state": "NOT_AUTHORIZED"}

    assert cli.main(["archive-status", manifest]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["sealed"] is True
    assert all(o["present"] for o in status["objects"])

    dest = tmp_path / "restore-out"
    assert cli.main(["archive-restore", manifest, str(dest)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["restored"] == ["F_000001.png", "F_000002.png",
                                    "F_000003.png"]
    for i, name in enumerate(restored["restored"]):
        assert (dest / name).read_bytes() == make_png(i, 16, 16)


def test_archive_create_rejects_empty_source(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["archive-create", str(empty),
                     "--root", str(tmp_path / "root")]) == 1
    assert "no PNG" in capsys.readouterr().err


def test_archive_restore_refuses_to_overwrite(tmp_path, capsys):
    source = _png_dir(tmp_path / "frames")
    root = tmp_path / "archive-root"
    cli.main(["archive-create", str(source), "--root", str(root)])
    manifest = json.loads(capsys.readouterr().out)["manifest"]
    dest = tmp_path / "restore-out"
    assert cli.main(["archive-restore", manifest, str(dest)]) == 0
    capsys.readouterr()
    # Second restore into the same destination is blocked, not merged.
    assert cli.main(["archive-restore", manifest, str(dest)]) == 1
    assert "OVERWRITE" in capsys.readouterr().err
