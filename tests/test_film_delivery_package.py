"""film-delivery-package: bundle a sealed picture with proof of what it is.

Synthetic Pillow frames and tiny byte masters. No network, no paid call,
no real artwork approval. Filename "Final" is not an approval input.
"""
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from engine.animation_review import load_reviews, record_film_review
from engine.cli import main as cli_main
from engine.core import FilmError, digest, write
from engine.delivery_package import (assemble_bundle, delivery_status,
                                     verify_bundle)
from engine.frame_clock import frame_filename
from engine.media_verify import frame_pixel_sha256, sequence_root

TOKEN = "ya29.a0AfH6SMBsecretvalue"
EMAIL = "private.person@example.com"
HOME = "/home/private/diary.txt"
AUDIO_MARK = b"ORIGINAL-AUDIO-MARKER"


def _sha_file(path):
    return digest(path)


def _project(tmp, *, profile="FRAME_ANIMATION_V1"):
    p = tmp / "proj"
    p.mkdir()
    audio = p / "audio"
    audio.mkdir()
    (audio / "master.wav").write_bytes(AUDIO_MARK)
    (p / "input").mkdir()
    (p / "input" / "lyrics.txt").write_text("Synthetic fixture line\n",
                                            encoding="utf-8")
    write(p / "project.yaml", {
        "production_profile": profile,
        "name": "delivery_fixture",
        "format": {"width": 8, "height": 8, "fps": 24},
        "audio": {"path": "audio/master.wav",
                  "sha256": _sha_file(audio / "master.wav")}})
    return p


def _seal(project, build_id="B0001", *, mode="FINAL_CANDIDATE", draft=False,
          candidate="FINAL_CANDIDATE_READY", profile="MV_H264_AAC_V1",
          frames=1, subbed=b"subbed-master-bytes",
          ass="[Script Info]\nTitle: fixture\n",
          output_sha=True):
    folder = project / "builds" / build_id
    final = folder / "final_frames"
    final.mkdir(parents=True)
    digests = []
    for index in range(frames):
        image = Image.new("RGB", (8, 8), (index + 1, 40, 80))
        image.save(final / frame_filename(index))
        digests.append(frame_pixel_sha256(final / frame_filename(index)))
    root = sequence_root("subbed", digests)
    (folder / "MASTER_CLEAN.mp4").write_bytes(b"clean-master-bytes")
    (folder / "MASTER_SUBBED.mp4").write_bytes(subbed)
    (folder / "lyrics.ass").write_text(ass, encoding="utf-8")
    (folder / "lyrics.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSynthetic fixture line\n",
        encoding="utf-8")
    snap = folder / "snapshot"
    snap.mkdir()
    (snap / "master.wav").write_bytes(AUDIO_MARK)
    write(folder / "lyrics_timed.json",
          {"review": {"lyrics_review_sha256": None}})
    clean_sha = _sha_file(folder / "MASTER_CLEAN.mp4")
    subbed_sha = _sha_file(folder / "MASTER_SUBBED.mp4")
    record = {
        "document_type": "animation_build",
        "schema_version": 2,
        "build_id": build_id,
        "mode": mode,
        "draft": draft,
        "status": "COMPLETE",
        "candidate_state": candidate,
        "profile": "FRAME_ANIMATION_V1",
        "storage_profile": "LOCAL_FULL",
        "format": {"fps": 24, "width": 8, "height": 8},
        "frame_clock": {"fps": {"num": 24, "den": 1},
                        "output_frames": frames},
        "output_frames": frames,
        "edit_digest": "ab" * 32,
        "audio": {"sha256": _sha_file(snap / "master.wav"),
                  "path": "snapshot/master.wav", "codec": "aac"},
        "lyrics": {"source_sha256": _sha_file(project / "input/lyrics.txt"),
                   "lyrics_review_sha256": None},
        "subtitles": {
            "ass": "lyrics.ass", "srt": "lyrics.srt",
            "ass_sha256": _sha_file(folder / "lyrics.ass"),
            "srt_sha256": _sha_file(folder / "lyrics.srt"),
            "font": {"sha256": None}},
        "sequences": {"delivery_dir": "final_frames", "frame_count": frames,
                      "subbed_sequence_root": root,
                      "clean_sequence_root": root},
        "outputs": {
            "clean": {"file": "MASTER_CLEAN.mp4",
                      "sha256": clean_sha if output_sha else "0" * 64},
            "subbed": {"file": "MASTER_SUBBED.mp4",
                       "sha256": subbed_sha if output_sha else "0" * 64}},
        "encoding": {"delivery_profile": profile, "driver": "FFMPEG"},
        "completed_at": "2026-01-01T00:00:00+00:00"}
    record["files"] = {
        path.relative_to(folder).as_posix(): _sha_file(path)
        for path in sorted(folder.rglob("*"))
        if path.is_file()}
    write(folder / "build.json", record)
    return folder


