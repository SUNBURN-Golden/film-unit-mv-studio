"""film-asset-library: the asset read model, detach/replace/cleanup actions.

Synthetic Pillow/sine fixtures only — no real artwork approval, service
qualification or paid call. The fixtures deliberately include a missing
member file, corrupted member bytes and the same source filename hashing
differently, per the node's acceptance checks.
"""
from pathlib import Path

import pytest

from engine.animation_assets import (import_frame_sequence,
                                     import_layer_rgba, import_mask,
                                     load_registry, resolve_shot_sequence,
                                     save_registry)
from engine.animation_locks import (declare_waves, record_plan_lock,
                                    record_wave_lock)
from engine.animation_preview import compile_draft_preview
from engine.animation_review import (record_cut_review,
                                     record_transition_review)
from engine.asset_library import (cleanup_unused, detach_asset, library_view,
                                  replace_asset)
from engine.core import FilmError, digest
from test_anim_003 import animation_project, make_sequence, rgba_frame


def import_seq(p, tmp_path, shot="S001", count=24, seed=0, name="seq"):
    folder = make_sequence(tmp_path / name, count=count, seed=seed)
    return import_frame_sequence(p, shot, folder=folder)


def revision(view, asset_id, rev):
    asset = next(a for a in view["assets"] if a["asset_id"] == asset_id)
    return next(r for r in asset["revisions"] if r["revision"] == rev)


def used_where(rev_block):
    return {u["where"] for u in rev_block["used_in"]}


# --- the read model --------------------------------------------------------

