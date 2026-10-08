"""film-review-diff: finding binding, before/after closure, pass isolation.

A finding binds one cut, a half-open frame range, a problem type and the
adopted sequence digest. A later render does not inherit that render's
PASS. Unrelated cut reviews stay current. Author self-check, independent
review and director approval are different roles. Fixtures are synthetic
Pillow frames. Review rows are protocol records — qualification
UNQUALIFIED, acceptance PENDING, release NOT_AUTHORIZED.
"""
import hashlib
from pathlib import Path

import pytest
from PIL import Image

from engine.animation_assets import import_frame_sequence, resolve_shot_sequence
from engine.animation_review import (load_reviews, record_cut_review,
                                     record_transition_review, review_status)
from engine.animation_schema import read_canon
from engine.core import FilmError, read
from engine.review_diff import (acknowledge, carry_pass, compare_finding,
                                diff_view, load_log, open_finding,
                                record_revision, render_pass_status)
from test_anim_003 import animation_project, make_sequence, rgba_frame
from test_compiler_v03 import fixture_project

REVIEWER = "Review diff fixture reviewer"
DIRECTOR = "Review diff fixture director"
AUTHOR = "Review diff fixture author"
METHODS = ["CUT_FULL_SPEED_PLAYBACK"]


def _project(tmp_path, shots=3):
    p = animation_project(tmp_path, shot_count=shots, seconds=1)
    document = read_canon(p / "timeline/edit.json")
    for entry in document["entries"]:
        count = entry["used_source_range"][1] - entry["used_source_range"][0]
        assert count >= 2
        import_frame_sequence(
            p, entry["shot_id"],
            folder=make_sequence(tmp_path / entry["shot_id"], count,
                                 size=(32, 24),
                                 seed=int(entry["shot_id"][1:]) * 40))
    return p


def _entry(p, instance_id):
    document = read_canon(p / "timeline/edit.json")
    return next(e for e in document["entries"] if e["instance_id"] == instance_id)


def _review_all(p):
    document = read_canon(p / "timeline/edit.json")
    for entry in document["entries"]:
        record_cut_review(p, entry["instance_id"], reviewer=REVIEWER,
                          methods=METHODS)
        transition = entry.get("transition_out")
        if transition:
            record_transition_review(p, transition["id"], reviewer=REVIEWER,
                                     methods=["TRANSITION_FULL_SPEED_PLAYBACK"])


def _retouch(p, tmp, instance_id, index):
    """Replace one adopted frame and import the result as a new revision."""
    entry = _entry(p, instance_id)
    resolved = resolve_shot_sequence(
        p, entry["shot_id"], entry["used_source_range"],
        entry["unused_handles"], entry["sequence_revision"])
    folder = tmp / f"retouch-{instance_id}-{index}"
    folder.mkdir()
    for member in resolved["record"]["files"]:
        source = p / member["relative_name"]
        dest = folder / f"f{member['frame_index']:04d}.png"
        if member["frame_index"] == index:
            with Image.open(source) as image:
                rgba_frame(dest, image.size, seed=4000 + index)
        else:
            dest.write_bytes(source.read_bytes())
    return import_frame_sequence(p, entry["shot_id"], folder=folder)


def _reimport(p, tmp, instance_id, seed):
    entry = _entry(p, instance_id)
    count = entry["used_source_range"][1] - entry["used_source_range"][0]
    return import_frame_sequence(
        p, entry["shot_id"],
        folder=make_sequence(tmp / f"again-{seed}", count, size=(32, 24),
                             seed=seed))


def _bytes(p):
    config = read(p / "project.yaml")
    wanted = ["input/lyrics.txt", config["audio"]["path"],
              "manifest/locks.json", "lyrics/lyrics_timed.json",
              "production/approvals.jsonl"]
    found = {}
    for relative in wanted:
        path = p / relative
        found[relative] = hashlib.sha256(path.read_bytes()).hexdigest() \
            if path.is_file() else None
    found["fps"] = config["format"]["fps"]
    return found


def _open(p, instance="I001", span=(0, 1), problem="FLICKER", **extra):
    fields = {"reviewer": REVIEWER, "role": "INDEPENDENT_REVIEW",
              "note": "flicker on the first used frame"}
    fields.update(extra)
    return open_finding(p, instance, list(span), problem, **fields)


# --- binding -------------------------------------------------------------------

