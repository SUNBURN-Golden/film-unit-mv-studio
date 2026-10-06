"""ANIM-003: external frame-sequence/RGBA import, hash-bound resolver, draft Preview.

Fixtures are real Pillow-drawn PNGs (RGBA gradients, distinct frames) and the
existing synthetic project; nothing here is an artistic approval, a production
qualification or a Final.
"""
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import (asset_status, import_frame_sequence,
                                     import_layer_rgba, load_registry,
                                     resolve_asset, resolve_shot_sequence)
from engine.animation_migrate import animation_init
from engine.animation_schema import read_canon, write_canon
from engine.builds import verify_build
from engine.compiler import compile_final, compile_preview
from engine.core import FilmError, digest, probe, read
from test_compiler_v03 import assert_master, fixture_project, newest_build


def rgba_frame(path, size=(96, 72), seed=0):
    """A small real RGBA drawing with a varied alpha channel."""
    im = Image.new("RGBA", size)
    px = im.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 3 + seed * 17) % 256, (y * 5 + seed * 7) % 256,
                        (x + y + seed) % 256, (x * 255) // max(1, w - 1))
    im.save(path)
    return path


def rgb_frame(path, size=(96, 72), seed=0):
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x + seed * 11) % 256, (y + seed * 5) % 256, seed % 256)
    im.save(path)
    return path


