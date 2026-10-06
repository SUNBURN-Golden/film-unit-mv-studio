"""Explicit LEGACY_MV -> FRAME_ANIMATION_V1 conversion (ANIM-002).

Runs only as an explicit user action (`animation-init`). All validation and
computation finish before anything is written, so a refused conversion leaves
the project byte-identical. Before writing, the original project metadata is
backed up under `migrations/` with exclusive creation, keyed on the bytes being
preserved. Legacy ms boundaries are mapped once through `frame_at`; the first
timeline preserves the current edit as HARD_CUT / overlap 0 entries. Twos,
crossfades, motion intent and production paths are never inferred, and no
legacy review or LOCK becomes a new-mode approval.
"""
import hashlib
import os
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
BACKUP_DIRS = ("manifest", "analysis", "lyrics")


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


def _backup_sources(p):
    """project.yaml plus every manifest/analysis/lyrics file being preserved."""
    sources = [p / "project.yaml"]
    for folder in BACKUP_DIRS:
        root = p / folder
        if root.is_dir():
            sources += [f for f in sorted(root.rglob("*")) if f.is_file()]
    return sources


def _backup_dir(p, sources=None):
    """Backup dir keyed on the bytes it preserves, not only project.yaml.

    A retry after the user edits an input hashes to a fresh directory; an
    identical retry reuses the matching backup. The mismatch check still
    refuses to overwrite an existing backup holding different bytes.
    """
    key = hashlib.sha256()
    for source in (sources if sources is not None else _backup_sources(p)):
        key.update(str(source.relative_to(p)).encode())
        key.update(b"\0")
        key.update(digest(source).encode())
        key.update(b"\0")
    return p / "migrations" / f"animation-init-{key.hexdigest()[:16]}"


def _claimed_by_completed_migration(p):
    """True when a finished animation-init report claims the existing timeline."""
    edit = p / TIMELINE_PATH
    if not edit.exists():
        return False
    sha = hashlib.sha256(edit.read_bytes()).hexdigest()
    migrations = p / "migrations"
    if not migrations.is_dir():
        return False
    for report_path in sorted(migrations.glob("*/migration.json")):
        try:
            report = read(report_path)
        except Exception:
            continue
        if (isinstance(report, dict)
                and report.get("operation") == "animation-init"
                and report.get("timeline", {}).get("sha256") == sha):
            return True
    return False


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
        if ((p / TIMELINE_PATH).exists() or (p / DERIVED_PATH).exists()):
            # On a still-LEGACY_MV project these files are leftovers of an
            # interrupted init and get replaced below — unless a completed
            # migration report claims them, which is never overwritten.
            if _claimed_by_completed_migration(p):
                raise FilmError(f"{TIMELINE_PATH} already exists; refusing to overwrite it")
        shots = read(p / "manifest/shots.json")
        audio = read(p / "analysis/audio.json")
        fps = config["format"]["fps"]
        duration_ms = audio["duration_ms"]
        # The preserved edit must already be valid; nothing is silently repaired.
        validate_manifest(shots, duration_ms, fps)

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

        # Validation is done; only now does anything get written. The backup is
        # keyed on the preserved bytes so an edited-input retry cannot collide
        # with an earlier attempt's backup.
        before = _snapshot(p)
        source_hash = digest(p / "project.yaml")
        sources = _backup_sources(p)
        backup_dir = _backup_dir(p, sources)
        backup = {}
        for source in sources:
            relative = str(source.relative_to(p))
            backup[relative] = _backup_file(source, backup_dir / relative)

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
        try:
            # Interrupted-init leftovers are replaced; write_canon is atomic.
            write_canon(p / TIMELINE_PATH, timeline)
            write_canon(p / DERIVED_PATH, derived)
            write(p / "project.yaml", updated)
            after = _snapshot(p)
            changed = sorted(name for name in set(before) | set(after)
                             if before.get(name) != after.get(name))
            if changed != ["project.yaml"]:
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
            # The completed report is written last: a project whose timeline
            # has no report is an interrupted init, not a finished conversion.
            write(backup_dir / "migration.json", report)
        except Exception:
            # Never leave a half-applied conversion: restore the declaration
            # and drop the new-mode files so a retry starts clean.
            (p / "project.yaml").write_bytes((backup_dir / "project.yaml").read_bytes())
            for target in (p / TIMELINE_PATH, p / DERIVED_PATH):
                target.unlink(missing_ok=True)
            try:
                (p / "timeline").rmdir()
            except OSError:
                pass
            raise
        return report