def test_finding_binds_shot_frame_range_hash_and_problem_type(tmp_path):
    p = _project(tmp_path)
    finding = _open(p)
    assert finding["instance_id"] == "I001"
    assert finding["shot_id"] == "S001"
    assert finding["frame_range"] == [0, 1]
    assert finding["problem_type"] == "FLICKER"
    assert finding["artifact_kind"] == "SEQUENCE"
    assert len(finding["artifact_sha256"]) == 64
    assert finding["member_sha256"] == [
        {"frame_index": 0,
         "sha256": finding["member_sha256"][0]["sha256"]}]
    assert finding["member_sha256"][0]["sha256"]
    view = diff_view(p)
    assert view["fps"] == 24
    assert view["screen_status"] == "ready"
    assert view["not_final_approval"] is True
    assert view["facets"]["qualification_state"] == "UNQUALIFIED"
    assert view["facets"]["release_state"] == "NOT_AUTHORIZED"
    row = view["findings"][0]
    assert row["changed"] is False
    assert row["full_playback_required"] is False
    assert row["pass_inherited"] is False
    assert "CUT:I001" in row["kept"]
    assert "CUT:I001" not in row["stale"]
    assert "CUT:I002" in row["kept"]


def test_opening_a_finding_does_not_stale_reviews_or_touch_sources(tmp_path):
    p = _project(tmp_path)
    _review_all(p)
    before = _bytes(p)
    before_status = review_status(p)
    _open(p)
    assert _bytes(p) == before
    assert review_status(p) == before_status
    assert before_status["targets"]["I001"]["state"] == "CURRENT"
    assert before_status["targets"]["I003"]["state"] == "CURRENT"
    compare = compare_finding(p, "RF0001")
    assert compare["change_class"] == "NONE"
    assert compare["closure"]["unchanged"] is True


def test_wrong_hash_and_technical_waiver_and_author_waiver_are_refused(tmp_path):
    p = _project(tmp_path)
    with pytest.raises(FilmError, match="현재 채택 시퀀스와 다릅니다"):
        _open(p, artifact_sha256="ab" * 32)
    with pytest.raises(FilmError, match="기술 실패는 표현상 수용으로 면제할 수 없습니다"):
        _open(p, problem="TECHNICAL", disposition="INTENTIONAL",
              reason="it is only a slate")
    with pytest.raises(FilmError, match="작성자 자기 확인은 문제를 면제하거나 승인하지 않습니다"):
        _open(p, role="AUTHOR_SELF_CHECK", disposition="ACCEPTED_LIMITATION",
              reason="author says so")
    with pytest.raises(FilmError, match="프레임 범위"):
        _open(p, span=(0, 0))
    waived = _open(p, problem="ENDPOINT", disposition="INTENTIONAL",
                   reason="the hold is the intended end pose")
    assert waived["disposition"] == "INTENTIONAL"
    assert compare_finding(p, waived["finding_id"])["finding_state"] == "WAIVED"
    assert compare_finding(p, waived["finding_id"])["full_playback_required"] is False


def test_legacy_project_is_refused(tmp_path):
    p = fixture_project(tmp_path, seconds=1, shot_count=1)
    with pytest.raises(FilmError, match="LEGACY_MV"):
        open_finding(p, "I001", [0, 1], "FLICKER", reviewer=REVIEWER,
                     role="INDEPENDENT_REVIEW", note="no")
    assert not (p / "production/review_diff.jsonl").exists()
    assert read(p / "project.yaml")["format"]["fps"] == 24


# --- acceptance 1: old PASS does not cover a new render ------------------------

