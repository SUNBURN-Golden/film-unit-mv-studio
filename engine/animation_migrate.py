"""Explicit LEGACY_MV -> FRAME_ANIMATION_V1 conversion (ANIM-002).

Runs only as an explicit user action (`animation-init`). Before writing, the
original project metadata is backed up under `migrations/` with exclusive
creation. Legacy ms boundaries are mapped once through `frame_at`; the first
timeline preserves the current edit as HARD_CUT / overlap 0 entries. Twos,
crossfades, motion intent and production paths are never inferred, and no
legacy review or LOCK becomes a new-mode approval.
"""
import hashlib
import os
import shutil
from pathlib import Path

from .animation_schema import (canon_bytes, derive_timeline_view,
                               validate_animation_timeline, write_canon)
from .core import (FilmError, digest, frame_at, now, project_mutex, read,
                   validate_manifest, write)
from .schema import (ANIMATION_PROFILE, ANIMATION_PROJECT_SCHEMA,
                     ANIMATION_SCHEMA_VERSIONS, _check_version)

TIMELINE_PATH = "timeline/edit.json"
DERIVED_PATH = "timeline/derived.json"

# Files the conversion itself is allowed to create or change. Everything else
# — master, lyrics, cues, manifests, LOCK, takes, builds — must hash identically
# before and after the run.
ALLOWED_WRITES = {"project.yaml"}
IGNORED_FILES = {".compile.lock"}
IGNORED_DIRS = {"migrations", "timeline"}


def _snapshot(p):
    """Preserved-byte map: the conversion may write only project.yaml here."""
    return {str(f.relative_to(p)): digest(f)
            for f in sorted(p.rglob("*"))
            if f.is_file() and f.name not in IGNORED_FILES
            and f.relative_to(p).parts[0] not in IGNORED_DIRS}


