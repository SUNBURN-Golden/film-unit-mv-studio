"""W00 redesign R2: shot briefs from the conti and take check-in against it.

Prompts put every beat in clip-relative seconds and snap the clip length to what
the video app makes; a returned take is checked mechanically, its criteria start
PENDING, a judge answer settles them, and the ledger adds up. Fixtures are
synthetic projects and ffmpeg test patterns — no service, no paid request.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import cli, conti, conti_brief, handoff
from engine.audio import analyze, synth_test_audio
from engine.compiler import import_asset
from engine.core import FilmError, digest, init_project, read
from engine.production import make_package

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="ffmpeg/ffprobe are required")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    audio = synth_test_audio(tmp_path / "t.wav", seconds=30)
    p = init_project(tmp_path, "brief", audio, "A woman remembers her old kitchen.",
                     "첫 줄 가사\n둘째 줄 가사", synthetic=True, aspect="16:9")
    analyze(p)
    make_package(p)
    return p


def _shot(p, shot_id):
    return next(s for s in read(p / "manifest/shots.json") if s["id"] == shot_id)


def _conti(p, shot_id, **overrides):
    shot = _shot(p, shot_id)
    in_ms = shot["in_ms"]
    doc = {"schema_version": 1, "shot_id": shot_id, "updated_at": "2026-10-14T00:00:00Z", "source": "test",
           "scene_goal": "Mina decides to leave the kitchen",
           "lyric_anchors": [{"text": "첫 줄 가사", "start_ms": in_ms + 1000, "end_ms": in_ms + 2500,
                              "intent": "she looks up on the line"}],
           "beat_map": [{"at_ms": in_ms + 500, "hit": "the cup lands on the table"}],
           "framing": "medium close-up, slightly low angle",
           "camera": {"type": "dolly", "moves": [{"at_ms": in_ms + 2000, "action": "slow push in"}]},
           "blocking": [{"character": c, "pose": "standing, weight on one hip", "expression": "quiet resolve",
                         "motion_beats": [{"at_ms": in_ms + 500, "action": "sets the cup down"},
                                          {"at_ms": in_ms + 3200, "action": "turns toward the door"}]}
                        for c in shot.get("characters", [])],
           "lighting": "warm window light from screen left", "palette": ["#ECEAE4", "moss green"],
           "continuity": {"from_previous": "she is already at the table", "to_next": "the cup stays",
                          "props": ["green cup"], "wardrobe": "red coat"},
           "references": [shot["references"][0]],
           "transition_in": "hard cut", "transition_out": "match cut on the cup",
           "acceptance_criteria": ["the red coat is visible", "the cup is set down, not thrown",
                                   "the camera pushes in, never cuts"],
           "open_questions": []}
    doc.update(overrides)
    conti.submit_conti(p, doc)
    return doc


def _clip(path, seconds, source="testsrc", size="640x360"):
    """A tiny clip: testsrc moves every frame; a flat colour never does."""
    pattern = f"{source}=size={size}:rate=24:duration={seconds}" if source == "testsrc" \
        else f"color=c=gray:s={size}:r=24:d={seconds}"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", pattern,
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


# ---- brief --------------------------------------------------------------------------

def test_clip_length_snaps_to_what_the_app_makes():
    assert conti_brief.clip_length({"duration_ms": 5000}) == {"seconds": 5.0, "clip_seconds": 6}
    assert conti_brief.clip_length({"duration_ms": 4000})["clip_seconds"] == 4
    assert conti_brief.clip_length({"duration_ms": 8000})["clip_seconds"] == 8
    assert conti_brief.clip_length({"duration_ms": 8001})["clip_seconds"] is None


def test_brief_without_conti_falls_back_to_the_manifest(project):
    brief = conti_brief.shot_brief(project, "S001")
    assert brief["first_frame_prompt"] is None and brief["conti_sha256"] is None
    assert brief["video_prompt"].startswith("Animate this approved first frame")
    assert brief["checklist"] == [] and "no conti" in brief["warnings"][0]


def test_brief_from_conti_puts_every_beat_in_clip_seconds(project):
    _conti(project, "S002")
    brief = conti_brief.shot_brief(project, "S002")
    shot = _shot(project, "S002")
    seconds = round(shot["duration_ms"] / 1000, 2)
    assert brief["in_ms"] == shot["in_ms"] and brief["clip_seconds"] == 6 and brief["seconds"] == seconds
    motion = brief["video_prompt"]
    assert f"Make a 6 s clip; the first {seconds} s are used." in motion
    assert "at 0.5 s: sets the cup down" in motion and "at 3.2 s: turns toward the door" in motion
    assert "Camera: dolly — at 2.0 s: slow push in" in motion
    assert "On the music: at 0.5 s: the cup lands on the table" in motion
    assert "Lyric 1.0–2.5 s “첫 줄 가사”: she looks up on the line" in motion
    assert "Keep unchanged: green cup; red coat" in motion and "Ends: match cut on the cup" in motion
    assert str(shot["in_ms"]) not in motion, "song-timeline milliseconds must not leak into the clip prompt"
    frame = brief["first_frame_prompt"]
    assert "Framing: medium close-up, slightly low angle" in frame and "Lighting: warm window light" in frame
    assert "Palette: #ECEAE4, moss green" in frame and "No text" in frame
    assert brief["checklist"] == ["the red coat is visible", "the cup is set down, not thrown",
                                  "the camera pushes in, never cuts"]
    assert brief["conti_sha256"] and brief["warnings"] == []
    assert shot["references"][0] in brief["attach"]


def test_brief_warns_when_the_conti_is_unsettled_and_static_has_no_motion(project):
    _conti(project, "S001", open_questions=["which door?"])
    brief = conti_brief.shot_brief(project, "S001")
    assert any(w.startswith("conti not READY") and "open_questions" in w for w in brief["warnings"])
    conti.draft_conti(project, ["S003"])
    shots = read(project / "manifest/shots.json")
    for s in shots:
        if s["id"] == "S003":
            s["render_mode"] = "STATIC"
    (project / "manifest/shots.json").write_text(json.dumps(shots), encoding="utf-8")
    assert conti_brief.shot_brief(project, "S003")["video_prompt"] is None
    with pytest.raises(FilmError, match="Unknown shot"):
        conti_brief.shot_brief(project, "S999")


# ---- take check-in -----------------------------------------------------------------

def test_check_take_passes_a_moving_clip_and_leaves_criteria_pending(project, tmp_path):
    _conti(project, "S001")
    take = import_asset(project, "S001", _clip(tmp_path / "ok.mp4", 6), kind="draft")
    record = conti_brief.check_take(project, "S001", service="flow", units=20, human_minutes=4.5)
    by = {c["check"]: c["status"] for c in record["mechanical"]}
    assert by == {"DURATION": "PASS", "ASPECT": "PASS", "RESOLUTION": "WARN", "MOTION": "PASS"}
    assert record["take_sha256"] == take["sha256"] and record["attempt"] == 1
    assert record["conti_sha256"] == conti_brief.shot_brief(project, "S001")["conti_sha256"]
    assert [c["verdict"] for c in record["criteria"]] == ["PENDING"] * 3 and record["verdict"] == "PENDING"
    assert len(record["frames"]) == 5 and all((project / f).is_file() for f in record["frames"])
    assert record["service"] == "flow" and record["units"] == 20 and record["human_minutes"] == 4.5
    again = conti_brief.check_take(project, "S001")
    assert again["attempt"] == 1, "re-checking the same take is not a new attempt"


def test_check_take_fails_a_short_clip(project, tmp_path):
    _conti(project, "S001")
    import_asset(project, "S001", _clip(tmp_path / "short.mp4", 2), kind="draft")
    record = conti_brief.check_take(project, "S001")
    duration = next(c for c in record["mechanical"] if c["check"] == "DURATION")
    need = _shot(project, "S001")["duration_ms"] / 1000
    assert duration["status"] == "FAIL" and f"2.00 s clip for a {need:.2f} s shot" in duration["detail"]
    assert record["verdict"] == "FAIL"


def test_check_take_fails_a_still_clip_on_a_motion_shot(project, tmp_path):
    _conti(project, "S001")
    import_asset(project, "S001", _clip(tmp_path / "still.mp4", 6, source="color"), kind="draft")
    record = conti_brief.check_take(project, "S001")
    motion = next(c for c in record["mechanical"] if c["check"] == "MOTION")
    assert motion["status"] == "FAIL" and record["verdict"] == "FAIL"


def test_check_take_needs_an_imported_take_and_sane_numbers(project):
    _conti(project, "S001")
    with pytest.raises(FilmError, match="no imported take"):
        conti_brief.check_take(project, "S001")
    with pytest.raises(FilmError, match="units"):
        conti_brief.check_take(project, "S001", units=-1)


def test_new_take_is_the_next_attempt_and_verdicts_settle_it(project, tmp_path):
    _conti(project, "S001")
    import_asset(project, "S001", _clip(tmp_path / "take1.mp4", 6), kind="draft")
    first = conti_brief.check_take(project, "S001", units=20)
    import_asset(project, "S001", _clip(tmp_path / "take2.mp4", 6, size="1920x1080"), kind="draft")
    second = conti_brief.check_take(project, "S001", units=20, human_minutes=3)
    assert second["attempt"] == 2 and second["take_sha256"] != first["take_sha256"]
    assert next(c for c in second["mechanical"] if c["check"] == "RESOLUTION")["status"] == "PASS"
    with pytest.raises(FilmError, match="reviewer"):
        conti_brief.record_verdicts(project, "S001", second["take_sha256"], {1: "PASS"}, "")
    with pytest.raises(FilmError, match="no criterion 9"):
        conti_brief.record_verdicts(project, "S001", second["take_sha256"], {9: "PASS"}, "JunTae")
    partial = conti_brief.record_verdicts(project, "S001", None, {1: "PASS", 2: "PASS"}, "JunTae")
    assert partial["verdict"] == "PENDING" and partial["take_sha256"] == second["take_sha256"]
    settled = conti_brief.record_verdicts(project, "S001", second["take_sha256"],
                                          {3: {"verdict": "PASS", "reason": "push-in visible across samples"}}, "JunTae")
    assert settled["verdict"] == "PASS" and settled["reviewer"] == "JunTae" and len(settled["judgements"]) == 2
    failed = conti_brief.record_verdicts(project, "S001", first["take_sha256"], {2: "FAIL"}, "JunTae")
    assert failed["verdict"] == "FAIL"
    metrics = conti_brief.conti_metrics(project)
    row = next(r for r in metrics["shots"] if r["shot_id"] == "S001")
    assert row["attempts"] == 2 and row["verdict"] == "PASS" and row["units"] == 40 and row["human_minutes"] == 3
    assert metrics["totals"] == {"shots_checked": 1, "attempts": 2, "passed": 1, "units": 40.0, "human_minutes": 3.0}
    assert all(r["attempts"] == 0 for r in metrics["shots"] if r["shot_id"] != "S001")


# ---- the judge ----------------------------------------------------------------------

def test_judge_request_and_answer_roundtrip(project, tmp_path):
    _conti(project, "S001")
    import_asset(project, "S001", _clip(tmp_path / "take.mp4", 6), kind="draft")
    record = conti_brief.check_take(project, "S001")
    request = conti_brief.judge_request(project, "S001")
    assert request["take"] == record["take_sha256"] and len(request["attach"]) == 5
    marker = "\n\n=== 입력 자료 (JSON) ===\n"
    payload = json.loads(request["text"].split(marker, 1)[1].split("\n\n=== 함께 올릴 프레임 ===", 1)[0])
    assert [c["index"] for c in payload["criteria"]] == [1, 2, 3] and payload["take"] == record["take_sha256"]
    assert "at 0.5 s: sets the cup down" in payload["brief"]["scene"]
    answer = "Here:\n```json\n" + json.dumps({"take": record["take_sha256"], "criteria": [
        {"index": 1, "verdict": "PASS", "reason": "red coat in every frame"},
        {"index": 2, "verdict": "UNSURE", "reason": "the set-down falls between samples"},
        {"index": 3, "verdict": "FAIL", "reason": "frame 1 and 5 share the same framing"}]}) + "\n```"
    judged = conti_brief.submit_judgement(project, "S001", answer, source="llm:claude")
    assert [c["verdict"] for c in judged["criteria"]] == ["PASS", "PENDING", "FAIL"]
    assert judged["criteria"][1]["reason"].startswith("the set-down") and judged["reviewer"] == "llm:claude"
    assert judged["verdict"] == "FAIL"
    with pytest.raises(FilmError, match="different take"):
        conti_brief.submit_judgement(project, "S001", {"take": "0" * 64, "criteria": [{"index": 1, "verdict": "PASS"}]})
    with pytest.raises(FilmError, match="PASS, FAIL or UNSURE"):
        conti_brief.submit_judgement(project, "S001", {"criteria": [{"index": 1, "verdict": "MAYBE"}]})
    with pytest.raises(FilmError, match="criteria"):
        conti_brief.submit_judgement(project, "S001", {"criteria": []})


def test_judge_request_needs_criteria(project, tmp_path):
    _conti(project, "S001", acceptance_criteria=[])
    import_asset(project, "S001", _clip(tmp_path / "take.mp4", 6), kind="draft")
    conti_brief.check_take(project, "S001")
    with pytest.raises(FilmError, match="acceptance_criteria"):
        conti_brief.judge_request(project, "S001")


# ---- hand-off integration -----------------------------------------------------------

def test_handoff_video_brief_and_dropped_clip_use_the_conti(project, tmp_path):
    plain = handoff.video_brief(project, "S001")
    assert plain["checklist"] == [] and plain["conti_sha256"] is None
    _conti(project, "S001")
    brief = handoff.video_brief(project, "S001")
    assert "at 0.5 s: sets the cup down" in brief["prompt"] and brief["clip_seconds"] == 6
    assert brief["checklist"][0] == "the red coat is visible" and brief["seconds"] == -(-_shot(project, "S001")["duration_ms"] // 1000)
    data = _clip(tmp_path / "drop.mp4", 2).read_bytes()
    result = handoff.import_video(project, "S001", data, "drop.mp4", service_id="flow")
    assert result["conti_check"]["verdict"] == "FAIL" and result["conti_check"]["attempt"] == 1
    assert any("DURATION" in w for w in result["warnings"])
    record = conti_brief.load_check(project, "S001")
    assert record["service"] == "flow" and record["take_sha256"] == read(project / "manifest/assets.json")["shots"]["S001"]["draft"]["sha256"]
    untouched = handoff.import_video(project, "S002", data, "drop.mp4")
    assert untouched["conti_check"] == {"verdict": None, "warnings": []}, "no conti, no check"


def test_checks_never_touch_production_files(project, tmp_path):
    _conti(project, "S001")
    import_asset(project, "S001", _clip(tmp_path / "take.mp4", 6), kind="draft")
    before = {name: digest(project / name) for name in ("manifest/shots.json", "manifest/assets.json",
                                                         "project.yaml", "conti/S001.json")}
    conti_brief.shot_brief(project, "S001")
    conti_brief.check_take(project, "S001")
    conti_brief.record_verdicts(project, "S001", None, {1: "PASS", 2: "PASS", 3: "PASS"}, "JunTae")
    assert before == {name: digest(project / name) for name in before}


# ---- CLI --------------------------------------------------------------------------

def test_cli_roundtrip(project, tmp_path, capsys):
    _conti(project, "S001")
    import_asset(project, "S001", _clip(tmp_path / "take.mp4", 6), kind="draft")
    assert cli.main(["conti-brief", str(project), "S001"]) == 0
    assert cli.main(["conti-check", str(project), "S001", "--service", "flow", "--units", "20", "--human-minutes", "4"]) == 0
    assert cli.main(["conti-judge-request", str(project), "S001"]) == 0
    sha = conti_brief.load_check(project, "S001")["take_sha256"]
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"take": sha, "criteria": [{"index": 1, "verdict": "PASS", "reason": "coat"},
                                                            {"index": 2, "verdict": "UNSURE", "reason": ""},
                                                            {"index": 3, "verdict": "PASS", "reason": "push-in"}]}),
                      encoding="utf-8")
    assert cli.main(["conti-judge-submit", str(project), "S001", "--file", str(answer), "--source", "llm:claude"]) == 0
    assert cli.main(["conti-verdict", str(project), "S001", "--reviewer", "JunTae", "--pass", "2"]) == 0
    assert cli.main(["conti-metrics", str(project)]) == 0
    out = capsys.readouterr().out
    assert '"verdict": "PASS"' in out and '"attempts": 1' in out
    assert conti_brief.load_check(project, "S001")["verdict"] == "PASS"