def test_view_reports_kind_revision_origin_and_rights(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    seq = import_seq(p, tmp_path, seed=1)
    layer = import_layer_rgba(p, rgba_frame(tmp_path / "hero.png"))
    view = library_view(p)
    assert {a["asset_id"] for a in view["assets"]} == \
        {seq["asset_id"], layer["asset_id"]}
    block = revision(view, seq["asset_id"], 1)
    assert block["kind"] == "FRAME_SEQUENCE"
    assert block["origin"] == "EXTERNAL_IMPORT"
    assert block["acceptance"] == "DRAFT"
    assert block["members"] == 24 and block["integrity"] == "VERIFIED"
    # A hash match is byte integrity — never a license or permission.
    assert block["rights"]["state"] == "UNVERIFIED"
    assert "license" in block["rights"]["note"] \
        and "permission" in block["rights"]["note"]
    assert layer["kind"] == "LAYER_RGBA"
    assert view["facets"] == {"qualification_state": "UNQUALIFIED",
                              "acceptance_state": "PENDING",
                              "release_state": "NOT_AUTHORIZED"}


def test_used_in_current_manifest_and_historical_build(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    first = import_seq(p, tmp_path, shot="S001", seed=1, name="seq1")
    second = import_seq(p, tmp_path, shot="S002", seed=50, name="seq2")
    build = compile_draft_preview(p)
    view = library_view(p)
    one = revision(view, first["asset_id"], 1)
    two = revision(view, second["asset_id"], 1)
    for block, shot in ((one, "S001"), (two, "S002")):
        assert "current manifest" in used_where(block)
        assert any(w.startswith(f"build {build['build_id']}")
                   for w in used_where(block))
        assert block["protected"] is True
    asset = next(a for a in view["assets"]
                 if a["asset_id"] == first["asset_id"])
    assert asset["builds"] == [build["build_id"]]
    cuts = {u["shot_id"] for u in asset["used_in"]}
    assert "S001" in cuts


def test_missing_member_file_is_reported_not_hidden(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=2)
    record = load_registry(p)["assets"][seq["asset_id"]]["revisions"]["1"]
    member = p / record["files"][0]["relative_name"]
    member.unlink()
    view = library_view(p)
    block = revision(view, seq["asset_id"], 1)
    assert block["integrity"] == "MISSING"
    statuses = {f["relative_name"]: f["status"] for f in block["files"]}
    assert statuses[record["files"][0]["relative_name"]] == "MISSING"
    assert view["summary"]["missing"] == 1


def test_corrupted_member_bytes_are_a_hash_mismatch(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=3)
    record = load_registry(p)["assets"][seq["asset_id"]]["revisions"]["1"]
    member = p / record["files"][1]["relative_name"]
    member.write_bytes(member.read_bytes() + b"tampered")
    view = library_view(p)
    block = revision(view, seq["asset_id"], 1)
    assert block["integrity"] == "CORRUPT"
    statuses = {f["relative_name"]: f["status"] for f in block["files"]}
    assert statuses[record["files"][1]["relative_name"]] == "LENGTH_MISMATCH"


def test_corrupted_member_same_length_is_still_caught(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=4)
    record = load_registry(p)["assets"][seq["asset_id"]]["revisions"]["1"]
    member = p / record["files"][2]["relative_name"]
    data = bytearray(member.read_bytes())
    data[-1] ^= 0xFF
    member.write_bytes(bytes(data))          # same length, different bytes
    block = revision(library_view(p), seq["asset_id"], 1)
    assert block["integrity"] == "CORRUPT"
    statuses = {f["relative_name"]: f["status"] for f in block["files"]}
    assert statuses[record["files"][2]["relative_name"]] == "HASH_MISMATCH"


def test_unrecorded_files_in_a_revision_dir_are_flagged(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=5)
    record = load_registry(p)["assets"][seq["asset_id"]]["revisions"]["1"]
    folder = (p / record["files"][0]["relative_name"]).parent
    (folder / "sneaky.png").write_bytes(b"not a member")
    block = revision(library_view(p), seq["asset_id"], 1)
    extra = [f for f in block["files"] if f["role"] == "UNRECORDED"]
    assert [f["relative_name"] for f in extra] == \
        [str((folder / "sneaky.png").relative_to(p))]
    assert extra[0]["status"] == "UNRECORDED"


def test_same_filename_different_hash_are_distinct_assets(tmp_path):
    """Acceptance 1: a filename never makes two byte contents one asset."""
    p = animation_project(tmp_path, shot_count=2)
    first = import_seq(p, tmp_path, shot="S001", seed=10, name="same")
    second = import_seq(p, tmp_path, shot="S002", seed=90, name="same2")
    # Both folders were called their own names but members share f####.png.
    view = library_view(p)
    assert first["asset_id"] != second["asset_id"]
    assert first["content_sha256"] != second["content_sha256"]
    collisions = {c["source_name"]: c["holders"]
                  for c in view["name_collisions"]}
    assert "f0000.png" in collisions
    holders = {h["asset_id"] for h in collisions["f0000.png"]}
    assert holders == {first["asset_id"], second["asset_id"]}


def test_same_filename_and_same_bytes_share_the_digest(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    folder = make_sequence(tmp_path / "shared", count=24, seed=7)
    first = import_frame_sequence(p, "S001", folder=folder)
    second = import_frame_sequence(p, "S002", folder=folder)
    # Identical bytes hash identically — equal digests are not a collision.
    assert second["content_sha256"] == first["content_sha256"]
    view = library_view(p)
    assert not any(c["source_name"] == "f0000.png"
                   for c in view["name_collisions"])


def test_filters_by_kind_origin_and_used_cut(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    seq = import_seq(p, tmp_path, shot="S001", seed=11, name="seq")
    layer = import_layer_rgba(p, rgba_frame(tmp_path / "hero.png"))
    view = library_view(p, kind="LAYER_RGBA")
    assert [a["asset_id"] for a in view["assets"]] == [layer["asset_id"]]
    view = library_view(p, shot_id="S001")
    assert {a["asset_id"] for a in view["assets"]} == {seq["asset_id"]}
    view = library_view(p, origin="EXTERNAL_IMPORT", kind="MASK")
    assert view["assets"] == []
    view = library_view(p, rights="LICENSED")   # can never match anything
    assert view["assets"] == []


# --- replace ---------------------------------------------------------------

def test_replace_sequence_opens_a_new_revision_and_repins_the_cut(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    first = import_seq(p, tmp_path, shot="S001", seed=20, name="old")
    old_files = [f["relative_name"]
                 for f in load_registry(p)["assets"][first["asset_id"]]
                 ["revisions"]["1"]["files"]]
    result = replace_asset(p, first["asset_id"],
                           folder=make_sequence(tmp_path / "new", 24, seed=60),
                           shot_id="S001")
    assert result["revision"] == 2 and result["asset_id"] == first["asset_id"]
    registry = load_registry(p)
    assert registry["assignments"]["S001"]["revision"] == 2
    assert registry["assignments"]["S001"]["content_sha256"] \
        == result["content_sha256"]
    # The pinned bytes of revision 1 were never rewritten.
    assert all((p / rel).is_file() for rel in old_files)
    assert digest(p / old_files[0]) == \
        registry["assets"][first["asset_id"]]["revisions"]["1"]["files"][0]["sha256"]
    block = revision(library_view(p), first["asset_id"], 1)
    assert "current manifest" not in used_where(block)


def test_replace_layer_reuses_recorded_geometry(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    layer = import_layer_rgba(p, rgba_frame(tmp_path / "hero.png"),
                              pivot=(3, 4), crop_origin=(1, 2), z_order=7)
    result = replace_asset(p, layer["asset_id"],
                           file=rgba_frame(tmp_path / "hero2.png", seed=9))
    assert result["revision"] == 2
    record = load_registry(p)["assets"][layer["asset_id"]]["revisions"]["2"]
    assert record["pivot"] == [3, 4] and record["z_order"] == 7
    assert record["crop_origin"] == [1, 2]


def test_replace_refuses_composite_and_unknown_assets(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    import_seq(p, tmp_path)
    with pytest.raises(FilmError, match="Unknown asset"):
        replace_asset(p, "A9999", file=tmp_path / "x.png")
    registry = load_registry(p)
    asset_id = next(iter(registry["assets"]))
    with pytest.raises(FilmError):
        replace_asset(p, asset_id, folder=make_sequence(tmp_path / "n", 4))


# --- detach ------------------------------------------------------------------

def test_detach_drops_the_assignment_pin(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    seq = import_seq(p, tmp_path, shot="S001", seed=30)
    result = detach_asset(p, "S001")
    assert result["pin"]["asset_id"] == seq["asset_id"]
    assert load_registry(p)["assignments"] == {}
    with pytest.raises(FilmError, match="no assigned animation sequence"):
        resolve_shot_sequence(p, "S001")
    # Registry record and member bytes are untouched.
    block = revision(library_view(p), seq["asset_id"], 1)
    assert block["integrity"] == "VERIFIED"
    assert "current manifest" not in used_where(block)
    with pytest.raises(FilmError, match="no assigned sequence"):
        detach_asset(p, "S001")


def test_detach_names_the_locks_it_stales(tmp_path):
    p = animation_project(tmp_path, shot_count=2)
    import_seq(p, tmp_path, shot="S001", seed=31, name="s1")
    import_seq(p, tmp_path, shot="S002", seed=61, name="s2")
    declare_waves(p, [{"wave": "W00", "shots": ["S001", "S002"],
                       "difficulty": [{"type": "HARD", "reason": "fixture"}]}])
    record_plan_lock(p, "fixture approver")
    record_wave_lock(p, "W00", "fixture approver")
    result = detach_asset(p, "S001")
    assert result["locks_staled"] == ["WAVE_LOCK L0002 W00"]


# --- cleanup -----------------------------------------------------------------

def test_cleanup_dry_run_lists_candidates_and_deletes_nothing(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=40)
    replace_asset(p, seq["asset_id"],
                  folder=make_sequence(tmp_path / "r2", 24, seed=80),
                  shot_id="S001")
    registry_bytes = (p / "manifest/animation_assets.json").read_bytes()
    result = cleanup_unused(p)
    assert result["dry_run"] is True
    assert [c["revision"] for c in result["candidates"]] == [1]
    protected = {(r["asset_id"], r["revision"]) for r in result["protected"]}
    assert (seq["asset_id"], 2) in protected
    assert (p / "manifest/animation_assets.json").read_bytes() == registry_bytes
    record = load_registry(p)["assets"][seq["asset_id"]]["revisions"]["1"]
    assert (p / record["files"][0]["relative_name"]).is_file()


def test_cleanup_execute_deletes_only_unprotected_revisions(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, shot="S001", seed=41, name="a")
    layer = import_layer_rgba(p, rgba_frame(tmp_path / "layer.png"))
    replace_asset(p, seq["asset_id"],
                  folder=make_sequence(tmp_path / "b", 24, seed=81),
                  shot_id="S001")
    old_dir = p / "animation/assets" / seq["asset_id"] / "r1"
    result = cleanup_unused(p, execute=True)
    deleted = {(d["asset_id"], d["revision"]) for d in result["deleted"]}
    assert deleted == {(seq["asset_id"], 1), (layer["asset_id"], 1)}
    assert not old_dir.exists()
    assert not (p / "animation/assets" / layer["asset_id"]).exists()
    registry = load_registry(p)
    assert layer["asset_id"] not in registry["assets"]
    assert registry["assets"][seq["asset_id"]]["current_revision"] == 2
    assert set(registry["assets"][seq["asset_id"]]["revisions"]) == {"2"}
    assert resolve_shot_sequence(p, "S001")["revision"] == 2


def test_cleanup_never_deletes_an_assigned_revision(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=42)
    result = cleanup_unused(p, execute=True)
    assert result["deleted"] == []
    assert resolve_shot_sequence(p, "S001")["revision"] == seq["revision"]


def test_cleanup_refuses_a_build_referenced_revision(tmp_path):
    """Acceptance 2: an asset a historical build resolved is never deleted."""
    p = animation_project(tmp_path, shot_count=2)
    first = import_seq(p, tmp_path, shot="S001", seed=43, name="one")
    import_seq(p, tmp_path, shot="S002", seed=83, name="two")
    compile_draft_preview(p)
    replace_asset(p, first["asset_id"],
                  folder=make_sequence(tmp_path / "again", 24, seed=84),
                  shot_id="S001")
    result = cleanup_unused(p, execute=True)
    kept = {(r["asset_id"], r["revision"]) for r in result["protected"]}
    assert (first["asset_id"], 1) in kept       # build pin survives unassign
    assert (first["asset_id"], 1) not in \
        {(d["asset_id"], d["revision"]) for d in result["deleted"]}
    record = load_registry(p)["assets"][first["asset_id"]]["revisions"]["1"]
    assert (p / record["files"][0]["relative_name"]).is_file()


def test_cleanup_refuses_lock_and_review_referenced_revisions(tmp_path):
    """Acceptance 2, continued: a revision a LOCK or review bound is kept."""
    p = animation_project(tmp_path, shot_count=2)
    first = import_seq(p, tmp_path, shot="S001", seed=44, name="one")
    import_seq(p, tmp_path, shot="S002", seed=85, name="two")
    declare_waves(p, [{"wave": "W00", "shots": ["S001", "S002"],
                       "difficulty": [{"type": "HARD", "reason": "fixture"}]}])
    record_plan_lock(p, "fixture approver")
    record_wave_lock(p, "W00", "fixture approver")
    record_cut_review(p, "I001", reviewer="fixture reviewer",
                      methods=["CUT_FULL_SPEED_PLAYBACK"])
    replace_asset(p, first["asset_id"],
                  folder=make_sequence(tmp_path / "again", 24, seed=86),
                  shot_id="S001")
    result = cleanup_unused(p, execute=True)
    kept = {r["revision"] for r in result["protected"]
            if r["asset_id"] == first["asset_id"]}
    assert 1 in kept                            # WAVE_LOCK + review pin
    reasons = next(r["reasons"] for r in result["protected"]
                   if r["asset_id"] == first["asset_id"] and r["revision"] == 1)
    assert any("WAVE_LOCK" in reason for reason in reasons)
    assert any("review RV" in reason for reason in reasons)


def test_cleanup_refuses_a_revision_pinned_by_another_asset(tmp_path):
    p = animation_project(tmp_path, shot_count=1)
    layer = import_layer_rgba(p, rgba_frame(tmp_path / "base.png"))
    pin = {"asset_id": layer["asset_id"], "revision": 1,
           "content_sha256": layer["content_sha256"]}
    mask = import_mask(p, rgba_frame(tmp_path / "mask.png"), target=pin)
    result = cleanup_unused(p)
    protected = {(r["asset_id"], r["revision"]) for r in result["protected"]}
    assert (layer["asset_id"], 1) in protected   # mask target pin
    assert (mask["asset_id"], 1) in \
        {(c["asset_id"], c["revision"]) for c in result["candidates"]}


def test_cleanup_protects_transition_digest_bound_revisions(tmp_path):
    """A TRANSITION review binds each endpoint's sequence digest: the bound
    revisions stay protected even after the cuts are detached, when the
    bare instance+revision link can no longer name an asset."""
    p = animation_project(tmp_path, shot_count=2)
    first = import_seq(p, tmp_path, shot="S001", seed=48, name="one")
    second = import_seq(p, tmp_path, shot="S002", seed=88, name="two")
    record_transition_review(p, "T001", reviewer="fixture reviewer",
                             methods=["TRANSITION_FULL_SPEED_PLAYBACK"])
    detach_asset(p, "S001")
    detach_asset(p, "S002")
    result = cleanup_unused(p, execute=True)
    protected = {(r["asset_id"], r["revision"]): r["reasons"]
                 for r in result["protected"]}
    for imported in (first, second):
        reasons = protected[(imported["asset_id"], 1)]
        assert any("transition sequence digest" in reason
                   for reason in reasons)
    assert result["deleted"] == []
    assert (p / "animation/assets" / first["asset_id"] / "r1").is_dir()


def test_cleanup_unlinks_a_member_symlink_without_touching_target(tmp_path):
    """A member path that is a symlink loses only the link, never the
    bytes it points at."""
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=46)
    replace_asset(p, seq["asset_id"],
                  folder=make_sequence(tmp_path / "r2", 24, seed=87),
                  shot_id="S001")
    record = load_registry(p)["assets"][seq["asset_id"]]["revisions"]["1"]
    member = p / record["files"][0]["relative_name"]
    target = p / "input/lyrics.txt"
    payload = target.read_bytes()
    member.unlink()
    member.symlink_to(target)
    result = cleanup_unused(p, execute=True)
    assert target.read_bytes() == payload
    assert not member.is_symlink() and not member.exists()
    deleted = {(d["asset_id"], d["revision"]) for d in result["deleted"]}
    assert (seq["asset_id"], 1) in deleted
    assert "1" not in \
        load_registry(p)["assets"][seq["asset_id"]]["revisions"]


def test_cleanup_refuses_a_symlinked_revision_dir(tmp_path):
    """A revision directory that is itself a symlink is refused whole —
    the target tree and the registry record are both untouched."""
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=47)
    replace_asset(p, seq["asset_id"],
                  folder=make_sequence(tmp_path / "r2", 24, seed=89),
                  shot_id="S001")
    revision_dir = p / "animation/assets" / seq["asset_id"] / "r1"
    elsewhere = tmp_path / "elsewhere"
    revision_dir.rename(elsewhere)
    revision_dir.symlink_to(elsewhere)
    result = cleanup_unused(p, execute=True)
    refused = {(r["asset_id"], r["revision"]) for r in result["refused"]}
    assert (seq["asset_id"], 1) in refused
    assert sorted(f.name for f in elsewhere.iterdir()) == \
        [f"f{i:06d}.png" for i in range(24)]
    assert "1" in \
        load_registry(p)["assets"][seq["asset_id"]]["revisions"]


def test_cleanup_refuses_a_member_path_with_dotdot(tmp_path):
    """A registry member whose relative_name escapes the revision
    directory is refused; nothing outside it is removed."""
    p = animation_project(tmp_path, shot_count=1)
    seq = import_seq(p, tmp_path, seed=45)
    replace_asset(p, seq["asset_id"],
                  folder=make_sequence(tmp_path / "r2", 24, seed=85),
                  shot_id="S001")
    outside = p / "input/lyrics.txt"
    payload = outside.read_bytes()
    registry = load_registry(p)
    record = registry["assets"][seq["asset_id"]]["revisions"]["1"]
    record["files"][0]["relative_name"] = \
        f"animation/assets/{seq['asset_id']}/r1/../../../input/lyrics.txt"
    save_registry(p, registry)
    result = cleanup_unused(p, execute=True)
    assert outside.read_bytes() == payload
    refused = {(r["asset_id"], r["revision"]) for r in result["refused"]}
    assert (seq["asset_id"], 1) in refused
    entry = load_registry(p)["assets"][seq["asset_id"]]
    assert "1" in entry["revisions"]
    assert (p / "animation/assets" / seq["asset_id"] / "r1").is_dir()


def test_cleanup_never_retargets_a_surviving_current_revision(tmp_path):
    """Identical-content reimport can adopt an older revision again;
    deleting a different unused revision must leave current alone."""
    p = animation_project(tmp_path, shot_count=2)
    first = make_sequence(tmp_path / "keep", 24, seed=49)
    seq = import_frame_sequence(p, "S001", folder=first)
    asset_id = seq["asset_id"]
    replace_asset(p, asset_id,
                  folder=make_sequence(tmp_path / "b", 24, seed=90),
                  shot_id="S001")                              # r2
    record_cut_review(p, "I001", reviewer="fixture reviewer",
                      methods=["CUT_FULL_SPEED_PLAYBACK"])     # pins r2
    replace_asset(p, asset_id,
                  folder=make_sequence(tmp_path / "c", 24, seed=91),
                  shot_id="S001")                              # r3
    reused = import_frame_sequence(p, "S001", folder=first)    # adopts r1
    assert reused["revision"] == 1 and reused["new_revision"] is False
    assert load_registry(p)["assets"][asset_id]["current_revision"] == 1
    result = cleanup_unused(p, execute=True)
    entry = load_registry(p)["assets"][asset_id]
    assert entry["current_revision"] == 1
    assert set(entry["revisions"]) == {"1", "2"}
    deleted = {(d["asset_id"], d["revision"]) for d in result["deleted"]}
    candidates = {(c["asset_id"], c["revision"])
                  for c in result["candidates"]}
    assert deleted == {(asset_id, 3)}
    assert (asset_id, 1) not in candidates


# --- boundaries ---------------------------------------------------------------

def test_legacy_mv_project_is_refused(tmp_path):
    from test_compiler_v03 import fixture_project
    p = fixture_project(tmp_path, seconds=2, shot_count=1)   # stays LEGACY_MV
    for call in (lambda: library_view(p), lambda: detach_asset(p, "S001"),
                 lambda: cleanup_unused(p),
                 lambda: replace_asset(p, "A0001", file=tmp_path / "x.png")):
        with pytest.raises(FilmError):
            call()
