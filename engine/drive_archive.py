"""Archive, restore and status on top of the verified primitives (ANIM-013).

`archive_frames` packs members into FAV1 bytes, uploads the pack and its
pinned `pack_index` through the bounded resumable transport, then verifies
the stored objects at the strongest available level and seals the
`storage_archive` manifest only when the configured minimum is met.

`restore_archive` reads only the needed member ranges through
`PackReader`, hash-checks and technically checks each member before it is
staged, and publishes `F_000001.png`-style application-generated names
into the destination only after the whole selection passed — a partial or
failed restore exposes nothing. Verified members land in the workspace's
VERIFIED_MEMBER class, so a resumed restore does not re-fetch them.

`offline=True` requires the whole pack to be downloaded and full-hash
verified inside the declared cap plus the space reservation to succeed —
only then may a restore report offline capability.
"""
from pathlib import Path
import os
import uuid

from .animation_schema import canon_bytes
from .archive_manifest import (DEFAULT_READBACK_CAP, DEFAULT_RETRY,
                               DEFAULT_UPLOAD, LEVELS, archive_sha,
                               make_archive, resumable_put, seal_archive,
                               validate_archive, verify_archive)
from .core import FilmError
from .fav_pack import (build_pack, image_contract_for, index_bytes,
                       make_index, sha256_bytes)
from .pack_reader import PackReader, fetch_index


def _object_id(data):
    return f"sha256-{sha256_bytes(data)}"


def archive_frames(backend, members, *, profile, image_contract=None,
                   frame_coverage=None, snapshot_digest=None,
                   recipe_digest=None, sequence_digest=None,
                   retention_refs=(), connection=None, archive_id=None,
                   transport=None, min_level="UPLOAD_HASH_MATCHED",
                   readback_cap=DEFAULT_READBACK_CAP, sleep_fn=None):
    """Pack members, upload, verify and seal an archive manifest.

    `backend` may be a raw backend or a DriveSession's `authorized`
    wrapper; every call still passes its grant boundary.
    """
    transport = transport or {"retry": DEFAULT_RETRY,
                              "upload": dict(DEFAULT_UPLOAD)}
    pack_bytes, entries = build_pack(members)
    contract = image_contract or image_contract_for(
        [m["data"] for m in members])
    if frame_coverage is None:
        frame_indices = sorted(m["frame_index"] for m in entries
                               if m["frame_index"] is not None)
        if not frame_indices:
            raise FilmError("frame_coverage is required when no member "
                            "carries a frame_index")
        frame_coverage = [frame_indices[0], frame_indices[-1] + 1]
    index_doc = make_index(
        pack_bytes, entries, image_contract=contract,
        frame_coverage=frame_coverage, snapshot_digest=snapshot_digest,
        recipe_digest=recipe_digest, sequence_digest=sequence_digest,
        retention_refs=list(retention_refs))
    index_raw = index_bytes(index_doc)

    def put(object_id, data):
        return resumable_put(backend, object_id, data, transport["upload"],
                             transport["retry"], sleep_fn or (lambda ms: None))

    pack_id = _object_id(pack_bytes)
    put(pack_id, pack_bytes)
    index_id = _object_id(index_raw)
    put(index_id, index_raw)
    objects = [
        {"object_id": pack_id, "kind": "pack",
         "byte_length": len(pack_bytes), "sha256": sha256_bytes(pack_bytes),
         "revision": None},
        {"object_id": index_id, "kind": "pack_index",
         "byte_length": len(index_raw), "sha256": sha256_bytes(index_raw),
         "revision": None}]
    doc = make_archive(
        archive_id or f"arc-{uuid.uuid4().hex[:12]}", profile, objects,
        pack={"object_id": pack_id, "index_object_id": index_id,
              "byte_length": len(pack_bytes),
              "sha256": sha256_bytes(pack_bytes),
              "index_sha256": sha256_bytes(index_raw)},
        transport=transport, connection=connection,
        verification={"level": "UPLOADED_UNVERIFIED",
                      "min_required": min_level,
                      "readback_cap_bytes": readback_cap})
    achieved = verify_archive(backend, doc, sleep_fn=sleep_fn
                              or (lambda ms: None))
    sealed = seal_archive(doc, achieved)
    manifest_raw = canon_bytes(sealed)
    manifest_id = f"manifest-{sealed['archive_id']}"
    backend.put_object(manifest_id, manifest_raw)
    return {"archive": sealed, "archive_sha256": archive_sha(sealed),
            "manifest_object_id": manifest_id,
            "pack_object_id": pack_id, "index_object_id": index_id,
            "achieved_level": achieved}


def _cache_key(member):
    return f"mbr-{member['sha256'][:24]}"


