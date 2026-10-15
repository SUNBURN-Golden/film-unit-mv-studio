"""Stage 2 of the real run: package -> cues -> hard-cut shot -> conti (persisted request)
-> take 1 -> check -> Preview B1 -> revision -> take 2 -> check -> Preview B2 -> invariants."""
import json, shutil, subprocess, sys, time
from pathlib import Path
WT = Path("/workspace/film-run/wt-claudia"); sys.path.insert(0, str(WT))
from engine import conti, conti_brief, sync_check
from engine.core import digest, read, write
from engine.production import make_package
from engine.lyrics import save_timing, validate_lyrics, timing_fingerprint
from engine.timeline import split_shot, move_cut
from engine.compiler import import_asset, compile_preview
from engine.builds import verify_build

P = WT / "projects/arirang_hardcut"; EV = WT / "docs/evidence/claudia-hardcut-01"; EV.mkdir(parents=True, exist_ok=True)
RUN = Path("/tmp/claudia-run"); PY = "/tmp/claudia-venv/bin/python"
clock = lambda: float(open("/proc/uptime").read().split()[0])
T = {"start": clock()}; log = {"steps": []}
def step(name, **kw):
    log["steps"].append({"step": name, "t_s": round(clock() - T["start"], 1), **kw}); print(name, kw, flush=True)

cfg = read(P / "project.yaml"); master_sha = cfg["audio"]["sha256"]
if not (P / "manifest/shots.json").exists():
    make_package(P, target_shot_ms=8000)
# the user's character + 2D style (task §3: not Claudia, not photoreal)
chars = read(P / "bible/characters.yaml")
c0 = chars["characters"][0]
c0.update(name="Haneul", look="young Korean woman, early 20s, short black bob hair blown by wind, round face, "
          "teal jacket over a white shirt, drawn as 2D cel animation with thin black ink outlines",
          far="small figure in a teal jacket with a black bob", behavior="sings with restrained, open gestures")
write(P / "bible/characters.yaml", chars)
style = read(P / "bible/style.yaml", {})
style["visual_style"] = {"medium": "2D cel animation", "background": "warm gray paper", "line": "thin black ink",
                         "shading": "flat, minimal", "camera": "slow pushes only"}
style["rules"] = ["no photorealism", "no text in frame"]
write(P / "bible/style.yaml", style)
step("package", character=c0["id"])

# cues: import the aligner's proposal as-is (UNREVIEWED: nobody has listened)
proposal = read(P / "lyrics/align_proposal.json")
save_timing(P, proposal)
doc, warns = validate_lyrics(P, read(P / "analysis/audio.json")["duration_ms"])
fp_before = timing_fingerprint(doc)
step("lyrics-import", cues=len(proposal["cues"]), reviewed=False, warnings=warns)
report = read(P / "lyrics/align_report.json")
row = next(r for r in report["rows"] if r.get("src", "").startswith("forced") and r["text"].startswith("나를"))
cue_in, cue_out = row["start_ms"], row["end_ms"]
gasi = next(w for w in row["words"] if w[0].startswith("가시"))
in_ms = cue_in - 500; out_ms = in_ms + 8000
shots = read(P / "manifest/shots.json")
s = next(x for x in shots if x["in_ms"] <= in_ms < x["out_ms"])
if s["in_ms"] < in_ms:
    split_shot(P, s["id"], in_ms)
shots = read(P / "manifest/shots.json"); t = next(x for x in shots if x["in_ms"] == in_ms)
if t["out_ms"] > out_ms:
    split_shot(P, t["id"], out_ms)
elif t["out_ms"] < out_ms:
    move_cut(P, t["id"], out_ms)
shots = read(P / "manifest/shots.json"); SHOT = next(x for x in shots if x["in_ms"] == in_ms)["id"]
others_before = json.dumps([x for x in shots if x["id"] != SHOT], sort_keys=True)
step("hard-cut shot", shot=SHOT, in_ms=in_ms, out_ms=out_ms, render_mode=next(x for x in shots if x["id"] == SHOT)["render_mode"],
     line=row["text"], cue=[cue_in, cue_out], word_gasi_s=gasi[1])

