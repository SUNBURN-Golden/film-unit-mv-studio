"""REDESIGN_W00 R6: the revision loop.

A FAILed criterion becomes a rewrite request for the subscription LLM, the answer
changes the conti, the brief regenerates and the next take is attempt n+1 —
capped by `production.max_attempts_per_shot`. Takes are ffmpeg test patterns, the
LLM answer is written by the test; no service is contacted and nothing is paid.
Nothing here shows that a real generation was revised: that is REDESIGN_W00 R5.
"""
import json

import pytest

from engine import cli, conti, conti_brief
from engine.compiler import import_asset
from engine.core import FilmError, read, set_production
from test_conti_brief import _clip, _conti, _shot, project  # noqa: F401  (project is a fixture)


def _take(p, tmp_path, name, seconds=6, **kw):
    import_asset(p, "S001", _clip(tmp_path / f"{name}.mp4", seconds, **kw), kind="draft")
    return conti_brief.check_take(p, "S001")


def _answer(p, **changes):
    """What the LLM would return: the shot's whole conti with one field changed."""
    doc = conti.load_conti(p, "S001")
    doc.update(changes)
    return json.dumps({"conti": [doc]}, ensure_ascii=False)


# ---- a verdict against a conti that changed is flagged -----------------------------------

def test_verdict_after_a_conti_change_is_flagged(project, tmp_path):
    _conti(project, "S001")
    record = _take(project, tmp_path, "t1")
    clean = conti_brief.record_verdicts(project, "S001", record["take_sha256"], {1: "PASS"}, "JunTae")
    assert not any("conti that changed" in w for w in clean["warnings"])
    assert "conti_changed" not in clean["judgements"][-1]

    _conti(project, "S001", framing="wide shot from behind")
    flagged = conti_brief.record_verdicts(project, "S001", record["take_sha256"], {2: "FAIL"}, "JunTae")
    note = flagged["judgements"][-1]["conti_changed"]
    assert note["checked_against"] == record["conti_sha256"] and note["now"] != note["checked_against"]
    assert sum("conti that changed" in w for w in flagged["warnings"]) == 1
    again = conti_brief.record_verdicts(project, "S001", record["take_sha256"], {3: "PASS"}, "JunTae")
    assert sum("conti that changed" in w for w in again["warnings"]) == 1, "the warning is not repeated"
    assert flagged["verdict"] == "FAIL", "the verdict is recorded, only flagged"

    judged = conti_brief.submit_judgement(project, "S001", {"criteria": [{"index": 1, "verdict": "PASS"}]})
    assert "conti_changed" in judged["judgements"][-1], "judge answers go through the same check"


def test_bookkeeping_edits_do_not_flag(project, tmp_path):
    _conti(project, "S001")
    record = _take(project, tmp_path, "t1")
    _conti(project, "S001", source="someone else", updated_at="2027-01-01T00:00:00Z")
    settled = conti_brief.record_verdicts(project, "S001", record["take_sha256"], {1: "PASS"}, "JunTae")
    assert "conti_changed" not in settled["judgements"][-1]


# ---- failed criterion → rewrite request → new brief → attempt n+1 -------------------------

def test_failed_criterion_runs_the_whole_loop(project, tmp_path):
    _conti(project, "S001")
    assert conti_brief.revision_loop(project, "S001")["stage"] == "NEEDS_TAKE"
    first = _take(project, tmp_path, "t1")
    assert conti_brief.revision_loop(project, "S001")["stage"] == "NEEDS_VERDICTS"
    with pytest.raises(FilmError, match="no FAIL"):
        conti_brief.rewrite_request(project, "S001")

    conti_brief.record_verdicts(project, "S001", first["take_sha256"],
                                {1: "PASS", 2: "PASS", 3: {"verdict": "FAIL", "reason": "the camera never moves"}}, "judge")
    loop = conti_brief.revision_loop(project, "S001")
    assert loop["stage"] == "NEEDS_REWRITE" and loop["failures"][0]["index"] == 3 and loop["cap"] == 3

    request = conti_brief.rewrite_request(project, "S001")
    assert request["attempt"] == 1 and request["next_attempt"] == 2 and request["attempts_left_after_this_one"] == 1
    text = (project / request["request_file"]).read_text(encoding="utf-8")
    assert text == request["text"] and request["request_file"] == f"conti/requests/{request['request_id']}.txt"
    assert "the camera never moves" in text and "the camera pushes in, never cuts" in text
    assert "complete conti document" in text and "Do not weaken" in text
    again = conti_brief.rewrite_request(project, "S001")
    assert again["request_id"] == request["request_id"], "asking twice is the same request"
    stored = conti_brief.load_check(project, "S001")["revision_request"]
    assert stored["rewrite_request_id"] == request["request_id"] and stored["fulfilled_by"] is None
    assert "criterion 3 failed" in stored["instruction"]
    assert conti_brief.revision_loop(project, "S001")["stage"] == "AWAITING_CONTI_CHANGE"

    old_prompt = conti_brief.shot_brief(project, "S001")["video_prompt"]
    moves = [{"at_ms": _shot(project, "S001")["in_ms"] + 1000, "action": "dolly in hard on the face"}]
    rows = conti.submit_answer(project, _answer(project, camera={"type": "dolly", "moves": moves}),
                               source="llm:claude", request_id=request["request_id"])
    assert rows[0]["shot_id"] == "S001"
    assert conti.load_conti(project, "S001")["request_sha256"] == conti.request_sha(project, request["request_id"])
    loop = conti_brief.revision_loop(project, "S001")
    assert loop["stage"] == "AWAITING_TAKE" and loop["next_attempt"] == 2
    assert conti_brief.shot_brief(project, "S001")["video_prompt"] != old_prompt, "the brief regenerates from the new conti"

    second = _take(project, tmp_path, "t2", size="1920x1080")
    assert second["attempt"] == 2
    revision = second["revision"]
    assert revision["revises_take"] == first["take_sha256"] and revision["revises_attempt"] == 1
    assert "camera.moves[0].action" in revision["conti_changed"]
    assert revision["prompt_diff"]["video_prompt"]
    assert conti_brief.load_check(project, "S001", first["take_sha256"])["revision_request"]["fulfilled_by"] == second["take_sha256"]
    assert conti_brief.revision_loop(project, "S001")["stage"] == "NEEDS_VERDICTS"

    conti_brief.record_verdicts(project, "S001", second["take_sha256"], {1: "PASS", 2: "PASS", 3: "PASS"}, "judge")
    assert conti_brief.revision_loop(project, "S001")["stage"] == "ACCEPTED"
    metrics = conti_brief.conti_metrics(project)
    assert metrics["totals"]["attempts"] == 2 and next(r for r in metrics["shots"] if r["shot_id"] == "S001")["revisions"] == 1


