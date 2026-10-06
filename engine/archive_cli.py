"""CLI-facing archive operations on a local backend (ANIM-013).

`archive-create` packs a folder of PNGs, uploads pack + pinned index with
the bounded transport, verifies and seals, then saves the manifest under
`<root>/manifests/`. `archive-restore` re-reads it and restores through
the bounded workspace into the destination. `archive-status` reports the
stored-object inventory and seal state. These commands use the local
backend — no OAuth, no Drive, no network.
"""
from pathlib import Path

from .core import FilmError
from .animation_schema import write_canon, read_canon, check_document
from .drive_archive import (archive_frames, archive_status,
                            restore_archive)
from .storage_backends.local import LocalArchiveBackend
from .workspace import Workspace

DEFAULT_CACHE_BYTES = 256 * 1024 * 1024


def _manifest_path(root, archive_id):
    return Path(root) / "manifests" / f"{archive_id}.json"


def archive_create(source, root, *, profile="LOCAL_FULL",
                   min_level="UPLOAD_HASH_MATCHED",
                   readback_cap=None, archive_id=None):
    source = Path(source)
    if not source.is_dir():
        raise FilmError(f"Archive source is not a folder: {source}")
    pngs = sorted(source.glob("*.png"))
    if not pngs:
        raise FilmError("The archive source holds no PNG members")
    members = [{"member_id": f.stem, "frame_index": i,
                "data": f.read_bytes()} for i, f in enumerate(pngs)]
    backend = LocalArchiveBackend(root)
    result = archive_frames(backend, members, profile=profile,
                            archive_id=archive_id or source.name,
                            min_level=min_level,
                            readback_cap=readback_cap or 64 * 1024 * 1024)
    path = _manifest_path(root, result["archive"]["archive_id"])
    write_canon(path, result["archive"])
    return {"archive_id": result["archive"]["archive_id"],
            "manifest": str(path),
            "members": len(members),
            "achieved_level": result["achieved_level"],
            "archive_sha256": result["archive_sha256"],
            "qualification": result["archive"]["qualification"]}


def _load_manifest(manifest):
    path = Path(manifest)
    doc = read_canon(path)
    check_document(doc, "storage_archive")
    return doc, path


def _root_for(manifest_path, root):
    if root:
        return Path(root)
    # manifests/<id>.json -> its grandparent is the archive root.
    if manifest_path.parent.name == "manifests":
        return manifest_path.parent.parent
    raise FilmError("Pass --root: the manifest is not under a 'manifests/' "
                    "directory")


def archive_restore(manifest, dest, *, root=None, workspace=None,
                    offline=False, whole_pack_cap=None, allowed_root=None):
    doc, path = _load_manifest(manifest)
    backend = LocalArchiveBackend(_root_for(path, root))
    cache = Workspace(workspace or Path(_root_for(path, root)) / "cache",
                      DEFAULT_CACHE_BYTES)
    # The restore boundary is a fixed configured root — the process working
    # directory unless the caller pins another one — never something
    # derived from the destination itself.
    result = restore_archive(
        backend, doc, cache, Path(dest).expanduser(),
        allowed_root=Path(allowed_root) if allowed_root else Path.cwd(),
        offline=offline,
        whole_pack_cap=whole_pack_cap or DEFAULT_CACHE_BYTES)
    return result


def archive_status_cli(manifest, *, root=None):
    doc, path = _load_manifest(manifest)
    backend = LocalArchiveBackend(_root_for(path, root))
    return archive_status(backend, doc)