# conti through the persisted request (answer written by Claude in this Claude Code session)
req = conti.conti_request(P, [SHOT], language="English")
shot = next(x for x in shots if x["id"] == SHOT); cast = shot.get("characters") or [c0["id"]]
def conti_doc(gesture_ms, gesture_note):
    return {"schema_version": 1, "shot_id": SHOT,
            "scene_goal": "Haneul sings the line to the hill pass the departing lover crossed; resolve, not tears",
            "lyric_anchors": [{"text": row["text"], "start_ms": cue_in, "end_ms": min(cue_out, out_ms),
                               "intent": "she sings it straight to the pass"}],
            "beat_map": [], "framing": "medium shot, character left third, the hill pass on the right",
            "camera": {"type": "push", "moves": [{"at_ms": in_ms + 200, "action": "slow push in (about 6%) over the whole shot"}]},
            "blocking": [{"character": c, "pose": "standing, facing the pass three-quarters", "expression": "steady, singing",
                          "motion_beats": [{"at_ms": gesture_ms, "action": gesture_note},
                                           {"at_ms": in_ms + 4500, "action": "turns her head toward the pass"}]} for c in cast],
            "lighting": "flat daylight on warm gray paper", "palette": ["#ECEAE4", "#181818", "#48A6A0"],
            "continuity": {"from_previous": "same hillside", "to_next": "she keeps facing the pass",
                           "props": [], "wardrobe": "teal jacket"},
            "references": [shot["references"][0]] if shot.get("references") else [],
            "transition_in": "hard cut", "transition_out": "hard cut",
            "acceptance_criteria": ["Haneul keeps the same face, hair and teal jacket in every frame",
                                    "her right hand rises toward the hill pass at the gesture beat, not before",
                                    "the camera only pushes in; there are no cuts",
                                    "the image stays 2D ink-on-paper, never photoreal; no text in frame"],
            "open_questions": []}
v1 = conti_doc(in_ms + 600, "raises her right hand toward the hill pass")
conti.submit_answer(P, {"conti": [v1]}, source="llm:claude-opus-5.5 (Claude Code subscription)", request_id=req["request_id"])
brief1 = conti_brief.shot_brief(P, SHOT)
step("conti v1", request=req["request_file"], conti_sha=brief1["conti_sha256"], clip_seconds=brief1.get("clip_seconds"),
     sung=brief1.get("sung"))

def render(name, gesture_s):
    spec = {"seconds": 8.0, "fps": 24, "size": [1920, 1080], "supersample": 1, "song_t0": in_ms / 1000,
            "vocals": str(P / "input/vocals.wav"), "push": [1.0, 1.06],
            "beats": [{"t": gesture_s, "dur": 0.7, "action": "raise_hand"},
                      {"t": 4.5, "dur": 0.8, "action": "turn_head"}]}
    (RUN / f"{name}.json").write_text(json.dumps(spec))
    t0 = clock()
    subprocess.run([PY, "-I", "/tmp/claudia-scripts/render_local_take.py", str(RUN / f"{name}.json"), str(RUN / f"{name}.mp4")], check=True)
    return RUN / f"{name}.mp4", round(clock() - t0, 1)

take1, sec1 = render("take1", 0.6)
a1 = import_asset(P, SHOT, take1, kind="draft")
r1 = conti_brief.check_take(P, SHOT, service="local-procedural (no generation)", units=0, human_minutes=0,
                            note="LOCAL_PROCEDURAL stand-in drawn by code; not an AI generation")
sync1 = sync_check.measure_take(P, SHOT)
j1 = conti_brief.judge_request(P, SHOT)
step("take 1", sha=a1["sha256"], render_s=sec1, verdict=r1["verdict"], mechanical={c["check"]: c["status"] for c in r1["mechanical"]},
     sync=sync1["status"], judge_request=j1["request_file"])
b1 = compile_preview(P)
step("preview 1", build=b1.get("build_id"), status=b1.get("status"))
b1_dir = Path(b1["build_dir"]); b1_files = {f.name: digest(f) for f in b1_dir.iterdir() if f.is_file()}

