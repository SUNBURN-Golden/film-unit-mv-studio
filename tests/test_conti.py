"""W00 redesign R1: detailed per-shot conti documents.

Draft skeletons seeded from the manifest, timed lyrics and measured beats;
structural validation; a paste-out discussion request that carries everything
the model needs; per-shot completeness. Conti never changes shots, lyrics,
reviews or LOCK. All fixtures are synthetic test projects.
"""
import json
from pathlib import Path

import pytest

from engine import cli, conti
from engine.audio import analyze, synth_test_audio
from engine.core import FilmError, digest, init_project, read
from engine.lyrics import prepare_lyrics, save_timing
from engine.production import make_package


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    audio = synth_test_audio(tmp_path / "t.wav", seconds=30)
    p = init_project(tmp_path, "conti", audio, "A woman remembers her old kitchen.",
                     "첫 줄 가사\n둘째 줄 가사", synthetic=True, aspect="16:9")
    analyze(p)
    make_package(p)
    return p


def _timed(p, reviewer=""):
    """Time every lyric row across the song so cues exist for anchoring."""
    doc = prepare_lyrics(p)
    duration = read(p / "analysis/audio.json")["duration_ms"]
    lyric_rows = [r for r in doc["rows"] if r["kind"] == "lyric"]
    step = duration // (len(lyric_rows) + 1)
    doc["cues"] = [{"id": f"cue{i+1}", "source_row_id": row["id"],
                    "start_ms": step * (i + 1) - 2000, "end_ms": step * (i + 1),
                    "text": row["text"]} for i, row in enumerate(lyric_rows)]
    return save_timing(p, doc, reviewer=reviewer)


def _shot(p, shot_id):
    return next(s for s in read(p / "manifest/shots.json") if s["id"] == shot_id)


def _full_doc(shot, **overrides):
    in_ms = shot["in_ms"]
    doc = {"schema_version": 1, "shot_id": shot["id"], "updated_at": "2026-10-14T00:00:00Z",
           "source": "test", "scene_goal": "Mina decides to leave the kitchen",
           "lyric_anchors": [{"text": "첫 줄 가사", "start_ms": in_ms, "end_ms": in_ms + 1000,
                              "intent": "she looks up on the line"}],
           "beat_map": [{"at_ms": in_ms + 500, "hit": "the cup lands on the table"}],
           "framing": "medium close-up, slightly low angle",
           "camera": {"type": "dolly", "moves": [{"at_ms": in_ms, "action": "slow push in"}]},
           "blocking": [{"character": c, "pose": "standing, weight on one hip",
                         "expression": "quiet resolve",
                         "motion_beats": [{"at_ms": in_ms + 500, "action": "sets the cup down"}]}
                        for c in shot.get("characters", ["CHAR_01"])],
           "lighting": "warm window light from screen left",
           "palette": ["#ECEAE4"],
           "continuity": {"from_previous": "she is already at the table",
                          "to_next": "the cup stays on the table",
                          "props": ["green cup"], "wardrobe": "red coat"},
           "references": [shot["references"][0]],
           "transition_in": "hard cut", "transition_out": "match cut on the cup",
           "acceptance_criteria": ["Mina's red coat is visible in frame",
                                   "the cup lands exactly on the beat"],
           "open_questions": []}
    doc.update(overrides)
    return doc


# ---- drafting and status ---------------------------------------------------------

def test_draft_seeds_skeletons_and_never_overwrites(project):
    _timed(project)
    result = conti.draft_conti(project)
    shots = read(project / "manifest/shots.json")
    assert result["written"] == [s["id"] for s in shots] and result["kept"] == []
    doc = conti.load_conti(project, "S001")
    shot = _shot(project, "S001")
    assert doc["schema_version"] == 1 and doc["shot_id"] == "S001" and doc["source"] == "draft"
    assert doc["framing"] == shot["composition"]
    assert doc["references"] == shot["references"]
    assert [b["character"] for b in doc["blocking"]] == shot["characters"]
    anchored = [s["id"] for s in shots if (conti.load_conti(project, s["id"]) or {}).get("lyric_anchors")]
    assert anchored, "timed cues overlapping some shot should seed anchors"
    for sid in anchored:
        shot, doc = _shot(project, sid), conti.load_conti(project, sid)
        for anchor in doc["lyric_anchors"]:
            assert anchor["start_ms"] < shot["out_ms"] and anchor["end_ms"] > shot["in_ms"]
    for beat in doc["beat_map"]:
        assert shot["in_ms"] <= beat["at_ms"] < shot["out_ms"]
    again = conti.draft_conti(project)
    assert again["written"] == [] and again["kept"] == [s["id"] for s in shots]


def test_draft_can_target_a_subset(project):
    result = conti.draft_conti(project, ["S002"])
    assert result["written"] == ["S002"]
    assert conti.load_conti(project, "S002") is not None
    assert conti.load_conti(project, "S001") is None
    with pytest.raises(FilmError, match="Unknown shot"):
        conti.draft_conti(project, ["S999"])


def test_status_reports_what_keeps_a_shot_unsettled(project):
    status = conti.conti_status(project)
    assert status["ready"] == 0 and all(r["missing"] == ["conti"] for r in status["shots"])
    conti.draft_conti(project)
    status = conti.conti_status(project)
    first, middle = status["shots"][0], status["shots"][1]
    assert first["has_conti"] and not first["ready"]
    assert "scene_goal" in first["missing"] and "acceptance_criteria" in first["missing"]
    assert "transition_in" not in first["missing"]     # first shot needs none
    assert "transition_out" in first["missing"]        # but it must hand off to S002
    assert "transition_in" in middle["missing"]


# ---- submit + validation -------------------------------------------------------------