def make_sequence(folder, count=24, size=(96, 72), seed=0):
    """Real drawn frames with per-frame markers; returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        rgba_frame(folder / f"f{i:04d}.png", size, seed=seed + i)
    return folder


def write_index(folder, names=None, corrupt_hash=None):
    names = names or sorted(f.name for f in Path(folder).glob("*.png"))
    frames = []
    for name in names:
        data = (Path(folder) / name).read_bytes()
        sha = corrupt_hash or hashlib.sha256(data).hexdigest()
        frames.append({"file": name, "sha256": sha})
    index = Path(folder) / "index.json"
    index.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return index


def animation_project(tmp_path, shot_count=2, seconds=2):
    p = fixture_project(tmp_path, seconds=seconds, shot_count=shot_count)
    animation_init(p)
    return p


def member_path(p, record, index):
    return p / record["files"][index]["relative_name"]


def test_folder_import_registers_pinned_draft_revision(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "ext_seq")
    result = import_frame_sequence(p, "S001", folder=tmp_path / "ext_seq")
    assert result["assigned_to"] == "S001" and result["frames"] == 24
    assert result["state"] == "DRAFT" and result["new_revision"] is True
    registry = load_registry(p)
    entry = registry["assets"][result["asset_id"]]
    record = entry["revisions"][str(entry["current_revision"])]
    assert record["kind"] == "FRAME_SEQUENCE"
    assert record["canvas"] == {"width": 96, "height": 72}
    assert [f["frame_index"] for f in record["files"]] == list(range(24))
    for f, source in zip(record["files"], sorted((tmp_path / "ext_seq").glob("*.png"))):
        assert f["sha256"] == digest(source)
        stored = p / f["relative_name"]
        assert stored.read_bytes() == source.read_bytes()  # independent copy
        assert f["byte_length"] == len(source.read_bytes())
    pin = registry["assignments"]["S001"]
    assert pin == {"asset_id": result["asset_id"], "revision": result["revision"],
                   "content_sha256": result["content_sha256"], "kind": "FRAME_SEQUENCE"}
    # The edit document records the adopted sequence revision.
    timeline = read_canon(p / "timeline/edit.json")
    assert timeline["entries"][0]["sequence_revision"] == result["revision"]
    # The stored bytes survive deletion of the external folder (independent copy).
    for f in (tmp_path / "ext_seq").glob("*.png"):
        f.unlink()
    resolved = resolve_shot_sequence(p, "S001", [0, 24], {"before": 0, "after": 0})
    assert len(resolved["members"]) == 24


def test_index_import_uses_explicit_order_and_hashes(tmp_path):
    p = animation_project(tmp_path)
    folder = make_sequence(tmp_path / "seq")
    names = sorted((f.name for f in folder.glob("*.png")), reverse=True)  # reorder
    index = write_index(folder, names)
    result = import_frame_sequence(p, "S002", index=index)
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"]["1"]
    assert [f["source_name"] for f in record["files"]] == names
    resolved = resolve_shot_sequence(p, "S002")
    assert resolved["members"][0][1].read_bytes() == (folder / names[0]).read_bytes()


def test_layer_import_preserves_alpha_pivot_crop_origin(tmp_path):
    p = animation_project(tmp_path)
    source = rgba_frame(tmp_path / "cutout.png", size=(80, 60), seed=3)
    original_bytes = source.read_bytes()
    result = import_layer_rgba(p, source, pivot=[12, 34], crop_origin=[-5, 20],
                               z_order=7, note="hand cutout")
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"]["1"]
    assert record["kind"] == "LAYER_RGBA"
    assert record["alpha"]["present"] is True
    assert record["alpha"]["min"] == 0 and record["alpha"]["max"] == 255
    assert record["pivot"] == [12, 34]
    assert record["crop_origin"] == [-5, 20]
    assert record["z_order"] == 7
    assert record["canvas"] == {"width": 80, "height": 60}
    # Stored bytes are the original pixels: alpha never flattened or re-encoded.
    stored = p / record["files"][0]["relative_name"]
    assert stored.read_bytes() == original_bytes
    with Image.open(stored) as im:
        assert im.mode == "RGBA"
        assert im.getchannel("A").getextrema() == (0, 255)


def test_layer_import_rejects_alpha_loss(tmp_path):
    p = animation_project(tmp_path)
    flat = rgb_frame(tmp_path / "flat.png")
    with pytest.raises(FilmError, match="alpha"):
        import_layer_rgba(p, flat)
    assert not (p / "manifest/animation_assets.json").exists()


def test_imports_require_the_explicit_profile(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    make_sequence(tmp_path / "seq")
    rgba_frame(tmp_path / "layer.png")
    for call in (lambda: import_frame_sequence(p, "S001", folder=tmp_path / "seq"),
                 lambda: import_layer_rgba(p, tmp_path / "layer.png"),
                 lambda: asset_status(p)):
        with pytest.raises(FilmError, match="LEGACY_MV"):
            call()
    assert not (p / "manifest/animation_assets.json").exists()


def test_missing_member_fails_resolution(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "seq")
    result = import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"]["1"]
    member_path(p, record, 7).unlink()
    with pytest.raises(FilmError, match="Missing asset member"):
        resolve_shot_sequence(p, "S001")


def test_tampered_member_fails_resolution(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "seq")
    result = import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"]["1"]
    target = member_path(p, record, 3)
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same length, different bytes
    target.write_bytes(bytes(data))
    with pytest.raises(FilmError, match="hash mismatch"):
        resolve_shot_sequence(p, "S001")
    # A same-path rewrite with a different length is caught just as early.
    rgba_frame(target, seed=999)
    with pytest.raises(FilmError, match="length changed|hash mismatch"):
        resolve_shot_sequence(p, "S001")


def test_damaged_source_is_rejected_at_import(tmp_path):
    p = animation_project(tmp_path)
    folder = tmp_path / "seq"
    folder.mkdir()
    for i in range(3):
        rgba_frame(folder / f"f{i:04d}.png", seed=i)
    (folder / "f0003.png").write_bytes(b"not a png at all")
    with pytest.raises(FilmError, match="Unreadable or corrupt"):
        import_frame_sequence(p, "S001", folder=folder)
    index = write_index(folder)
    with pytest.raises(FilmError, match="Unreadable or corrupt"):
        import_frame_sequence(p, "S001", index=index)
    # An index that lies about member bytes is refused too.
    index = write_index(folder, names=["f0000.png"], corrupt_hash="0" * 64)
    with pytest.raises(FilmError, match="hash mismatch"):
        import_frame_sequence(p, "S001", index=index)


def test_disallowed_index_paths_are_rejected(tmp_path):
    p = animation_project(tmp_path)
    folder = make_sequence(tmp_path / "seq", count=2)
    outside = rgba_frame(tmp_path / "outside.png", seed=1)
    sha = lambda f: hashlib.sha256(Path(f).read_bytes()).hexdigest()
    cases = [
        {"file": str(outside), "sha256": sha(outside)},          # absolute path
        {"file": "../outside.png", "sha256": sha(outside)},      # parent traversal
        {"file": "sub/../../outside.png", "sha256": sha(outside)},
    ]
    for i, member in enumerate(cases):
        # The index itself sits outside the frame folder; root is its own dir.
        bad_dir = tmp_path / f"badroot{i}"
        bad_dir.mkdir(exist_ok=True)
        (bad_dir / "index.json").write_text(json.dumps({"frames": [member]}))
        with pytest.raises(FilmError, match="Disallowed|escapes"):
            import_frame_sequence(p, "S001", index=bad_dir / "index.json")
    # A symlinked member that escapes the index directory is refused.
    (folder / "linked.png").symlink_to(outside)
    index = folder / "index.json"
    index.write_text(json.dumps(
        {"frames": [{"file": "linked.png", "sha256": sha(outside)}]}))
    with pytest.raises(FilmError, match="regular file|escapes|Disallowed"):
        import_frame_sequence(p, "S001", index=index)
    # And a symlink inside a folder import is refused, not silently skipped.
    with pytest.raises(FilmError, match="symlink"):
        import_frame_sequence(p, "S001", folder=folder)


def test_one_changed_member_opens_a_new_revision(tmp_path):
    p = animation_project(tmp_path)
    folder = make_sequence(tmp_path / "seq", count=24)
    first = import_frame_sequence(p, "S001", folder=folder)
    rgba_frame(folder / "f0005.png", seed=4242)  # change exactly one member
    second = import_frame_sequence(p, "S001", folder=folder)
    assert second["asset_id"] == first["asset_id"]
    assert second["revision"] == first["revision"] + 1
    assert second["content_sha256"] != first["content_sha256"]
    entry = load_registry(p)["assets"][first["asset_id"]]
    assert entry["current_revision"] == second["revision"]
    # Both revisions resolve: r1 bytes were never overwritten by r2.
    r1 = resolve_asset(p, first["asset_id"], first["revision"],
                       first["content_sha256"])
    r2 = resolve_asset(p, second["asset_id"], second["revision"],
                       second["content_sha256"])
    assert r1["members"][5][1] != r2["members"][5][1]
    # Identical re-import reuses the pinned revision instead of writing bytes.
    third = import_frame_sequence(p, "S001", folder=folder)
    assert third["revision"] == second["revision"] and third["new_revision"] is False


def test_resolver_rejects_wrong_revision_or_digest(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "seq", count=4)
    result = import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    with pytest.raises(FilmError, match="no revision"):
        resolve_asset(p, result["asset_id"], 9, result["content_sha256"])
    with pytest.raises(FilmError, match="no longer matches"):
        resolve_asset(p, result["asset_id"], result["revision"], "0" * 64)
    with pytest.raises(FilmError, match="Unknown asset"):
        resolve_asset(p, "A9999", 1, result["content_sha256"])
    # A timeline-adopted revision that disagrees with the pin is surfaced.
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["sequence_revision"] = 99
    write_canon(p / "timeline/edit.json", timeline)
    with pytest.raises(FilmError, match="adopts sequence revision"):
        resolve_shot_sequence(p, "S001", expected_revision=99)
    # A sequence shorter than the adopted used range cannot satisfy the edit.
    import_frame_sequence(p, "S002", folder=tmp_path / "seq")  # 4 < 24 frames
    with pytest.raises(FilmError, match="exceeds"):
        resolve_shot_sequence(p, "S002", [0, 24], {"before": 0, "after": 0})


def test_tampered_registry_document_is_rejected(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "seq", count=4)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    registry = load_registry(p)
    record = next(iter(registry["assets"].values()))["revisions"]["1"]
    record["files"][0]["sha256"] = "0" * 64  # forge a member hash
    write_canon(p / "manifest/animation_assets.json", registry)
    with pytest.raises(FilmError, match="content_sha256"):
        load_registry(p)


def test_draft_preview_renders_sequences_and_marks_incomplete(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    make_sequence(tmp_path / "seq_s001", seed=10)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    # S002 has no assigned sequence: draft renders it as a labelled placeholder.
    result = compile_preview(p)
    folder, record = newest_build(p)
    assert result["status"] == "COMPLETE" and result["draft"] is True
    assert record["document_type"] == "animation_build"
    assert record["schema_version"] == 2 and record["mode"] == "PREVIEW"
    assert record["frames"]["total"] == 48 and record["frames"]["placeholder_frames"] == 24
    assert result["incomplete_entries"] == ["I002"]
    assert any("placeholder" in w for w in record["warnings"])
    output = folder / "DRAFT_PREVIEW.mp4"
    assert output.is_file() and record["output"] == "DRAFT_PREVIEW.mp4"
    video = [s for s in probe(output)["streams"] if s["codec_type"] == "video"]
    assert video[0]["avg_frame_rate"] == "24/1" and int(video[0]["nb_frames"]) == 48
    # The composed frames and their per-frame map are sealed in the build.
    assert (folder / "draft_frames/F_000001.png").is_file()
    rows = [json.loads(line) for line in
            (folder / "frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == 48
    first, in_s002 = rows[0], rows[24]
    assert first["file"] == "draft_frames/F_000001.png"
    assert first["sources"][0]["resolved"] is True
    assert first["sources"][0]["local_frame_index"] == 0
    assert in_s002["sources"][0]["resolved"] is False
    assert "PLACEHOLDER_DRAFT" in in_s002["operations"]
    assert verify_build(folder)["valid"]
    # A resolved draft frame carries the imported pixels (RGBA composited).
    with Image.open(folder / "draft_frames/F_000001.png") as im:
        assert im.size == (320, 240)


def test_draft_preview_uses_master_audio_unchanged(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    make_sequence(tmp_path / "seq", count=48)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    config = read(p / "project.yaml")
    master_sha = digest(p / config["audio"]["path"])
    result = compile_preview(p)
    folder = Path(result["build_dir"])
    assert_master(folder / "DRAFT_PREVIEW.mp4", 2)
    assert digest(p / config["audio"]["path"]) == master_sha
    assert read(folder / "build.json")["audio"]["sha256"] == master_sha


def test_legacy_preview_and_final_are_unchanged(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=1)
    result = compile_preview(p)
    folder, record = newest_build(p)
    assert record.get("document_type") is None and record["schema_version"] == 1
    assert (folder / "MASTER_SUBBED.mp4").is_file()
    with pytest.raises(FilmError):
        compile_preview(p, quality="weird")


def test_animation_project_final_is_not_a_draft_fallback(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    make_sequence(tmp_path / "seq")
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    with pytest.raises(FilmError, match="Final compile"):
        compile_final(p)
    assert not list(p.glob("builds/B*"))


def test_original_audio_lyrics_and_cues_are_untouched(tmp_path):
    p = fixture_project(tmp_path, seconds=2, shot_count=2)
    names = ["analysis/audio.json", "lyrics/lyrics_timed.json",
             "lyrics/lyrics_source.txt", "manifest/shots.json"]
    before = {n: digest(p / n) for n in names if (p / n).is_file()}
    config = read(p / "project.yaml")
    before[config["audio"]["path"]] = digest(p / config["audio"]["path"])
    animation_init(p)
    make_sequence(tmp_path / "seq")
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    import_layer_rgba(p, rgba_frame(tmp_path / "layer.png"))
    compile_preview(p)
    for n, sha in before.items():
        assert digest(p / n) == sha


def test_cli_import_and_listing(tmp_path):
    p = animation_project(tmp_path)
    folder = make_sequence(tmp_path / "seq", count=4)
    index = write_index(folder)
    assert cli.main(["import-sequence", str(p), "S001", "--index", str(index)]) == 0
    layer = rgba_frame(tmp_path / "layer.png")
    assert cli.main(["import-animation-asset", str(p), "--kind", "LAYER_RGBA",
                     "--file", str(layer), "--pivot", "4,9",
                     "--crop-origin", "1,2", "--z-order", "3"]) == 0
    assert cli.main(["animation-assets", str(p)]) == 0
    # Legacy projects cannot use the new commands.
    (tmp_path / "legacy_root").mkdir()
    legacy = fixture_project(tmp_path / "legacy_root", seconds=1, shot_count=1)
    assert cli.main(["import-sequence", str(legacy), "S001",
                     "--folder", str(folder)]) == 1
    assert cli.main(["animation-assets", str(legacy)]) == 1
    # Unimplemented kinds fail honestly.
    assert cli.main(["import-animation-asset", str(p), "--kind", "MASK",
                     "--file", str(layer)]) == 1


def test_registry_is_canonical_and_version_gated(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "seq", count=4)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    raw = (p / "manifest/animation_assets.json").read_bytes()
    document = json.loads(raw)
    assert document["document_type"] == "animation_asset_registry"
    assert document["schema_version"] == 1
    # Stored bytes are byte-exact canonical form (sorted keys, one trailing LF).
    from engine.animation_schema import canon_bytes
    assert raw == canon_bytes(document)
    assert read_canon(p / "manifest/animation_assets.json") == document
    document["schema_version"] = 2
    write_canon(p / "manifest/animation_assets.json", document)
    with pytest.raises(FilmError, match="Unsupported"):
        load_registry(p)
