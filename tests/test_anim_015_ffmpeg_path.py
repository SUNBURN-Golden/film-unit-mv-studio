"""anim-015-ffmpeg-path: the encoding-decision record stays honest.

Fast file checks only. No network, no encode, no driver probe.
"""
import json
from pathlib import Path

from engine.animation_schema import read_canon
from engine.encoder_backends import get_driver
from engine.encoder_backends.ffmpeg import FFmpegDriver

ROOT = Path(__file__).resolve().parents[1]
DECISION = ROOT / "docs" / "evidence" / "anim-015" / "encoding-decision.json"
DOC = ROOT / "docs" / "ANIM_015_ENCODING_DECISION_KO.md"
STORAGE = ROOT / "docs" / "FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"

NON_FFMPEG_FACETS = (
    "NO_FFMPEG_ENCODING",
    "NO_FFMPEG_RUNTIME",
    "GSTREAMER",
    "NVIDIA_NATIVE",
    "VIDEOTOOLBOX_NATIVE",
    "SERVICE",
)
FORBIDDEN_STATES = ("QUALIFIED_FOR_SCOPE", "PARTIAL", "PASS")
FACET_KEYS = (
    "FFMPEG",
    "GSTREAMER",
    "NVIDIA_NATIVE",
    "VIDEOTOOLBOX_NATIVE",
    "SERVICE",
    "NO_FFMPEG_ENCODING",
    "NO_FFMPEG_RUNTIME",
)


def test_encoding_decision_json_is_canonical():
    document = read_canon(DECISION)
    assert document["schema"] == 1
    assert document["node"] == "anim-015"
    assert document["decision_id"] == "ANIM015-ENC-20261008"
    assert document["source_head"] == "228cf715b5678dbc01bc7e40be891ef3c8f96b3c"
    assert document["replaced_plan"] == (
        "anim-015-gstreamer-ci (issue #95, cancelled before build)")
    assert isinstance(document["commands"], list) and document["commands"]
    assert all(isinstance(command, str) and command for command in
               document["commands"])
    assert set(document["facets"]) == set(FACET_KEYS)
    for facet in document["facets"].values():
        assert set(facet) == {"evidence", "scope", "state"}


def test_decision_identity_is_exact():
    document = read_canon(DECISION)
    assert document["decided_by"] == "JunTae (project owner)"
    assert document["decided_at"] == "2026-10-08T18:42:00+09:00"
    assert document["instruction"] == "FFmpeg 넣어"


def test_requirement_waived_and_non_ffmpeg_facets_never_qualified():
    document = read_canon(DECISION)
    assert document["requirement"] == "NO_FFMPEG_ENCODING"
    assert document["requirement_state"] == "WAIVED"
    facets = document["facets"]
    assert facets["NO_FFMPEG_ENCODING"]["state"] == "WAIVED"
    assert "NOT_DEMONSTRATED" in json.dumps(
        facets["NO_FFMPEG_ENCODING"], ensure_ascii=False)
    assert facets["NO_FFMPEG_RUNTIME"]["state"] == "NOT_DEMONSTRATED"
    for name in ("GSTREAMER", "NVIDIA_NATIVE", "VIDEOTOOLBOX_NATIVE",
                 "SERVICE"):
        assert facets[name]["state"] == "UNQUALIFIED"
    for name in NON_FFMPEG_FACETS:
        blob = json.dumps(facets[name], ensure_ascii=False)
        for token in FORBIDDEN_STATES:
            assert token not in blob
        assert facets[name]["state"] not in FORBIDDEN_STATES


def test_encoding_path_is_existing_ffmpeg_driver():
    document = read_canon(DECISION)
    assert document["encoding_path"] == "FFMPEG"
    ffmpeg = document["facets"]["FFMPEG"]
    assert ffmpeg["state"] == "ENCODING_PATH"
    assert "tests/test_anim_015.py" in ffmpeg["evidence"]
    assert "64x48" in ffmpeg["scope"]
    assert "1920x1080" in ffmpeg["scope"]
    assert "5760" in ffmpeg["scope"]
    driver = get_driver("FFMPEG")
    assert isinstance(driver, FFmpegDriver)
    assert driver.name == "FFMPEG"


def test_license_codec_matches_driver_contract():
    document = read_canon(DECISION)
    codec, _rate = FFmpegDriver().codec_contract({"crf": 18})
    license_note = document["license"]
    assert license_note["current_codec"] == codec
    assert license_note["new_gpl_dependency"] is False
    assert license_note["ffmpeg_invocation"] == "external_process"
    assert license_note["ffmpeg_core"] == "LGPL-2.1-or-later"
    assert license_note["current_codec_license"] == "GPL-2.0-or-later"


def test_ci_installs_ffmpeg_and_not_gstreamer():
    text = CI.read_text(encoding="utf-8").lower()
    assert "ffmpeg" in text
    assert "gstreamer" not in text


def test_decision_doc_mentions_waiver_date_and_links_json():
    text = DOC.read_text(encoding="utf-8")
    assert "2026-10-08" in text
    assert "evidence/anim-015/encoding-decision.json" in text
    assert "FFmpeg 넣어" in text


def test_execution_storage_keeps_criterion_and_notes_waiver():
    text = STORAGE.read_text(encoding="utf-8")
    assert ("NO_FFMPEG_ENCODING 경로 실제 성공; 완전한 NO_FFMPEG_RUNTIME은 "
            "별도 probe·mux/verify 구현 근거") in text
    assert "2026-10-08" in text
    assert "ANIM_015_ENCODING_DECISION_KO.md" in text
    assert "JunTae" in text