def _approve(project, build_id="B0001"):
    return record_film_review(
        project, build_id, reviewer="박준태",
        methods=["FULL_SPEED_WHOLE_FILM", "TECHNICAL_VALIDATION"])


def _tree_text(root):
    chunks = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def test_filename_final_does_not_change_approval(tmp_path):
    project = _project(tmp_path)
    plain = _seal(project, "B0001", mode="PREVIEW", draft=True,
                  candidate="FINAL_CANDIDATE_READY")
    named = _seal(project, "B0002", mode="PREVIEW", draft=True,
                  candidate="FINAL_CANDIDATE_READY")
    (named / "Final.mp4").write_bytes(b"this file is named Final")
    (named / "My_Final_Master.mp4").write_bytes(b"APPROVED RELEASED")
    without = delivery_status(project, "B0001")
    with_name = delivery_status(project, "B0002")
    assert without["states"] == with_name["states"]
    assert with_name["states"]["preview"] == "PREVIEW"
    assert with_name["states"]["final_candidate"] == "NOT_A_CANDIDATE"
    assert with_name["states"]["director_adoption"] == "NOT_ADOPTED"
    assert with_name["states"]["external_publication"] == "NOT_AUTHORIZED"
    assert with_name["states"]["artwork_acceptance"] == "PENDING"
    assert with_name["states"]["filename_is_not_approval"] is True
    bundled = assemble_bundle(project, "B0002", tmp_path / "named")
    assert bundled["states"]["director_adoption"] == "NOT_ADOPTED"
    names = [member["path"] for member in
             json.loads((tmp_path / "named" / "bundle.json").read_text())
             ["members"]]
    assert not any("Final" in name for name in names)
    assert plain.name == "B0001"


def test_candidate_name_is_not_adoption(tmp_path):
    project = _project(tmp_path)
    _seal(project, "B0001")
    _seal(project, "B0002")
    (project / "builds" / "B0002" / "Final.mp4").write_bytes(b"Final")
    left = delivery_status(project, "B0001")
    right = delivery_status(project, "B0002")
    assert left["states"]["final_candidate"] == "FINAL_CANDIDATE_READY"
    assert right["states"] == left["states"]
    assert right["states"]["director_adoption"] == "NOT_ADOPTED"
    assert right["facets"]["qualification_state"] == "UNQUALIFIED"
    assert right["facets"]["release_state"] == "NOT_AUTHORIZED"


def test_artifact_hash_matches_current_scope(tmp_path):
    project = _project(tmp_path)
    _seal(project, "B0001")
    review = _approve(project, "B0001")
    result = assemble_bundle(project, "B0001", tmp_path / "bundle")
    document = json.loads((tmp_path / "bundle" / "bundle.json").read_text())
    scope = json.loads((tmp_path / "bundle" / "review" / "scope.json")
                       .read_text())
    assert result["scope_match"] == "MATCH"
    assert result["states"]["director_adoption"] == "PROTOCOL_ADOPTED"
    assert result["states"]["artwork_acceptance"] == "PENDING"
    assert result["states"]["external_publication"] == "NOT_AUTHORIZED"
    assert result["facets"]["qualification_state"] == "UNQUALIFIED"
    subbed = next(m for m in document["members"] if m["role"] == "subbed_master")
    compared = next(row for row in scope["compared"]
                    if row["role"] == "subbed_master")
    assert subbed["scope_class"] == "IN_SCOPE"
    assert subbed["sha256"] == review["deliverable_sha256"]
    assert subbed["sha256"] == compared["artifact_sha256"]
    assert subbed["sha256"] == compared["scope_sha256"]
    assert compared["match"] is True
    assert scope["inherited_reviews"] is False
    assert scope["locks_are_not_approval"] is True
    clean = next(m for m in document["members"] if m["role"] == "clean_master")
    assert clean["scope_class"] == "OUT_OF_SCOPE"


