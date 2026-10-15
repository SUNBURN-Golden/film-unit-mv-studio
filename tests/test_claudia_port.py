"""Claudia port (docs/CLAUDIA_PORT.md): lyric alignment proposals, vocal
stems, take sync measurement, revision records, persisted requests, the
production block and minimal-gate autopilot. Fixtures are synthetic projects
and ffmpeg test media — no service, no paid request.
"""
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from engine import align, cli, conti, conti_brief, handoff, providers, sync_check, w00_gate
from engine.audio import analyze, ensure_vocals, synth_test_audio
from engine.autopilot import autopilot
from engine.compiler import import_asset
from engine.core import FilmError, digest, init_project, read, write
from engine.production import import_frame, make_package
from engine.settings import set_settings

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="ffmpeg/ffprobe are required")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    audio = synth_test_audio(tmp_path / "t.wav", seconds=30)
    p = init_project(tmp_path, "port", audio, "A woman remembers her old kitchen.",
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


def _clip(path, seconds, size="640x360", audio=None):
    cmd = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi",
           "-i", f"testsrc=size={size}:rate=24:duration={seconds}"]
    if audio is not None:
        cmd += ["-i", str(audio[0] if isinstance(audio, tuple) else audio)]
        cmd += ["-map", "0:v", "-map", "1:a", "-af", audio[1], "-shortest"] \
            if isinstance(audio, tuple) else ["-map", "0:v", "-map", "1:a", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


# ---- the vocal stem --------------------------------------------------------------

def test_vocal_stem_proxy_then_supplied(project, tmp_path):
    stem = ensure_vocals(project)
    assert stem["source"] == "proxy" and (project / "analysis/vocals.wav").is_file()
    again = ensure_vocals(project)
    assert again["sha256"] == stem["sha256"], "the stem is cached on the same master"
    voice = tmp_path / "vocal.wav"
    t = np.arange(22050 * 5) / 22050
    sf.write(voice, 0.4 * np.sin(2 * np.pi * 220 * t), 22050, subtype="PCM_16")
    shutil.copyfile(voice, project / "input/vocals.wav")
    supplied = ensure_vocals(project)
    assert supplied["source"] == "supplied" and supplied["sha256"] != stem["sha256"]


# ---- lyric alignment ---------------------------------------------------------------

def test_align_normalization_is_hangul_safe():
    assert align.norm_text("첫 줄 가사") == "첫줄가사"
    assert align.norm_text("Hello, world!") == "helloworld"
    assert align.norm_text("아리랑, 아리랑, 아라리요") == "아리랑아리랑아라리요"
    assert align.norm_text("it's 12°C") == "its12c"


class FakeAligner:
    """Forced-alignment double: a sheet line scores high only when the window
    starts within 0.5 s before where the song 'sings' it."""
    id = "fake-forced"

    def __init__(self, truth):
        self.truth = truth          # text -> list of (start_s, end_s) in song order

    def force(self, audio, t0, lines):
        out, cursor = [], t0
        for k, line in enumerate(lines):
            spots = [s for s in self.truth.get(line, []) if s[0] >= cursor - 1e-6]
            start, end = spots[0] if spots else (cursor, cursor + 1.0)
            good = bool(spots) and (k > 0 or start - t0 <= 0.5)
            words = line.split()
            step = (end - start) / len(words)
            out.append([{"w": w, "t0": (cursor if j == 0 and k == 0 else start + j * step),
                         "t1": start + (j + 1) * step, "conf": 0.9 if good else 0.05}
                        for j, w in enumerate(words)])
            cursor = end
        return out


def _sung_track(path, spans, seconds=30):
    """Noise bursts where the 'voice' sings — the energy the onset and rise rules read."""
    sr = 22050
    rng = np.random.default_rng(0)
    y = np.zeros(int(sr * seconds), np.float32)
    for a, b in spans:
        y[int(a * sr):int(b * sr)] = 0.3 * rng.standard_normal(int(b * sr) - int(a * sr))
    sf.write(path, y, sr, subtype="PCM_16")


def test_align_places_lines_and_repeats_in_order(project):
    (project / "input/lyrics.txt").write_text("[후렴]\n아리랑 아리랑 아라리요\n아리랑 고개로 넘어간다\n\n[후렴]\n",
                                              encoding="utf-8")
    spans = [(2.0, 5.0), (5.6, 8.6), (16.0, 19.0), (19.6, 22.6)]
    _sung_track(project / "input/vocals.wav", spans)
    a, b = "아리랑 아리랑 아라리요", "아리랑 고개로 넘어간다"
    result = align.propose(project, backend=FakeAligner({a: [spans[0], spans[2]], b: [spans[1], spans[3]]}))
    assert result["counts"]["forced"] == 4 and result["counts"]["unplaced"] == 0
    proposal = read(project / "lyrics/align_proposal.json")
    starts = [c["start_ms"] for c in proposal["cues"]]
    assert all(abs(s - e * 1000) <= 150 for s, e in zip(starts, [x[0] for x in spans])), starts
    assert proposal["review"] is None and proposal["repeat_expansions"], "repeat expansion is proposed"
    timed = read(project / "lyrics/lyrics_timed.json")
    assert not timed.get("cues"), "the proposal never writes authoritative timing"
    from engine.lyrics import save_timing
    saved = save_timing(project, proposal)          # imports as-is, unreviewed
    assert saved["review"] is None and not saved["unresolved_row_ids"]


def test_align_never_guesses_edge_lines(project):
    (project / "input/lyrics.txt").write_text("첫 줄 가사\n둘째 줄 가사\n없는 줄\n", encoding="utf-8")
    _sung_track(project / "input/vocals.wav", [(3.0, 6.0), (7.0, 10.0)])
    truth = {"첫 줄 가사": [(3.0, 6.0)], "둘째 줄 가사": [(7.0, 10.0)]}
    align.propose(project, backend=FakeAligner(truth))
    report = read(project / "lyrics/align_report.json")
    assert report["counts"] == {"forced": 2, "forced+repair": 0, "interp": 0, "unplaced": 1}
    assert [e["reason"] for e in report["ear_check"]] == ["unplaced"]


def test_align_interpolates_between_placed_neighbours():
    placed = [{"t0": 1.0, "t1": 2.0}, None, None, {"t0": 8.0, "t1": 9.0}, None]
    out = align.interpolate(placed, 20.0)
    assert out[1]["src"] == out[2]["src"] == "interp" and 2.0 <= out[1]["t0"] < out[2]["t0"] < 8.0
    assert out[4] is None, "after the last placed line nothing is guessed"


def test_align_energy_rise_skips_the_lead_in():
    R = np.zeros(1000)
    R[400:450] = 1.0                                   # voice starts at 4.00 s
    word = {"t0": 0.0, "t1": 4.5}                      # a forced first token that absorbed the lead-in
    assert abs(align.rise(R, word) - 4.0) < 0.02
    assert align.rise(R, {"t0": 3.9, "t1": 4.2}) == 3.9, "short tokens keep their start"


def test_propose_needs_the_local_aligner(project, monkeypatch):
    import builtins
    real = builtins.__import__

    def deny(name, *args, **kwargs):
        if name.startswith("faster_whisper"):
            raise ImportError(name)
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(FilmError, match="로컬 정렬기"):
        align.propose(project)


# ---- take sync measurement -------------------------------------------------------

def _vocal_copy_take(p, tmp_path, shot_id, delay_ms=0):
    """A take whose audio is the vocal reference, optionally delayed — like a
    Seedance-style service that copies @Audio1 into its own soundtrack."""
    ref = sync_check.vocal_reference(p, shot_id)
    ref_path = tmp_path / "ref.wav"
    shutil.copyfile(p / ref["path"], ref_path)
    clip = tmp_path / "clip.mp4"
    seconds = _shot(p, shot_id)["duration_ms"] / 1000 + 0.5
    if delay_ms:
        _clip(clip, seconds, audio=(ref_path, f"adelay={delay_ms}|{delay_ms}"))
    else:
        _clip(clip, seconds, audio=ref_path)
    take = import_asset(p, shot_id, clip, kind="draft")
    conti_brief.check_take(p, shot_id)
    return take


def _supplied_voice(p, seconds=30):
    t = np.arange(int(22050 * seconds)) / 22050
    tone = 0.4 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 440 * t)
    sf.write(p / "input/vocals.wav", tone, 22050, subtype="PCM_16")


