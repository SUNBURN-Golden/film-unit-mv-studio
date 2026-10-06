"""ANIM-003: external frame-sequence/RGBA import, hash-bound resolver, draft Preview.

Fixtures are real Pillow-drawn PNGs (RGBA gradients, distinct frames) and the
existing synthetic project; nothing here is an artistic approval, a production
qualification or a Final.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from engine import cli
from engine.animation_assets import (asset_status, content_digest,
                                     import_frame_sequence, import_layer_rgba,
                                     load_registry, resolve_asset,
                                     resolve_shot_sequence, save_registry)
from engine.animation_migrate import animation_init
from engine.animation_schema import canon_bytes, read_canon, write_canon
from engine.builds import replay_build, verify_build
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
    with pytest.raises(FilmError, match="regular file|escapes|Disallowed|ymlink"):
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
    # A draft preview record, not a Build 2 animation_build.
    assert record["document_type"] == "animation_draft_preview"
    assert record["schema_version"] == 1 and record["mode"] == "PREVIEW"
    assert record["draft"] is True and "storage_profile" not in record
    assert record["frames"]["total"] == 48 and record["frames"]["placeholder_frames"] == 24
    assert result["incomplete_entries"] == ["I002"]
    assert any("placeholder" in w for w in record["warnings"])
    output = folder / "DRAFT_PREVIEW.mp4"
    assert output.is_file() and record["output"] == "DRAFT_PREVIEW.mp4"
    video = [s for s in probe(output)["streams"] if s["codec_type"] == "video"]
    assert video[0]["avg_frame_rate"] == "24/1" and int(video[0]["nb_frames"]) == 48
    # The composed frames, used member snapshots and per-frame draft map are sealed.
    assert (folder / "draft_frames/F_000001.png").is_file()
    assert "snapshot/animation/assets/A0001/r1/f000000.png" in record["files"]
    assert not (folder / "frame_map.jsonl").exists()
    rows = [json.loads(line) for line in
            (folder / "draft_frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == 48
    first, in_s002 = rows[0], rows[24]
    assert first["file"] == "draft_frames/F_000001.png"
    assert first["sources"][0]["resolved"] is True
    assert first["sources"][0]["local_frame_index"] == 0
    assert first["sources"][0]["sequence_revision"] == 1
    assert in_s002["sources"][0]["resolved"] is False
    assert in_s002["sources"][0]["sequence_revision"] is None
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
    assert raw == canon_bytes(document)
    assert read_canon(p / "manifest/animation_assets.json") == document
    document["schema_version"] = 2
    write_canon(p / "manifest/animation_assets.json", document)
    with pytest.raises(FilmError, match="Unsupported"):
        load_registry(p)


def test_draft_preview_is_not_replayable(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    make_sequence(tmp_path / "seq", count=48)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    compile_preview(p)
    folder, record = newest_build(p)
    assert verify_build(folder)["valid"]  # sealed and intact, just not Build 1
    with pytest.raises(FilmError, match="not replayable"):
        replay_build(folder, tmp_path / "replayed")
    assert not (tmp_path / "replayed").exists()


def test_draft_preview_crossfade_weights_and_snapshot_pixels(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    make_sequence(tmp_path / "seq_s001", count=30, seed=10)
    make_sequence(tmp_path / "seq_s002", count=30, seed=200)
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    # A real CROSSFADE: extend I001's used range so sum(L) - sum(O) stays 48.
    timeline = read_canon(p / "timeline/edit.json")
    timeline["entries"][0]["used_source_range"] = [0, 26]
    timeline["entries"][0]["transition_out"] = {
        "id": "T001", "type": "CROSSFADE", "to_instance": "I002",
        "overlap_frames": 2, "curve": "LINEAR_INTERIOR_V1"}
    write_canon(p / "timeline/edit.json", timeline)
    result = compile_preview(p)
    assert result["status"] == "COMPLETE" and result["incomplete_entries"] == []
    folder, record = newest_build(p)
    rows = [json.loads(line) for line in
            (folder / "draft_frame_map.jsonl").read_text().splitlines()]
    assert len(rows) == 48
    # Every line is one CANON_JSON_V1 document plus its trailing LF.
    raw = (folder / "draft_frame_map.jsonl").read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    for line in raw.split(b"\n")[:-1]:
        assert line + b"\n" == canon_bytes(json.loads(line))
    # The overlap covers global frames 24-25; LINEAR_INTERIOR_V1 gives
    # incoming (k + 1) / (O + 1) and outgoing 1 - incoming for O = 2.
    for index, weights in ((24, ([2, 3], [1, 3])), (25, ([1, 3], [2, 3]))):
        sources = rows[index]["sources"]
        assert [s["shot_id"] for s in sources] == ["S001", "S002"]
        assert [s["weight"] for s in sources] == [list(w) for w in weights]
        assert sources[0]["local_frame_index"] == index
        assert sources[1]["local_frame_index"] == index - 24
        assert all(s["resolved"] and s["sequence_revision"] == 1
                   for s in sources)
        assert rows[index]["operations"] == ["DRAFT_TAG", "CROSSFADE",
                                             "LINEAR_INTERIOR_V1"]
    # Outside the overlap each frame has a single full-weight source.
    assert [s["weight"] for s in rows[0]["sources"]] == [[1, 1]]
    assert len(rows[23]["sources"]) == len(rows[26]["sources"]) == 1
    # The compose reads the snapshot copies frozen inside the build folder.
    offset = ((320 - 96) // 2, (240 - 72) // 2)  # _fit centers a 96x72 source
    spot = (offset[0] + 8, offset[1] + 6)        # clear of the DRAFT tag area
    snap = folder / "snapshot" / "animation" / "assets"
    with Image.open(snap / "A0001/r1/f000000.png") as im:
        expected = im.convert("RGB").getpixel((8, 6))
    with Image.open(folder / "draft_frames/F_000001.png") as im:
        assert im.convert("RGB").getpixel(spot) == expected
    # Inside the crossfade the pixel is the weighted blend of both members.
    with Image.open(snap / "A0001/r1/f000024.png") as im:
        outgoing = im.convert("RGB").getpixel((8, 6))
    with Image.open(snap / "A0002/r1/f000000.png") as im:
        incoming = im.convert("RGB").getpixel((8, 6))
    with Image.open(folder / "draft_frames/F_000025.png") as im:
        blended = im.convert("RGB").getpixel(spot)
    for left, right, got in zip(outgoing, incoming, blended):
        assert abs(got - round(left * 2 / 3 + right / 3)) <= 2
    # Used members are sealed in the build inventory; unused tails are not.
    assert "snapshot/animation/assets/A0001/r1/f000025.png" in record["files"]
    assert "snapshot/animation/assets/A0001/r1/f000026.png" not in record["files"]
    assert verify_build(folder)["valid"]


def test_recipe_refs_participate_in_content_digest(tmp_path):
    p = animation_project(tmp_path)
    make_sequence(tmp_path / "seq", count=4)
    result = import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    record = load_registry(p)["assets"][result["asset_id"]]["revisions"]["1"]
    assert content_digest(record) == record["content_sha256"]
    # A recipe-only change opens a new revision identity (design section 11.4).
    changed = copy.deepcopy(record)
    changed["exposure_recipe_ref"] = "production/recipes/exposure_v1.json"
    assert content_digest(changed) != record["content_sha256"]
    changed = copy.deepcopy(record)
    changed["composite_recipe_ref"] = "production/recipes/composite_v1.json"
    assert content_digest(changed) != record["content_sha256"]


def test_index_member_symlink_inside_root_is_rejected(tmp_path):
    p = animation_project(tmp_path)
    folder = tmp_path / "seq"
    folder.mkdir()
    target = rgba_frame(folder / "real.png")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    (folder / "link.png").symlink_to("real.png")  # resolves inside the index dir
    index = folder / "index.json"
    index.write_text(json.dumps({"frames": [{"file": "link.png", "sha256": sha}]}),
                     encoding="utf-8")
    with pytest.raises(FilmError, match="ymlink"):
        import_frame_sequence(p, "S001", index=index)
    # A member reached through a symlinked directory is refused the same way.
    real_dir = folder / "real_dir"
    real_dir.mkdir()
    inner = rgba_frame(real_dir / "inner.png")
    (folder / "linked_dir").symlink_to("real_dir", target_is_directory=True)
    inner_sha = hashlib.sha256(inner.read_bytes()).hexdigest()
    index.write_text(json.dumps(
        {"frames": [{"file": "linked_dir/inner.png", "sha256": inner_sha}]}),
        encoding="utf-8")
    with pytest.raises(FilmError, match="ymlink"):
        import_frame_sequence(p, "S001", index=index)


def test_tampered_stored_member_blocks_revision_reuse(tmp_path):
    p = animation_project(tmp_path)
    folder = make_sequence(tmp_path / "seq", count=8)
    first = import_frame_sequence(p, "S001", folder=folder)
    record = load_registry(p)["assets"][first["asset_id"]]["revisions"]["1"]
    target = member_path(p, record, 2)
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same length, different bytes
    target.write_bytes(bytes(data))
    # Re-importing identical external content matches r1's digest, but its
    # stored bytes no longer verify, so reuse is refused, not reported.
    with pytest.raises(FilmError, match="hash mismatch|length changed|Missing"):
        import_frame_sequence(p, "S001", folder=folder)
    entry = load_registry(p)["assets"][first["asset_id"]]
    assert entry["current_revision"] == 1 and list(entry["revisions"]) == ["1"]


def test_decode_error_marks_frame_unresolved(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    make_sequence(tmp_path / "seq", count=48)
    result = import_frame_sequence(p, "S001", folder=tmp_path / "seq")
    registry = load_registry(p)
    record = registry["assets"][result["asset_id"]]["revisions"]["1"]
    # Corrupt the stored member so the bytes pass the pinned hash check but
    # fail PNG decode at compose time.
    target = member_path(p, record, 0)
    broken = target.read_bytes()[: len(target.read_bytes()) // 3]
    target.write_bytes(broken)
    record["files"][0]["sha256"] = hashlib.sha256(broken).hexdigest()
    record["files"][0]["byte_length"] = len(broken)
    record["content_sha256"] = content_digest(record)
    registry["assignments"]["S001"]["content_sha256"] = record["content_sha256"]
    save_registry(p, registry)
    result = compile_preview(p)
    folder, record = newest_build(p)
    assert result["status"] == "COMPLETE"
    rows = [json.loads(line) for line in
            (folder / "draft_frame_map.jsonl").read_text().splitlines()]
    source = rows[0]["sources"][0]
    assert source["resolved"] is False
    assert source["sequence_revision"] is None
    assert "PLACEHOLDER_DRAFT" in rows[0]["operations"]
    assert any("decode" in w for w in record["warnings"])
    assert record["frames"]["placeholder_frames"] == 1
    # Later frames still resolve and compose normally.
    assert rows[1]["sources"][0]["resolved"] is True
    assert rows[1]["sources"][0]["sequence_revision"] == 1