def _backup_file(source, destination):
    """Exclusive backup; a conflicting earlier backup aborts the conversion."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = Path(source).read_bytes()
    try:
        with destination.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        existing = destination.read_bytes()
        if existing != data:
            raise FilmError(f"Existing migration backup does not match: {destination.name}")
    return hashlib.sha256(data).hexdigest()


def animation_init(project, profile="frame-animation-v1"):
    """Convert a LEGACY_MV project to FRAME_ANIMATION_V1 in one explicit step."""
    if profile not in {"frame-animation-v1", ANIMATION_PROFILE}:
        raise FilmError("animation-init supports only the FRAME_ANIMATION_V1 profile")
    p = Path(project)
    with project_mutex(p):
        config = read(p / "project.yaml")
        current = config.get("production_profile") or "LEGACY_MV"
        if current != "LEGACY_MV":
            raise FilmError(f"animation-init converts only LEGACY_MV projects (found {current})")
        _check_version(config)
        for target in (p / TIMELINE_PATH, p / DERIVED_PATH):
            if target.exists():
                raise FilmError(f"{target.relative_to(p)} already exists; refusing to overwrite it")
        shots = read(p / "manifest/shots.json")
        audio = read(p / "analysis/audio.json")
        fps = config["format"]["fps"]
        duration_ms = audio["duration_ms"]
        # The preserved edit must already be valid; nothing is silently repaired.
        validate_manifest(shots, duration_ms, fps)

        before = _snapshot(p)
        source_hash = digest(p / "project.yaml")
        backup_dir = p / "migrations" / f"animation-init-{source_hash[:16]}"
        backup = {"project.yaml": _backup_file(p / "project.yaml", backup_dir / "project.yaml")}
        for folder in ("manifest", "analysis", "lyrics"):
            root = p / folder
            if not root.is_dir():
                continue
            for source in sorted(root.rglob("*")):
                if not source.is_file():
                    continue
                relative = str(source.relative_to(p))
                backup[relative] = _backup_file(source, backup_dir / relative)

        # One-time ms -> frame mapping; boundary rounding is reported, not hidden.
        output_frames = frame_at(duration_ms, fps)
        entries, mapping, differences, sub_frame = [], [], [], []
        for index, shot in enumerate(shots):
            in_frame = frame_at(shot["in_ms"], fps)
            out_frame = frame_at(shot["out_ms"], fps)
            length = out_frame - in_frame
            instance = f"I{index + 1:03d}"
            row = {"shot_id": shot["id"], "instance_id": instance,
                   "in_ms": shot["in_ms"], "out_ms": shot["out_ms"],
                   "duration_ms": shot["duration_ms"],
                   "in_frame": in_frame, "out_frame": out_frame, "frames": length,
                   "in_remainder_ms_fps": (shot["in_ms"] * fps) % 1000,
                   "out_remainder_ms_fps": (shot["out_ms"] * fps) % 1000}
            mapping.append(row)
            if row["in_remainder_ms_fps"] or row["out_remainder_ms_fps"]:
                differences.append(shot["id"])
            if length < 1:
                sub_frame.append(shot["id"])
            entries.append({"instance_id": instance, "shot_id": shot["id"],
                            "sequence_revision": None,
                            "used_source_range": [0, length],
                            "unused_handles": {"before": 0, "after": 0},
                            "transition_out": None})
        if sub_frame:
            raise FilmError("Shots shorter than one frame cannot be converted: "
                            + ", ".join(sub_frame))
        for index, entry in enumerate(entries[:-1]):
            entry["transition_out"] = {
                "id": f"T{index + 1:03d}", "type": "HARD_CUT",
                "to_instance": entries[index + 1]["instance_id"],
                "overlap_frames": 0}
        timeline = {"document_type": "animation_timeline", "schema_version": 1,
                    "target_frames": output_frames, "entries": entries}
        layout = validate_animation_timeline(timeline, output_frames)
        derived = derive_timeline_view(timeline)
        timeline_sha = hashlib.sha256(canon_bytes(timeline)).hexdigest()

        write_canon(p / TIMELINE_PATH, timeline)
        write_canon(p / DERIVED_PATH, derived)
        updated = dict(config)
        updated["schema_version"] = ANIMATION_PROJECT_SCHEMA
        updated["production_profile"] = ANIMATION_PROFILE
        updated["schema_versions"] = {**config.get("schema_versions", {}),
                                      **ANIMATION_SCHEMA_VERSIONS}
        updated["animation"] = {
            "schema_version": 1,
            "output_frames": output_frames,
            "timeline": TIMELINE_PATH,
            "assets": "manifest/animation_assets.json",
            "roles": "production/roles.json",
            "schedule": "production/schedule.json",
            "storage_archive": "manifest/storage_archive.json",
            "execution_plan": "execution/plan.json",
            "initial_wave": "W00",
            "delivery_sequence": "SUBBED",
        }
        write(p / "project.yaml", updated)

        after = _snapshot(p)
        changed = sorted(name for name in set(before) | set(after)
                         if before.get(name) != after.get(name))
        if changed != ["project.yaml"]:
            # Conversion must never be half-applied: restore the declaration
            # and drop the new-mode files, then report the violation.
            (p / "project.yaml").write_bytes((backup_dir / "project.yaml").read_bytes())
            if (p / "timeline").is_dir():
                shutil.rmtree(p / "timeline")
            raise FilmError("Conversion touched preserved project bytes: "
                            + ", ".join(changed))
        report = {
            "operation": "animation-init",
            "converted_at": now(),
            "from": {"schema_version": config.get("schema_version", "0.1"),
                     "production_profile": "LEGACY_MV",
                     "schema_versions": config.get("schema_versions", {})},
            "to": {"schema_version": ANIMATION_PROJECT_SCHEMA,
                   "production_profile": ANIMATION_PROFILE,
                   "schema_versions": updated["schema_versions"]},
            "fps": fps, "duration_ms": duration_ms, "output_frames": output_frames,
            "backup_dir": str(backup_dir.relative_to(p)),
            "backup": backup,
            "before_sha256": source_hash,
            "after_sha256": digest(p / "project.yaml"),
            "timeline": {"path": TIMELINE_PATH, "sha256": timeline_sha,
                         "entries": len(entries),
                         "transitions": sum(1 for e in entries if e["transition_out"])},
            "derived": {"path": DERIVED_PATH,
                        "entries": layout["entries"], "transitions": layout["transitions"]},
            "mapping": mapping,
            "mapping_differences": differences,
            "sub_one_frame_shots": sub_frame,
            "approvals": {"inherited": False, "legacy_locks_copied": 0,
                          "legacy_reviews_copied": 0, "animation_approvals_created": 0},
            "preserved": {"verified_unchanged": len(after) - len(ALLOWED_WRITES),
                          "changed_paths": [name for name in changed if name in ALLOWED_WRITES]},
            "note": "ms boundaries were mapped once with frame_at; listed shots had off-grid boundaries and keep their nearest frame. All first transitions are HARD_CUT/overlap 0. No motion intent, twos, crossfade or production path was inferred. Legacy reviews and LOCK stay legacy; nothing became an animation approval.",
        }
        write(backup_dir / "migration.json", report)
        return report
