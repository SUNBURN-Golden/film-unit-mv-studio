"""REDESIGN_W00 R7: hand-off sittings — batch plan, download assignment, a Preview per batch.

Fixtures are the synthetic six-shot project and ffmpeg test patterns standing in for
downloaded clips; the Preview is the real `compile_preview` over them. No service is
contacted. This shows that the batching, the matching (and its refusals) and the
metrics record work on files; it does not show a real Flow/Veo download or a real
sitting's minutes — those need R5.
"""
import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from engine import cli, conti_batch, conti_brief, handoff
from engine.core import FilmError, digest, read, set_production, write
from test_conti_brief import _clip, _conti, project  # noqa: F401  (project is a fixture)

SHOTS = [f"S00{i}" for i in range(1, 7)]


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720), "teal").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def sitting(project):
    """Six shots, every conti READY and every first frame imported."""
    for sid in SHOTS:
        _conti(project, sid)
        handoff.import_frame(project, sid, _png(), f"{sid}.png", "gemini")
    return project


def _downloads(tmp_path, names, base=6.0, folder="dl"):
    """Distinct clips (different lengths → different bytes) under the given names."""
    root = tmp_path / folder
    root.mkdir(exist_ok=True)
    for i, name in enumerate(names):
        _clip(root / name, base + i * .25)
    return root


# ---- plan ----------------------------------------------------------------------------------

def test_plan_groups_shots_by_the_sitting_limit(sitting):
    plan = conti_batch.plan_batches(sitting)
    assert plan["sitting_limit"] == 4, "default"
    assert [g["shots"] for g in plan["sittings"]] == [SHOTS[:4], SHOTS[4:]]
    assert [g["clip_seconds"] for g in plan["sittings"]] == [24, 12] and plan["open_batch"] is None
    set_production(sitting, sitting_limit=2)
    assert [g["shots"] for g in conti_batch.plan_batches(sitting)["sittings"]] == [SHOTS[:2], SHOTS[2:4], SHOTS[4:]]
    assert [g["shots"] for g in conti_batch.plan_batches(sitting, 3)["sittings"]] == [SHOTS[:3], SHOTS[3:]], "an explicit size wins"
    for bad in (0, -1, True, "3"):
        with pytest.raises(FilmError, match="size"):
            conti_batch.plan_batches(sitting, bad)


def test_plan_says_why_a_shot_is_not_in_a_sitting(sitting, tmp_path):
    (sitting / "conti/S002.json").unlink()                                        # no conti
    _conti(sitting, "S003", open_questions=["which door?"])                       # not READY
    shots = read(sitting / "manifest/shots.json")
    shots[3]["storyboard_kind"] = "placeholder"                                   # first frame missing
    shots[4]["duration_ms"] = 9000                                                # no 8 s clip covers it
    write(sitting / "manifest/shots.json", shots)
    plan = conti_batch.plan_batches(sitting)
    reasons = {b["shot_id"]: " ".join(b["reasons"]) for b in plan["blocked"]}
    assert "no conti" in reasons["S002"] and "not READY" in reasons["S003"]
    assert "first frame" in reasons["S004"] and "split" in reasons["S005"]
    assert [g["shots"] for g in plan["sittings"]] == [["S001", "S006"]]


def test_plan_leaves_out_shots_waiting_on_a_check_or_accepted(sitting, tmp_path):
    from test_conti_revision_loop import _take
    first = _take(sitting, tmp_path, "t1")                       # S001 checked: criteria pending
    plan = conti_batch.plan_batches(sitting)
    assert plan["waiting_on_checks_or_conti"][0]["shot_id"] == "S001"
    assert plan["waiting_on_checks_or_conti"][0]["stage"] == "NEEDS_VERDICTS"
    conti_brief.record_verdicts(sitting, "S001", first["take_sha256"], {1: "PASS", 2: "PASS", 3: "PASS"}, "JunTae")
    plan = conti_batch.plan_batches(sitting)
    assert plan["accepted"] == ["S001"] and [g["shots"] for g in plan["sittings"]] == [SHOTS[1:5], SHOTS[5:]]


