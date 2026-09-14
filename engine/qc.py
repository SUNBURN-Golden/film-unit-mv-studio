"""Separate measurable technical checks from evidence-backed semantic review."""
from pathlib import Path
from fractions import Fraction
import math
import numpy as np
from PIL import Image
from .core import FilmError, digest, ffmpeg, frame_at, probe, read, visual_context_fingerprint, write
from .resolver import clip_review_fingerprint

SEMANTIC_ITEMS = ["character_identity", "style", "composition", "palette", "camera", "background", "props", "unwanted_text", "anatomy", "motion"]


def inspect_clip(clip, shot, project, fmt, generated, production_id, source_in_ms=0):
    p, clip = Path(project), Path(clip)
    clip_hash = digest(clip)
    result = {"shot_id": shot["id"], "clip_path": str(clip.relative_to(p)), "clip_sha256": clip_hash,
        "production_id": production_id, "generated": generated, "technical": {},
        "semantic": {k: None for k in SEMANTIC_ITEMS}, "failures": [], "status": "PENDING"}
    result.update(visual_context_id=visual_context_fingerprint(p), source_in_ms=source_in_ms)
    result["review_binding"] = clip_review_fingerprint(p, shot, clip_hash, source_in_ms, result["visual_context_id"])
    try:
        info = probe(clip)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        count = int(video.get("nb_frames", 0))
        fps = float(Fraction(video["avg_frame_rate"]))
        expected = frame_at(shot["out_ms"], fmt["fps"]) - frame_at(shot["in_ms"], fmt["fps"])
        duration = float(video.get("duration", info["format"]["duration"]))
        result["technical"] = {
            "resolution": [video["width"], video["height"]], "fps": fps, "duration_ms": round(duration*1000),
            "frames": count, "expected_frames": expected,
            "aspect_ratio_ok": abs(video["width"] / video["height"] - fmt["width"] / fmt["height"]) < 0.002,
            "fps_ok": abs(fps - fmt["fps"]) < 0.001,
            "frame_count_ok": count == expected,
            "no_audio": not any(s["codec_type"] == "audio" for s in info["streams"]),
        }
        for key in ["aspect_ratio_ok", "fps_ok", "frame_count_ok", "no_audio"]:
            if not result["technical"][key]:
                result["failures"].append(key)
        folder = p / "qc/frames" / f"{shot['id']}_{clip_hash[:12]}"
        folder.mkdir(parents=True, exist_ok=True)
        indices = sorted(set(round((count-1)*f) for f in [0, .25, .5, .75, 1]))
        selection = "+".join(f"eq(n\\,{i})" for i in indices)
        ffmpeg(["-i", clip, "-an", "-vf", f"select='{selection}',scale=480:-1", "-fps_mode", "vfr", folder / "frame_%02d.jpg"])
        frames = sorted(folder.glob("frame_*.jpg"))
        if len(frames) != len(indices):
            result["failures"].append("sample_decode_failed")
        result["frame_samples"] = [{"frame_index": i, "path": str(f.relative_to(p))} for i, f in zip(indices, frames)]
        if frames:
            means = [np.asarray(Image.open(f).convert("RGB"), dtype=float).mean(axis=(0, 1)).tolist() for f in frames]
            result["diagnostics"] = {"mean_rgb_per_frame": means, "note": "Color statistics only; not an identity or style score"}
    except (FilmError, KeyError, StopIteration, ValueError, ZeroDivisionError) as e:
        result["failures"].append("decode_or_metadata_error: " + str(e)[:300])
    if result["failures"]:
        result["status"] = "FAIL"
        return result
    if not generated:
        result["status"] = "PASS_LOCAL"
        result["semantic_note"] = "No AI video generated. Local technical checks passed; no semantic model score claimed."
        return result
    review_path = p / "qc/reviews" / f"{shot['id']}_{clip_hash[:12]}.json"
    review = read(review_path, {})
    if (review.get("schema_version") != 2 or review.get("clip_sha256") != clip_hash or
            review.get("review_binding") != result["review_binding"]):
        result["status"] = "NEEDS_REVIEW"
        result["review_file"] = str(review_path.relative_to(p))
        return result
    scores = review.get("scores", {})
    valid = all(type(scores.get(k)) in {int, float} and math.isfinite(scores[k]) and 0 <= scores[k] <= 100 for k in SEMANTIC_ITEMS)
    if not valid or not review.get("reviewer") or not review.get("notes"):
        result["status"] = "NEEDS_REVIEW"
        result["failures"] = ["Semantic review requires all scores, reviewer and evidence notes"]
        return result
    threshold = read(p / "project.yaml")["qc"]["threshold"]
    result["semantic"] = scores
    result["reviewer"] = review["reviewer"]
    result["evidence_notes"] = review["notes"]
    result["semantic_mean"] = round(sum(scores.values()) / len(SEMANTIC_ITEMS), 2)
    # Every dimension must pass. A missing tie must not be hidden by a high average.
    result["failures"] = [f"{k}: {scores[k]} < {threshold}; {review['notes']}" for k in SEMANTIC_ITEMS if scores[k] < threshold]
    result["status"] = "FAIL" if result["failures"] else "PASS"
    return result


def save_review(project, record, scores, reviewer, notes):
    if not reviewer.strip() or not notes.strip():
        raise FilmError("Record reviewer and evidence notes")
    write(Path(project) / "qc/reviews" / f"{record['shot_id']}_{record['clip_sha256'][:12]}.json", {
        "clip_sha256": record["clip_sha256"], "production_id": record["production_id"],
        "schema_version": 2, "visual_context_id": record.get("visual_context_id"),
        "review_binding": record.get("review_binding"),
        "scores": scores, "reviewer": reviewer, "notes": notes,
    })