def test_sync_measures_a_shifted_copy(project, tmp_path):
    _supplied_voice(project)
    _conti(project, "S001")
    take = _vocal_copy_take(project, tmp_path, "S001", delay_ms=300)
    out = sync_check.measure_take(project, "S001")
    assert out["status"] == "measured" and out["mode"] == "copy"
    assert 0.25 < out["lag_s"] < 0.35, f"measured lag {out['lag_s']} for a 300 ms delay"
    record = conti_brief.load_check(project, "S001", take["sha256"])
    assert record["sync"]["lag_s"] == out["lag_s"]


def test_sync_reports_a_silent_take_as_unmeasurable(project, tmp_path):
    _conti(project, "S001")
    take = import_asset(project, "S001", _clip(tmp_path / "mute.mp4", 6), kind="draft")
    conti_brief.check_take(project, "S001")
    out = sync_check.measure_take(project, "S001")
    assert out["status"] == "unmeasurable" and "no audio" in out["reason"]
    assert conti_brief.load_check(project, "S001", take["sha256"])["sync"]["status"] == "unmeasurable"


def test_cli_align_sync_revision(project, tmp_path, capsys):
    _supplied_voice(project)
    _conti(project, "S001")
    take = _vocal_copy_take(project, tmp_path, "S001")
    assert cli.main(["conti-sync", str(project), "S001"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "measured" and out["mode"] == "copy"
    assert cli.main(["conti-revision", str(project), "S001",
                     "--instruction", "카메라를 더 천천히 밀어주세요"]) == 0
    assert json.loads(capsys.readouterr().out)["revises_take"] == take["sha256"]


# ---- revision records --------------------------------------------------------------

def test_revision_chain_lives_in_the_check_records(project, tmp_path):
    doc = _conti(project, "S001")
    with pytest.raises(FilmError, match="no checked take"):
        conti_brief.record_revision(project, "S001", "raise the hand on the second line")
    first = import_asset(project, "S001", _clip(tmp_path / "take1.mp4", 6), kind="draft")
    conti_brief.check_take(project, "S001", service="local", units=0, human_minutes=1)
    req = conti_brief.record_revision(project, "S001", "raise the hand on the second line")
    assert req["revises_take"] == first["sha256"] and req["fulfilled_by"] is None
    # the person changes ONE conti field (the gesture beat), the brief regenerates
    blocking = [dict(b, motion_beats=[dict(b["motion_beats"][0]),
                                      {"at_ms": b["motion_beats"][1]["at_ms"], "action": "raises the right hand"}])
                for b in doc["blocking"]]
    _conti(project, "S001", blocking=blocking)
    second = import_asset(project, "S001", _clip(tmp_path / "take2.mp4", 6.5), kind="draft")
    record = conti_brief.check_take(project, "S001")
    rev = record["revision"]
    assert rev["instruction"] == "raise the hand on the second line" and rev["revises_take"] == first["sha256"]
    assert rev["conti_changed"] and all(c.startswith("blocking[") for c in rev["conti_changed"])
    assert "raises the right hand" in rev["prompt_diff"]["video_prompt"]
    old = conti_brief.load_check(project, "S001", first["sha256"])
    assert old["revision_request"]["fulfilled_by"] == second["sha256"]
    assert sorted(f.name for f in (project / "qc/conti/S001").glob("*.json")) == sorted(
        [first["sha256"][:16] + ".json", second["sha256"][:16] + ".json"]), "no extra record file"
    metrics = conti_brief.conti_metrics(project)
    row = next(r for r in metrics["shots"] if r["shot_id"] == "S001")
    assert row["attempts"] == 2 and row["revisions"] == 1
    with pytest.raises(FilmError, match="instruction"):
        conti_brief.record_revision(project, "S001", "  ")


# ---- persisted requests --------------------------------------------------------------

def test_conti_and_judge_requests_are_persisted(project, tmp_path):
    import hashlib
    request = conti.conti_request(project, ["S001"])
    stored = project / request["request_file"]
    assert stored.read_bytes() == request["text"].encode("utf-8")
    assert request["request_file"] == f"conti/requests/{request['request_id']}.txt"
    doc = _conti(project, "S001")
    conti.submit_answer(project, {"conti": [doc]}, request_id=request["request_id"])
    stored_doc = conti.load_conti(project, "S001")
    assert stored_doc["request_sha256"] == hashlib.sha256(stored.read_bytes()).hexdigest()
    take = import_asset(project, "S001", _clip(tmp_path / "take.mp4", 6), kind="draft")
    before = conti_brief.check_take(project, "S001")
    assert before["conti_sha256"] == conti_brief.conti_digest({k: v for k, v in stored_doc.items()
                                                                 if k != "request_sha256"})
    judged = conti_brief.judge_request(project, "S001")
    saved = project / judged["request_file"]
    assert saved.read_text(encoding="utf-8") == judged["text"]
    assert saved.name.startswith(f"{take['sha256'][:16]}.judge-") and saved.suffix == ".txt"
    assert conti_brief.conti_metrics(project)["totals"]["attempts"] == 1, "the request file is not a take"
    assert conti_brief.load_check(project, "S001")["take_sha256"] == take["sha256"]
    with pytest.raises(FilmError, match="16-hex"):
        conti.submit_answer(project, {"conti": [doc]}, request_id="nope")


def test_handoff_requests_are_logged(project):
    handoff.world_request(project)
    log = (project / "bible" / "director_requests.jsonl").read_text(encoding="utf-8")
    entry = json.loads(log.strip().splitlines()[-1])
    assert entry["kind"] == "world" and "=== 입력 자료" in entry["text"]


# ---- far field and the sung/motion split ----------------------------------------------

def test_far_identity_on_wide_shots(project):
    chars = read(project / "bible/characters.yaml")
    chars["characters"][0].update(look="adult woman, red coat, black bob hair, round glasses",
                                  far="woman in a red coat")
    write(project / "bible/characters.yaml", chars)
    _conti(project, "S001", framing="wide establishing shot, empty street")
    frame = conti_brief.shot_brief(project, "S001")["first_frame_prompt"]
    assert "woman in a red coat" in frame and "round glasses" not in frame
    _conti(project, "S001", framing="medium close-up, slightly low angle")
    frame = conti_brief.shot_brief(project, "S001")["first_frame_prompt"]
    assert "black bob hair" in frame


def test_sung_vs_motion_prompt(project):
    _conti(project, "S001")
    brief = conti_brief.shot_brief(project, "S001")
    assert brief["sung"] is True
    assert "lip-syncs the attached vocal exactly" in brief["video_prompt"]
    assert "“첫 줄 가사”" in brief["video_prompt"]
    assert "Only the lead sings" in brief["video_prompt"]
    _conti(project, "S001", lyric_anchors=[])
    brief = conti_brief.shot_brief(project, "S001")
    assert brief["sung"] is False and "Nobody's lips form words" in brief["video_prompt"]
    video = handoff.video_brief(project, "S001")
    assert video["sung"] is False and "conti-check" in video["after_import"]


# ---- the production block and minimal gates -------------------------------------------

def test_init_writes_the_production_block(project):
    block = read(project / "project.yaml")["production"]
    assert block["gates"] == "minimal" and "image" in block["services"]


def test_minimal_gates_skip_lock_for_the_manual_path(project, tmp_path):
    providers.select(project, "image", "manual_image")
    providers.select(project, "video", "manual_video")
    assert autopilot(project)["stage"] == "NEEDS_MANUAL_FRAMES"
    for shot in read(project / "manifest/shots.json"):
        image = tmp_path / f"{shot['id']}.png"
        Image.new("RGB", (160, 90), "white").save(image)
        import_frame(project, shot["id"], image)
    step = autopilot(project)
    assert step["stage"] == "NEEDS_MANUAL_VIDEO", "minimal gates: no lock ceremony on the manual path"
    for shot in read(project / "manifest/shots.json"):
        if shot["render_mode"] != "STATIC":
            import_asset(project, shot["id"], _clip(tmp_path / f"{shot['id']}.mp4", 6), kind="draft")
    step = autopilot(project)
    assert step["stage"] == "PREVIEW_READY" and Path(step["build"]).is_dir()


def test_full_gates_still_need_the_lock(project, tmp_path):
    config = read(project / "project.yaml")
    config["production"]["gates"] = "full"
    write(project / "project.yaml", config)
    providers.select(project, "image", "manual_image")
    providers.select(project, "video", "manual_video")
    autopilot(project)
    for shot in read(project / "manifest/shots.json"):
        image = tmp_path / f"{shot['id']}.png"
        Image.new("RGB", (160, 90), "white").save(image)
        import_frame(project, shot["id"], image)
    assert autopilot(project)["stage"] == "NEEDS_LOCK"


# ---- the W00 approver from settings ------------------------------------------------------

def test_primary_approver_comes_from_settings(project):
    assert w00_gate.primary_approver() == "박준태"
    set_settings("w00_gate", {"primary_approver": "이감독"})
    assert w00_gate.primary_approver() == "이감독"
    config = read(project / "project.yaml")
    config["production"]["approver"] = "김감독"
    write(project / "project.yaml", config)
    assert w00_gate.primary_approver(project) == "김감독", "the project's production block wins"