def test_old_render_pass_does_not_cover_new_render(tmp_path):
    p = _project(tmp_path)
    _review_all(p)
    finding = _open(p)
    first = finding["artifact_sha256"]
    first_review = review_status(p)["targets"]["I001"]["review_id"]
    _reimport(p, tmp_path, "I001", seed=77)
    mid = compare_finding(p, finding["finding_id"])
    assert mid["changed"] is True
    assert mid["before"]["artifact_sha256"] == first
    assert mid["after"]["artifact_sha256"] != first
    assert mid["pass_inherited"] is False
    assert mid["cut_pass_covers_current"] is False
    assert mid["full_playback_required"] is True
    stale = render_pass_status(p, "I001")
    assert stale["animation_review_state"] == "STALE"
    assert stale["animation_review_id"] is None
    assert stale["cut_pass_covers_current"] is False
    assert stale["pass_inherited"] is False
    assert stale["old_pass_covers_new_render"] is False
    assert stale["final_approval"] is False
    with pytest.raises(FilmError, match="승계되지 않습니다"):
        carry_pass(p, "I001")
    with pytest.raises(FilmError, match="옛 렌더의 PASS는 새 렌더로 승계되지 않습니다"):
        acknowledge(p, finding["finding_id"], role="DIRECTOR_APPROVAL",
                    reviewer=DIRECTOR, decision="CUT_PASS",
                    artifact_sha256=first, note="reuse the old pass",
                    methods=METHODS)
    record_revision(p, finding["finding_id"], reviewer=AUTHOR,
                    role="AUTHOR_SELF_CHECK", note="replaced the cut")
    passed = acknowledge(p, finding["finding_id"], role="DIRECTOR_APPROVAL",
                         reviewer=DIRECTOR, decision="CUT_PASS",
                         artifact_sha256=mid["after"]["artifact_sha256"],
                         note="watched the new cut", methods=METHODS)
    assert passed["pass_inherited"] is False
    assert passed["grants_cut_pass"] is True
    assert passed["grants_final_approval"] is False
    assert passed["artifact_sha256"] == mid["after"]["artifact_sha256"]
    current = render_pass_status(p, "I001")
    assert current["cut_pass_covers_current"] is True
    assert current["animation_review_state"] == "CURRENT"
    assert current["animation_review_id"] != first_review
    latest = load_reviews(p)[-1]
    assert latest["sequence_digest"] == mid["after"]["artifact_sha256"]
    assert latest["scope"] == "CUT"
    _reimport(p, tmp_path, "I001", seed=88)
    newest = render_pass_status(p, "I001")
    assert newest["artifact_sha256"] != passed["artifact_sha256"]
    assert newest["cut_pass_covers_current"] is False
    assert newest["animation_review_state"] == "STALE"
    assert newest["stale_pass_ids"] == [passed["ack_id"]]
    assert newest["pass_inherited"] is False
    with pytest.raises(FilmError, match="승계되지 않습니다"):
        carry_pass(p, "I001")
    assert all(row["scope"] != "FINAL_FILM" for row in load_reviews(p))


# --- acceptance 2: unrelated cut reviews stay ----------------------------------

def test_unrelated_cut_reviews_stay_current(tmp_path):
    p = _project(tmp_path)
    _review_all(p)
    before_ids = {target: row["review_id"]
                  for target, row in review_status(p)["targets"].items()}
    approvals = (p / "production/approvals.jsonl").read_bytes()
    finding = _open(p, span=(0, 1))
    _retouch(p, tmp_path, "I001", index=1)
    view = compare_finding(p, finding["finding_id"])
    assert view["changed_member_indices"] == [1]
    assert view["finding_frames_changed"] is False
    assert view["change_class"] == "PICTURE"
    assert view["closure"]["affected_instances"] == ["I001"]
    assert "I002" not in view["closure"]["affected_instances"]
    assert "I003" not in view["closure"]["affected_instances"]
    assert "CUT:I001" in view["stale"]
    assert "CUT:I002" in view["kept"]
    assert "CUT:I003" in view["kept"]
    assert "TRANSITION:T002" in view["kept"]
    assert "FINAL_FILM" in view["stale"]
    status = review_status(p)
    assert status["targets"]["I001"]["state"] == "STALE"
    assert status["targets"]["I002"]["state"] == "CURRENT"
    assert status["targets"]["I003"]["state"] == "CURRENT"
    assert status["targets"]["T002"]["state"] == "CURRENT"
    assert status["targets"]["I002"]["review_id"] == before_ids["I002"]
    assert status["targets"]["I003"]["review_id"] == before_ids["I003"]
    assert status["targets"]["T002"]["review_id"] == before_ids["T002"]
    # The log still holds the original reviews; nothing was deleted.
    assert (p / "production/approvals.jsonl").read_bytes() == approvals
    record_revision(p, finding["finding_id"], reviewer=AUTHOR,
                    role="AUTHOR_SELF_CHECK", note="retouched frame 1 only")
    with pytest.raises(FilmError, match="지적한 프레임이 바뀌지 않아"):
        acknowledge(p, finding["finding_id"], role="INDEPENDENT_REVIEW",
                    reviewer=REVIEWER, decision="FINDING_RESOLVED",
                    artifact_sha256=view["after"]["artifact_sha256"],
                    note="claiming the flicker is gone")
    assert review_status(p)["targets"]["I002"]["state"] == "CURRENT"
    assert (p / "production/approvals.jsonl").read_bytes() == approvals