def test_hash_mismatch_is_not_current_scope(tmp_path):
    project = _project(tmp_path)
    folder = _seal(project, "B0001")
    _approve(project, "B0001")
    target = folder / "MASTER_SUBBED.mp4"
    target.write_bytes(target.read_bytes() + b"!")
    status = delivery_status(project, "B0001")
    assert status["scope_match"] == "MISMATCH"
    assert status["states"]["director_adoption"] == "NOT_ADOPTED"
    assert status["states"]["final_candidate"] == "NOT_A_CANDIDATE"
    result = assemble_bundle(project, "B0001", tmp_path / "bundle")
    scope = json.loads((tmp_path / "bundle" / "review" / "scope.json")
                       .read_text())
    assert result["scope_match"] == "MISMATCH"
    assert result["approval_consistent"] is False
    compared = next(row for row in scope["compared"]
                    if row["role"] == "subbed_master")
    assert compared["match"] is False
    assert compared["artifact_sha256"] != compared["scope_sha256"]
    document = json.loads((tmp_path / "bundle" / "bundle.json").read_text())
    subbed = next(m for m in document["members"] if m["role"] == "subbed_master")
    assert subbed["scope_class"] == "OUT_OF_SCOPE"
    assert subbed["sha256"] == compared["artifact_sha256"]


def test_other_build_review_and_lock_are_not_inherited(tmp_path):
    project = _project(tmp_path)
    _seal(project, "B0001")
    _seal(project, "B0002")
    _approve(project, "B0002")
    write(project / "manifest" / "animation_locks.json", {
        "note": "old FINAL_LOCK", "approver_email": EMAIL})
    before = (project / "manifest" / "animation_locks.json").read_bytes()
    status = delivery_status(project, "B0001")
    assert status["scope_match"] == "NO_CURRENT_SCOPE"
    assert status["states"]["director_adoption"] == "NOT_ADOPTED"
    result = assemble_bundle(project, "B0001", tmp_path / "bundle")
    assert result["states"]["director_adoption"] == "NOT_ADOPTED"
    blob = _tree_text(tmp_path / "bundle")
    assert EMAIL.encode() not in blob
    assert b"FINAL_LOCK" not in blob
    assert (project / "manifest" / "animation_locks.json").read_bytes() == before
    assert len(load_reviews(project)) == 1
    assert load_reviews(project)[0]["build_id"] == "B0002"


def test_secrets_and_personal_data_stay_out(tmp_path):
    project = _project(tmp_path)
    folder = _seal(project, "B0001")
    (project / ".env").write_text(
        "API_KEY=sk-testsecretvalue1234567890\n", encoding="utf-8")
    (project / "secrets").mkdir()
    (project / "secrets" / "token.json").write_text(
        json.dumps({"access_token": TOKEN}), encoding="utf-8")
    (project / "personal.txt").write_text(
        f"reach me at {EMAIL} via {HOME}\n", encoding="utf-8")
    (folder / "credentials.json").write_text(
        json.dumps({"token": TOKEN, "home": HOME}), encoding="utf-8")
    (folder / "Final.mp4").write_bytes(TOKEN.encode())
    lyrics_before = (project / "input" / "lyrics.txt").read_bytes()
    audio_before = (project / "audio" / "master.wav").read_bytes()
    result = assemble_bundle(project, "B0001", tmp_path / "bundle")
    blob = _tree_text(tmp_path / "bundle")
    for needle in (TOKEN, EMAIL, HOME, "sk-testsecretvalue", "API_KEY",
                   "credentials"):
        assert needle.encode() not in blob
    assert AUDIO_MARK not in blob
    assert (project / "input" / "lyrics.txt").read_bytes() == lyrics_before
    assert (project / "audio" / "master.wav").read_bytes() == audio_before
    assert result["states"]["external_publication"] == "NOT_AUTHORIZED"
    sources = json.loads((tmp_path / "bundle" / "bundle.json").read_text())[
        "sources"]
    audio = next(row for row in sources if row["role"] == "audio")
    lyrics = next(row for row in sources if row["role"] == "lyrics_source")
    assert audio["preservation"] == "PRESERVED"
    assert lyrics["preservation"] == "PRESERVED"