def test_a_failed_shot_returns_as_the_next_attempt_after_its_rewrite(sitting, tmp_path):
    from test_conti_revision_loop import _answer, _take
    _take(sitting, tmp_path, "short", seconds=2)                 # S001 FAILs DURATION
    assert conti_batch.plan_batches(sitting)["waiting_on_checks_or_conti"][0]["stage"] == "NEEDS_REWRITE"
    request = conti_brief.rewrite_request(sitting, "S001")
    from engine import conti
    conti.submit_answer(sitting, _answer(sitting, framing="close-up on the hands"), request_id=request["request_id"])
    plan = conti_batch.plan_batches(sitting)
    assert plan["sittings"][0]["shots"][0] == "S001"
    assert next(c for c in plan["candidates"] if c["shot_id"] == "S001") == {
        "shot_id": "S001", "seconds": 5.02, "clip_seconds": 6, "stage": "AWAITING_TAKE", "attempt": 2}


# ---- open ----------------------------------------------------------------------------------

def test_open_freezes_the_next_sitting_and_writes_its_sheet(sitting):
    batch = conti_batch.open_batch(sitting, 4)
    assert batch["id"] == "SIT001" and batch["status"] == "OPEN" and batch["reused"] is False
    assert [s["shot_id"] for s in batch["slots"]] == SHOTS[:4]
    assert batch["slots"][0]["stem"] == "SIT001_S001" and batch["slots"][0]["clip_seconds"] == 6
    sheet = (sitting / batch["sheet"]).read_text(encoding="utf-8")
    assert "SIT001_S001.mp4" in sheet and "dolly" in sheet.lower() and "the red coat is visible" in sheet
    assert sheet.count("## S0") == 4
    again = conti_batch.open_batch(sitting, 2)
    assert again["id"] == "SIT001" and again["reused"] is True and len(again["slots"]) == 4, "one open sitting at a time"
    assert read(sitting / "qc/batches/SIT001.json")["slots"] == batch["slots"]


def test_nothing_to_open_is_explained(project):
    with pytest.raises(FilmError, match="no shot needs a take.*S001: no conti"):
        conti_batch.open_batch(project)


# ---- assign --------------------------------------------------------------------------------

def test_assign_matches_names_and_imports_through_the_ordinary_path(sitting, tmp_path):
    conti_batch.open_batch(sitting, 4)
    # S0010 and XS004 are traps: a shot id inside a longer token is not that shot.
    folder = _downloads(tmp_path, ["SIT001_S001.mp4", "flow-S002-v2.MP4", "Flow_8f3a.mp4", "S0010.mp4", "XS004.mp4"])
    (folder / "notes.txt").write_text("not a video")
    dry = conti_batch.assign_downloads(sitting, "SIT001", folder=folder, apply=False)
    assert [(a["shot_id"], a["file"]) for a in dry["assignments"]] == [("S001", "SIT001_S001.mp4"), ("S002", "flow-S002-v2.MP4")]
    assert dry["applied"] is False and not read(sitting / "manifest/assets.json", {"shots": {}})["shots"].get("S001")
    assert {u["file"] for u in dry["unmatched"]} == {"Flow_8f3a.mp4", "S0010.mp4", "XS004.mp4"}
    assert dry["open_slots"] == ["S003", "S004"]

    done = conti_batch.assign_downloads(sitting, "SIT001", folder=folder, mapping={"Flow_8f3a.mp4": "S003"}, service="flow")
    assert done["applied"] and [i["shot_id"] for i in done["imported"]] == ["S001", "S002", "S003"] and done["failed"] == []
    assert done["open_slots"] == ["S004"]
    assets = read(sitting / "manifest/assets.json")["shots"]
    for entry in done["imported"]:
        record = conti_brief.load_check(sitting, entry["shot_id"])
        assert assets[entry["shot_id"]]["draft"]["sha256"] == record["take_sha256"] and record["attempt"] == 1
        assert record["service"] == "flow" and entry["verdict"] == "PENDING", "auto-checked against its conti, criteria pending"
    assert digest(folder / "SIT001_S001.mp4") == read(sitting / "qc/batches/SIT001.json")["assigned"]["S001"]["sha256"]

    again = conti_batch.assign_downloads(sitting, "SIT001", folder=folder, mapping={"Flow_8f3a.mp4": "S003"})
    assert again["imported"] == [] and {a["shot_id"] for a in again["already_imported"]} == {"S001", "S002", "S003"}
    assert len(conti_brief._records(sitting, "S001")) == 1, "re-running imports and checks nothing twice"

    folder2 = _downloads(tmp_path, ["take-s004-final.mp4"], base=8, folder="dl2")
    last = conti_batch.assign_downloads(sitting, "SIT001", folder=folder2)
    assert [i["shot_id"] for i in last["imported"]] == ["S004"] and last["open_slots"] == []