def test_waiver_does_not_cover_a_new_render(tmp_path):
    p = _project(tmp_path)
    finding = _open(p, problem="OCCLUSION", disposition="INTENTIONAL",
                    reason="the overlap is the designed cover")
    assert compare_finding(p, finding["finding_id"])["waiver_covers_current"] is True
    _reimport(p, tmp_path, "I001", seed=91)
    again = compare_finding(p, finding["finding_id"])
    assert again["waiver_covers_current"] is False
    assert again["finding_state"] == "OPEN"
    assert again["full_playback_required"] is True
    assert again["pass_inherited"] is False


# --- acceptance 3: author / independent / director -----------------------------

def test_author_self_check_is_not_review_or_director_approval(tmp_path):
    p = _project(tmp_path)
    _review_all(p)
    finding = _open(p)
    approvals = (p / "production/approvals.jsonl").read_bytes()
    sources = _bytes(p)
    _reimport(p, tmp_path, "I001", seed=64)
    # The reimport itself changes the timeline, not lyrics or the old reviews.
    assert _bytes(p)["input/lyrics.txt"] == sources["input/lyrics.txt"]
    assert _bytes(p)[read(p / "project.yaml")["audio"]["path"]] \
        == sources[read(p / "project.yaml")["audio"]["path"]]
    record_revision(p, finding["finding_id"], reviewer=AUTHOR,
                    role="AUTHOR_SELF_CHECK", note="I replaced the frames")
    current = compare_finding(p, finding["finding_id"])["after"]["artifact_sha256"]
    with pytest.raises(FilmError, match="작성자 자기 확인은 독립 검토나 감독 승인이 아닙니다"):
        acknowledge(p, finding["finding_id"], role="AUTHOR_SELF_CHECK",
                    reviewer=AUTHOR, decision="FINDING_RESOLVED",
                    artifact_sha256=current, note="I fixed it")
    with pytest.raises(FilmError, match="작성자 자기 확인은 독립 검토나 감독 승인이 아닙니다"):
        acknowledge(p, finding["finding_id"], role="AUTHOR_SELF_CHECK",
                    reviewer=AUTHOR, decision="CUT_PASS",
                    artifact_sha256=current, note="I approve it",
                    methods=METHODS)
    checked = acknowledge(p, finding["finding_id"], role="AUTHOR_SELF_CHECK",
                          reviewer=AUTHOR, decision="CHECKED",
                          artifact_sha256=current, note="I looked at my fix")
    assert checked["resolves_finding"] is False
    assert checked["grants_cut_pass"] is False
    assert checked["grants_final_approval"] is False
    view = compare_finding(p, finding["finding_id"])
    assert view["author_self_check"] is True
    assert view["independent_review"] is False
    assert view["director_approval"] is False
    assert view["finding_resolved"] is False
    assert view["finding_state"] == "SELF_CHECKED"
    assert view["cut_pass_covers_current"] is False
    assert view["full_playback_required"] is True
    assert view["final_approval"] is False
    assert render_pass_status(p, "I001")["animation_review_state"] == "STALE"
    assert (p / "production/approvals.jsonl").read_bytes() == approvals
    with pytest.raises(FilmError, match="독립 검토는 감독의 컷 PASS가 아닙니다"):
        acknowledge(p, finding["finding_id"], role="INDEPENDENT_REVIEW",
                    reviewer=REVIEWER, decision="CUT_PASS",
                    artifact_sha256=current, note="reviewer passing the cut",
                    methods=METHODS)
    resolved = acknowledge(p, finding["finding_id"], role="INDEPENDENT_REVIEW",
                           reviewer=REVIEWER, decision="FINDING_RESOLVED",
                           artifact_sha256=current,
                           note="the cited frames no longer flicker")
    assert resolved["resolves_finding"] is True
    assert resolved["grants_cut_pass"] is False
    view = compare_finding(p, finding["finding_id"])
    assert view["finding_resolved"] is True
    assert view["independent_review"] is True
    assert view["director_approval"] is False
    assert view["author_self_check"] is True
    assert view["cut_pass_covers_current"] is False
    assert view["full_playback_required"] is True
    assert view["final_approval"] is False
    assert render_pass_status(p, "I001")["animation_review_state"] == "STALE"
    assert render_pass_status(p, "I002")["animation_review_state"] == "CURRENT"
    assert (p / "production/approvals.jsonl").read_bytes() == approvals
    stored = load_log(p)
    assert [row["pass_inherited"] for row in stored
            if "pass_inherited" in row] == [False, False, False]