def test_subtitle_email_refuses_bundle_without_editing_cues(tmp_path):
    project = _project(tmp_path)
    folder = _seal(project, "B0001",
                   ass=f"[Script Info]\nTitle: {EMAIL}\n")
    before = (folder / "lyrics.ass").read_bytes()
    with pytest.raises(FilmError, match="email"):
        assemble_bundle(project, "B0001", tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert (folder / "lyrics.ass").read_bytes() == before
    assert (project / "input" / "lyrics.txt").read_text(
        encoding="utf-8") == "Synthetic fixture line\n"


def test_symlink_master_is_refused(tmp_path):
    project = _project(tmp_path)
    folder = _seal(project, "B0001")
    secret = tmp_path / "secret.txt"
    secret.write_text(TOKEN, encoding="utf-8")
    master = folder / "MASTER_SUBBED.mp4"
    master.unlink()
    master.symlink_to(secret)
    with pytest.raises(FilmError, match="symlink"):
        assemble_bundle(project, "B0001", tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert secret.read_text(encoding="utf-8") == TOKEN


def test_qc_personal_fields_are_dropped_and_hash_is_bound(tmp_path):
    project = _project(tmp_path)
    folder = _seal(project, "B0001")
    subbed = _sha_file(folder / "MASTER_SUBBED.mp4")
    write(project / "qc" / "diagnostics" / "B0001.json", {
        "report_type": "quality_diagnostics",
        "not_an_approval": True,
        "summary": {"DEFECT": 0, "CANDIDATE": 1},
        "subject": {"path": HOME, "contact": EMAIL},
        "artifacts": {"MASTER_SUBBED.mp4": {"sha256": subbed, "path": HOME}},
        "findings": [{
            "id": "F1", "kind": "SILENCE_RUN", "severity": "CANDIDATE",
            "artifact": "MASTER_SUBBED.mp4",
            "detail": EMAIL, "repro": "ffmpeg -i " + HOME}]})
    assemble_bundle(project, "B0001", tmp_path / "bundle")
    qc = json.loads((tmp_path / "bundle" / "qc" / "diagnostics.json")
                    .read_text())
    blob = _tree_text(tmp_path / "bundle")
    assert EMAIL.encode() not in blob
    assert HOME.encode() not in blob
    assert qc["not_an_approval"] is True
    assert qc["artifact_sha256"]["MASTER_SUBBED.mp4"] == subbed
    assert qc["findings"] == [{
        "id": "F1", "kind": "SILENCE_RUN", "severity": "CANDIDATE",
        "artifact": "MASTER_SUBBED.mp4"}]
    document = json.loads((tmp_path / "bundle" / "bundle.json").read_text())
    assert document["qc"]["binding"] == "MATCH"
    assert document["qc"]["not_an_approval"] is True


def test_download_integrity_and_reproducible_members(tmp_path):
    project = _project(tmp_path)
    folder = _seal(project, "B0001")
    first = assemble_bundle(project, "B0001", tmp_path / "one")
    second = assemble_bundle(project, "B0001", tmp_path / "two")
    def members(path):
        document = json.loads((path / "bundle.json").read_text())
        return {m["role"]: m["sha256"] for m in document["members"]
                if m["role"] != "review_scope"}
    assert members(tmp_path / "one") == members(tmp_path / "two")
    assert members(tmp_path / "one")["subbed_master"] == _sha_file(
        folder / "MASTER_SUBBED.mp4")
    downloaded = tmp_path / "downloaded"
    shutil.copytree(tmp_path / "one", downloaded)
    assert verify_bundle(downloaded)["valid"] is True
    thumb = downloaded / "media" / "thumbnail.png"
    thumb.write_bytes(thumb.read_bytes() + b"\x00")
    checked = verify_bundle(downloaded)
    assert checked["valid"] is False
    assert any("thumbnail" in error for error in checked["errors"])
    shutil.copytree(tmp_path / "two", tmp_path / "extra")
    (tmp_path / "extra" / ".env").write_text("API_KEY=sk-leakedvalue123456\n",
                                             encoding="utf-8")
    leaked = verify_bundle(tmp_path / "extra")
    assert leaked["valid"] is False
    assert any("extra" in error for error in leaked["errors"])
    assert first["valid"] is True and second["valid"] is True


def test_profile_and_work_contract_are_reported_honestly(tmp_path):
    project = _project(tmp_path)
    _seal(project, "B0001")
    good = assemble_bundle(project, "B0001", tmp_path / "good")
    document = json.loads((tmp_path / "good" / "bundle.json").read_text())
    assert good["profile_check"] == "MATCH"
    assert document["delivery_profile"] == "MV_H264_AAC_V1"
    assert document["work_contract"] == {"fps": {"num": 24, "den": 1},
                                         "seconds": 240, "frames": 5760}
    assert document["meets_work_contract"] is False
    assert document["delivery_clock"]["frames"] == 1
    assert document["states"]["external_publication"] == "NOT_AUTHORIZED"
    assert good["thumbnail"] == "FROM_DELIVERY_FRAME"
    roles = {m["role"] for m in document["members"]}
    assert {"clean_master", "subbed_master", "thumbnail", "subtitle_ass",
            "subtitle_srt", "review_scope"} <= roles
    _seal(project, "B0002", output_sha=False)
    bad = assemble_bundle(project, "B0002", tmp_path / "bad")
    assert bad["profile_check"] == "HASH_MISMATCH"
    assert bad["states"]["director_adoption"] == "NOT_ADOPTED"
    live = (project / "input" / "lyrics.txt").read_bytes()
    (project / "input" / "lyrics.txt").write_bytes(live + b"\n")
    diverged = assemble_bundle(project, "B0001", tmp_path / "diverged")
    sources = json.loads(
        (tmp_path / "diverged" / "bundle.json").read_text())["sources"]
    lyrics = next(row for row in sources if row["role"] == "lyrics_source")
    assert lyrics["preservation"] == "DIVERGED"
    assert (project / "builds" / "B0001" / "lyrics.ass").read_text(
        encoding="utf-8").startswith("[Script Info]")
    assert diverged["states"]["artwork_acceptance"] == "PENDING"


def test_legacy_project_is_unchanged(tmp_path):
    project = _project(tmp_path, profile="LEGACY_MV")
    (project / "input" / "lyrics.txt").write_text("original lyric line\n",
                                                  encoding="utf-8")
    before = (project / "input" / "lyrics.txt").read_bytes()
    with pytest.raises(FilmError, match="LEGACY_MV"):
        delivery_status(project, "B0001")
    with pytest.raises(FilmError, match="LEGACY_MV"):
        assemble_bundle(project, "B0001", tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert (project / "input" / "lyrics.txt").read_bytes() == before
    assert not (project / "builds").exists()
    assert cli_main(["delivery-bundle", str(project), "B0001",
                     "--output", str(tmp_path / "cli")]) == 1
    assert not (tmp_path / "cli").exists()


def test_cli_status_bundle_and_verify(tmp_path, capsys):
    project = _project(tmp_path)
    _seal(project, "B0001")
    assert cli_main(["delivery-status", str(project), "B0001"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["states"]["filename_is_not_approval"] is True
    assert status["meets_work_contract"] is False
    output = tmp_path / "cli-bundle"
    assert cli_main(["delivery-bundle", str(project), "B0001",
                     "--output", str(output)]) == 0
    bundled = json.loads(capsys.readouterr().out)
    assert bundled["scope_match"] == "NO_CURRENT_SCOPE"
    assert cli_main(["delivery-verify", str(output)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["valid"] is True
    assert checked["facets"]["release_state"] == "NOT_AUTHORIZED"