@pytest.mark.parametrize("case", ["two shots in one name", "two files for one slot", "map contradicts name",
                                   "map outside the sitting", "map to a missing file"])
def test_ambiguity_refuses_everything(sitting, tmp_path, case):
    conti_batch.open_batch(sitting, 4)
    names = ["SIT001_S001.mp4", "SIT001_S002.mp4"]
    mapping = None
    if case == "two shots in one name":
        names.append("S003_to_S004.mp4")
    elif case == "two files for one slot":
        names.append("S002-again.mp4")
    elif case == "map contradicts name":
        names.append("S003.mp4")
        mapping = {"S003.mp4": "S004"}
    elif case == "map outside the sitting":
        names.append("generic.mp4")
        mapping = {"generic.mp4": "S006"}
    else:
        mapping = {"ghost.mp4": "S003"}
    folder = _downloads(tmp_path, names)
    before = (sitting / "qc/batches/SIT001.json").read_bytes()
    with pytest.raises(FilmError, match="ambiguous: nothing was imported"):
        conti_batch.assign_downloads(sitting, "SIT001", folder=folder, mapping=mapping)
    assert (sitting / "qc/batches/SIT001.json").read_bytes() == before
    assert not read(sitting / "manifest/assets.json", {"shots": {}})["shots"].get("S001"), "the unambiguous files were not imported either"
    assert not (sitting / "qc/conti/S001").exists()


def test_the_same_clip_saved_twice_is_not_ambiguous(sitting, tmp_path):
    conti_batch.open_batch(sitting, 2)
    folder = _downloads(tmp_path, ["S001.mp4"])
    shutil.copy(folder / "S001.mp4", folder / "S001 (1).mp4")
    result = conti_batch.assign_downloads(sitting, "SIT001", folder=folder)
    assert [i["shot_id"] for i in result["imported"]] == ["S001"]


def test_a_second_different_file_for_a_filled_slot_waits_for_the_next_sitting(sitting, tmp_path):
    conti_batch.open_batch(sitting, 2)
    folder = _downloads(tmp_path, ["S001.mp4"])
    conti_batch.assign_downloads(sitting, "SIT001", folder=folder)
    _clip(folder / "S001-retake.mp4", 7.1)
    result = conti_batch.assign_downloads(sitting, "SIT001", folder=folder)
    assert result["imported"] == [] and "retake belongs to the next sitting" in result["unmatched"][0]["reason"]


def test_assign_validates_its_inputs(sitting, tmp_path):
    with pytest.raises(FilmError, match="no batch SIT001"):
        conti_batch.assign_downloads(sitting, "SIT001", folder=tmp_path)
    with pytest.raises(FilmError, match="batch id"):
        conti_batch.assign_downloads(sitting, "../x", folder=tmp_path)
    conti_batch.open_batch(sitting, 2)
    with pytest.raises(FilmError, match="file name → shot id"):
        conti_batch.assign_downloads(sitting, "SIT001", folder=tmp_path, mapping={"a.mp4": 3})
    empty = conti_batch.assign_downloads(sitting, "SIT001", folder=tmp_path / "missing")
    assert empty["scanned"] == 0 and empty["assignments"] == [] and empty["open_slots"] == ["S001", "S002"]


# ---- preview and metrics ---------------------------------------------------------------------