def test_director_cut_pass_is_not_final_and_keeps_other_cuts(tmp_path):
    p = _project(tmp_path)
    _review_all(p)
    finding = _open(p)
    _reimport(p, tmp_path, "I001", seed=55)
    record_revision(p, finding["finding_id"], reviewer=AUTHOR,
                    role="AUTHOR_SELF_CHECK", note="new frames")
    current = compare_finding(p, finding["finding_id"])["after"]["artifact_sha256"]
    sources = _bytes(p)
    acknowledge(p, finding["finding_id"], role="DIRECTOR_APPROVAL",
                reviewer=DIRECTOR, decision="CUT_PASS",
                artifact_sha256=current, note="watched the replaced cut",
                methods=METHODS)
    view = compare_finding(p, finding["finding_id"])
    assert view["director_approval"] is True
    assert view["cut_pass_covers_current"] is True
    assert view["finding_resolved"] is False
    assert view["full_playback_required"] is True
    assert view["final_approval"] is False
    assert view["pass_inherited"] is False
    status = review_status(p)
    assert status["targets"]["I001"]["state"] == "CURRENT"
    assert status["targets"]["I002"]["state"] == "CURRENT"
    assert status["targets"]["I003"]["state"] == "CURRENT"
    assert _bytes(p)["input/lyrics.txt"] == sources["input/lyrics.txt"]
    audio = read(p / "project.yaml")["audio"]["path"]
    assert _bytes(p)[audio] == sources[audio]
    assert all(row["scope"] != "FINAL_FILM" for row in load_reviews(p))
    with pytest.raises(FilmError, match="승계되지 않습니다"):
        carry_pass(p, "I001")


def test_other_cut_revision_is_rejected(tmp_path):
    p = _project(tmp_path)
    finding = _open(p)
    _reimport(p, tmp_path, "I001", seed=33)
    view = compare_finding(p, finding["finding_id"])
    record_revision(p, finding["finding_id"], reviewer=AUTHOR,
                    role="AUTHOR_SELF_CHECK", note="replaced S001")
    other = compare_finding(p, finding["finding_id"])
    # I002's adopted digest is a different cut's revision.
    i002 = next(row for row in diff_view(p)["entries"]
                if row["instance_id"] == "I002")
    with pytest.raises(FilmError, match="다른 컷의 리비전"):
        acknowledge(p, finding["finding_id"], role="INDEPENDENT_REVIEW",
                    reviewer=REVIEWER, decision="FINDING_RESOLVED",
                    artifact_sha256=i002["artifact_sha256"],
                    note="close it against the other cut")
    with pytest.raises(FilmError, match="다른 컷의 리비전"):
        acknowledge(p, finding["finding_id"], role="INDEPENDENT_REVIEW",
                    reviewer=REVIEWER, decision="FINDING_RESOLVED",
                    artifact_sha256=other["after"]["artifact_sha256"],
                    note="name the other cut", instance_id="I002")
    assert compare_finding(p, finding["finding_id"])["finding_resolved"] is False
    assert view["after"]["artifact_sha256"] != i002["artifact_sha256"]


def test_comparison_separates_resolution_from_full_playback(tmp_path):
    p = _project(tmp_path)
    finding = _open(p, span=(0, 2), problem="CONTACT")
    _retouch(p, tmp_path, "I001", index=0)
    view = compare_finding(p, finding["finding_id"])
    assert 0 in view["changed_member_indices"]
    assert view["finding_frames_changed"] is True
    assert view["finding_resolved"] is False
    assert view["full_playback_required"] is True
    assert "I002" not in view["closure"]["needed_members"]
    record_revision(p, finding["finding_id"], reviewer=AUTHOR,
                    role="AUTHOR_SELF_CHECK", note="redrew the contact")
    stored = next(row for row in load_log(p)
                  if row["document_type"] == "review_revision")
    assert stored["finding_resolved"] is False
    assert stored["pass_inherited"] is False
    assert stored["full_playback_required"] is True
    assert stored["closure"]["affected_instances"] == ["I001"]
    current = view["after"]["artifact_sha256"]
    acknowledge(p, finding["finding_id"], role="DIRECTOR_APPROVAL",
                reviewer=DIRECTOR, decision="FINDING_RESOLVED",
                artifact_sha256=current, note="the contact frames are fixed")
    after = compare_finding(p, finding["finding_id"])
    assert after["finding_resolved"] is True
    assert after["director_approval"] is True
    assert after["cut_pass_covers_current"] is False
    assert after["full_playback_required"] is True
    assert after["final_approval"] is False
    assert "전체 재생" in after["full_playback_reason"]