def test_submit_stores_a_settled_conti(project):
    conti.draft_conti(project)
    row = conti.submit_conti(project, _full_doc(_shot(project, "S001")))
    assert row["ready"] and row["missing"] == []
    doc = conti.load_conti(project, "S001")
    assert doc["source"] == "manual" and doc["scene_goal"]
    assert doc["blocking"][0]["motion_beats"][0]["action"] == "sets the cup down"


def test_submit_answer_accepts_a_batch_and_flags_gaps(project):
    shots = read(project / "manifest/shots.json")
    docs = [_full_doc(s) for s in shots[:2]]
    del docs[1]["transition_out"]                      # S002 must still hand off
    rows = conti.submit_answer(project, {"conti": docs}, source="handoff:claude")
    assert [r["shot_id"] for r in rows] == [shots[0]["id"], shots[1]["id"]]
    assert rows[0]["ready"] and rows[1]["missing"] == ["transition_out"]
    assert conti.load_conti(project, shots[1]["id"])["source"] == "handoff:claude"


def test_submit_answer_parses_a_pasted_code_block(project):
    shot = _shot(project, "S001")
    answer = "여기 있습니다:\n```json\n" + json.dumps({"conti": [_full_doc(shot)]}) + "\n```"
    rows = conti.submit_answer(project, answer)
    assert rows[0]["ready"]


def test_submit_answer_is_all_or_nothing(project):
    shots = read(project / "manifest/shots.json")
    good, bad = _full_doc(shots[0]), _full_doc(shots[1])
    bad["beat_map"] = [{"at_ms": shots[1]["out_ms"] + 500, "hit": "outside"}]
    with pytest.raises(FilmError, match="outside"):
        conti.submit_answer(project, {"conti": [good, bad]})
    assert conti.load_conti(project, shots[0]["id"]) is None
    assert conti.load_conti(project, shots[1]["id"]) is None


def test_validation_rejects_out_of_bounds_and_bad_ids(project):
    shot = _shot(project, "S001")
    for patch, match in [
        ({"lyric_anchors": [{"text": "x", "start_ms": shot["out_ms"], "end_ms": shot["out_ms"] + 100}]}, "lyric_anchors"),
        ({"beat_map": [{"at_ms": shot["in_ms"] - 1, "hit": "early"}]}, "beat_map"),
        ({"blocking": [{"character": "CHAR_ZZZ"}]}, "blocking"),
        ({"references": ["../escape.png"]}, "references"),
        ({"references": ["/abs/path.png"]}, "references"),
        ({"surprise": "field"}, "Unknown conti fields"),
        ({"shot_id": "S999"}, "Unknown shot ID"),
        ({"schema_version": 2}, "schema_version"),
    ]:
        with pytest.raises(FilmError, match=match):
            conti.submit_conti(project, _full_doc(shot, **patch))
    with pytest.raises(FilmError, match="conti"):
        conti.submit_answer(project, {"shots": []})
    with pytest.raises(FilmError, match="conti"):
        conti.submit_answer(project, {"conti": []})


def test_blocking_cast_completeness(project):
    shot = _shot(project, "S001")
    doc = _full_doc(shot, blocking=[])
    if shot.get("characters"):
        row = conti.submit_conti(project, doc)
        assert any(m.startswith("blocking.") for m in row["missing"])


# ---- the discussion request ----------------------------------------------------------

def test_request_carries_everything_the_model_needs(project):
    _timed(project)
    shots = read(project / "manifest/shots.json")
    mid = shots[1]
    req = conti.conti_request(project, [mid["id"]])
    assert req["ids"] == [mid["id"]]
    marker = "\n\n=== 입력 자료 (JSON) ===\n"
    assert marker in req["text"]
    payload = json.loads(req["text"].split(marker, 1)[1].rsplit("\n\n위 지시대로", 1)[0])
    entry = payload["shots"][0]
    assert entry["id"] == mid["id"] and entry["in_ms"] == mid["in_ms"]
    assert entry["timed_lyrics"], "overlapping cues must ride along"
    assert all(mid["in_ms"] <= b < mid["out_ms"] for b in entry["beat_times_ms"])
    assert {c["id"] for c in entry["characters"]} == set(mid["characters"])
    assert entry["neighbours"]["previous"]["id"] == shots[0]["id"]
    assert "acceptance_criteria" in req["text"]        # the schema asks for checkable criteria


def test_request_is_empty_when_no_package(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    audio = synth_test_audio(tmp_path / "t.wav", seconds=5)
    p = init_project(tmp_path, "bare", audio, "brief", synthetic=True)
    with pytest.raises(FilmError):
        conti.conti_request(p)


# ---- isolation --------------------------------------------------------------------

def test_conti_never_touches_production_files(project):
    _timed(project, reviewer="FixtureReviewer")
    before = {name: digest(project / name)
              for name in ("manifest/shots.json", "lyrics/lyrics_timed.json", "project.yaml")}
    conti.draft_conti(project)
    conti.submit_answer(project, {"conti": [_full_doc(s) for s in read(project / "manifest/shots.json")[:2]]})
    assert before == {name: digest(project / name) for name in before}
    assert conti.conti_status(project)["ready"] == 2


# ---- CLI --------------------------------------------------------------------------

def test_cli_conti_roundtrip(project, tmp_path, capsys):
    shot = _shot(project, "S001")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"conti": [_full_doc(shot)]}), encoding="utf-8")
    assert cli.main(["conti-draft", str(project)]) == 0
    assert cli.main(["conti-request", str(project), "--shots", "S001"]) == 0
    assert cli.main(["conti-submit", str(project), "--file", str(answer)]) == 0
    assert cli.main(["conti-status", str(project)]) == 0
    printed = capsys.readouterr().out
    assert '"ready": 1' in printed
    assert conti.conti_status(project)["ready"] == 1