def test_a_preview_per_sitting_with_a_metrics_record(sitting, tmp_path, capsys):
    set_production(sitting, sitting_limit=2)
    with pytest.raises(FilmError, match="no assigned take"):
        conti_batch.preview_batch(sitting, conti_batch.open_batch(sitting)["id"])
    folder = _downloads(tmp_path, ["S001.mp4"])
    conti_batch.assign_downloads(sitting, "SIT001", folder=folder)
    conti_brief.check_take(sitting, "S001", units=3, human_minutes=2)    # per-take figures
    with pytest.raises(FilmError, match="nonnegative"):
        conti_batch.preview_batch(sitting, "SIT001", units=-1)

    first = conti_batch.preview_batch(sitting, "SIT001", units=1.5, human_minutes=10)
    assert first["status"] == "OPEN", "S002 is still unfilled"
    m = first["metrics"]
    assert m["attempts"] == 1 and m["takes_in_preview"] == 1 and m["unassigned_slots"] == ["S002"]
    assert m["units"] == {"per_take": 3, "sitting": 1.5, "total": 4.5, "takes_without_units": 0}
    assert m["human_minutes"] == {"per_take": 2, "sitting": 10, "total": 12, "takes_without_minutes": 0}
    build = read(Path(first["output"]).parent / "build.json")
    rows = {r["id"]: r for r in build["shots"]}
    assert rows["S001"]["kind"] == "draft" and rows["S001"]["source_sha256"] == m["per_take"][0]["take_sha256"]
    assert rows["S002"]["kind"] != "draft", "unfilled slots stay as they were in the Preview"
    assert Path(first["output"]).is_file() and Path(first["output"]).parent.name == first["build_id"]

    # the second take arrives; same sitting, second Preview, now complete
    folder2 = _downloads(tmp_path, ["S002.mp4"], base=7, folder="dl2")
    conti_batch.assign_downloads(sitting, "SIT001", folder=folder2)
    second = conti_batch.preview_batch(sitting, "SIT001")
    assert second["status"] == "CLOSED" and second["metrics"]["attempts"] == 2 and second["metrics"]["takes_in_preview"] == 2
    assert second["metrics"]["units"]["takes_without_units"] == 1 and second["build_id"] != first["build_id"]
    record = read(sitting / "qc/batches/SIT001.json")
    assert [p["build_id"] for p in record["previews"]] == [first["build_id"], second["build_id"]]
    assert {r["shot_id"]: r["attempts"] for r in second["metrics"]["shots_so_far"]} == {"S001": 1, "S002": 1}

    nxt = conti_batch.open_batch(sitting)
    assert nxt["id"] == "SIT002" and [s["shot_id"] for s in nxt["slots"]] == ["S003", "S004"]


def test_close_returns_unfilled_slots_to_the_pool(sitting):
    set_production(sitting, sitting_limit=2)
    conti_batch.open_batch(sitting)
    closed = conti_batch.close_batch(sitting, "SIT001", "app credits ran out")
    assert closed["status"] == "CLOSED" and closed["open_slots"] == ["S001", "S002"]
    with pytest.raises(FilmError, match="is CLOSED"):
        conti_batch.close_batch(sitting, "SIT001")
    assert conti_batch.open_batch(sitting)["slots"][0]["shot_id"] == "S001" and conti_batch.open_batch(sitting)["id"] == "SIT002"
    assert [b["id"] for b in conti_batch.batch_status(sitting)] == ["SIT001", "SIT002"]


# ---- CLI -----------------------------------------------------------------------------------

def test_cli_sitting_round_trip(sitting, tmp_path, capsys):
    def run(*argv):
        capsys.readouterr()
        assert cli.main(["conti-batch", *argv]) == 0, argv
        return json.loads(capsys.readouterr().out)

    assert cli.main(["production", str(sitting), "--sitting-limit", "2"]) == 0
    assert [g["shots"] for g in run("plan", str(sitting))["sittings"]][0] == ["S001", "S002"]
    assert run("open", str(sitting))["id"] == "SIT001"
    folder = _downloads(tmp_path, ["a-S001.mp4", "b.mp4"])
    (tmp_path / "map.json").write_text(json.dumps({"b.mp4": "S002"}), encoding="utf-8")
    dry = run("assign", str(sitting), "SIT001", "--folder", str(folder), "--map", str(tmp_path / "map.json"), "--dry-run")
    assert dry["applied"] is False and len(dry["assignments"]) == 2
    done = run("assign", str(sitting), "SIT001", "--folder", str(folder), "--map", str(tmp_path / "map.json"), "--service", "flow")
    assert done["open_slots"] == [] and [i["shot_id"] for i in done["imported"]] == ["S001", "S002"]
    out = run("preview", str(sitting), "SIT001", "--units", "2", "--human-minutes", "15")
    assert out["status"] == "CLOSED" and out["metrics"]["units"]["total"] == 2 and out["metrics"]["human_minutes"]["total"] == 15
    assert run("status", str(sitting))[0]["preview_build"] == out["build_id"]
    assert cli.main(["conti-batch", "assign", str(sitting)]) != 0, "assign needs a sitting id"
    assert cli.main(["conti-batch", "assign", str(sitting), "SIT009", "--folder", str(folder)]) != 0