def test_a_failed_mechanical_check_can_be_rewritten_too(project, tmp_path):
    _conti(project, "S001")
    record = _take(project, tmp_path, "short", seconds=2)
    assert record["verdict"] == "FAIL"
    request = conti_brief.rewrite_request(project, "S001")
    assert [f["check"] for f in request["failures"]] == ["DURATION"]
    assert "DURATION failed" in conti_brief.load_check(project, "S001")["revision_request"]["instruction"]


def test_rewrite_request_needs_a_conti_and_a_take(project, tmp_path):
    with pytest.raises(FilmError, match="no conti"):
        conti_brief.rewrite_request(project, "S001")
    _conti(project, "S001")
    with pytest.raises(FilmError, match="no checked take"):
        conti_brief.rewrite_request(project, "S001")
    with pytest.raises(FilmError, match="Unknown shot"):
        conti_brief.revision_loop(project, "S999")


# ---- the attempt cap ----------------------------------------------------------------------

def test_attempt_cap_stops_the_loop_until_it_is_raised(project, tmp_path):
    set_production(project, max_attempts_per_shot=2)
    _conti(project, "S001")
    first = _take(project, tmp_path, "t1")
    conti_brief.record_verdicts(project, "S001", first["take_sha256"], {3: "FAIL"}, "judge")
    request = conti_brief.rewrite_request(project, "S001")
    assert request["attempts_left_after_this_one"] == 0
    conti.submit_answer(project, _answer(project, framing="close-up on the hands"), request_id=request["request_id"])
    second = _take(project, tmp_path, "t2", size="1920x1080")
    assert second["attempt"] == 2 and not any("cap" in w for w in second["warnings"])
    conti_brief.record_verdicts(project, "S001", second["take_sha256"], {3: "FAIL"}, "judge")

    loop = conti_brief.revision_loop(project, "S001")
    assert loop["stage"] == "CAP_REACHED" and loop["cap"] == 2 and "production.max_attempts_per_shot" in loop["next"]
    for call in (lambda: conti_brief.rewrite_request(project, "S001"),
                 lambda: conti_brief.record_revision(project, "S001", "make the hand bigger")):
        with pytest.raises(FilmError, match="per-shot cap 2"):
            call()
    assert not (conti_brief.load_check(project, "S001").get("revision_request")), "a refused request leaves no record"

    set_production(project, max_attempts_per_shot=3)
    assert conti_brief.revision_loop(project, "S001")["stage"] == "NEEDS_REWRITE"
    assert conti_brief.rewrite_request(project, "S001")["next_attempt"] == 3


def test_cap_applies_only_when_set(project, tmp_path):
    set_production(project, max_attempts_per_shot=1)
    _conti(project, "S001")
    first = _take(project, tmp_path, "t1")
    conti_brief.record_verdicts(project, "S001", first["take_sha256"], {3: "FAIL"}, "judge")
    assert conti_brief.revision_loop(project, "S001")["stage"] == "CAP_REACHED"
    config = read(project / "project.yaml")
    assert config["production"]["max_attempts_per_shot"] == 1


# ---- CLI ---------------------------------------------------------------------------------

def test_cli_loop_commands(project, tmp_path, capsys):
    _conti(project, "S001")
    first = _take(project, tmp_path, "t1")
    conti_brief.record_verdicts(project, "S001", first["take_sha256"], {3: "FAIL"}, "judge")
    capsys.readouterr()
    assert cli.main(["conti-loop", str(project)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["shot_id"] for r in rows] == ["S001"] and rows[0]["stage"] == "NEEDS_REWRITE"
    assert cli.main(["conti-rewrite-request", str(project), "S001", "--language", "Korean"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["request_id"] and "Korean" in out["text"]
    assert cli.main(["conti-loop", str(project), "S001"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["stage"] == "AWAITING_CONTI_CHANGE"
    assert cli.main(["conti-rewrite-request", str(project), "S002"]) != 0