def _check_restore_dest(dest, allowed_root):
    """Destination gate evaluated on the *unresolved* destination.

    `..` components are refused outright; with an allowed root the
    destination must sit inside it; and neither the destination nor any
    existing ancestor between it and the root may be a symlink or a
    non-directory — a resolved-path check alone would follow a planted
    link and still look inside the root.
    """
    if ".." in dest.parts:
        raise FilmError("RESTORE_PATH_REJECTED: '..' components are not "
                        "allowed in the restore destination")
    probe = dest if dest.is_absolute() else Path.cwd() / dest
    probe = Path(os.path.normpath(probe))
    if allowed_root is not None:
        stop = Path(allowed_root).resolve()
        if not probe.is_relative_to(stop):
            raise FilmError("RESTORE_PATH_REJECTED: destination escapes "
                            "the allowed root")
    else:
        stop = Path(probe.anchor)
    for node in (probe, *probe.parents):
        if node == stop:
            break
        if node.is_symlink():
            raise FilmError("RESTORE_PATH_REJECTED: destination path "
                            "crosses a symlink")
        if node.exists() and not node.is_dir():
            raise FilmError("RESTORE_PATH_REJECTED: destination path "
                            "crosses a non-directory")


def restore_archive(backend, archive_doc, workspace, dest_dir, *,
                    allowed_root=None, members=None, prefix="F",
                    offline=False, whole_pack_cap=None, sleep_fn=None):
    """Restore selected members through staging into `dest_dir`.

    Nothing reaches the destination until every selected member was
    fetched, hash-verified and technically checked. `members=None` restores
    the full index order; names are generated here — index fields never
    become paths. `allowed_root` is a fixed configured root supplied by the
    caller — never something derived from the destination.
    """
    archive_doc = validate_archive(archive_doc)
    if archive_doc.get("pack") is None:
        raise FilmError("This archive has no pack object")
    dest = Path(dest_dir)
    _check_restore_dest(dest, allowed_root)
    pack = archive_doc["pack"]
    retry = archive_doc["transport"]["retry"]
    index = fetch_index(backend, pack["index_object_id"],
                        pack["index_sha256"], retry=retry,
                        sleep_fn=sleep_fn)
    reader = PackReader(backend, pack["object_id"], index,
                        whole_pack_cap=whole_pack_cap, retry=retry,
                        sleep_fn=sleep_fn)
    wanted = set(members) if members is not None else None
    selected = [m for m in index["members"]
                if wanted is None or m["member_id"] in wanted]
    if not selected:
        raise FilmError("No archive members selected for restore")
    if wanted is not None:
        missing = wanted - {m["member_id"] for m in index["members"]}
        if missing:
            raise FilmError(f"Unknown archive members: {sorted(missing)}")
    total = sum(m["byte_length"] for m in selected)
    stage = workspace.begin_restore(reserve_bytes=total)
    offline_ready = False
    try:
        if offline:
            # Full-pack download + hash + reservation first; only then is
            # the restore allowed to report offline capability.
            reader.verify_full_pack()
            offline_ready = True
        names = []
        for position, member in enumerate(selected):
            cached = workspace.get(_cache_key(member))
            if cached is not None and sha256_bytes(cached) == member["sha256"]:
                data = cached
            else:
                data = reader.read_member(member["member_id"])
            workspace.check_member(data, index["image_contract"],
                                   member["member_id"])
            workspace.put(_cache_key(member), data,
                          retention="VERIFIED_MEMBER")
            name = f"{prefix}_{position + 1:06d}.png"
            stage.write_member(name, data)
            names.append(name)
        published = stage.commit(dest)
    except Exception:
        stage.abort()
        raise
    state = reader.state
    if state == "UNVERIFIED":
        # Every selected member was hash-checked on the cache-hit path.
        state = "VERIFIED_MEMBERS"
    return {"restored": published, "archive_id": archive_doc["archive_id"],
            "verification": state,
            "offline_capable": offline and offline_ready,
            "destination": str(dest)}


def archive_status(backend, archive_doc):
    """Live object inventory + seal state for a manifest."""
    archive_doc = validate_archive(archive_doc)
    objects = []
    for obj in archive_doc["objects"]:
        state = {"object_id": obj["object_id"], "kind": obj["kind"],
                 "byte_length": obj["byte_length"]}
        try:
            info = backend.object_info(obj["object_id"])
            state["present"] = True
            state["byte_length_match"] = \
                info.get("byte_length") == obj["byte_length"]
            if info.get("provider_checksum") == "sha256":
                state["sha256_match"] = info.get("sha256") == obj["sha256"]
        except Exception as e:
            state["present"] = False
            state["error"] = str(e)
        objects.append(state)
    level = archive_doc["verification"]["level"]
    return {"archive_id": archive_doc["archive_id"],
            "storage_profile": archive_doc["storage_profile"],
            "objects": objects,
            "verification": archive_doc["verification"],
            "sealed": LEVELS[level]
            >= LEVELS[archive_doc["verification"]["min_required"]],
            "qualification": archive_doc["qualification"]}
