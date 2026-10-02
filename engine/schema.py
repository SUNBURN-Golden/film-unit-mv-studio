"""Independent data versions and explicit, lossless legacy metadata migration.

Legacy shot records already contain the timing and rendering fields required by
shot schema 2. Migration declares that compatibility without rewriting approved
production files. New optional fields are resolved with defaults by consumers.
"""
from pathlib import Path
import os

PROJECT_SCHEMA = 3
SHOT_SCHEMA = 2
LYRICS_SCHEMA = 1
BUILD_SCHEMA = 1
AUDIO_SCHEMA = 1
SCHEMA_VERSIONS = {
    "project": PROJECT_SCHEMA,
    "shot": SHOT_SCHEMA,
    "lyrics": LYRICS_SCHEMA,
    "build": BUILD_SCHEMA,
    "audio": AUDIO_SCHEMA,
}

# FRAME_ANIMATION_V1 declarations (ADR 0001): produced only by the explicit
# animation-init conversion. The legacy readers above keep rejecting version 4.
ANIMATION_PROFILE = "FRAME_ANIMATION_V1"
ANIMATION_PROJECT_SCHEMA = 4
ANIMATION_SHOT_SCHEMA = 3
ANIMATION_BUILD_SCHEMA = 2
ANIMATION_SCHEMA_VERSIONS = {
    "project": ANIMATION_PROJECT_SCHEMA,
    "shot": ANIMATION_SHOT_SCHEMA,
    "lyrics": LYRICS_SCHEMA,
    "audio": AUDIO_SCHEMA,
    "build": ANIMATION_BUILD_SCHEMA,
}


def schema_metadata():
    """Use a fresh mapping for every project; package versions are unrelated."""
    return {"schema_version": PROJECT_SCHEMA, "schema_versions": dict(SCHEMA_VERSIONS)}


def _check_version(config):
    from .core import FilmError
    version = config.get("schema_version", "0.1")
    supported = ("0.1", "0.2", "1", "2", "3", 1, 2, 3)
    if isinstance(version, bool) or version not in supported:
        raise FilmError(f"Unsupported project schema: {version}")
    versions = config.get("schema_versions", {})
    if not isinstance(versions, dict):
        raise FilmError("schema_versions must be an object")
    for key, value in versions.items():
        if key in SCHEMA_VERSIONS and (type(value) is not int or value > SCHEMA_VERSIONS[key] or value < 1):
            raise FilmError(f"Unsupported {key} schema: {value}")


def migrate_project(project):
    """Explicitly update metadata, preserving source assets, manifests and LOCK.

    An exact original project.yaml is saved before writing. Production fingerprint
    hashes the project format, not these metadata fields, so approval does not
    silently disappear or become re-authorized during migration.
    """
    from .core import FilmError, digest, now, project_mutex, read, write

    p = Path(project)
    with project_mutex(p):
        path = p / "project.yaml"
        config = read(path)
        _check_version(config)
        if config.get("schema_version") == PROJECT_SCHEMA and all(config.get("schema_versions", {}).get(key) == value for key, value in SCHEMA_VERSIONS.items()):
            return {"changed": False, "schema_versions": dict(SCHEMA_VERSIONS), "backup": None,
                    "production_files_changed": False, "lock_preserved": True}
        source_hash = digest(path)
        backup_dir = p / "migrations" / f"project-{source_hash}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / "project.yaml"
        # Exclusive creation protects the original if migration is resumed.
        try:
            with backup.open("xb") as stream:
                stream.write(path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if digest(backup) != source_hash:
                raise FilmError("Existing migration backup does not match project metadata")
        updated = dict(config)
        # Unknown metadata remains available to extensions.
        updated.update(schema_metadata())
        updated["schema_versions"] = {**config.get("schema_versions", {}), **SCHEMA_VERSIONS}
        write(path, updated)
        report = {
            "changed": True, "from_schema": config.get("schema_version", "0.1"),
            "schema_versions": dict(SCHEMA_VERSIONS), "migrated_at": now(),
            "backup": str(backup.relative_to(p)), "before_sha256": source_hash,
            "after_sha256": digest(path), "production_files_changed": False,
            "lock_preserved": True,
            "note": "Existing shots are compatible with schema 2; missing optional fields use defaults. No audio, lyrics, bibles, references, shot records, build records or LOCK were rewritten.",
        }
        write(backup_dir / "migration.json", report)
        return report
