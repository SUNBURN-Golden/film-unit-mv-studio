"""Audit fixes 20261009-1630: open ANIM-015 acceptance, provisional
document types, and the FINAL_FILM approver rule.

Fixtures are synthetic. Nothing here is a hardware encode, a paid call,
or a real artwork approval. Qualification stays UNQUALIFIED.
"""
import json
from pathlib import Path

import pytest

from engine import cli
from engine.anim015_status import anim015_acceptance_status
from engine.animation_review import film_review_status, record_film_review
from engine.animation_schema import (PROVISIONAL_DOCUMENT_TYPES,
                                     check_document)
from engine.core import FilmError
from engine.w00_gate import approve_delivery, record_delegation
from test_anim_006 import FILM_METHODS, approved_project, make_build
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
ADR_0001 = ROOT / "docs" / "adr" / "0001-frame-animation-v1-schemas.md"
ADR_0002 = ROOT / "docs" / "adr" / "0002-provisional-document-types.md"
DECISION = ROOT / "docs" / "evidence" / "anim-015" / "encoding-decision.json"
GUIDE = ROOT / "docs" / "FRAME_ANIMATION_V1_USER_GUIDE_KO.md"


def test_anim015_chat_waiver_does_not_close_acceptance():
    status = anim015_acceptance_status(ROOT)
    assert status["node_report"] == "MERGED_WITH_OPEN_ACCEPTANCE"
    assert status["acceptance_item"] == "NO_FFMPEG_ENCODING"
    assert status["acceptance_state"] == "OPEN"
    assert status["demonstration"] == "NOT_DEMONSTRATED"
    assert status["chat_note_recorded"] is True
    assert status["chat_note_closes_acceptance"] is False
    assert status["plan_ratified"] is False
    assert status["pinned_acceptance_present"] is True
    assert status["adr_requires_real_native_encode"] is True
    assert status["qualification_state"] == "UNQUALIFIED"
    assert status["release_state"] == "NOT_AUTHORIZED"
    decision = json.loads(DECISION.read_text(encoding="utf-8"))
    assert decision["requirement_state"] == "WAIVED"
    assert decision["acceptance_closure"] == "OPEN"
    assert decision["node_report"] == "MERGED_WITH_OPEN_ACCEPTANCE"
    assert decision["waiver_closes_pinned_acceptance"] is False
    assert decision["plan_ratified"] is False
    assert decision["facets"]["NO_FFMPEG_ENCODING"]["state"] == "WAIVED"
    text = (ROOT / "docs" / "ANIM_015_ENCODING_DECISION_KO.md").read_text(
        encoding="utf-8")
    assert "MERGED_WITH_OPEN_ACCEPTANCE" in text
    assert "채팅 면제를 노드 종료나 자격 완료로 세지 않는다" in text


def test_provisional_types_are_specified_and_version_gated():
    amendment = ADR_0002.read_text(encoding="utf-8")
    locked = ADR_0001.read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")
    assert "PENDING" in amendment
    assert "0001" in amendment
    assert len(PROVISIONAL_DOCUMENT_TYPES) == 15
    for name in PROVISIONAL_DOCUMENT_TYPES:
        assert f"`{name}`" in amendment
        assert name not in locked
        assert name in guide
        with pytest.raises(FilmError):
            check_document({"document_type": name, "schema_version": 2})
        with pytest.raises(FilmError):
            check_document({"document_type": name, "schema_version": True})
        check_document({"document_type": name, "schema_version": 1})


def test_arbitrary_reviewer_never_yields_current_film_approval(tmp_path):
    p = approved_project(tmp_path)
    _folder, record, _result = make_build(p)
    build_id = record["build_id"]
    with pytest.raises(FilmError, match="박준태"):
        record_film_review(p, build_id, reviewer="anyone",
                           methods=FILM_METHODS)
    assert film_review_status(p, build_id)["state"] == "UNREVIEWED"
    record_film_review(p, build_id, reviewer="anyone", methods=FILM_METHODS,
                       allow_ungoverned=True)
    ungoverned = film_review_status(p, build_id)
    assert ungoverned["state"] == "UNGOVERNED"
    assert ungoverned["state"] != "CURRENT"
    assert ungoverned["review_id"] is None
    # A synthetic delivery stored under the primary name would be
    # indistinguishable from a human approval: film_review_status reads
    # only the reviewer name. That combination is refused.
    with pytest.raises(FilmError, match="SYNTHETIC_FIXTURE"):
        approve_delivery(p, build_id, "MASTER_SUBBED.mp4",
                         approver="박준태",
                         reviewer_kind="SYNTHETIC_FIXTURE")
    assert film_review_status(p, build_id)["state"] == "UNGOVERNED"
    synthetic = approve_delivery(
        p, build_id, "MASTER_SUBBED.mp4",
        approver="synthetic fixture reviewer",
        reviewer_kind="SYNTHETIC_FIXTURE")
    assert synthetic["approval"]["reviewer_kind"] == "SYNTHETIC_FIXTURE"
    assert synthetic["review"]["reviewer"] != "박준태"
    assert film_review_status(p, build_id)["state"] == "UNGOVERNED"
    assert cli.main(["approve-film", str(p), "--build", build_id,
                     "--reviewer", "박준태",
                     "--reviewer-kind", "SYNTHETIC_FIXTURE",
                     "--methods",
                     "FULL_SPEED_WHOLE_FILM,TECHNICAL_VALIDATION"]) == 1
    assert film_review_status(p, build_id)["state"] == "UNGOVERNED"

    primary = record_film_review(p, build_id, reviewer="박준태",
                                 methods=FILM_METHODS)
    current = film_review_status(p, build_id)
    assert current["state"] == "CURRENT"
    assert current["review_id"] == primary["review_id"]

    record_delegation(p, delegate="Fixture Delegate",
                      scope=["DELIVERY_APPROVAL"], expires_at_ms=5_000,
                      now_ms=1_000)
    delegated = record_film_review(p, build_id, reviewer="Fixture Delegate",
                                   methods=FILM_METHODS, now_ms=1_000)
    assert film_review_status(p, build_id, now_ms=1_000)["state"] \
        == "CURRENT"
    assert film_review_status(p, build_id, now_ms=1_000)["review_id"] \
        == delegated["review_id"]
    expired = film_review_status(p, build_id, now_ms=9_000)
    assert expired["state"] == "UNGOVERNED"
    assert expired["state"] != "CURRENT"

    assert cli.main(["approve-film", str(p), "--build", build_id,
                     "--reviewer", "anyone", "--methods",
                     "FULL_SPEED_WHOLE_FILM,TECHNICAL_VALIDATION"]) == 1
    assert film_review_status(p, build_id, now_ms=9_000)["state"] \
        != "CURRENT"
    assert cli.main(["approve-film", str(p), "--build", build_id,
                     "--reviewer", "박준태", "--methods",
                     "FULL_SPEED_WHOLE_FILM,TECHNICAL_VALIDATION"]) == 0
    assert film_review_status(p, build_id)["state"] == "CURRENT"


def test_legacy_approve_film_stays_refused(tmp_path):
    legacy = fixture_project(tmp_path, seconds=1, shot_count=1)
    assert cli.main(["approve-film", str(legacy), "--build", "B0001",
                     "--reviewer", "박준태", "--methods",
                     "FULL_SPEED_WHOLE_FILM,TECHNICAL_VALIDATION"]) == 1