instruction = "손은 첫 박에 들지 말고 '가시는'에서 고개 쪽으로 드세요"
rev = conti_brief.record_revision(P, SHOT, instruction)
gesture_ms = int(round(gasi[1] * 1000))
v2 = conti_doc(gesture_ms, "raises her right hand toward the hill pass on the word '가시는'")
conti.submit_answer(P, {"conti": [v2]}, source="llm:claude-opus-5.5 (Claude Code subscription)", request_id=req["request_id"])
take2, sec2 = render("take2", round(gesture_ms / 1000 - in_ms / 1000, 2))
a2 = import_asset(P, SHOT, take2, kind="draft")
r2 = conti_brief.check_take(P, SHOT, service="local-procedural (no generation)", units=0, human_minutes=0,
                            note="LOCAL_PROCEDURAL stand-in drawn by code; not an AI generation")
sync2 = sync_check.measure_take(P, SHOT)
j2 = conti_brief.judge_request(P, SHOT)
step("revision + take 2", instruction=instruction, sha=a2["sha256"], render_s=sec2, verdict=r2["verdict"],
     conti_changed=r2["revision"]["conti_changed"], gesture_clip_s=round(gesture_ms / 1000 - in_ms / 1000, 2))
b2 = compile_preview(P)
step("preview 2", build=b2.get("build_id"), status=b2.get("status"))

# invariants
doc2, _ = validate_lyrics(P, read(P / "analysis/audio.json")["duration_ms"])
shots2 = read(P / "manifest/shots.json")
inv = {"master_sha_unchanged": digest(P / cfg["audio"]["path"]) == master_sha,
       "lyric_timing_fingerprint_unchanged": timing_fingerprint(doc2) == fp_before,
       "other_shots_unchanged": json.dumps([x for x in shots2 if x["id"] != SHOT], sort_keys=True) == others_before,
       "build1_files_unchanged": {f.name: digest(f) for f in b1_dir.iterdir() if f.is_file()} == b1_files}
try:
    vb = verify_build(b1_dir); inv["build1_verify"] = vb if isinstance(vb, (bool, str)) else (vb.get("status") or vb.get("ok") or "ran")
except Exception as e:
    inv["build1_verify"] = f"error: {e}"
step("invariants", **inv)
metrics = conti_brief.conti_metrics(P)
log.update(shot=SHOT, in_ms=in_ms, out_ms=out_ms, line=row["text"], builds=[b1.get("build_id"), b2.get("build_id")],
           takes=[a1["sha256"], a2["sha256"]], revision=r2["revision"], invariants=inv,
           metrics=next(r for r in metrics["shots"] if r["shot_id"] == SHOT), totals=metrics["totals"],
           master={"source": "Wikimedia Commons File:2021 아리랑 응원가 윤도현 가창 음원.flac (CC BY 4.0, Korea Copyright Commission)",
                   "flac_sha256": "498a0e7684cee199d4e8257e4795f2f3c5efcaf2218fa56c6fc00553e7773bba", "master_wav_sha256": master_sha},
           elapsed_s=round(clock() - T["start"], 1))
for name in ("lyrics/align_report.json", "lyrics/align_proposal.json", f"conti/{SHOT}.json", req["request_file"]):
    shutil.copyfile(P / name, EV / Path(name).name)
for f in (P / "qc/conti" / SHOT).glob("*.json"):
    shutil.copyfile(f, EV / f"check_{f.name}")
for k, b in (("before", b1), ("after", b2)):
    for sec in (1.0, 2.5, 4.0, 6.0):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{in_ms / 1000 + sec:.2f}", "-i", str(Path(b["build_dir"]) / "MASTER_SUBBED.mp4"),
                        "-frames:v", "1", "-vf", "scale=480:-2", str(EV / f"{k}_{sec:.1f}s.jpg")], check=False)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{in_ms / 1000:.2f}", "-t", "8", "-i", str(Path(b["build_dir"]) / "MASTER_SUBBED.mp4"),
                    "-vf", "scale=640:-2", "-c:v", "libx264", "-crf", "30", "-c:a", "aac", "-b:a", "96k", str(EV / f"scene_{k}.mp4")], check=False)
(EV / "run.json").write_text(json.dumps(log, indent=2, ensure_ascii=False))
print("STAGE2_DONE")
